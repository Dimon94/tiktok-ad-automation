"""JB64 real local chain, independent ratios; SDK transport double only."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from urllib3.response import HTTPResponse

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ad_management.preparation import prepare_direct_task
from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.ad_management.conftest import _row, _set_selection
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


def configure(env, cbo, kind="campaign"):
    env.cbo = cbo
    peer = EntityRef(
        env.context.tenant_id, env.account.advertiser_id, "adgroup", "adgroup-peer"
    )
    with Session(engine) as db, db.begin():
        group = db.get(
            AdObject,
            (
                env.context.tenant_id,
                env.account.advertiser_id,
                "adgroup",
                "adgroup-native",
            ),
        )
        group.configuration = {
            **group.configuration,
            "roas_bid": "1.20",
            "deep_bid_type": "VO_MIN_ROAS",
        }
        db.add(group)
        db.add(
            _row(
                peer,
                parent=env.refs["campaign"],
                ad_type="SMART_PLUS",
                connection_id=env.route.connection_id,
                configuration={
                    **group.configuration,
                    "roas_bid": "1.2000" if cbo else "1.50",
                },
            )
        )
        campaign = db.get(
            AdObject,
            (
                env.context.tenant_id,
                env.account.advertiser_id,
                "campaign",
                "campaign-native",
            ),
        )
        campaign.configuration = {**campaign.configuration, "budget_optimize_on": cbo}
        db.add(campaign)
        record = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(
            record, list(env.refs.values()) if kind == "all" else [env.refs[kind]]
        )
        record.created_at = datetime.now(UTC) + timedelta(seconds=1)
        db.add(record)


def wire(monkeypatch, env, missing=False):
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
            assert url.endswith("/smart_plus/adgroup/update/")
            assert set(body) == {"advertiser_id", "adgroup_id", "roas_bid"}
            assert body["roas_bid"] in {1.32, 1.65}
            data = {}
        else:
            found = []
            if url.endswith("/smart_plus/adgroup/get/"):
                for identity in json.loads(body["filtering"]).get("adgroup_ids", []):
                    if missing and identity == "adgroup-peer":
                        continue
                    found.append(
                        {
                            "adgroup_id": identity,
                            "campaign_id": "campaign-native",
                            "advertiser_id": env.account.advertiser_id,
                            "roas_bid": "1.65"
                            if identity == "adgroup-peer" and not env.cbo
                            else "1.3200",
                            "deep_bid_type": "VO_MIN_ROAS",
                            "operation_status": "ENABLE",
                            "promotion_type": "NATIVE_SERIES",
                        }
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
                {"code": 0, "request_id": "jb64-local", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    return calls


@pytest.mark.parametrize("cbo", [True, False])
@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad", "all"])
def test_roas_chain(native_status_env, redis_client, monkeypatch, cbo, kind):
    env = native_status_env
    configure(env, cbo, kind)
    monkeypatch.setattr("app.modules.ad_management.preparation.PREPARE_CHUNK_SIZE", 1)
    calls = wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base = f"/api/tenants/{env.context.tenant_id}"
        body = {
            "bc_id": env.bc.bc_id,
            "selection_id": str(env.selection.id),
            "mutation": {"field": "roas", "mode": "increase_percent", "value": "10"},
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

        def items():
            return [
                i
                for i in client.get(base + f"/ad-management-tasks/{task}").json()[
                    "items"
                ]
                if i["ref"]["kind"] == "adgroup"
            ]

        frozen = items()
        if kind == "all":
            by_id = {item["ref"]["remote_id"]: item for item in frozen}
            assert {
                r["kind"] for r in by_id["adgroup-native"]["roas_facts"]["sources"]
            } == {
                "campaign",
                "adgroup",
                "ad",
            }
            assert {
                r["kind"] for r in by_id["adgroup-peer"]["roas_facts"]["sources"]
            } == ({"campaign", "adgroup", "ad"} if cbo else {"campaign"})
        assert len(frozen) == (2 if cbo or kind in {"campaign", "all"} else 1)
        assert all(i["execution_result"] == "PENDING" for i in frozen), frozen
        assert frozen[0]["roas_facts"]["budget_strategy"] == ("CBO" if cbo else "ABO")
        assert sorted(Decimal(i["final_value"]) for i in frozen) == (
            [Decimal("1.32"), Decimal("1.32")]
            if cbo
            else [Decimal("1.32"), Decimal("1.65")]
            if kind in {"campaign", "all"}
            else [Decimal("1.32")]
        )
        with Session(engine) as db, db.begin():
            row = db.get(
                AdObject,
                (
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    "adgroup",
                    "adgroup-native",
                ),
            )
            row.configuration = {**row.configuration, "roas_bid": "1.2000"}
            db.add(row)
        drive_management(env, task)
        drive_management(env, task)
        assert all(i["execution_result"] == "ACCEPTED" for i in items()), items()
        assert all(i["observation_state"] == "REFRESH_PENDING" for i in items())
        drive_directory(env, redis_client)
        drive_management(env, task)
        assert all(i["observation_state"] == "ACCEPTED" for i in items()), items()
        assert [i["roas_facts"] for i in items()] == [i["roas_facts"] for i in frozen]
        client.post(base + f"/ad-management-tasks/{task}/refresh")
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == (
            1 if cbo or kind not in {"campaign", "all"} else 2
        )


@pytest.mark.parametrize("cbo", [True, False])
def test_roas_workspace(native_status_env, redis_client, monkeypatch, cbo):
    from tests.modules.ad_management.test_native_status import run_native_workspace

    env = native_status_env
    configure(env, cbo, "ad")
    calls = wire(monkeypatch, env)

    def advance(task_id: str, phase: str):
        if phase == "write":
            drive_management(env, task_id)
        else:
            drive_directory(env, redis_client)
            drive_management(env, task_id)
        return {"advanced": True}

    run_native_workspace(env, "ad", "1.32", advance, grep="@native-roas-live")
    assert len([c for c in calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("boundary", ["mode", "strategy", "peer_mode", "grant"])
def test_roas_unverified_zero_write(native_status_env, monkeypatch, boundary):
    from app.modules.accounts.models import BCAccountAccess

    env = native_status_env
    configure(env, True)
    calls = wire(monkeypatch, env)
    with Session(engine) as db, db.begin():
        if boundary == "grant":
            grant = db.get(
                BCAccountAccess,
                (
                    env.context.tenant_id,
                    env.bc.bc_id,
                    env.account.advertiser_id,
                    env.route.connection_id,
                ),
            )
            # 既有统一管理权限字段。
            grant.can_build = False
            db.add(grant)
        else:
            kind, identity = (
                ("campaign", "campaign-native")
                if boundary == "strategy"
                else (
                    "adgroup",
                    "adgroup-peer" if boundary == "peer_mode" else "adgroup-native",
                )
            )
            row = db.get(
                AdObject,
                (env.context.tenant_id, env.account.advertiser_id, kind, identity),
            )
            row.configuration = {
                k: v
                for k, v in row.configuration.items()
                if k
                != ("budget_optimize_on" if boundary == "strategy" else "deep_bid_type")
            }
            db.add(row)
    with TestClient(env.app) as client:
        base = f"/api/tenants/{env.context.tenant_id}"
        response = client.post(
            base + "/ad-management-requests",
            json={
                "bc_id": env.bc.bc_id,
                "selection_id": str(env.selection.id),
                "mutation": {"field": "roas", "mode": "set", "value": "1.32"},
                "idempotency_key": str(uuid4()),
            },
        )
        if response.status_code == 202:
            task = response.json()["task_id"]
            prepare_direct_task(engine, task)
            drive_management(env, task)
            items = client.get(base + f"/ad-management-tasks/{task}").json()["items"]
            assert not any(i["execution_result"] == "ACCEPTED" for i in items), items
        else:
            assert response.status_code in {403, 409, 422}
    assert not [c for c in calls if c[0] == "POST"]


@pytest.mark.parametrize(
    "boundary",
    ["missing", "stale", "different", "grant", "connection", "already_target"],
)
def test_roas_each_member_observed(
    native_status_env, redis_client, monkeypatch, boundary
):
    from app.modules.accounts.models import BCAccountAccess

    env = native_status_env
    configure(env, True)
    calls = wire(monkeypatch, env, missing=boundary == "missing")
    if boundary == "already_target":
        with Session(engine) as db, db.begin():
            peer = db.get(
                AdObject,
                (
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    "adgroup",
                    "adgroup-peer",
                ),
            )
            peer.configuration = {**peer.configuration, "roas_bid": "1.32"}
            db.add(peer)
    with TestClient(env.app) as client:
        base = f"/api/tenants/{env.context.tenant_id}"
        task = client.post(
            base + "/ad-management-requests",
            json={
                "bc_id": env.bc.bc_id,
                "selection_id": str(env.selection.id),
                "mutation": {"field": "roas", "mode": "set", "value": "1.32"},
                "idempotency_key": str(uuid4()),
            },
        ).json()["task_id"]
        prepare_direct_task(engine, task)
        drive_management(env, task)
        drive_directory(env, redis_client)
        with Session(engine) as db, db.begin():
            row = db.get(
                AdObject,
                (
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    "adgroup",
                    "adgroup-peer",
                ),
            )
            if boundary == "stale":
                row.observed_at = datetime.now(UTC) - timedelta(days=1)
            if boundary == "different":
                row.configuration = {**row.configuration, "roas_bid": "1.99"}
            if boundary == "connection":
                from app.modules.accounts.models import TikTokConnection

                other = TikTokConnection(
                    tenant_id=env.context.tenant_id, status="ACTIVE"
                )
                db.add(other)
                db.flush()
                row.source_connection_id = other.id
            db.add(row)
            if boundary == "grant":
                grant = db.get(
                    BCAccountAccess,
                    (
                        env.context.tenant_id,
                        env.bc.bc_id,
                        env.account.advertiser_id,
                        env.route.connection_id,
                    ),
                )
                grant.authorized = False
                db.add(grant)
        drive_management(env, task)
        if boundary == "grant":
            from uuid import UUID

            from sqlmodel import select

            from app.modules.ad_management.models import ManagementTaskItem

            with Session(engine) as db:
                peer = db.exec(
                    select(ManagementTaskItem).where(
                        ManagementTaskItem.task_id == UUID(task),
                        ManagementTaskItem.ref["remote_id"].astext == "adgroup-peer",
                    )
                ).one()
                assert peer.execution_result == "ACCEPTED"
                assert peer.observation_state == "NEEDS_REVIEW"
            assert len([c for c in calls if c[0] == "POST"]) == 1
            return
        items = client.get(base + f"/ad-management-tasks/{task}").json()["items"]
        peer = next(i for i in items if i["ref"]["remote_id"] == "adgroup-peer")
        assert peer["execution_result"] == "ACCEPTED"
        assert (peer["observation_state"] == "ACCEPTED") == (
            boundary == "already_target"
        ), peer
        assert len([c for c in calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("cbo", [True, False])
def test_roas_sources_follow_actual_owner(native_status_env, cbo):
    """同批两组选中来源：CBO 联动共享，ABO 独立，不串入兄弟来源。"""
    env = native_status_env
    configure(env, cbo)
    peer = EntityRef(
        env.context.tenant_id, env.account.advertiser_id, "adgroup", "adgroup-peer"
    )
    with Session(engine) as db, db.begin():
        record = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(record, [env.refs["adgroup"], peer])
        db.add(record)
    with TestClient(env.app) as client:
        base = f"/api/tenants/{env.context.tenant_id}"
        response = client.post(
            base + "/ad-management-requests",
            json={
                "bc_id": env.bc.bc_id,
                "selection_id": str(env.selection.id),
                "mutation": {
                    "field": "roas",
                    "mode": "increase_percent",
                    "value": "10",
                },
                "idempotency_key": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        task = response.json()["task_id"]
        prepare_direct_task(engine, task)
        items = client.get(base + f"/ad-management-tasks/{task}").json()["items"]
        assert len(items) == 2
        for item in items:
            expected = (
                {"adgroup-native", "adgroup-peer"}
                if cbo
                else {item["ref"]["remote_id"]}
            )
            assert {
                source["remote_id"] for source in item["roas_facts"]["sources"]
            } == expected
