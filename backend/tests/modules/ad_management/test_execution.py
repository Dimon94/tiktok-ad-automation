"""C5 execution tests use the repository PostgreSQL and Redis fixtures.

Only the provider transport is replaced: claims, attempts, receipts, route checks
and targeted refresh persistence stay on real database/Redis implementations.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, select

from app.core.config import settings
from app.core.db import engine
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.management import ManagementReceipt
from app.jobs.models import PendingDispatch
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route
from app.modules.ad_management.execution import execute_item
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.models import (
    ManagementReceipt as ReceiptRow,
)
from app.modules.ad_management.tasks import execute_management_task
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.store_models import AdSyncBc
from tests.modules.conftest import create_context


@pytest.fixture
def c5_env(monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    with Session(engine, expire_on_commit=False) as setup:
        context = create_context(setup)
        connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
        bc = TenantBC(tenant_id=context.tenant_id, bc_id="c5-bc")
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="c5-account",
            currency="USD",
            timezone="UTC",
            remote_status="STATUS_ENABLE",
        )
        setup.add_all([connection, bc, account])
        setup.flush()
        now = datetime.now(UTC)
        setup.add_all(
            [
                BCAccountAccess(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    advertiser_id=account.advertiser_id,
                    connection_id=connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_build=True,
                    can_upload=True,
                    permission_state="VERIFIED",
                    checked_at=now,
                ),
                BCConnectionBinding(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    connection_id=connection.id,
                    kind=connection.kind,
                ),
                ConnectionAuthorization(
                    tenant_id=context.tenant_id,
                    connection_id=connection.id,
                    authorization_revision=connection.authorization_revision,
                    scopes=["2", "6"],
                    permission_summary={
                        "read_authorized": True,
                        "build_authorized": True,
                    },
                    source="SYNTHETIC_COMPLETE_EVIDENCE",
                    verified_at=now,
                ),
                BCDefaultRoute(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    connection_id=connection.id,
                ),
            ]
        )
        selection = FrozenSelectionRecord(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            snapshot_id=uuid4(),
            advertiser_ids=[account.advertiser_id],
            filters={},
            filter_digest="f" * 64,
            publication_versions={},
            naming_versions={},
            refs=[],
            material_uses=[],
            membership_digest="m" * 64,
        )
        setup.add(selection)
        setup.flush()
        route = freeze_route(setup, context=context, bc_id=bc.bc_id)
        setup.add(
            AdSyncBc(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                actor_id=context.actor_id,
                frozen_route=route.model_dump(mode="json"),
                next_hot_at=now,
                next_material_at=now,
                next_daily_at=now,
                next_directory_at=now,
            )
        )
        setup.commit()
    try:
        yield context, bc, account, route, selection
    finally:
        # 成功写入现在会立即生成回读 outbox；完整清理本夹具的已提交租户，
        # 避免它的后台任务污染后续用例的全局派发窗口与空队列断言。
        with Session(engine) as cleanup, cleanup.begin():
            # 删除名称投影会生成新范围证据，先完成触发器写入，再按外键顺序清理。
            names = SQLModel.metadata.tables["campaign_name_projection"]
            cleanup.exec(names.delete().where(names.c.tenant_id == context.tenant_id))
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    cleanup.exec(
                        table.delete().where(table.c.tenant_id == context.tenant_id)
                    )
            tenant = SQLModel.metadata.tables["tenant"]
            cleanup.exec(tenant.delete().where(tenant.c.id == context.tenant_id))


class _Gateway:
    def __init__(self, apply):
        self.management = type("Management", (), {"apply": apply})()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _committed_item(
    management_env, *, original="20", final="25", operation="update_budget"
):
    context, bc, account, route, selection = management_env
    # 改名冻结的是推广系列名称；数值动作冻结在广告组配置里。
    rename = operation == "rename"
    material = operation == "set_material_status"
    kind, remote_id = (
        ("ad", "ad-c5")
        if material
        else (("campaign", "campaign-c5") if rename else ("adgroup", "group-c5"))
    )
    configuration = {} if rename else {"budget": original}
    with Session(engine, expire_on_commit=False) as setup:
        preview = ManagementPreview(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            selection_id=selection.id,
            digest="e" * 64,
            mutation={"field": "name" if rename else "budget"},
            route=route.model_dump(mode="json"),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            status="READY",
            counts={"selected": 1, "targets": 1, "linked": 0, "unsupported": 0},
        )
        setup.add(preview)
        setup.add(
            AdObject(
                tenant_id=context.tenant_id,
                advertiser_id=account.advertiser_id,
                kind=kind,
                remote_id=remote_id,
                name=original if rename else "",
                ad_type="SMART_PLUS" if material else "REGULAR",
                configuration=configuration,
                operation_status="ENABLE",
                delivery_status="DELIVERING",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=route.connection_id,
                source_channel=route.channel,
            )
        )
        setup.flush()
        preview_item = ManagementPreviewItem(
            tenant_id=context.tenant_id,
            preview_id=preview.id,
            position=0,
            ref={
                "tenant_id": str(context.tenant_id),
                "advertiser_id": account.advertiser_id,
                "kind": kind,
                "remote_id": remote_id,
            },
            original_value=original,
            final_value=final,
            execution_result="PENDING",
            capability={
                "operation": operation,
                "entity_kind": kind,
                "ad_type": "REGULAR",
                "configuration": configuration,
                "source_connection_id": str(route.connection_id),
                "source_channel": route.channel,
                "route": route.model_dump(mode="json"),
            },
        )
        setup.add(preview_item)
        setup.flush()
        task = ManagementTask(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            preview_id=preview.id,
            idempotency_key=uuid4(),
            digest=preview.digest,
            route=route.model_dump(mode="json"),
            status="QUEUED",
            counts=preview.counts,
        )
        setup.add(task)
        setup.flush()
        item = ManagementTaskItem(
            tenant_id=context.tenant_id,
            task_id=task.id,
            preview_item_id=preview_item.id,
            preview_id=preview.id,
            position=0,
            ref=dict(preview_item.ref),
            original_value=original,
            final_value=final,
            execution_result="PENDING",
            capability=dict(preview_item.capability),
        )
        if material:
            use = {
                "ad_ref": dict(item.ref),
                "platform_material_id": "video-c5",
                "ad_material_id": "material-current-c5",
                "material_type": "VIDEO",
            }
            item.material_use = use
            preview_item.material_use = use
            item.capability = dict(
                item.capability,
                ad_type="SMART_PLUS",
                ad_material_id="material-current-c5",
                platform_material_id="video-c5",
            )
            setup.add_all(
                [
                    AdMaterialReference(
                        tenant_id=context.tenant_id,
                        advertiser_id=account.advertiser_id,
                        ad_remote_id=remote_id,
                        platform_material_id="video-c5",
                        ad_material_id=mid,
                        material_type="VIDEO",
                        operation_status=status,
                        complete=True,
                        published_version=2 if mid == "material-current-c5" else 1,
                    )
                    for mid, status in [
                        ("material-current-c5", original),
                        ("material-old-c5", final),
                    ]
                ]
            )
        setup.add(item)
        setup.commit()
        return context, task.id, item.id


@pytest.mark.parametrize(
    "original,final", [("ENABLE", "DISABLE"), ("DISABLE", "ENABLE")]
)
def test_material_toggle_executes_exact_usage_and_persists_receipt(
    c5_env, redis_client, monkeypatch, original, final
):
    context, task_id, item_id = _committed_item(
        c5_env, original=original, final=final, operation="set_material_status"
    )

    def gateway(**kwargs):
        def apply(_self, command):
            # 模拟真实适配器发送门：重复核验本素材，不能误取同视频的历史记录。
            kwargs["before_request"]()
            assert command.ad_material_id == "material-current-c5"
            assert command.desired == {"operation_status": final}
            return ManagementReceipt("ACCEPTED", "material-toggle-request", False)

        return _Gateway(apply)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway", gateway
    )
    execute_item(engine, item_id)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == "ACCEPTED"
        assert session.get(ManagementTask, task_id).status == "SUCCEEDED"
        receipt = session.exec(
            select(ReceiptRow).where(ReceiptRow.tenant_id == context.tenant_id)
        ).one()
        assert receipt.outcome == "ACCEPTED"


def test_unexpected_preflight_error_finishes_without_provider_send(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)

    def broken_observation(*_args):
        raise ValueError("synthetic observation failure")

    def forbidden_gateway(**_kwargs):
        pytest.fail("发送前校验失败不得打开平台写入网关")

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution._same_frozen_value", broken_observation
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway", forbidden_gateway
    )
    assert (
        execute_management_task(
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload={"task_id": str(task_id), "generation": 1},
        )
        == "FAILED"
    )
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == "NOT_SENT"
        assert item.reason == "management_preflight_failed"
        assert item.claim_token is None
        assert session.get(ManagementTask, task_id).status == "FAILED"
        attempt = session.exec(
            select(ManagementRequestAttempt).where(
                ManagementRequestAttempt.task_item_id == item_id
            )
        ).one()
        assert attempt.outcome == "NOT_SENT" and not attempt.retryable
        assert attempt.response == {"reason": "management_preflight_failed"}
        receipt = session.exec(
            select(ReceiptRow).where(ReceiptRow.tenant_id == context.tenant_id)
        ).one()
        assert receipt.outcome == "NOT_SENT"
    # 再次投递只读终态，不能把确定未发送的本地失败误改成 UNKNOWN。
    execute_item(engine, item_id)
    with Session(engine) as session:
        assert session.get(ManagementTaskItem, item_id).execution_result == "NOT_SENT"


@pytest.mark.parametrize("change", ["missing", "status", "scope", "replaced"])
def test_material_toggle_blocks_changed_or_mismatched_usage(
    c5_env, redis_client, monkeypatch, change
):
    context, task_id, item_id = _committed_item(
        c5_env, original="ENABLE", final="DISABLE", operation="set_material_status"
    )
    with Session(engine) as session:
        material = session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == context.tenant_id,
                AdMaterialReference.ad_material_id == "material-current-c5",
            )
        ).one()
        if change == "missing":
            session.delete(material)
        elif change == "status":
            material.operation_status = "DISABLE"
            session.add(material)
        elif change == "replaced":
            newer = session.exec(
                select(AdMaterialReference).where(
                    AdMaterialReference.tenant_id == context.tenant_id,
                    AdMaterialReference.ad_material_id == "material-old-c5",
                )
            ).one()
            newer.published_version = 3
            session.add(newer)
        else:
            item = session.get(ManagementTaskItem, item_id)
            use = dict(item.material_use)
            use["ad_ref"] = dict(use["ad_ref"], tenant_id=str(uuid4()))
            item.material_use = use
            session.add(item)
        session.commit()

    def forbidden_gateway(**_kwargs):
        pytest.fail("素材不存在、原值变化或越界时不得写入平台")

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway", forbidden_gateway
    )
    execute_item(engine, item_id)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == (
            "CONFLICT" if change == "status" else "NOT_SENT"
        )
        assert session.get(ManagementTask, task_id).status == "FAILED"


@pytest.mark.parametrize("retryable,expected", [(False, "FAILED"), (True, "RUNNING")])
def test_not_sent_task_status_respects_retryability(
    c5_env, redis_client, monkeypatch, retryable, expected
):
    context, task_id, item_id = _committed_item(c5_env)
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(
            lambda *_args: ManagementReceipt("NOT_SENT", None, retryable)
        ),
    )
    result = execute_management_task(
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        payload={"task_id": str(task_id), "generation": 1},
    )
    assert result == expected
    with Session(engine) as session:
        assert session.get(ManagementTaskItem, item_id).execution_result == "NOT_SENT"
        assert session.get(ManagementTask, task_id).status == expected


def _cleanup(task_id):
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        if task is not None:
            session.delete(task)
        for dispatch in session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "ad_management.execute"
            )
        ).all():
            if dispatch.payload.get("task_id") == str(task_id):
                session.delete(dispatch)
        session.commit()


def test_accepted_write_commits_receipt_without_synchronous_readback(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []
    refreshes = []

    def apply(_self, command):
        calls.append(command)
        return ManagementReceipt("ACCEPTED", "request-c5-accepted", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.request_directory",
        lambda _session, **kwargs: refreshes.append(kwargs) or 1,
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "ACCEPTED"
            assert len(calls) == 1
            assert (
                len(
                    session.exec(
                        select(ManagementRequestAttempt).where(
                            ManagementRequestAttempt.task_item_id == item_id
                        )
                    ).all()
                )
                == 1
            )
            receipt = session.exec(
                select(ReceiptRow).where(ReceiptRow.tenant_id == context.tenant_id)
            ).one()
            assert receipt.outcome == "ACCEPTED"
            assert len(refreshes) == 1
            assert refreshes[0]["scope"] == "targeted"
            assert refreshes[0]["advertiser_ids"] == ["c5-account"]
    finally:
        _cleanup(task_id)


def test_accepted_write_creates_real_directory_readback(
    c5_env, redis_client, monkeypatch
):
    from app.modules.ads.sync_models import AdDirectoryRun

    context, task_id, item_id = _committed_item(c5_env)
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(
            lambda *_args: ManagementReceipt("ACCEPTED", "offline-accepted", False)
        ),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item.execution_result == "ACCEPTED"
            runs = session.exec(
                select(AdDirectoryRun).where(
                    AdDirectoryRun.tenant_id == context.tenant_id,
                )
            ).all()
            assert runs
            assert all(
                r.query["ids"] == ["group-c5"] or r.query["parent_ids"] == ["group-c5"]
                for r in runs
            )
    finally:
        _cleanup(task_id)


def test_rename_skips_when_name_changed(c5_env, redis_client, monkeypatch):
    context, task_id, item_id = _committed_item(
        c5_env, original="嘉书-剧A", final="嘉书-剧B", operation="rename"
    )
    with Session(engine) as session:
        row = session.get(
            AdObject, (context.tenant_id, "c5-account", "campaign", "campaign-c5")
        )
        assert row is not None
        row.name = "嘉书-剧X"
        session.add(row)
        session.commit()
    calls = []
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(lambda *_args: calls.append(True)),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "CONFLICT"
            assert item.reason == "management_original_changed"
        assert calls == []
    finally:
        _cleanup(task_id)


def test_budget_skips_when_budget_changed(c5_env, redis_client, monkeypatch):
    """原值被他人改过：预算同样记为冲突（终态，不再重试），且不调用平台。"""
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        row = session.get(
            AdObject, (context.tenant_id, "c5-account", "adgroup", "group-c5")
        )
        assert row is not None
        row.configuration = {"budget": "30"}
        session.add(row)
        session.commit()
    calls = []
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(lambda *_args: calls.append(True)),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "CONFLICT"
            assert item.reason == "management_original_changed"
        assert calls == []
    finally:
        _cleanup(task_id)


def test_rename_accepted_requests_targeted_refresh(c5_env, redis_client, monkeypatch):
    _context, task_id, item_id = _committed_item(
        c5_env, original="嘉书-剧A", final="嘉书-剧B", operation="rename"
    )
    commands = []
    refreshes = []

    def apply(_self, command):
        commands.append(command)
        return ManagementReceipt("ACCEPTED", "request-c5-rename", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.request_directory",
        lambda _session, **kwargs: refreshes.append(kwargs) or 1,
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "ACCEPTED"
        assert len(commands) == 1
        assert commands[0].field == "name"
        assert commands[0].operation == "rename"
        assert commands[0].desired == {"campaign_name": "嘉书-剧B"}
        assert len(refreshes) == 1
        assert refreshes[0]["scope"] == "targeted"
        assert refreshes[0]["advertiser_ids"] == ["c5-account"]
    finally:
        _cleanup(task_id)


def test_unknown_result_is_terminal_and_is_never_resent(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []

    def apply(_self, _command):
        calls.append(True)
        raise RemoteCallError(
            "mcp_call_failed",
            effect="UNKNOWN",
            evidence=CallEvidence(request_id="unknown-c5"),
        )

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        execute_item(engine, item_id)
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "UNKNOWN"
            attempts = session.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == item_id
                )
            ).all()
            assert len(attempts) == 1
            assert attempts[0].outcome == "UNKNOWN"
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_targeted_refresh_failure_is_durable_and_retry_does_not_resend(
    c5_env, redis_client, monkeypatch
):
    _context, task_id, item_id = _committed_item(c5_env)
    calls = []
    refreshes = []
    failed = {"once": True}

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "refresh-retry", False)

    def request_refresh(_session, **kwargs):
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("synthetic refresh outage")
        refreshes.append(kwargs)
        return uuid4()

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.request_directory", request_refresh
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None
            assert item.execution_result == "ACCEPTED"
            assert item.observation_state == "REFRESH_PENDING"
            assert (
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == item.tenant_id,
                        PendingDispatch.task_name == "ad_management.execute",
                        PendingDispatch.payload["task_id"].as_string()
                        == str(item.task_id),
                    )
                ).first()
                is not None
            )
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None
            assert item.execution_result == "ACCEPTED"
            # 目录请求排队不证明预算已生效；真实发布由 Native 纵链覆盖。
            assert item.observation_state == "REFRESH_PENDING"
        assert calls == [True]
        assert len(refreshes) == 1
    finally:
        _cleanup(task_id)


def test_revoked_management_permission_blocks_request_after_claim(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    _, bc, account, route, _ = c5_env
    calls = []
    with Session(engine) as session:
        # 领取后账户同步撤销了搭建权限：物理请求前的重检必须拦下，不发送。
        grant = session.get(
            BCAccountAccess,
            (context.tenant_id, bc.bc_id, account.advertiser_id, route.connection_id),
        )
        grant.can_build = False
        session.add(grant)
        session.commit()

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "should-not-send", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "NOT_SENT"
            assert item.claim_token is None
        assert calls == []
    finally:
        _cleanup(task_id)


def test_missing_current_object_fails_closed_before_provider(
    c5_env, redis_client, monkeypatch
):
    _context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        session.delete(
            session.get(
                AdObject, (_context.tenant_id, "c5-account", "adgroup", "group-c5")
            )
        )
        session.commit()
    calls = []
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(lambda *_args: calls.append(True)),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "NOT_SENT"
            assert item.reason == "management_observation_conflict"
        assert calls == []
    finally:
        _cleanup(task_id)


def test_frozen_route_revision_change_blocks_without_channel_fallback(
    c5_env, redis_client, monkeypatch
):
    _context, task_id, item_id = _committed_item(c5_env)
    calls = []
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        assert task is not None
        task.route = {
            **task.route,
            "authorization_revision": task.route["authorization_revision"] + 1,
        }
        session.add(task)
        session.commit()

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "should-not-send", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "NOT_SENT"
            assert item.reason == "frozen_route_changed"
            assert item.claim_token is None
        assert calls == []
    finally:
        _cleanup(task_id)


def test_one_unknown_item_does_not_block_sibling(c5_env, redis_client, monkeypatch):
    context, task_id, first_id = _committed_item(c5_env)
    with Session(engine) as session:
        first = session.get(ManagementTaskItem, first_id)
        assert first is not None
        second = ManagementTaskItem(
            tenant_id=first.tenant_id,
            task_id=first.task_id,
            preview_item_id=first.preview_item_id,
            preview_id=first.preview_id,
            position=1,
            ref={**first.ref, "remote_id": "group-c5-sibling"},
            original_value=first.original_value,
            final_value=first.final_value,
            execution_result="PENDING",
            capability=dict(first.capability),
        )
        session.add(second)
        session.add(
            AdObject(
                tenant_id=first.tenant_id,
                advertiser_id=first.ref["advertiser_id"],
                kind="adgroup",
                remote_id="group-c5-sibling",
                ad_type="REGULAR",
                configuration={"budget": first.original_value},
                operation_status="ENABLE",
                delivery_status="DELIVERING",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=c5_env[3].connection_id,
                source_channel=c5_env[3].channel,
            )
        )
        session.commit()
        second_id = second.id
    calls = []

    def apply(_self, command):
        calls.append(command.ref.remote_id)
        if command.ref.remote_id == "group-c5":
            raise RemoteCallError(
                "mcp_call_failed",
                effect="UNKNOWN",
                evidence=CallEvidence(request_id="unknown-sibling"),
            )
        return ManagementReceipt("ACCEPTED", "accepted-sibling", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        execute_item(engine, first_id)
        execute_item(engine, second_id)
        with Session(engine) as session:
            assert (
                session.get(ManagementTaskItem, first_id).execution_result == "UNKNOWN"
            )
            assert (
                session.get(ManagementTaskItem, second_id).execution_result
                == "ACCEPTED"
            )
        assert calls == ["group-c5", "group-c5-sibling"]
    finally:
        _cleanup(task_id)


def test_receipt_commit_failure_is_fenced_unknown_and_not_replayed(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "accepted-before-db-failure", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    original_persist = __import__(
        "app.modules.ad_management.execution", fromlist=["_persist_outcome"]
    )._persist_outcome
    failed = {"once": True}

    def fail_once(*args, **kwargs):
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("synthetic receipt commit failure")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._persist_outcome", fail_once
    )
    try:
        execute_item(engine, item_id)
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "UNKNOWN"
            attempt = session.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == item_id
                )
            ).one()
            assert attempt.outcome == "UNKNOWN"
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_management_queue_is_registered_with_one_slot():
    from app.jobs.celery_app import celery_app
    from app.jobs.tasks import dispatch_queue

    assert dispatch_queue("ad_management.execute") == "ad-management"
    assert (
        celery_app.conf.task_routes["ad_management.execute"]["queue"] == "ad-management"
    )
    from pathlib import Path

    service = (
        Path(__file__).resolve().parents[4] / "deploy/staging-ad-management.service"
    )
    assert "--concurrency 1" in service.read_text()


def test_terminal_task_dispatches_durable_refresh_pending_item(c5_env, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        task = session.get(ManagementTask, task_id)
        assert item is not None and task is not None
        item.execution_result = "ACCEPTED"
        item.observation_state = "REFRESH_PENDING"
        task.status = "SUCCEEDED"
        session.add_all([item, task])
        session.commit()
    calls = []
    monkeypatch.setattr(
        "app.modules.ad_management.dispatch_work.ex.execute_item",
        lambda _engine, current_id: calls.append(current_id),
    )
    from app.modules.ad_management.tasks import execute_management_task

    try:
        execute_management_task(
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload={"task_id": str(task_id), "generation": 1},
        )
        assert calls == [item_id]
    finally:
        _cleanup(task_id)


def test_stale_lease_cannot_write_provider_receipt(c5_env, redis_client, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []

    class StaleAfterSendLease:
        def __init__(self):
            self.checks = 0

        def fence(self, *_args):
            self.checks += 1
            if self.checks >= 3:
                from app.core.errors import DomainError

                raise DomainError("mutation_lease_stale", "stale")

        def release(self, *_args):
            return None

    lease = StaleAfterSendLease()

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "accepted-stale", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.claim_mutation",
        lambda *_args, **_kwargs: lease,
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "UNKNOWN"
            attempt = session.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == item_id
                )
            ).one()
            assert attempt.outcome == "UNKNOWN"
        assert calls == [True]
        execute_item(engine, item_id)
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_expired_pre_attempt_recovers_unknown_receipt_without_resend(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item is not None
        item.claim_generation = 4
        item.claim_token = uuid4()
        item.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        item.delivery_status = "ATTEMPT_RECORDED"
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id,
                task_item_id=item_id,
                attempt=1,
                outcome="NOT_SENT",
                payload={},
                response={},
                retryable=False,
            )
        )
        session.add(item)
        session.commit()
    calls = []
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(lambda *_args: calls.append(True)),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            attempt = session.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == item_id
                )
            ).one()
            receipt = session.exec(
                select(ReceiptRow).where(ReceiptRow.attempt_id == attempt.id)
            ).one()
            assert item is not None and item.execution_result == "UNKNOWN"
            assert attempt.outcome == "UNKNOWN"
            assert receipt.outcome == "UNKNOWN"
        assert calls == []
    finally:
        _cleanup(task_id)


def test_duplicate_delivery_sends_once_while_first_provider_call_is_in_flight(
    c5_env, redis_client, monkeypatch
):
    _context, task_id, item_id = _committed_item(c5_env)
    calls = []
    provider_started = Event()
    release_provider = Event()

    def apply(_self, _command):
        calls.append(True)
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ManagementReceipt("ACCEPTED", "duplicate-once", False)

    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(execute_item, engine, item_id)
            assert provider_started.wait(timeout=5)
            second = pool.submit(execute_item, engine, item_id)
            second.result(timeout=5)
            release_provider.set()
            first.result(timeout=5)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "ACCEPTED"
            assert (
                len(
                    session.exec(
                        select(ManagementRequestAttempt).where(
                            ManagementRequestAttempt.task_item_id == item_id
                        )
                    ).all()
                )
                == 1
            )
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_unknown_sibling_does_not_prevent_known_unsent_retry(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        sibling = ManagementTaskItem(**item.model_dump(exclude={"id"}))
        sibling.execution_result = "NOT_SENT"
        item.execution_result = "UNKNOWN"
        session.add_all([item, sibling])
        session.flush()
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id, task_item_id=item.id, outcome="UNKNOWN"
            )
        )
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id,
                task_item_id=sibling.id,
                outcome="NOT_SENT",
                retryable=True,
            )
        )
        from app.modules.ad_management.execution import _update_task

        _update_task(session, task_id, context.tenant_id)
        session.commit()
        assert session.get(ManagementTask, task_id).status == "RUNNING"
        sibling_id = sibling.id
    sent = []
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_: _Gateway(
            lambda _self, command: (
                sent.append(command),
                ManagementReceipt("ACCEPTED", "synthetic-retry", False),
            )[1]
        ),
    )
    result = execute_management_task(
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        payload={"task_id": str(task_id), "generation": 1},
    )
    assert result == "NEEDS_REVIEW" and len(sent) == 1
    with Session(engine) as session:
        assert session.get(ManagementTaskItem, item_id).execution_result == "UNKNOWN"
        assert (
            session.get(ManagementTaskItem, sibling_id).execution_result == "ACCEPTED"
        )
