"""合批执行的领取/回执保存在真实PG和Redis，只有平台传输替身。"""

import os
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.integrations.tiktok.contracts.management import ManagementReceipt
from app.modules.ad_management import execution
from app.modules.ad_management.batch_execution import execute_batch
from app.modules.ad_management.batch_models import (
    ManagementPhysicalMember,
    ManagementPhysicalRequest,
)
from app.modules.ad_management.models import (
    ManagementPreviewItem,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ads.models import AdObject
from tests.modules.ad_management.test_execution import _committed_item
from tests.modules.ad_management.test_execution import c5_env as c5_env


def make_items(env, count):
    context, task_id, item_id = _committed_item(
        env, operation="rename", original="old", final="new"
    )
    with Session(engine) as session:
        first = session.get(ManagementTaskItem, item_id)
        first.original_value, first.final_value = "ENABLE", "DISABLE"
        first.capability = {
            **first.capability,
            "operation": "set_status",
            "configuration": {},
        }
        session.add(first)
        for i in range(1, count):
            ref = {**first.ref, "remote_id": f"bulk-c-{i}"}
            session.add(
                AdObject(
                    tenant_id=context.tenant_id,
                    advertiser_id=ref["advertiser_id"],
                    kind="campaign",
                    remote_id=ref["remote_id"],
                    name="bulk",
                    ad_type="REGULAR",
                    configuration={},
                    operation_status="ENABLE",
                    observed_at=datetime.now(UTC),
                    published_version=1,
                    source_connection_id=env[3].connection_id,
                    source_channel=env[3].channel,
                )
            )
            preview_item = ManagementPreviewItem(
                tenant_id=context.tenant_id,
                preview_id=first.preview_id,
                position=i,
                ref=ref,
                original_value="ENABLE",
                final_value="DISABLE",
                execution_result="PENDING",
                capability=dict(first.capability),
            )
            session.add(preview_item)
            session.flush()
            session.add(
                ManagementTaskItem(
                    tenant_id=context.tenant_id,
                    task_id=task_id,
                    preview_id=first.preview_id,
                    preview_item_id=preview_item.id,
                    position=i,
                    ref=ref,
                    original_value="ENABLE",
                    final_value="DISABLE",
                    capability=dict(first.capability),
                )
            )
        session.commit()
        ids = tuple(
            session.exec(
                select(ManagementTaskItem.id)
                .where(ManagementTaskItem.task_id == task_id)
                .order_by(ManagementTaskItem.position)
            ).all()
        )
    return context, task_id, ids


def test_twenty_members_one_request_and_no_replay(c5_env, monkeypatch):
    context, task_id, ids = make_items(c5_env, 20)
    calls = []

    class Wire:
        def __init__(self, before):
            self.management = self
            self.before = before

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def apply_batch(self, commands):
            self.before()
            calls.append(commands)
            return tuple(
                ManagementReceipt("ACCEPTED", "shared-batch-request", False)
                for _ in commands
            )

    monkeypatch.setattr(
        execution, "open_tiktok_gateway", lambda **kw: Wire(kw["before_request"])
    )
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setattr(execution, "_refresh_after_commit", lambda *a, **k: None)
    execute_batch(engine, ids)
    execute_batch(engine, ids)
    assert [len(c) for c in calls] == [20]
    with Session(engine) as session:
        rows = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.task_id == task_id)
        ).all()
        assert {r.execution_result for r in rows} == {"ACCEPTED"}
        physical = session.exec(
            select(ManagementPhysicalRequest).where(
                ManagementPhysicalRequest.task_id == task_id
            )
        ).all()
        assert len(physical) == 1
        members = session.exec(
            select(ManagementPhysicalMember).where(
                ManagementPhysicalMember.batch_id == physical[0].id
            )
        ).all()
        assert len(members) == 20
        assert (
            len(
                session.exec(
                    select(ManagementRequestAttempt).where(
                        ManagementRequestAttempt.task_item_id.in_(ids)
                    )
                ).all()
            )
            == 20
        )


def test_cancelled_batch_never_calls_provider(c5_env, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    _, task_id, ids = make_items(c5_env, 2)
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        task.status = "CANCELLED"
        session.add(task)
        session.commit()

    def forbidden(**_kwargs):
        raise AssertionError("cancelled task reached gateway")

    monkeypatch.setattr(execution, "open_tiktok_gateway", forbidden)
    execute_batch(engine, ids)


def test_41_members_dispatch_as_twenty_twenty_one(c5_env, redis_client, monkeypatch):
    import json
    from threading import Lock
    from types import SimpleNamespace

    from cryptography.fernet import Fernet
    from urllib3.response import HTTPResponse

    from app.core.credentials import encrypt_credentials
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.modules.accounts.models import TikTokConnection
    from app.modules.ad_management.dispatch_work import execute_task_items
    from tests.modules.ad_management.test_native_status import drive_directory

    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", "bulk-offline")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "synthetic")
    monkeypatch.setattr(
        settings, "TIKTOK_REDIRECT_URI", "https://example.test/callback"
    )
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    context, task_id, ids = make_items(c5_env, 41)
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, c5_env[3].connection_id)
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value={"access_token": "synthetic-offline"}
        )
        session.add(connection)
    calls, observed = [], set()
    remote = {}
    lock = Lock()

    def transport(pool, method, url, **kwargs):
        body = (
            json.loads(kwargs["body"]) if method == "POST" else dict(kwargs["fields"])
        )
        assert body["advertiser_id"] == c5_env[2].advertiser_id
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer:
            observer[0].attempt(observer[1])
        if method == "POST":
            assert (
                url.endswith("/campaign/status/update/") and "/smart_plus/" not in url
            )
            assert body["operation_status"] == "DISABLE"
            members = body["campaign_ids"]
            with lock:
                calls.append(tuple(members))
                remote.update(dict.fromkeys(members, "DISABLE"))
            data = {"campaign_ids": members}
        else:
            assert method == "GET" and url.endswith("/get/")
            filtering = json.loads(body["filtering"])
            members = filtering["campaign_ids"]
            found = []
            if url.endswith("/campaign/get/") and "/smart_plus/" not in url:
                with lock:
                    found = [
                        {
                            "advertiser_id": body["advertiser_id"],
                            "campaign_id": identity,
                            "campaign_name": "bulk",
                            "operation_status": remote[identity],
                        }
                        for identity in members
                        if identity in remote
                    ]
                    observed.update(row["campaign_id"] for row in found)
            data = {
                "list": found,
                "page_info": {
                    "page": 1,
                    "page_size": int(body["page_size"]),
                    "total_page": 1 if found else 0,
                    "total_number": len(found),
                },
            }
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "bulk-" + method, "data": data}
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    assert execute_task_items(engine, ids)
    assert sorted(map(len, calls)) == [1, 20, 20]
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        assert task.status == "RUNNING" and task.counts["refresh_pending"] == 41
        assert task.counts["accepted"] == 41
    # 原 outbox、目录 worker 和发布器写入发送后的观察，再由管理执行器核验。
    drive_directory(SimpleNamespace(context=context), redis_client)
    assert execute_task_items(engine, ids)
    assert execute_task_items(engine, ids)
    assert sorted(map(len, calls)) == [1, 20, 20]
    assert observed == set(remote) and len(observed) == 41
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        assert task.status == "SUCCEEDED" and task.counts["refresh_pending"] == 0
        assert {
            session.get(ManagementTaskItem, key).observation_state for key in ids
        } == {"ACCEPTED"}


def test_unknown_members_are_not_replayed(c5_env, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    _, task_id, ids = make_items(c5_env, 2)
    calls = []

    class Wire:
        def __init__(self, before):
            self.management, self.before = self, before

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def apply_batch(self, commands):
            self.before()
            calls.append(commands)
            return (
                ManagementReceipt("ACCEPTED", "partial", False),
                ManagementReceipt("UNKNOWN", "partial", False),
            )

    monkeypatch.setattr(
        execution, "open_tiktok_gateway", lambda **kw: Wire(kw["before_request"])
    )
    monkeypatch.setattr(execution, "_refresh_after_commit", lambda *a, **k: None)
    execute_batch(engine, ids)
    execute_batch(engine, ids)
    assert len(calls) == 1
    with Session(engine) as session:
        assert session.get(ManagementTask, task_id).status == "NEEDS_REVIEW"
        assert session.get(ManagementTaskItem, ids[1]).execution_result == "UNKNOWN"


def test_roas_conflict_only_blocks_affected_series(c5_env):
    from app.modules.ad_management.dispatch_work import task_work

    _, _, ids = make_items(c5_env, 3)
    with Session(engine) as session:
        for index, item_id in enumerate(ids):
            item = session.get(ManagementTaskItem, item_id)
            item.ref = {**item.ref, "kind": "adgroup"}
            item.capability = {
                "operation": "update_roas",
                "ad_type": "SMART_PLUS",
                "roas_facts": {"budget_strategy": "CBO"},
                "configuration": {
                    "campaign_id": "conflicting" if index < 2 else "valid"
                },
            }
            item.final_value = str(index + 1)
            session.add(item)
        session.commit()
    work = task_work(engine, ids)
    assert [member for _, members in work for member in members] == [ids[2]]
    with Session(engine) as session:
        assert [
            session.get(ManagementTaskItem, key).execution_result for key in ids
        ] == ["CONFLICT", "CONFLICT", "PENDING"]
