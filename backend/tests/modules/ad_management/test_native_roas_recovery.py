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
from tests.modules.ad_management.test_native_recovery import public
from tests.modules.ad_management.test_native_roas import configure
from tests.modules.ad_management.test_native_status import (
    drive_directory,
)
from tests.modules.ad_management.test_native_status import (
    drive_management as drive_once,
)
from tests.modules.ad_management.test_native_status import (
    native_status_env as native_status_env,
)
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


def drive_management(env, task):
    # ABO 同父共享协调锁；沿原 Worker 重入处理被持久延后的兄弟。
    from sqlmodel import select

    from app.modules.ad_management.models import ManagementTaskItem

    for _ in range(3):
        drive_once(env, task)
        with Session(engine) as db:
            pending = db.exec(
                select(ManagementTaskItem.id).where(
                    ManagementTaskItem.task_id == UUID(str(task)),
                    ManagementTaskItem.execution_result == "PENDING",
                )
            ).first()
        if pending is None:
            break


def reconcile(env, *, payload=None):
    from sqlmodel import select

    from app.jobs.models import PendingDispatch
    from app.modules.ad_management.models import ManagementReceipt
    from app.modules.ad_management.reconciliation_tasks import reconcile_management_task

    if payload is not None:
        payloads = [payload]
    else:
        with Session(engine) as db:
            # 每项人工 round 独立；先捕获本轮所有持久消息，读内产生的新 round 留给下一次驱动。
            requests = db.exec(
                select(ManagementReceipt)
                .where(
                    ManagementReceipt.tenant_id == env.context.tenant_id,
                    ManagementReceipt.response["kind"].astext
                    == "state_observation_request",
                )
                .distinct(ManagementReceipt.attempt_id)
                .order_by(
                    ManagementReceipt.attempt_id, ManagementReceipt.observed_at.desc()
                )
            ).all()
            messages = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == env.context.tenant_id,
                    PendingDispatch.task_name == "ad_management.reconcile",
                )
            ).all()
            payloads = [
                dict(m.payload)
                for m in messages
                if any(
                    m.payload["round"] == r.response["round"]
                    and m.payload["attempt"] == r.response["attempt"]
                    for r in requests
                )
            ]
    assert payloads
    for message in payloads:
        reconcile_management_task(
            tenant_id=str(env.context.tenant_id),
            actor_id=str(env.context.actor_id),
            payload=message,
        )
    return payloads[0]


def roas_wire(monkeypatch, env, cbo=True, unknown=True):
    configure(env, cbo)
    calls, state = (
        [],
        {
            "unknown": unknown,
            "read": {
                "adgroup-native": "1.3200",
                "adgroup-peer": "1.32" if cbo else "1.65",
            },
            "late": None,
            "incomplete": False,
        },
    )

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
            assert Decimal(str(body["roas_bid"])) in {
                Decimal("1.32"),
                Decimal("1.65"),
                Decimal("1.2"),
                Decimal("1.5"),
            }
            if state["unknown"]:
                raise TimeoutError("sent, response lost")
            data = {}
        else:
            ids = json.loads(body["filtering"]).get("adgroup_ids", [])
            found = (
                [
                    {
                        "adgroup_id": identity,
                        "campaign_id": "campaign-native",
                        "advertiser_id": env.account.advertiser_id,
                        "roas_bid": state["read"][identity],
                        "deep_bid_type": "VO_MIN_ROAS",
                        "operation_status": "ENABLE",
                        "promotion_type": "NATIVE_SERIES",
                    }
                    for identity in ids
                    if identity in state["read"] and state["read"][identity] is not None
                ]
                if url.endswith("/smart_plus/adgroup/get/")
                else []
            )
            data = {
                "list": found,
                "page_info": {
                    "page": 1,
                    "page_size": int(body["page_size"]),
                    "total_page": 2 if state["incomplete"] else int(bool(found)),
                    "total_number": len(found),
                },
            }
            if state["late"]:
                callback, state["late"] = state["late"], None
                callback()
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "jb65-wire", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    return calls, state


def submit_roas(client, env):
    from app.modules.ad_management.preparation import prepare_direct_task

    base = f"/api/tenants/{env.context.tenant_id}"
    response = client.post(
        base + "/ad-management-requests",
        json={
            "bc_id": env.bc.bc_id,
            "selection_id": str(env.selection.id),
            "mutation": {"field": "roas", "mode": "increase_percent", "value": "10"},
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    task = response.json()["task_id"]
    prepare_direct_task(engine, task)
    return base, task


def publish(env, redis_client):
    from app.integrations.tiktok.contracts.ads import EntityRef
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
            refs=[
                env.refs["adgroup"],
                EntityRef(
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    "adgroup",
                    "adgroup-peer",
                ),
            ],
        )
    drive_directory(env, redis_client)


def confirm(client, base, preview):
    response = client.post(
        base + "/ad-management-tasks",
        json={
            "preview_id": preview["preview_id"],
            "preview_digest": preview["digest"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


@pytest.mark.parametrize("cbo", [True, False])
@pytest.mark.parametrize("unknown", [True, False])
@pytest.mark.parametrize("restore_unknown", [True, False])
def test_roas_reverse(
    native_status_env, redis_client, monkeypatch, cbo, unknown, restore_unknown
):
    env = native_status_env
    calls, state = roas_wire(monkeypatch, env, cbo, unknown)
    with TestClient(env.app) as client:
        base, task = submit_roas(client, env)
        drive_management(env, task)
        assert all(
            i["execution_result"] == ("UNKNOWN" if unknown else "ACCEPTED")
            for i in public(client, base, task)["items"]
            if i["ref"]["kind"] == "adgroup"
        ), [
            (i["ref"]["remote_id"], i["execution_result"], i["reason"])
            for i in public(client, base, task)["items"]
        ]
        if unknown:
            reconcile(env)
        publish(env, redis_client)
        drive_management(env, task)
        frozen = [
            i
            for i in public(client, base, task)["items"]
            if i["ref"]["kind"] == "adgroup"
        ]
        response = client.post(base + f"/ad-management-tasks/{task}/restore")
        assert response.status_code == 201, response.text
        preview = response.json()
        assert len([i for i in preview["items"] if i["ref"]["kind"] == "adgroup"]) == 2
        for item, old in zip(
            [i for i in preview["items"] if i["ref"]["kind"] == "adgroup"],
            frozen,
            strict=True,
        ):
            assert item["roas_facts"] == old["roas_facts"]
            assert Decimal(item["restore_observation"]["value"]) == Decimal(
                old["final_value"]
            )
            assert item["restore_observation"]["source_task_id"] == task
            assert Decimal(item["original_value"]) == Decimal(old["final_value"])
            assert Decimal(item["final_value"]) == Decimal(old["original_value"])
            assert item["execution_result"] == "PENDING", (
                item["ref"]["remote_id"],
                item["reason"],
                [(i["ref"]["remote_id"], i["execution_result"]) for i in frozen],
            )
        restore = confirm(client, base, preview)
        state["unknown"] = restore_unknown
        state["read"] = {
            "adgroup-native": "1.2000",
            "adgroup-peer": "1.20" if cbo else "1.5000",
        }
        drive_management(env, restore)
        if restore_unknown:
            reconcile(env)
        else:
            drive_directory(env, redis_client)
        drive_management(env, restore)
        restored = [
            i
            for i in public(client, base, restore)["items"]
            if i["ref"]["kind"] == "adgroup"
        ]
        assert all(
            i["execution_result"] == ("UNKNOWN" if restore_unknown else "ACCEPTED")
            for i in restored
        ), restored
        assert all(
            i["observation_state"]
            == ("OBSERVED_MATCH" if restore_unknown else "ACCEPTED")
            for i in restored
        ), restored
        assert all(len(i["attempts"]) == 1 for i in restored)
        assert [
            i
            for i in public(client, base, task)["items"]
            if i["ref"]["kind"] == "adgroup"
        ] == frozen
        assert len([c for c in calls if c[0] == "POST"]) == (2 if cbo else 4)
        with Session(engine) as db:
            from app.modules.ad_management.models import ManagementPreview

            assert (
                db.get(
                    ManagementPreview, db.get(ManagementTask, UUID(restore)).preview_id
                ).mutation["source_task_id"]
                == task
            )


@pytest.mark.parametrize(
    "gate",
    [
        "changed",
        "missing",
        "stale",
        "connection",
        "grant",
        "manage",
        "route",
        "actor",
        "original",
        "members",
        "deleted",
        "frozen_original",
    ],
)
@pytest.mark.parametrize("phase", ["preview", "send"])
def test_roas_restore_current_gate(
    native_status_env, redis_client, monkeypatch, gate, phase
):
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection
    from app.modules.tenants.models import TenantMembership

    env = native_status_env
    calls, state = roas_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit_roas(client, env)
        drive_management(env, task)
        publish(env, redis_client)
        if phase == "send":
            response = client.post(base + f"/ad-management-tasks/{task}/restore")
            assert response.status_code == 201, response.text
            preview = response.json()
            assert (
                next(i for i in preview["items"] if i["ref"]["kind"] == "adgroup")[
                    "execution_result"
                ]
                == "PENDING"
            )
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
                    "adgroup",
                    "adgroup-peer",
                ),
            )
            if gate in {"changed", "missing"}:
                row.configuration = {
                    **row.configuration,
                    "roas_bid": "1.99" if gate == "changed" else None,
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
            elif gate == "members":
                from app.integrations.tiktok.contracts.ads import EntityRef
                from tests.modules.ad_management.conftest import _row

                row = _row(
                    EntityRef(
                        env.context.tenant_id,
                        env.account.advertiser_id,
                        "adgroup",
                        "new-peer",
                    ),
                    parent=env.refs["campaign"],
                    ad_type="SMART_PLUS",
                    connection_id=env.route.connection_id,
                    configuration={"roas_bid": "1.32", "deep_bid_type": "VO_MIN_ROAS"},
                )
            elif gate == "deleted":
                db.delete(row)
                row = None
            elif gate == "original":
                row.configuration = {**row.configuration, "roas_bid": "1.2000"}
            elif gate == "frozen_original":
                from sqlmodel import select

                from app.modules.ad_management.models import (
                    ManagementTaskItem,
                )

                # 合成不一致历史，验证整系列不能拿首成员原值覆盖兄弟。
                if phase == "preview":
                    row = db.exec(
                        select(ManagementTaskItem).where(
                            ManagementTaskItem.task_id == UUID(task),
                            ManagementTaskItem.ref["remote_id"].astext
                            == "adgroup-peer",
                        )
                    ).one()
                    row.original_value = "1.5"
                else:
                    row = db.exec(
                        select(ManagementTaskItem).where(
                            ManagementTaskItem.task_id == UUID(restore),
                            ManagementTaskItem.ref["remote_id"].astext
                            == "adgroup-peer",
                        )
                    ).one()
                    row.final_value = "1.5"
            else:
                row = db.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                row.active = False
            if row is not None:
                db.add(row)
        count = len(calls)
        if phase == "send":
            state["unknown"] = False
            drive_management(env, restore)
        else:
            response = client.post(base + f"/ad-management-tasks/{task}/restore")
            if response.status_code == 201:
                assert all(
                    i["execution_result"] == "CONFLICT"
                    for i in response.json()["items"]
                    if i["ref"]["kind"] == "adgroup"
                ), response.json()
            else:
                assert response.status_code in {403, 404, 409}
        assert len(calls) == count


@pytest.mark.parametrize(
    "observation, expected",
    [
        ("1.32000", "OBSERVED_MATCH"),
        ("1.99", "OBSERVED_DIFFERENT"),
        (None, "RECONCILE_FAILED"),
        ("stale", "RECONCILE_FAILED"),
        ("incomplete", "RECONCILE_FAILED"),
    ],
)
def test_roas_unknown_rounds(native_status_env, monkeypatch, observation, expected):
    env = native_status_env
    calls, state = roas_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit_roas(client, env)
        drive_management(env, task)
        state["read"] = {
            "adgroup-native": "1.32",
            "adgroup-peer": "1.32"
            if observation in {"stale", "incomplete"}
            else observation,
        }
        if observation == "incomplete":
            state["incomplete"] = True
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
        if observation in {None, "stale", "incomplete"}:
            assert (
                next(
                    i
                    for i in public(client, base, task)["items"]
                    if i["ref"]["remote_id"] == "adgroup-peer"
                )["observation_state"]
                == "RECONCILE_REQUESTED"
            )
            reconcile(env)
            last = reconcile(env)
            reconcile(env, payload=last)
        item = next(
            i
            for i in public(client, base, task)["items"]
            if i["ref"]["remote_id"] == "adgroup-peer"
        )
        assert item["observation_state"] == expected
        assert item["execution_result"] == "UNKNOWN"
        assert len(item["attempts"]) == 1
        assert (Decimal(item["original_value"]), Decimal(item["final_value"])) == (
            Decimal("1.20"),
            Decimal("1.32"),
        )
        assert item["attempts"][0]["request_id"] is None
        assert (
            len([c for c in calls if c[0] == "GET" and int(c[2]["page_size"]) == 100])
            - before_reads
            <= 6
        )
        if observation == "stale":
            monkeypatch.setattr(ads_read, "datetime", datetime)
        state["incomplete"] = False
        client.post(base + f"/ad-management-tasks/{task}/reconcile")
        state["late"] = lambda: client.post(
            base + f"/ad-management-tasks/{task}/reconcile"
        )
        reconcile(env)
        assert (
            next(
                i
                for i in public(client, base, task)["items"]
                if i["ref"]["remote_id"] == "adgroup-peer"
            )["observation_state"]
            == "RECONCILE_REQUESTED"
        )
        state["read"] = {"adgroup-native": "1.99", "adgroup-peer": "1.99"}
        reconcile(env)
        count = len(calls)
        reconcile(env, payload=auto)
        reconcile(env)
        assert len(calls) == count
        assert (
            next(
                i
                for i in public(client, base, task)["items"]
                if i["ref"]["remote_id"] == "adgroup-peer"
            )["observation_state"]
            == "OBSERVED_DIFFERENT"
        )
        assert (
            next(
                i
                for i in public(client, base, task)["items"]
                if i["ref"]["remote_id"] == "adgroup-peer"
            )["execution_result"]
            == "UNKNOWN"
        )
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("cbo", [True, False])
def test_roas_recovery_workspace(native_status_env, redis_client, monkeypatch, cbo):
    from tests.modules.ad_management.test_native_status import run_native_workspace

    env = native_status_env
    calls, state = roas_wire(monkeypatch, env, cbo)
    state["read"] = {"adgroup-native": "1.32", "adgroup-peer": "1.32"}

    def advance(task_id, phase):
        if phase == "write":
            drive_management(env, task_id)
        elif phase == "reconcile":
            reconcile(env)
        elif phase == "publish":
            publish(env, redis_client)
        else:
            state["unknown"] = False
            state["read"] = {
                "adgroup-native": "1.2000",
                "adgroup-peer": "1.20" if cbo else "1.5000",
            }
            drive_management(env, task_id)
            drive_directory(env, redis_client)
            drive_management(env, task_id)
        return {"advanced": True}

    run_native_workspace(
        env, "campaign", "1.32", advance, grep="@native-roas-recovery-live"
    )
    assert len([c for c in calls if c[0] == "POST"]) == (2 if cbo else 4)


@pytest.mark.parametrize("cbo", [True, False])
def test_roas_restore_already_original(
    native_status_env, redis_client, monkeypatch, cbo
):
    env = native_status_env
    calls, state = roas_wire(monkeypatch, env, cbo)
    with TestClient(env.app) as client:
        base, task = submit_roas(client, env)
        drive_management(env, task)
        state["read"] = {
            "adgroup-native": "1.2000",
            "adgroup-peer": "1.20" if cbo else "1.5000",
        }
        publish(env, redis_client)
        response = client.post(base + f"/ad-management-tasks/{task}/restore")
        assert response.status_code == 201, response.text
        preview = response.json()
        assert all(
            i["execution_result"] == "NO_CHANGE"
            for i in preview["items"]
            if i["ref"]["kind"] == "adgroup"
        )
        restore = confirm(client, base, preview)
        drive_management(env, restore)
        assert len([c for c in calls if c[0] == "POST"]) == (1 if cbo else 2)


def test_roas_mixed_and_revoked_unknown(native_status_env, redis_client, monkeypatch):
    from sqlmodel import select

    from app.modules.accounts.models import BCAccountAccess
    from app.modules.ad_management.models import ManagementTaskItem

    env = native_status_env
    calls, state = roas_wire(monkeypatch, env, False)
    transport = __import__("urllib3").PoolManager.request

    def mixed(pool, method, url, **kwargs):
        if method == "POST":
            state["unknown"] = (
                json.loads(kwargs["body"])["adgroup_id"] == "adgroup-peer"
            )
        return transport(pool, method, url, **kwargs)

    monkeypatch.setattr("urllib3.PoolManager.request", mixed)
    with TestClient(env.app) as client:
        base, task = submit_roas(client, env)
        drive_management(env, task)
        items = {i["ref"]["remote_id"]: i for i in public(client, base, task)["items"]}
        assert items["adgroup-native"]["execution_result"] == "ACCEPTED"
        assert items["adgroup-peer"]["execution_result"] == "UNKNOWN"
        before = items["adgroup-peer"]
        drive_directory(env, redis_client)
        drive_management(env, task)
        assert (
            next(
                i
                for i in public(client, base, task)["items"]
                if i["ref"]["remote_id"] == "adgroup-native"
            )["observation_state"]
            == "ACCEPTED"
        )
        with Session(engine) as db, db.begin():
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
        count = len(calls)
        reconcile(env)
        assert len(calls) == count
        with Session(engine) as db:
            peer = db.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == UUID(task),
                    ManagementTaskItem.ref["remote_id"].astext == "adgroup-peer",
                )
            ).one()
            assert peer.observation_state == "RECONCILE_FAILED"
            assert peer.execution_result == "UNKNOWN"
            assert peer.original_value == before["original_value"]
            assert peer.final_value == before["final_value"]
        assert len([c for c in calls if c[0] == "POST"]) == 2


def test_roas_restore_member_own_send_time(
    native_status_env, redis_client, monkeypatch
):
    from sqlmodel import select

    from app.modules.ad_management.models import (
        ManagementRequestAttempt,
        ManagementTaskItem,
    )

    env = native_status_env
    calls, state = roas_wire(monkeypatch, env)
    with TestClient(env.app) as client:
        base, task = submit_roas(client, env)
        drive_management(env, task)
        publish(env, redis_client)
        with Session(engine) as db, db.begin():
            latest = db.exec(
                select(ManagementRequestAttempt, ManagementTaskItem)
                .join(
                    ManagementTaskItem,
                    ManagementRequestAttempt.task_item_id == ManagementTaskItem.id,
                )
                .where(ManagementTaskItem.task_id == UUID(task))
                .order_by(ManagementRequestAttempt.request_at.desc())
            ).first()
            attempt, item = latest
            row = db.get(
                AdObject,
                (
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    "adgroup",
                    item.ref["remote_id"],
                ),
            )
            # 该成员证据早于自己的发送，但晚于兄弟发送；全组必须拒绝。
            row.observed_at = attempt.request_at - timedelta(microseconds=1)
            db.add(row)
        preview = client.post(base + f"/ad-management-tasks/{task}/restore")
        assert preview.status_code == 201, preview.text
        assert all(
            i["execution_result"] == "CONFLICT"
            for i in preview.json()["items"]
            if i["ref"]["kind"] == "adgroup"
        ), preview.json()
        assert len([c for c in calls if c[0] == "POST"]) == 1
