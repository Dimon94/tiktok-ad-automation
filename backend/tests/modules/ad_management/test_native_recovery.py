"""JB-60: real HTTP/PG/Redis workers, official SDK transport double only."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ad_management.models import ManagementReceipt
from app.modules.ad_management.reconciliation_tasks import reconcile_management_task
from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.ad_management.conftest import _row, _set_selection
from tests.modules.ad_management.test_native_status import (
    committed_management_env as committed_management_env,
)
from tests.modules.ad_management.test_native_status import (
    drive_directory,
    drive_management,
)
from tests.modules.ad_management.test_native_status import (
    native_status_env as native_status_env,
)


def setup_wire(monkeypatch, env, *, mixed=False, unknown=True, original="ENABLE"):
    refs = [env.refs["ad"]]
    if mixed:
        refs = [
            EntityRef(env.context.tenant_id, env.account.advertiser_id, kind, name)
            for kind, name in (
                ("campaign", "a-rejected"),
                ("adgroup", "b-unknown"),
                ("ad", "c-accepted"),
                ("campaign", "d-nochange"),
            )
        ]
    target = "DISABLE" if original == "ENABLE" else "ENABLE"
    with Session(engine) as db, db.begin():
        for ref in refs:
            db.merge(
                _row(
                    ref,
                    parent=env.refs.get(
                        {"ad": "adgroup", "adgroup": "campaign"}.get(ref.kind)
                    ),
                    ad_type="SMART_PLUS",
                    status=target if ref.remote_id == "d-nochange" else original,
                    connection_id=env.route.connection_id,
                    configuration={
                        "promotion_type": "NATIVE_SERIES",
                        "native_series_id": "series",
                        "identity_type": "TT_USER",
                        "identity_id": "owner",
                    },
                )
            )
        selection = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(selection, refs)
        db.add(selection)
    calls, state = [], {"read": target, "unknown": unknown, "late": None}

    def wire(pool, method, url, **kwargs):
        body = (
            json.loads(kwargs["body"]) if method == "POST" else dict(kwargs["fields"])
        )
        calls.append((method, url, body))
        assert body["advertiser_id"] == env.account.advertiser_id
        assert "/open_api/v1.3/" in url
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer:
            observer[0].attempt(observer[1])
        if method == "POST":
            kind = url.split("/smart_plus/")[1].split("/")[0]
            assert url.endswith(f"/smart_plus/{kind}/status/update/")
            ids = body[
                {
                    "ad": "smart_plus_ad_ids",
                    "campaign": "campaign_ids",
                    "adgroup": "adgroup_ids",
                }[kind]
            ]
            if "a-rejected" in ids:
                return HTTPResponse(
                    body=b'{"code":40002,"message":"explicit refusal","request_id":"reject-id"}',
                    status=200,
                )
            if state["unknown"] and ("b-unknown" in ids or not mixed):
                raise TimeoutError("sent, response lost")
            data = {
                {
                    "ad": "smart_plus_ad_ids",
                    "campaign": "campaign_ids",
                    "adgroup": "adgroup_ids",
                }[kind]: ids
            }
        else:
            assert method == "GET" and url.endswith("/get/")
            filtering = json.loads(body["filtering"])
            ids = [
                v
                for values in filtering.values()
                if isinstance(values, list)
                for v in values
            ]
            kind = (
                url.split("/open_api/v1.3/")[1]
                .removeprefix("smart_plus/")
                .split("/")[0]
            )
            found = [
                {
                    {
                        "ad": "smart_plus_ad_id",
                        "campaign": "campaign_id",
                        "adgroup": "adgroup_id",
                    }[kind]: ref.remote_id,
                    **(
                        {"adgroup_id": "adgroup-native"}
                        if kind == "ad"
                        else {"campaign_id": "campaign-native"}
                        if kind == "adgroup"
                        else {}
                    ),
                    "advertiser_id": env.account.advertiser_id,
                    "ad_name": "native observed",
                    "operation_status": state["read"],
                    "creative_list": [],
                }
                for ref in refs
                if "/smart_plus/" in url
                and ref.kind == kind
                and ref.remote_id in ids
                and state["read"] is not None
            ]
            data = {
                "list": found,
                "page_info": {
                    "page": 1,
                    "page_size": int(body["page_size"]),
                    "total_page": 1 if found else 0,
                    "total_number": len(found),
                },
            }
            if state["late"]:
                callback, state["late"] = state["late"], None
                callback()
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "wire-" + method, "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    return calls, state, target


def submit(client, env, target):
    base = f"/api/tenants/{env.context.tenant_id}"
    response = client.post(
        base + "/ad-management-requests",
        json={
            "bc_id": env.bc.bc_id,
            "selection_id": str(env.selection.id),
            "mutation": {"field": "status", "mode": "set", "value": target},
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    return base, response.json()["task_id"]


def public(client, base, task):
    response = client.get(base + f"/ad-management-tasks/{task}")
    assert response.status_code == 200, response.text
    return response.json()


def reconcile(env, *, payload=None):
    if payload is None:
        with Session(engine) as db:
            request = db.exec(
                select(ManagementReceipt)
                .where(
                    ManagementReceipt.tenant_id == env.context.tenant_id,
                    ManagementReceipt.response["kind"].astext
                    == "state_observation_request",
                )
                .order_by(ManagementReceipt.observed_at.desc())
            ).first()
            message = db.exec(
                select(PendingDispatch)
                .where(
                    PendingDispatch.payload["round"].astext
                    == request.response["round"],
                    PendingDispatch.payload["attempt"].as_integer()
                    == request.response["attempt"],
                    PendingDispatch.tenant_id == env.context.tenant_id,
                    PendingDispatch.task_name == "ad_management.reconcile",
                )
                .order_by(PendingDispatch.available_at.desc())
            ).first()
            assert message is not None
            payload = dict(message.payload)
    reconcile_management_task(
        tenant_id=str(env.context.tenant_id),
        actor_id=str(env.context.actor_id),
        payload=payload,
    )
    return payload


def test_native_mixed_results_continue(native_status_env, redis_client, monkeypatch):
    env = native_status_env
    calls, state, target = setup_wire(monkeypatch, env, mixed=True)
    with TestClient(env.app) as client:
        base, task = submit(client, env, target)
        drive_management(env, task)
        drive_management(env, task)
        items = {i["ref"]["remote_id"]: i for i in public(client, base, task)["items"]}
        assert {k: v["execution_result"] for k, v in items.items()} == {
            "a-rejected": "REJECTED",
            "b-unknown": "UNKNOWN",
            "c-accepted": "ACCEPTED",
            "d-nochange": "NO_CHANGE",
        }
        assert items["a-rejected"]["attempts"][0]["request_id"] == "reject-id"
        assert items["a-rejected"]["reason"]
        assert items["b-unknown"]["attempts"][0]["request_id"] is None
        assert items["c-accepted"]["observation_state"] == "REFRESH_PENDING"
        assert items["d-nochange"]["attempts"] == []
        for key, item in items.items():
            assert item["original_value"] == (
                target if key == "d-nochange" else "ENABLE"
            )
            assert item["final_value"] == target
        assert len([c for c in calls if c[0] == "POST"]) == 3
        auto = reconcile(env)
        drive_directory(env, redis_client)
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 3
        items = {i["ref"]["remote_id"]: i for i in public(client, base, task)["items"]}
        assert items["b-unknown"]["execution_result"] == "UNKNOWN"
        assert items["b-unknown"]["observation_state"] == "OBSERVED_MATCH"
        assert items["b-unknown"]["attempts"][0]["request_id"] is None
        assert items["c-accepted"]["observation_state"] == "ACCEPTED", (
            items["c-accepted"],
            calls,
        )
        reconcile(env, payload=auto)
        client.post(
            base + f"/ad-management-tasks/{task}/retry",
            json={"idempotency_key": str(uuid4())},
        )
        client.post(base + f"/ad-management-tasks/{task}/refresh")
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 3


@pytest.mark.parametrize(
    "observation, expected",
    [
        ("DISABLE", "OBSERVED_MATCH"),
        ("ENABLE", "OBSERVED_DIFFERENT"),
        (None, "RECONCILE_FAILED"),
        ("stale", "RECONCILE_FAILED"),
    ],
)
def test_native_unknown_rounds(native_status_env, monkeypatch, observation, expected):
    env = native_status_env
    calls, state, target = setup_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit(client, env, target)
        drive_management(env, task)
        state["read"] = "DISABLE" if observation == "stale" else observation
        if observation == "stale":
            from app.integrations.tiktok.adapters import ads_read

            class Stale(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) - timedelta(hours=1)

            monkeypatch.setattr(ads_read, "datetime", Stale)
        before_reads = len(
            [c for c in calls if c[0] == "GET" and int(c[2]["page_size"]) == 100]
        )
        auto = reconcile(env)
        assert len([c for c in calls if c[0] == "POST"]) == 1
        if observation in {None, "stale"}:
            assert (
                public(client, base, task)["items"][0]["observation_state"]
                == "RECONCILE_REQUESTED"
            )
            reconcile(env)
            last = reconcile(env)
            reconcile(env, payload=last)
        item = public(client, base, task)["items"][0]
        assert item["observation_state"] == expected
        assert item["execution_result"] == "UNKNOWN"
        assert (item["original_value"], item["final_value"]) == ("ENABLE", "DISABLE")
        assert item["attempts"][0]["request_id"] is None
        assert len(
            [c for c in calls if c[0] == "GET" and int(c[2]["page_size"]) == 100]
        ) - before_reads == (3 if observation in {None, "stale"} else 1)
        if observation == "stale":
            monkeypatch.setattr(ads_read, "datetime", datetime)
        client.post(base + f"/ad-management-tasks/{task}/reconcile")
        state["late"] = lambda: client.post(
            base + f"/ad-management-tasks/{task}/reconcile"
        )
        reconcile(env)
        assert (
            public(client, base, task)["items"][0]["observation_state"]
            == "RECONCILE_REQUESTED"
        )
        state["read"] = "ENABLE"
        reconcile(env)
        count = len(calls)
        reconcile(env, payload=auto)
        reconcile(env)
        assert len(calls) == count
        assert (
            public(client, base, task)["items"][0]["observation_state"]
            == "OBSERVED_DIFFERENT"
        )
        assert public(client, base, task)["items"][0]["execution_result"] == "UNKNOWN"
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("original", ["ENABLE", "DISABLE"])
@pytest.mark.parametrize(
    "current", ["sent", "original", "missing", "unsafe", "changed"]
)
def test_native_restore_current_gate(
    native_status_env, redis_client, monkeypatch, original, current, unknown
):
    env = native_status_env
    calls, state, target = setup_wire(
        monkeypatch, env, original=original, unknown=unknown
    )
    with TestClient(env.app) as client:
        base, task = submit(client, env, target)
        drive_management(env, task)
        if unknown:
            # 独立目录同步发布当前事实，不伪造原请求回执或核查结果。
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
                    refs=[env.refs["ad"]],
                )
        drive_directory(env, redis_client)
        drive_management(env, task)
        frozen = public(client, base, task)["items"][0]
        assert frozen["execution_result"] == ("UNKNOWN" if unknown else "ACCEPTED")
        state["unknown"] = False
        if current != "sent":
            with Session(engine) as db, db.begin():
                row = db.get(
                    AdObject,
                    (
                        env.context.tenant_id,
                        env.account.advertiser_id,
                        "ad",
                        "ad-native",
                    ),
                )
                if current == "missing":
                    db.delete(row)
                else:
                    row.operation_status = (
                        original
                        if current == "original"
                        else "DELETE"
                        if current == "changed"
                        else None
                    )
                    db.add(row)
        response = client.post(base + f"/ad-management-tasks/{task}/restore")
        assert response.status_code == 201, response.text
        preview = response.json()
        item = preview["items"][0]
        expected = (
            "PENDING"
            if current == "sent"
            else "NO_CHANGE"
            if current == "original"
            else "CONFLICT"
        )
        assert item["execution_result"] == expected
        assert item["final_value"] == original
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
        state["read"] = original
        drive_management(env, restore)
        if current == "sent":
            drive_directory(env, redis_client)
            drive_management(env, restore)
        assert len([c for c in calls if c[0] == "POST"]) == (
            2 if current == "sent" else 1
        )
        assert public(client, base, restore)["items"][0]["execution_result"] == (
            "ACCEPTED" if current == "sent" else expected
        )
        assert public(client, base, task)["items"][0] == frozen


@pytest.mark.parametrize("observation", ["match", "different", "missing", "stale"])
def test_native_unknown_workspace_restore(
    native_status_env, redis_client, monkeypatch, observation
):
    from tests.modules.ad_management.test_native_status import run_native_workspace

    env = native_status_env
    calls, state, target = setup_wire(monkeypatch, env)

    def advance(task_id: str, phase: str):
        if phase == "write":
            drive_management(env, task_id)
        elif phase == "reconcile":
            from app.integrations.tiktok.adapters import ads_read

            state["read"] = (
                "ENABLE"
                if observation == "different"
                else None
                if observation == "missing"
                else target
            )
            if observation == "stale":

                class Stale(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return datetime.now(tz) - timedelta(hours=1)

                monkeypatch.setattr(ads_read, "datetime", Stale)
            try:
                for _ in range(3 if observation in {"missing", "stale"} else 1):
                    reconcile(env)
            finally:
                monkeypatch.setattr(ads_read, "datetime", datetime)
        elif phase == "publish":
            state["read"] = target
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
                    refs=[env.refs["ad"]],
                )
            drive_directory(env, redis_client)
        else:
            state["unknown"] = False
            state["read"] = "ENABLE"
            drive_management(env, task_id)
            drive_directory(env, redis_client)
            drive_management(env, task_id)
        return {"advanced": True}

    run_native_workspace(
        env,
        "ad",
        target,
        advance,
        grep="@native-recovery-live",
        extra_env={"NATIVE_RECOVERY_OBSERVATION": observation},
    )
    assert len([c for c in calls if c[0] == "POST"]) == 2


@pytest.mark.parametrize(
    "boundary", ["grant", "connection", "actor", "route", "account", "bc"]
)
@pytest.mark.parametrize("phase", ["read", "restore"])
def test_native_recovery_revocation(
    native_status_env, redis_client, monkeypatch, boundary, phase
):
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection
    from app.modules.tenants.models import TenantMembership

    env = native_status_env
    calls, state, target = setup_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit(client, env, target)
        drive_management(env, task)
        if phase == "restore":
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
                    refs=[env.refs["ad"]],
                )
            drive_directory(env, redis_client)
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
            assert response.status_code == 202, response.text
            restore_task = response.json()["task_id"]
            state["unknown"] = False
        before = len(calls)
        with Session(engine) as db, db.begin():
            if boundary in {"grant", "account"}:
                row = db.get(
                    BCAccountAccess,
                    (
                        env.context.tenant_id,
                        env.bc.bc_id,
                        env.account.advertiser_id,
                        env.route.connection_id,
                    ),
                )
                if boundary == "grant":
                    row.authorized = False
                else:
                    row.active = False
            elif boundary == "actor":
                row = db.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                row.active = False
            elif boundary == "bc":
                row = db.exec(
                    select(BCConnectionBinding).where(
                        BCConnectionBinding.tenant_id == env.context.tenant_id,
                        BCConnectionBinding.bc_id == env.bc.bc_id,
                    )
                ).one()
                row.status = "DISABLED"
            else:
                row = db.get(TikTokConnection, env.route.connection_id)
                if boundary == "route":
                    row.authorization_revision += 1
                else:
                    row.status = "DISABLED"
            if row:
                db.add(row)
        if phase == "read":
            reconcile(env)
        else:
            drive_management(env, restore_task)
        assert len(calls) == before
