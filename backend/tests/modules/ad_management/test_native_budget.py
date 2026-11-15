"""JB62: hand-derived money/ownership, real HTTP/PG17/Redis, SDK wire only."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from urllib3.response import HTTPResponse

from app.core.db import engine
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ad_management.preparation import prepare_direct_task
from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.ad_management.conftest import _set_selection
from tests.modules.ad_management.test_native_status import (
    drive_directory,
    drive_management,
)
from tests.modules.ad_management.test_native_status import (
    native_status_env as native_status_env,
)
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


def configure(env, cbo):
    with Session(engine) as db, db.begin():
        for kind, ref in env.refs.items():
            row = db.get(
                AdObject, (ref.tenant_id, ref.advertiser_id, kind, ref.remote_id)
            )
            row.configuration = {
                **row.configuration,
                "budget": "100.25" if kind == "campaign" else "50.50",
                "budget_optimize_on": cbo,
                "budget_mode": "BUDGET_MODE_DAY" if cbo else "BUDGET_MODE_INFINITE",
                "schedule_type": "SCHEDULE_START_END",
                "schedule_start_time": "2026-12-01 00:00:00",
                "schedule_end_time": "2026-12-31 00:00:00",
                "targeting_spec": {"location_ids": ["6252001"]},
            }
            db.add(row)
        record = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(record, list(env.refs.values()))
        record.created_at = datetime.now(UTC) + timedelta(seconds=1)
        db.add(record)


def wire(monkeypatch, env, owner, observed):
    calls = []

    def transport(pool, method, url, **kwargs):
        body = (
            json.loads(kwargs["body"]) if method == "POST" else dict(kwargs["fields"])
        )
        calls.append((method, url, body))
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer:
            observer[0].attempt(observer[1])
        if method == "POST":
            if owner == "campaign":
                assert url.endswith("/smart_plus/campaign/update/")
                assert body == {
                    "advertiser_id": env.account.advertiser_id,
                    "campaign_id": "campaign-native",
                    "budget": 110.275,
                }
            else:
                assert url.endswith("/smart_plus/adgroup/budget/update/"), (url, body)
                assert body == {
                    "advertiser_id": env.account.advertiser_id,
                    "budget": [{"adgroup_id": "adgroup-native", "budget": 55.55}],
                }
            data = {} if owner == "campaign" else {"adgroup_ids": ["adgroup-native"]}
        else:
            assert body["advertiser_id"] == env.account.advertiser_id
            ids = json.loads(body["filtering"])
            assert env.refs[owner].remote_id in [
                v for values in ids.values() if isinstance(values, list) for v in values
            ]
            found = (
                [
                    {
                        f"{owner}_id": env.refs[owner].remote_id,
                        "campaign_id": "campaign-native",
                        "advertiser_id": env.account.advertiser_id,
                        "budget": observed,
                        "operation_status": "ENABLE",
                        "promotion_type": "NATIVE_SERIES",
                        "native_series_id": "series",
                    }
                ]
                if url.endswith(f"/smart_plus/{owner}/get/")
                else []
            )
            data = {
                "list": found,
                "page_info": {
                    "page": 1,
                    "page_size": int(body["page_size"]),
                    "total_page": int(bool(found)),
                    "total_number": len(found),
                },
            }
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "jb62-wire", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    return calls


@pytest.mark.parametrize("cbo", [True, False])
@pytest.mark.parametrize("observed_match", [True, False])
def test_native_budget_chain(
    native_status_env, redis_client, monkeypatch, cbo, observed_match
):
    env = native_status_env
    configure(env, cbo)
    monkeypatch.setattr("app.modules.ad_management.preparation.PREPARE_CHUNK_SIZE", 1)
    owner = "campaign" if cbo else "adgroup"
    original, target = ("100.25", "110.275") if cbo else ("50.50", "55.55")
    calls = wire(monkeypatch, env, owner, target if observed_match else original)
    base = f"/api/tenants/{env.context.tenant_id}"
    with TestClient(env.app) as client:
        body = {
            "bc_id": env.bc.bc_id,
            "selection_id": str(env.selection.id),
            "mutation": {"field": "budget", "mode": "increase_percent", "value": "10"},
            "idempotency_key": str(uuid4()),
        }
        response = client.post(base + "/ad-management-requests", json=body)
        assert response.status_code == 202, response.text
        task = response.json()["task_id"]
        assert (
            client.post(base + "/ad-management-requests", json=body).json()["task_id"]
            == task
        )
        prepare_direct_task(engine, task)

        def detail():
            return client.get(base + f"/ad-management-tasks/{task}").json()

        items = [i for i in detail()["items"] if i["reason"] != "linked_impact"]
        assert len(items) == 1, [(i["ref"]["kind"], i["reason"]) for i in items]
        item = items[0]
        assert item["ref"]["kind"] == owner
        assert (Decimal(item["original_value"]), Decimal(item["final_value"])) == (
            Decimal(original),
            Decimal(target),
        )
        assert item["budget_facts"]["currency"] == "USD"
        assert {s["ref"]["kind"] for s in item["budget_facts"]["sources"]} == {
            "campaign",
            "adgroup",
            "ad",
        }
        assert item["budget_facts"]["affected_refs"] == [item["ref"]]
        frozen = item["budget_facts"]
        with Session(engine) as db, db.begin():
            account = db.get(
                AdvertiserAccount, (env.context.tenant_id, env.account.advertiser_id)
            )
            account.currency = "JPY"
            db.add(account)
        drive_management(env, task)
        drive_management(env, task)
        assert detail()["items"][0]["execution_result"] == "ACCEPTED"
        assert detail()["items"][0]["observation_state"] == "REFRESH_PENDING"
        drive_directory(env, redis_client)
        drive_management(env, task)
        result = detail()["items"][0]
        assert result["budget_facts"] == frozen
        assert result["observation_state"] == (
            "ACCEPTED" if observed_match else "NEEDS_REVIEW"
        ), result
        assert Decimal(result["original_value"]) == Decimal(original)
        assert Decimal(result["final_value"]) == Decimal(target)
        client.post(base + f"/ad-management-tasks/{task}/refresh")
        drive_directory(env, redis_client)
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("cbo", [True, False])
@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("currency", ["USD", "JPY"])
@pytest.mark.parametrize(
    "mode,value,campaign_target,group_target",
    [
        ("set", "123.456", "123.456", "123.456"),
        ("add", "0.125", "100.375", "50.625"),
        ("subtract", "0.25", "100", "50.25"),
        ("increase_percent", "10", "110.275", "55.55"),
        ("decrease_percent", "10", "90.225", "45.45"),
    ],
)
def test_native_budget_finite_money_owners(
    session,
    management_env,
    cbo,
    kind,
    currency,
    mode,
    value,
    campaign_target,
    group_target,
):
    """60 bounded cases; constants hand-derived, no platform minimum assumptions."""
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.ad_management.previews import prepare_preview
    from app.modules.ad_management.schemas import MutationSpec
    from tests.modules.ad_management.conftest import _row

    context, bc, account, route, selection = management_env
    account.currency = currency
    session.add(account)
    refs = {
        k: EntityRef(context.tenant_id, account.advertiser_id, k, k + "-finite")
        for k in ("campaign", "adgroup", "ad")
    }
    for k, ref in refs.items():
        session.add(
            _row(
                ref,
                parent=refs.get({"adgroup": "campaign", "ad": "adgroup"}.get(k)),
                ad_type="SMART_PLUS",
                connection_id=route.connection_id,
                observed_at=datetime.now(UTC) - timedelta(seconds=2),
                configuration={
                    "budget": "100.25" if k == "campaign" else "50.50",
                    "budget_optimize_on": cbo,
                    "promotion_type": "NATIVE_SERIES",
                },
            )
        )
    session.flush()
    _set_selection(selection, [refs[kind]])
    result = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="budget", mode=mode, value=Decimal(value)),
    )
    pending = [i for i in result.items if i.execution_result == "PENDING"]
    assert len(pending) == 1
    item = pending[0]
    assert item.ref == refs["campaign" if cbo else "adgroup"]
    assert item.final_value == Decimal(campaign_target if cbo else group_target)
    assert item.budget_facts.currency == currency
    assert [s.ref for s in item.budget_facts.sources] == [refs[kind]]


@pytest.mark.parametrize("cbo", [True, False])
def test_native_budget_workspace(native_status_env, redis_client, monkeypatch, cbo):
    from tests.modules.ad_management.test_native_status import run_native_workspace

    env = native_status_env
    configure(env, cbo)
    calls = wire(monkeypatch, env, "campaign" if cbo else "adgroup", "120.125")
    # 浏览器使用 set 120.125；传输仍只接受该明确金额，不能因夹具放宽字段。
    original_transport = __import__("urllib3").PoolManager.request

    def transport(pool, method, url, **kwargs):
        if method == "POST":
            body = json.loads(kwargs["body"])
            expected = {
                "advertiser_id": env.account.advertiser_id,
                **(
                    {"campaign_id": "campaign-native", "budget": 120.125}
                    if cbo
                    else {
                        "budget": [{"adgroup_id": "adgroup-native", "budget": 120.125}]
                    }
                ),
            }
            assert body == expected
            calls.append((method, url, body))
            observer = getattr(pool, "_sync_attempt_observer", None)
            if observer:
                observer[0].attempt(observer[1])
            return HTTPResponse(
                body=json.dumps(
                    {
                        "code": 0,
                        "request_id": "jb62-browser",
                        "data": {} if cbo else {"adgroup_ids": ["adgroup-native"]},
                    }
                ).encode(),
                status=200,
            )
        return original_transport(pool, method, url, **kwargs)

    monkeypatch.setattr("urllib3.PoolManager.request", transport)

    def advance(task_id: str, phase: str):
        if phase == "write":
            drive_management(env, task_id)
        else:
            drive_directory(env, redis_client)
            drive_management(env, task_id)
        return {"advanced": True}

    run_native_workspace(
        env, "campaign" if cbo else "ad", "120.125", advance, grep="@native-budget-live"
    )
    assert len([c for c in calls if c[0] == "POST"]) == 1


def test_native_multi_account_currency_and_frozen_children(session, management_env):
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.ad_management.previews import prepare_preview
    from app.modules.ad_management.schemas import MutationSpec
    from tests.modules.ad_management.conftest import _row

    context, bc, account, route, selection = management_env
    refs = []
    for advertiser, currency in [
        (account.advertiser_id, "USD"),
        ("native-peer-account", "JPY"),
    ]:
        if advertiser != account.advertiser_id:
            session.add(
                AdvertiserAccount(
                    tenant_id=context.tenant_id,
                    advertiser_id=advertiser,
                    currency=currency,
                    timezone="UTC",
                    remote_status="STATUS_ENABLE",
                )
            )
            session.add(
                BCAccountAccess(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    advertiser_id=advertiser,
                    connection_id=route.connection_id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_build=True,
                    can_upload=True,
                    permission_state="VERIFIED",
                    checked_at=datetime.now(UTC),
                )
            )
        campaign = EntityRef(context.tenant_id, advertiser, "campaign", "same-series")
        refs.append(campaign)
        session.add(
            _row(
                campaign,
                ad_type="SMART_PLUS",
                connection_id=route.connection_id,
                configuration={
                    "budget_optimize_on": False,
                    "budget_mode": "BUDGET_MODE_INFINITE",
                    "promotion_type": "NATIVE_SERIES",
                },
            )
        )
        for name, amount, age in [
            ("a", "50.50", -2),
            ("b", "80.25", -2),
            ("late", "90", 10),
        ]:
            ref = EntityRef(context.tenant_id, advertiser, "adgroup", name)
            session.add(
                _row(
                    ref,
                    parent=campaign,
                    ad_type="SMART_PLUS",
                    connection_id=route.connection_id,
                    observed_at=selection.created_at + timedelta(seconds=age),
                    configuration={"budget": amount, "promotion_type": "NATIVE_SERIES"},
                )
            )
    session.flush()
    _set_selection(selection, refs)
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="budget", mode="add", value=Decimal("0.25")),
    )
    items = [i for i in preview.items if i.execution_result == "PENDING"]
    assert len(items) == 4
    assert {
        (
            i.ref.advertiser_id,
            i.ref.remote_id,
            str(i.final_value),
            i.budget_facts.currency,
        )
        for i in items
    } == {
        (advertiser, group, amount, currency)
        for advertiser, currency in [
            (account.advertiser_id, "USD"),
            ("native-peer-account", "JPY"),
        ]
        for group, amount in [("a", "50.75"), ("b", "80.50")]
    }


@pytest.mark.parametrize("boundary", ["grant", "route"])
def test_native_budget_readback_revocation(
    native_status_env, redis_client, monkeypatch, boundary
):
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    env = native_status_env
    configure(env, True)
    calls = wire(monkeypatch, env, "campaign", "110.275")
    base = f"/api/tenants/{env.context.tenant_id}"
    with TestClient(env.app) as client:
        response = client.post(
            base + "/ad-management-requests",
            json={
                "bc_id": env.bc.bc_id,
                "selection_id": str(env.selection.id),
                "mutation": {
                    "field": "budget",
                    "mode": "increase_percent",
                    "value": "10",
                },
                "idempotency_key": str(uuid4()),
            },
        )
        assert response.status_code == 202
        task = response.json()["task_id"]
        drive_management(env, task)
        drive_directory(env, redis_client)
        with Session(engine) as db, db.begin():
            if boundary == "grant":
                row = db.get(
                    BCAccountAccess,
                    (
                        env.context.tenant_id,
                        env.bc.bc_id,
                        env.account.advertiser_id,
                        env.route.connection_id,
                    ),
                )
                row.authorized = False
            else:
                row = db.get(TikTokConnection, env.route.connection_id)
                row.authorization_revision += 1
            db.add(row)
        count = len(calls)
        drive_management(env, task)
        with Session(engine) as db:
            from sqlmodel import select

            from app.modules.ad_management.models import ManagementTaskItem

            item = db.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == __import__("uuid").UUID(task),
                    ManagementTaskItem.ref["kind"].astext == "campaign",
                )
            ).one()
            assert item.observation_state == "NEEDS_REVIEW"
        assert len(calls) == count
