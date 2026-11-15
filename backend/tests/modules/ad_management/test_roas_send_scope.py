"""JB66: frozen CBO scope cannot be trimmed before a series write."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import TikTokConnection
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTaskItem,
)
from app.modules.ad_management.preparation import prepare_direct_task
from app.modules.ads.models import AdObject
from tests.modules.ad_management.conftest import _row
from tests.modules.ad_management.test_native_roas import configure, wire
from tests.modules.ad_management.test_native_status import drive_management
from tests.modules.ad_management.test_native_status import (
    native_status_env as native_status_env,
)
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


def change_peer(env, change):
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
        if change == "original":
            peer.configuration = {**peer.configuration, "roas_bid": "1.25"}
        elif change == "source":
            other = TikTokConnection(tenant_id=env.context.tenant_id, status="ACTIVE")
            db.add(other)
            db.flush()
            peer.source_connection_id = other.id
        elif change == "mode":
            peer.configuration = {**peer.configuration, "deep_bid_type": "OTHER"}
        elif change == "deleted":
            db.delete(peer)
            return
        elif change == "added":
            peer = _row(
                EntityRef(
                    env.context.tenant_id,
                    env.account.advertiser_id,
                    "adgroup",
                    "new-peer",
                ),
                parent=env.refs["campaign"],
                ad_type="SMART_PLUS",
                connection_id=env.route.connection_id,
                configuration=dict(peer.configuration),
            )
        db.add(peer)


def submit(client, env):
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
    return base + f"/ad-management-tasks/{task}", task


def groups(client, url):
    return [i for i in client.get(url).json()["items"] if i["ref"]["kind"] == "adgroup"]


@pytest.mark.parametrize(
    "change, phase",
    [
        ("original", "before_claim"),
        ("source", "before_claim"),
        ("mode", "before_claim"),
        ("added", "before_claim"),
        ("deleted", "before_claim"),
        ("original", "before_send"),
        ("source", "before_send"),
        ("original", "excluded"),
        ("source", "excluded"),
    ],
)
def test_cbo_conflict_zero_write(native_status_env, monkeypatch, change, phase):
    env = native_status_env
    configure(env, True)
    calls = wire(monkeypatch, env)
    with TestClient(env.app) as client:
        url, task = submit(client, env)
        frozen = groups(client, url)
        assert len(frozen) == 2
        assert all(i["execution_result"] == "PENDING" for i in frozen)
        changed = []
        if phase == "before_send":
            real_eval = Redis.eval

            def eval_with_change(cache, script, numkeys, *args, **kwargs):
                result = real_eval(cache, script, numkeys, *args, **kwargs)
                if not changed and "pexpire" in script:
                    # 保留真实 Redis 围栏，在领取已提交、发送前续租时模拟目录发布。
                    change_peer(env, change)
                    changed.append(True)
                return result

            monkeypatch.setattr(Redis, "eval", eval_with_change)
        else:
            change_peer(env, change)
            if phase == "excluded":
                # 恢复投递可能只剩首项可运行；冻结范围不能随待发送队列缩小。
                with Session(engine) as db, db.begin():
                    peer = db.exec(
                        select(ManagementTaskItem).where(
                            ManagementTaskItem.task_id == UUID(task),
                            ManagementTaskItem.ref["remote_id"].astext
                            == "adgroup-peer",
                        )
                    ).one()
                    peer.execution_result = "CONFLICT"
                    peer.reason = "management_original_changed"
                    db.add(peer)
        drive_management(env, task)
        drive_management(env, task)
        items = groups(client, url)
        writes = len([c for c in calls if c[0] == "POST"])
        if phase == "before_send":
            assert changed, "late interference must run after claim"
        assert writes == 0, f"CBO scope changed: expected 0 writes, observed {writes}"
        assert all(i["execution_result"] in {"CONFLICT", "NOT_SENT"} for i in items), (
            items
        )
        assert [i["roas_facts"] for i in items] == [i["roas_facts"] for i in frozen]


@pytest.mark.parametrize("change", ["original", "source"])
def test_abo_conflict_keeps_independent_owner(native_status_env, monkeypatch, change):
    env = native_status_env
    configure(env, False)
    calls = wire(monkeypatch, env)
    with TestClient(env.app) as client:
        url, task = submit(client, env)
        change_peer(env, change)
        drive_management(env, task)
        drive_management(env, task)
        items = {i["ref"]["remote_id"]: i for i in groups(client, url)}
        assert items["adgroup-native"]["execution_result"] == "ACCEPTED"
        assert items["adgroup-peer"]["execution_result"] in {"CONFLICT", "NOT_SENT"}
        writes = [c[2] for c in calls if c[0] == "POST"]
        assert len(writes) == 1
        assert writes[0]["adgroup_id"] == "adgroup-native"
        assert writes[0]["roas_bid"] == 1.32


@pytest.mark.parametrize("sibling", ["waiting", "terminal", "excluded"])
def test_cbo_retry_preserves_complete_receipts(native_status_env, monkeypatch, sibling):
    env = native_status_env
    configure(env, True)
    calls = wire(monkeypatch, env)
    with TestClient(env.app) as client:
        url, task = submit(client, env)
        with Session(engine) as db, db.begin():
            members = db.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == UUID(task),
                    ManagementTaskItem.ref["kind"].astext == "adgroup",
                )
            ).all()
            for member in members:
                peer = member.ref["remote_id"] == "adgroup-peer"
                member.execution_result = (
                    "CONFLICT" if peer and sibling == "excluded" else "NOT_SENT"
                )
                db.add(member)
                db.add(
                    ManagementRequestAttempt(
                        tenant_id=member.tenant_id,
                        task_item_id=member.id,
                        attempt=1,
                        outcome="NOT_SENT",
                        retryable=not (peer and sibling != "waiting"),
                        response={
                            "retry_at": (
                                datetime.now(UTC) + timedelta(hours=1 if peer else -1)
                            ).isoformat()
                        },
                    )
                )
        drive_management(env, task)
        assert not [c for c in calls if c[0] == "POST"]
        with Session(engine) as db:
            attempts = db.exec(
                select(ManagementRequestAttempt)
                .join(
                    ManagementTaskItem,
                    ManagementTaskItem.id == ManagementRequestAttempt.task_item_id,
                )
                .where(ManagementTaskItem.task_id == UUID(task))
            ).all()
            if sibling == "waiting":
                assert len(attempts) == 2, (
                    "waiting for a sibling must not consume attempts"
                )
        if sibling != "waiting":
            status = client.get(url).json()["status"]
            assert status in {"FAILED", "PARTIAL"}
            native = next(
                i
                for i in groups(client, url)
                if i["ref"]["remote_id"] == "adgroup-native"
            )
            assert native["execution_result"] == "CONFLICT"
            drive_management(env, task)
            assert client.get(url).json()["status"] == status
            assert not [c for c in calls if c[0] == "POST"]
            with Session(engine) as db:
                after = db.exec(
                    select(ManagementRequestAttempt)
                    .join(
                        ManagementTaskItem,
                        ManagementTaskItem.id == ManagementRequestAttempt.task_item_id,
                    )
                    .where(ManagementTaskItem.task_id == UUID(task))
                ).all()
                assert len(after) == len(attempts)
            return
        with Session(engine) as db, db.begin():
            for attempt in attempts:
                attempt.response = {
                    "retry_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()
                }
                db.add(attempt)
        drive_management(env, task)
        drive_management(env, task)
        assert len([c for c in calls if c[0] == "POST"]) == 1
        assert all(i["execution_result"] == "ACCEPTED" for i in groups(client, url))
        with Session(engine) as db:
            attempts = db.exec(
                select(ManagementRequestAttempt)
                .join(
                    ManagementTaskItem,
                    ManagementTaskItem.id == ManagementRequestAttempt.task_item_id,
                )
                .where(ManagementTaskItem.task_id == UUID(task))
            ).all()
            assert len(attempts) == 4
            assert sorted(a.outcome for a in attempts) == [
                "ACCEPTED",
                "ACCEPTED",
                "NOT_SENT",
                "NOT_SENT",
            ]
