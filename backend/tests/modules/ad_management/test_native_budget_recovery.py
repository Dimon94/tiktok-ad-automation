"""JB63: frozen budget history, read-only UNKNOWN and explicit reverse intents."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from urllib3.response import HTTPResponse

from app.core.db import engine
from app.modules.ad_management.models import ManagementTask
from app.modules.ads.models import AdObject
from tests.modules.ad_management.test_native_budget import configure
from tests.modules.ad_management.test_native_recovery import public, reconcile
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


def budget_wire(monkeypatch, env, cbo=True, unknown=True):
    configure(env, cbo)
    owner = "campaign" if cbo else "adgroup"
    calls, state = [], {"unknown": unknown, "read": "120.1250", "wrong_account": False}

    def transport(pool, method, url, **kwargs):
        body = (
            json.loads(kwargs["body"]) if method == "POST" else dict(kwargs["fields"])
        )
        calls.append((method, url, body))
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer:
            observer[0].attempt(observer[1])
        assert body["advertiser_id"] == env.account.advertiser_id
        if method == "POST":
            assert url.endswith(
                "/smart_plus/campaign/update/"
                if cbo
                else "/smart_plus/adgroup/budget/update/"
            )
            amount = body["budget"] if cbo else body["budget"][0]["budget"]
            assert Decimal(str(amount)) in {
                Decimal("120.125"),
                Decimal("100.25" if cbo else "50.50"),
            }
            assert set(body) == (
                {"advertiser_id", "campaign_id", "budget"}
                if cbo
                else {"advertiser_id", "budget"}
            )
            if state["unknown"]:
                raise TimeoutError("sent, response lost")
            data = {} if cbo else {"adgroup_ids": ["adgroup-native"]}
        else:
            ids = [
                v
                for vs in json.loads(body["filtering"]).values()
                if isinstance(vs, list)
                for v in vs
            ]
            assert env.refs[owner].remote_id in ids
            found = (
                [
                    {
                        f"{owner}_id": env.refs[owner].remote_id,
                        "campaign_id": "campaign-native",
                        "advertiser_id": "wrong-account"
                        if state["wrong_account"]
                        else env.account.advertiser_id,
                        "budget": state["read"],
                        "operation_status": "ENABLE",
                        "promotion_type": "NATIVE_SERIES",
                    }
                ]
                if state["read"] is not None
                and url.endswith(f"/smart_plus/{owner}/get/")
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
                {"code": 0, "request_id": "jb63-wire", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    return calls, state, owner


def submit_budget(client, env):
    base = f"/api/tenants/{env.context.tenant_id}"
    body = {
        "bc_id": env.bc.bc_id,
        "selection_id": str(env.selection.id),
        "mutation": {"field": "budget", "mode": "set", "value": "120.125"},
        "idempotency_key": str(uuid4()),
    }
    response = client.post(base + "/ad-management-requests", json=body)
    assert response.status_code == 202, response.text
    assert (
        client.post(base + "/ad-management-requests", json=body).json()["task_id"]
        == response.json()["task_id"]
    )
    return base, response.json()["task_id"]


def publish(env, redis_client, owner):
    from app.modules.ads.directory_requests import request_directory

    with Session(engine) as db, db.begin():
        request_directory(
            db,
            tenant_id=env.context.tenant_id,
            bc_id=env.bc.bc_id,
            frozen_route=env.route.model_dump(mode="json"),
            actor_id=env.context.actor_id,
            advertiser_ids=[env.account.advertiser_id],
            scope="targeted",
            refs=[env.refs[owner]],
        )
    drive_directory(env, redis_client)


@pytest.mark.parametrize("cbo", [True, False])
@pytest.mark.parametrize("unknown", [True, False])
@pytest.mark.parametrize("restore_unknown", [True, False])
def test_budget_reverse_frozen_preview(
    native_status_env, redis_client, monkeypatch, cbo, unknown, restore_unknown
):
    env = native_status_env
    calls, state, owner = budget_wire(monkeypatch, env, cbo, unknown)
    with TestClient(env.app) as client:
        base, task = submit_budget(client, env)
        drive_management(env, task)
        publish(env, redis_client, owner)
        drive_management(env, task)
        frozen = public(client, base, task)["items"][0]
        response = client.post(base + f"/ad-management-tasks/{task}/restore")
        assert response.status_code == 201, response.text
        preview = response.json()
        item = preview["items"][0]
        assert item["budget_facts"] == frozen["budget_facts"]
        assert Decimal(item["original_value"]) == Decimal("120.125")
        assert Decimal(item["final_value"]) == Decimal("100.25" if cbo else "50.50")
        assert item["execution_result"] == "PENDING"
        state["unknown"], state["read"] = (
            restore_unknown,
            "100.2500" if cbo else "50.500",
        )
        response = client.post(
            base + "/ad-management-tasks",
            json={
                "preview_id": preview["preview_id"],
                "preview_digest": preview["digest"],
                "idempotency_key": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        restore = response.json()["task_id"]
        drive_management(env, restore)
        if restore_unknown:
            reconcile(env)
            drive_management(env, restore)
            restored = public(client, base, restore)["items"][0]
            assert restored["execution_result"] == "UNKNOWN"
            assert restored["observation_state"] == "OBSERVED_MATCH"
            assert len(restored["attempts"]) == 1
            assert (
                client.post(
                    base + f"/ad-management-tasks/{restore}/retry",
                    json={"idempotency_key": str(uuid4())},
                ).status_code
                == 409
            )
        else:
            assert (
                public(client, base, restore)["items"][0]["observation_state"]
                == "REFRESH_PENDING"
            )
            drive_directory(env, redis_client)
            drive_management(env, restore)
            restored = public(client, base, restore)["items"][0]
            assert restored["execution_result"] == "ACCEPTED"
            assert restored["observation_state"] == "ACCEPTED"
        assert restored["budget_facts"] == frozen["budget_facts"]
        assert len([c for c in calls if c[0] == "POST"]) == 2
        assert public(client, base, task)["items"][0] == frozen
        with Session(engine) as db:
            source = db.get(ManagementTask, UUID(restore))
            from app.modules.ad_management.models import ManagementPreview

            assert (
                db.get(ManagementPreview, source.preview_id).mutation["source_task_id"]
                == task
            )


@pytest.mark.parametrize(
    "observation,expected",
    [
        ("120.12500", "OBSERVED_MATCH"),
        ("120.126", "OBSERVED_DIFFERENT"),
        (None, "RECONCILE_FAILED"),
        ("stale", "RECONCILE_FAILED"),
        ("wrong-account", "RECONCILE_FAILED"),
    ],
)
def test_budget_unknown_read_only(
    native_status_env, monkeypatch, observation, expected
):
    env = native_status_env
    calls, state, _ = budget_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit_budget(client, env)
        drive_management(env, task)
        before = public(client, base, task)["items"][0]
        state["read"] = (
            "120.125" if observation in {"stale", "wrong-account"} else observation
        )
        state["wrong_account"] = observation == "wrong-account"
        if observation == "stale":
            from app.integrations.tiktok.adapters import ads_read

            class Stale(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) - timedelta(hours=1)

            monkeypatch.setattr(ads_read, "datetime", Stale)
        for _ in range(3 if expected == "RECONCILE_FAILED" else 1):
            payload = reconcile(env)
        count = len(calls)
        reconcile(env, payload=payload)
        assert len(calls) == count
        after = public(client, base, task)["items"][0]
        assert after["observation_state"] == expected
        for key in [
            "original_value",
            "final_value",
            "budget_facts",
            "attempts",
            "execution_result",
        ]:
            assert after[key] == before[key]
        assert after["execution_result"] == "UNKNOWN"
        assert len(after["attempts"]) == 1
        assert after["attempts"][0]["outcome"] == "UNKNOWN"
        assert (
            client.post(
                base + f"/ad-management-tasks/{task}/retry",
                json={"idempotency_key": str(uuid4())},
            ).status_code
            == 409
        )
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "gate",
    ["changed", "missing", "stale", "connection", "grant", "manage", "route", "actor"],
)
@pytest.mark.parametrize("phase", ["preview", "send"])
def test_budget_restore_current_gate(
    native_status_env, redis_client, monkeypatch, gate, phase
):
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection
    from app.modules.tenants.models import TenantMembership

    env = native_status_env
    calls, state, owner = budget_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit_budget(client, env)
        drive_management(env, task)
        publish(env, redis_client, owner)
        if phase == "send":
            response = client.post(base + f"/ad-management-tasks/{task}/restore")
            assert response.status_code == 201, response.text
            preview = response.json()
            assert preview["items"][0]["execution_result"] == "PENDING"
            response = client.post(
                base + "/ad-management-tasks",
                json={
                    "preview_id": preview["preview_id"],
                    "preview_digest": preview["digest"],
                    "idempotency_key": str(uuid4()),
                },
            )
            assert response.status_code == 202
            restore = response.json()["task_id"]
        with Session(engine) as db, db.begin():
            row = db.get(
                AdObject,
                (
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    owner,
                    env.refs[owner].remote_id,
                ),
            )
            if gate in {"changed", "missing"}:
                row.configuration = {
                    **row.configuration,
                    "budget": "121" if gate == "changed" else None,
                }
            elif gate == "stale":
                row.observed_at = datetime.now(UTC) - timedelta(hours=1)
            elif gate == "connection":
                peer = TikTokConnection(
                    tenant_id=env.context.tenant_id, status="ACTIVE"
                )
                db.add(peer)
                db.flush()
                row.source_connection_id = peer.id
            elif gate in {"grant", "manage"}:
                row = db.get(
                    BCAccountAccess,
                    (
                        env.context.tenant_id,
                        env.bc.bc_id,
                        env.account.advertiser_id,
                        env.route.connection_id,
                    ),
                )
                if gate == "grant":
                    row.authorized = False
                else:
                    row.permission_state = "UNKNOWN"
            elif gate == "route":
                row = db.get(TikTokConnection, env.route.connection_id)
                row.authorization_revision += 1
            else:
                row = db.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                row.active = False
            db.add(row)
        count = len(calls)
        if phase == "send":
            state["unknown"] = False
            drive_management(env, restore)
        else:
            response = client.post(base + f"/ad-management-tasks/{task}/restore")
            if response.status_code == 201:
                assert response.json()["items"][0]["execution_result"] == "CONFLICT"
            else:
                assert response.status_code in {403, 404, 409}
        assert len(calls) == count


@pytest.mark.parametrize("cbo", [True, False])
def test_budget_recovery_workspace(native_status_env, redis_client, monkeypatch, cbo):
    from tests.modules.ad_management.test_native_status import run_native_workspace

    env = native_status_env
    calls, state, owner = budget_wire(monkeypatch, env, cbo)

    def advance(task_id, phase):
        if phase == "write":
            drive_management(env, task_id)
        elif phase == "reconcile":
            reconcile(env)
        elif phase == "publish":
            publish(env, redis_client, owner)
        else:
            state["unknown"], state["read"] = False, "100.2500" if cbo else "50.500"
            drive_management(env, task_id)
            drive_directory(env, redis_client)
            drive_management(env, task_id)
        return {"advanced": True}

    run_native_workspace(
        env,
        "campaign" if cbo else "ad",
        "120.125",
        advance,
        grep="@native-budget-recovery-live",
    )
    assert len([c for c in calls if c[0] == "POST"]) == 2


def test_budget_mixed_owners_exact_once(native_status_env, redis_client, monkeypatch):
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.reporting.query_models import FrozenSelectionRecord
    from tests.modules.ad_management.conftest import _row, _set_selection

    env = native_status_env
    refs = {
        name: EntityRef(env.context.tenant_id, env.account.advertiser_id, kind, name)
        for name, kind in [
            ("accepted", "campaign"),
            ("rejected", "campaign"),
            ("group-parent", "campaign"),
            ("unknown", "adgroup"),
            ("unsupported", "adgroup"),
        ]
    }
    with Session(engine) as db, db.begin():
        for name, ref in refs.items():
            db.add(
                _row(
                    ref,
                    parent=refs["group-parent"] if name == "unknown" else None,
                    ad_type="SMART_PLUS",
                    connection_id=env.route.connection_id,
                    configuration={
                        "promotion_type": "NATIVE_SERIES",
                        "budget": "100.25",
                        "budget_optimize_on": name != "group-parent",
                        "budget_mode": "BUDGET_MODE_DAY"
                        if name != "group-parent"
                        else "BUDGET_MODE_INFINITE",
                    },
                )
            )
        # 同一 CBO 从系列和组两次选择，必须只产生一个物理预算写。
        child = EntityRef(
            env.context.tenant_id,
            env.account.advertiser_id,
            "adgroup",
            "accepted-child",
        )
        db.add(
            _row(
                child,
                parent=refs["accepted"],
                ad_type="SMART_PLUS",
                connection_id=env.route.connection_id,
                configuration={"budget": "50", "promotion_type": "NATIVE_SERIES"},
            )
        )
        record = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(
            record,
            [refs[n] for n in ["accepted", "rejected", "unknown", "unsupported"]]
            + [child],
        )
        record.created_at = datetime.now(UTC) + timedelta(seconds=1)
        db.add(record)
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
            if url.endswith("/campaign/update/"):
                if body["campaign_id"] == "rejected":
                    return HTTPResponse(
                        body=b'{"code":40002,"message":"explicit refusal","request_id":"reject"}',
                        status=200,
                    )
                assert body == {
                    "advertiser_id": env.account.advertiser_id,
                    "campaign_id": "accepted",
                    "budget": 120.125,
                }
            else:
                assert body["budget"] == [{"adgroup_id": "unknown", "budget": 120.125}]
                raise TimeoutError("sent, lost receipt")
            data = {}
        else:
            ids = [
                v
                for vs in json.loads(body["filtering"]).values()
                if isinstance(vs, list)
                for v in vs
            ]
            kind = "adgroup" if "/adgroup/" in url else "campaign"
            found = [
                {
                    f"{kind}_id": n,
                    "advertiser_id": env.account.advertiser_id,
                    "campaign_id": "group-parent" if kind == "adgroup" else n,
                    "budget": "120.12500",
                    "promotion_type": "NATIVE_SERIES",
                }
                for n in ids
                if n in ({"accepted"} if kind == "campaign" else {"unknown"})
                and "/smart_plus/" in url
            ]
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
            body=json.dumps({"code": 0, "request_id": "mixed", "data": data}).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    with TestClient(env.app) as client:
        base, task = submit_budget(client, env)
        drive_management(env, task)
        drive_management(env, task)
        reconcile(env)
        drive_directory(env, redis_client)
        drive_management(env, task)
        result = public(client, base, task)
        items = {
            i["ref"]["remote_id"]: i
            for i in result["items"]
            if i["reason"] != "linked_impact"
        }
        assert {n: i["execution_result"] for n, i in items.items()} == {
            "accepted": "ACCEPTED",
            "rejected": "REJECTED",
            "unknown": "UNKNOWN",
            "unsupported": "UNSUPPORTED",
        }
        assert items["accepted"]["observation_state"] == "ACCEPTED"
        assert items["unknown"]["observation_state"] == "OBSERVED_MATCH"
        assert len(items["accepted"]["budget_facts"]["sources"]) == 2
        for name in ["accepted", "rejected", "unknown"]:
            item = items[name]
            assert Decimal(item["original_value"]) == Decimal("100.25")
            assert Decimal(item["final_value"]) == Decimal("120.125")
            assert item["budget_facts"]["currency"] == "USD"
            assert item["budget_facts"]["affected_refs"] == [item["ref"]]
            assert len(item["attempts"]) == 1
        assert {
            k: result["counts"][k]
            for k in ["accepted", "rejected", "unknown", "unsupported", "pending"]
        } == {
            "accepted": 1,
            "rejected": 1,
            "unknown": 1,
            "unsupported": 1,
            "pending": 0,
        }
        assert len([c for c in calls if c[0] == "POST"]) == 3
