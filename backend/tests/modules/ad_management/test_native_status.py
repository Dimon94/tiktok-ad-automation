"""JB-59: real HTTP/PG/Redis/management Worker and SDK wire, synthetic native data."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError, domain_error_handler
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import TikTokConnection
from app.modules.ad_management.api import router as management_router
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.tasks import execute_management_task
from app.modules.ads.models import AdObject
from app.modules.ads.tasks import run_directory_step
from app.modules.reporting.api import router as reporting_router
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.store_models import AdSyncBc
from tests.modules.ad_management.conftest import _row, _set_selection
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


@pytest.fixture
def native_status_env(committed_management_env, monkeypatch):
    context, _, account, route, selection = committed_management_env
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import BCAccountAccess, TenantBC
    from app.modules.accounts.routing import freeze_route

    with Session(engine, expire_on_commit=False) as db, db.begin():
        bc = TenantBC(
            tenant_id=context.tenant_id,
            bc_id="900000000000059",
            name="M5a synthetic BC",
        )
        db.add(bc)
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                connection_id=route.connection_id,
                kind="OFFICIAL_API",
            )
        )
        db.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                connection_id=route.connection_id,
            )
        )
        db.add(
            BCAccountAccess(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                advertiser_id=account.advertiser_id,
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
        record = db.get(FrozenSelectionRecord, selection.id)
        record.bc_id = bc.bc_id
        db.add(record)
        db.flush()
        route = freeze_route(db, context=context, bc_id=bc.bc_id)
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", "m5a-" + uuid4().hex)
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "synthetic")
    monkeypatch.setattr(
        settings, "TIKTOK_REDIRECT_URI", "https://example.test/callback"
    )
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    refs = {
        k: EntityRef(context.tenant_id, account.advertiser_id, k, k + "-native")
        for k in ("campaign", "adgroup", "ad")
    }
    with Session(engine) as db, db.begin():
        connection = db.get(TikTokConnection, route.connection_id)
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value={"access_token": "synthetic-offline"}
        )
        db.add(connection)
        for kind, ref in refs.items():
            db.add(
                _row(
                    ref,
                    parent=refs.get({"adgroup": "campaign", "ad": "adgroup"}.get(kind)),
                    ad_type="SMART_PLUS",
                    connection_id=route.connection_id,
                    configuration={
                        "promotion_type": "NATIVE_SERIES",
                        "native_series_id": "series",
                        "identity_type": "TT_USER",
                        "identity_id": "owner",
                    },
                )
            )
        future = datetime.now(UTC) + timedelta(days=1)
        db.add(
            AdSyncBc(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                actor_id=context.actor_id,
                frozen_route=route.model_dump(mode="json"),
                next_hot_at=future,
                next_material_at=future,
                next_directory_at=future,
                next_daily_at=future,
            )
        )
    app = FastAPI()
    app.include_router(management_router, prefix="/api")
    app.include_router(reporting_router, prefix="/api")
    app.add_exception_handler(DomainError, domain_error_handler)

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=context.actor_id
    )
    return SimpleNamespace(
        app=app,
        context=context,
        bc=bc,
        account=account,
        route=route,
        selection=selection,
        refs=refs,
    )


def install_wire(monkeypatch, env, target, final):
    calls = []
    state = {"fail_read": False}

    def transport(pool, method, url, **kwargs):
        assert "/open_api/v1.3/" in url, (method, url)
        body = (
            json.loads(kwargs["body"]) if method == "POST" else dict(kwargs["fields"])
        )
        calls.append((method, url, body))
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer:
            observer[0].attempt(observer[1])
        id_key = {
            "campaign": "campaign_ids",
            "adgroup": "adgroup_ids",
            "ad": "smart_plus_ad_ids",
        }[target]
        if method == "POST":
            assert url.endswith(f"/smart_plus/{target}/status/update/")
            assert body == {
                "advertiser_id": env.account.advertiser_id,
                id_key: [env.refs[target].remote_id],
                "operation_status": final,
            }
            data = {id_key: [env.refs[target].remote_id]}
        else:
            assert method == "GET" and url.endswith("/get/")
            if state["fail_read"]:
                return HTTPResponse(
                    body=b'{"code":40002,"message":"synthetic read rejected","request_id":"read-failed"}',
                    status=200,
                )
            filtering = json.loads(body["filtering"])
            assert env.refs[target].remote_id in [
                v
                for values in filtering.values()
                if isinstance(values, list)
                for v in values
            ]
            assert body["advertiser_id"] == env.account.advertiser_id
            singular = {
                "campaign": "campaign_id",
                "adgroup": "adgroup_id",
                "ad": "smart_plus_ad_id",
            }[target]
            row = {
                singular: env.refs[target].remote_id,
                f"{target}_name": "native observed",
                "operation_status": final,
                "advertiser_id": env.account.advertiser_id,
            }
            if target == "adgroup":
                row.update(
                    campaign_id=env.refs["campaign"].remote_id,
                    promotion_type="NATIVE_SERIES",
                    native_series_id="series",
                    identity_type="TT_USER",
                    identity_id="owner",
                )
            if target == "ad":
                row.update(adgroup_id=env.refs["adgroup"].remote_id, creative_list=[])
            found = [row] if url.endswith(f"/smart_plus/{target}/get/") else []
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
                {"code": 0, "request_id": "synthetic-" + method, "data": data}
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    return calls, state


def drive_management(env, task_id):
    execute_management_task(
        tenant_id=str(env.context.tenant_id),
        actor_id=str(env.context.actor_id),
        payload={"task_id": str(task_id), "generation": 1},
    )


def drive_directory(env, redis_client, *, allow_failure=False):
    # Consume durable production outbox payloads, with the existing worker body.
    seen = set()
    for _ in range(20):
        from app.modules.sync_control.directory_dispatch import dispatch_directories_due

        with Session(engine) as db, db.begin():
            dispatch_directories_due(db, redis_client, now=datetime.now(UTC), limit=100)
        with Session(engine) as db, db.begin():
            messages = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == env.context.tenant_id,
                    PendingDispatch.task_name.in_(
                        ["ads.targeted_step", "ads.sync_step", "ads.publish_step"]
                    ),
                    PendingDispatch.superseded_at.is_(None),
                    PendingDispatch.available_at <= datetime.now(UTC),
                )
            ).all()
            payloads = [
                (m.task_name, dict(m.payload)) for m in messages if m.id not in seen
            ]
            seen.update(m.id for m in messages)
            for message in messages:
                message.published_at = datetime.now(UTC)
                db.add(message)
        if not payloads:
            break
        for name, payload in payloads:
            try:
                result = run_directory_step(
                    database_engine=engine,
                    redis_client=redis_client,
                    context=env.context,
                    run_id=UUID(payload["run_id"]),
                    claim_generation=payload["claim_generation"],
                    publish_only=name == "ads.publish_step",
                )
            except DomainError as error:
                if not allow_failure or error.code != "report_query_invalid":
                    raise
                continue
            assert allow_failure or result != "FAILED", (name, payload, result)


@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("final", ["DISABLE", "ENABLE"])
def test_native_status_http_worker_readback(
    native_status_env,
    redis_client,
    monkeypatch,
    kind,
    final,
    read_failure=False,
    disabled_parent=False,
):
    env = native_status_env
    original = "ENABLE" if final == "DISABLE" else "DISABLE"
    with Session(engine) as db, db.begin():
        row = db.get(
            AdObject,
            (
                env.context.tenant_id,
                env.account.advertiser_id,
                kind,
                env.refs[kind].remote_id,
            ),
        )
        row.operation_status = original
        db.add(row)
        if disabled_parent:
            parent = env.refs["campaign"]
            parent_row = db.get(
                AdObject,
                (parent.tenant_id, parent.advertiser_id, parent.kind, parent.remote_id),
            )
            parent_row.operation_status = "DISABLE"
            db.add(parent_row)
        from app.modules.accounts.models import AdvertiserAccount, TenantBC
        from tests.modules.conftest import create_context

        peer = create_context(db)
        db.add(TenantBC(tenant_id=peer.tenant_id, bc_id=env.bc.bc_id))
        db.add(
            AdvertiserAccount(
                tenant_id=peer.tenant_id, advertiser_id=env.account.advertiser_id
            )
        )
        peer_connection = TikTokConnection(tenant_id=peer.tenant_id, status="ACTIVE")
        db.add(peer_connection)
        db.flush()
        peer_ref = EntityRef(
            peer.tenant_id, env.account.advertiser_id, kind, env.refs[kind].remote_id
        )
        db.add(
            _row(
                peer_ref,
                connection_id=peer_connection.id,
                ad_type="SMART_PLUS",
                status=original,
                configuration={
                    "promotion_type": "NATIVE_SERIES",
                    "native_series_id": "peer-series",
                },
            )
        )
        selection = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(selection, [env.refs[kind]])
        db.add(selection)
    calls, wire_state = install_wire(monkeypatch, env, kind, final)
    base = f"/api/tenants/{env.context.tenant_id}"
    with TestClient(env.app) as client:
        body = {
            "bc_id": env.bc.bc_id,
            "selection_id": str(env.selection.id),
            "mutation": {"field": "status", "mode": "set", "value": final},
            "idempotency_key": str(uuid4()),
        }
        receipt = client.post(base + "/ad-management-requests", json=body)
        assert receipt.status_code == 202, receipt.text
        task_id = receipt.json()["task_id"]
        assert receipt.json()["status"] == "PREPARING"
        assert (
            client.post(
                base + "/ad-management-requests",
                json={
                    **body,
                    "bc_id": "management-race-bc",
                    "idempotency_key": str(uuid4()),
                },
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/tenants/{peer.tenant_id}/ads/{kind}/{peer_ref.remote_id}",
                params={
                    "bc_id": env.bc.bc_id,
                    "advertiser_id": env.account.advertiser_id,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(base + "/ad-management-requests", json=body).json()["task_id"]
            == task_id
        )
        # Directory and selection changes after HTTP cannot add another target.
        with Session(engine) as db, db.begin():
            selection = db.get(FrozenSelectionRecord, env.selection.id)
            _set_selection(selection, list(env.refs.values()))
            db.add(selection)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: drive_management(env, task_id), range(2)))
        drive_management(env, task_id)
        public = client.get(base + f"/ad-management-tasks/{task_id}").json()
        selected = [
            i
            for i in public["items"]
            if i["ref"]["remote_id"] == env.refs[kind].remote_id
        ][0]
        if disabled_parent:
            hints = [i for i in public["items"] if i["reason"] == "parent_disabled"]
            assert len(hints) == 1
            assert hints[0]["execution_result"] == "NO_CHANGE"
            assert hints[0]["ref"]["remote_id"] == "campaign-native"
        assert selected["execution_result"] == "ACCEPTED", (
            selected["reason"],
            selected["attempts"],
            calls,
        )
        assert selected["original_value"] == original
        assert selected["observation_state"] == "REFRESH_PENDING"
        with Session(engine) as db:
            # 原准备检查点保留了未来 Worker 消息；回读后由该原消息结束等待。
            wakes = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == env.context.tenant_id,
                    PendingDispatch.task_name == "ad_management.execute",
                    PendingDispatch.payload["task_id"].astext == task_id,
                    PendingDispatch.available_at > datetime.now(UTC),
                )
            ).all()
            assert wakes
        before = client.get(
            base + f"/ads/{kind}/{env.refs[kind].remote_id}",
            params={"bc_id": env.bc.bc_id, "advertiser_id": env.account.advertiser_id},
        )
        assert before.status_code == 200, before.text
        assert before.json()["statuses"]["operation"] == original
        if read_failure:
            wire_state["fail_read"] = True
            drive_directory(env, redis_client, allow_failure=True)
            failed = client.get(base + f"/ad-management-tasks/{task_id}").json()
            assert failed["items"][0]["execution_result"] == "ACCEPTED"
            assert failed["items"][0]["observation_state"] == "REFRESH_PENDING"
            assert (
                client.get(
                    base + f"/ads/{kind}/{env.refs[kind].remote_id}",
                    params={
                        "bc_id": env.bc.bc_id,
                        "advertiser_id": env.account.advertiser_id,
                    },
                ).json()["statuses"]["operation"]
                == original
            )
            assert (
                client.post(
                    base + f"/ad-management-tasks/{task_id}/refresh"
                ).status_code
                == 202
            )
            wire_state["fail_read"] = False
            # 平台拒绝后的旧读取领取按现有租约恢复；只推进时钟，不改任务/目录事实。
            import sys

            from app.modules.ads import tasks as directory_tasks

            real_datetime = datetime

            class Later(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    return real_datetime.now(tz) + timedelta(minutes=3)

            monkeypatch.setattr(sys.modules[__name__], "datetime", Later)
            monkeypatch.setattr(directory_tasks, "datetime", Later)
            drive_management(env, task_id)
        drive_directory(env, redis_client)
        drive_management(env, task_id)
        refreshed = client.get(base + f"/ad-management-tasks/{task_id}").json()
        assert (
            next(
                i
                for i in refreshed["items"]
                if i["ref"]["remote_id"] == env.refs[kind].remote_id
            )["observation_state"]
            == "ACCEPTED"
        )
        after = client.get(
            base + f"/ads/{kind}/{env.refs[kind].remote_id}",
            params={"bc_id": env.bc.bc_id, "advertiser_id": env.account.advertiser_id},
        )
        assert after.json()["statuses"]["operation"] == final, (
            after.json()["statuses"],
            calls,
        )
        with Session(engine) as db:
            items = db.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == UUID(task_id)
                )
            ).all()
            target = next(
                i for i in items if i.ref["remote_id"] == env.refs[kind].remote_id
            )
            assert (target.original_value, target.final_value) == (original, final)
            assert (
                db.get(
                    AdObject,
                    (
                        peer_ref.tenant_id,
                        peer_ref.advertiser_id,
                        peer_ref.kind,
                        peer_ref.remote_id,
                    ),
                ).operation_status
                == original
            )
            attempts = db.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == target.id
                )
            ).all()
            assert len(attempts) == 1 and attempts[0].outcome == "ACCEPTED"
            from app.modules.ad_management.models import ManagementReceipt as ReceiptRow

            receipts = db.exec(
                select(ReceiptRow).where(ReceiptRow.attempt_id == attempts[0].id)
            ).all()
            assert len(receipts) == 1 and receipts[0].outcome == "ACCEPTED"
            task = db.get(ManagementTask, UUID(task_id))
            assert task.route == env.route.model_dump(mode="json")
        assert len([c for c in calls if c[0] == "POST"]) == 1
        assert any(c[0] == "GET" for c in calls)


@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("final", ["DISABLE", "ENABLE"])
def test_native_status_workspace(
    native_status_env, redis_client, monkeypatch, kind, final
):
    """同一真实详情操作直达 Worker，再由工作区和任务页读取结果。"""
    import os
    import socket
    import subprocess
    import threading
    from pathlib import Path

    import uvicorn
    from fastapi.routing import APIRoute

    from app.main import app
    from app.models import User

    env = native_status_env
    original = "ENABLE" if final == "DISABLE" else "DISABLE"
    with Session(engine) as db, db.begin():
        row = db.get(
            AdObject,
            (
                env.context.tenant_id,
                env.account.advertiser_id,
                kind,
                env.refs[kind].remote_id,
            ),
        )
        row.operation_status = original
        db.add(row)
        actor = db.get(User, env.context.actor_id)
        db.expunge(actor)
    calls, _ = install_wire(monkeypatch, env, kind, final)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = env.app.dependency_overrides[get_db]
    app.dependency_overrides[get_current_user] = lambda: actor

    def advance(task_id: str, phase: str):
        if phase == "write":
            drive_management(env, task_id)
        else:
            drive_directory(env, redis_client)
            drive_management(env, task_id)
        return {"advanced": True}

    driver = APIRoute(
        "/api/test-native-management/{task_id}/{phase}", advance, methods=["POST"]
    )
    app.router.routes.insert(0, driver)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    # 工作区首屏使用既有异步查询缓存；由原规划/构建 owner 完成，不伪造页面响应。
    from redis import Redis

    from app.modules.reporting.page_cache_tasks import plan_due, refresh_query_page

    stop_cache = threading.Event()
    cache_errors = []

    def query_worker():
        try:
            with Redis.from_url(settings.REDIS_URL) as cache:
                while not stop_cache.is_set():
                    plan_due(
                        cache,
                        lambda key, token: refresh_query_page.run(key=key, token=token),
                    )
                    stop_cache.wait(0.1)
        except Exception as error:
            cache_errors.append(error)

    cache_thread = threading.Thread(target=query_worker, daemon=True)
    cache_thread.start()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        frontend_port = port_socket.getsockname()[1]
    try:
        result = subprocess.run(
            [
                "bun",
                "run",
                "test",
                "--project=workspace",
                "tests/ads-detail.spec.ts",
                "-g",
                "@native-management-live",
                "--workers=1",
                "--reporter=line",
            ],
            cwd=Path(__file__).resolve().parents[4] / "frontend",
            env={
                **os.environ,
                "API_PROXY_TARGET": f"http://127.0.0.1:{sock.getsockname()[1]}",
                "PLAYWRIGHT_PORT": str(frontend_port),
                "NATIVE_MANAGEMENT_TENANT": str(env.context.tenant_id),
                "NATIVE_MANAGEMENT_BC": env.bc.bc_id,
                "NATIVE_MANAGEMENT_KIND": kind,
                "NATIVE_MANAGEMENT_FINAL": final,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert not cache_errors, cache_errors
        assert result.returncode == 0, result.stdout + result.stderr
        assert len([c for c in calls if c[0] == "POST"]) == 1
        assert any(c[0] == "GET" for c in calls)
    finally:
        stop_cache.set()
        cache_thread.join(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        app.router.routes.remove(driver)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_native_failed_readback_retry_is_read_only(
    native_status_env, redis_client, monkeypatch
):
    test_native_status_http_worker_readback(
        native_status_env, redis_client, monkeypatch, "ad", "DISABLE", read_failure=True
    )


@pytest.mark.parametrize("boundary", ["grant", "connection", "actor", "route"])
def test_native_revocation_before_send(native_status_env, monkeypatch, boundary):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.ad_management.preparation import prepare_direct_task
    from app.modules.tenants.models import TenantMembership

    env = native_status_env
    with Session(engine) as db, db.begin():
        record = db.get(FrozenSelectionRecord, env.selection.id)
        _set_selection(record, [env.refs["ad"]])
        db.add(record)
    calls, _ = install_wire(monkeypatch, env, "ad", "DISABLE")
    base = f"/api/tenants/{env.context.tenant_id}"
    with TestClient(env.app) as client:
        response = client.post(
            base + "/ad-management-requests",
            json={
                "bc_id": env.bc.bc_id,
                "selection_id": str(env.selection.id),
                "mutation": {"field": "status", "mode": "set", "value": "DISABLE"},
                "idempotency_key": str(uuid4()),
            },
        )
        assert response.status_code == 202
        task_id = UUID(response.json()["task_id"])
    prepare_direct_task(engine, task_id)
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
        elif boundary == "actor":
            row = db.get(
                TenantMembership, (env.context.tenant_id, env.context.actor_id)
            )
            row.active = False
        else:
            row = db.get(TikTokConnection, env.route.connection_id)
            if boundary == "route":
                row.authorization_revision += 1
            else:
                row.status = "DISABLED"
        db.add(row)
    drive_management(env, task_id)
    assert not calls
    with Session(engine) as db:
        item = db.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.task_id == task_id)
        ).one()
        assert item.execution_result != "ACCEPTED"


def test_native_disabled_parent_is_only_a_hint(
    native_status_env, redis_client, monkeypatch
):
    test_native_status_http_worker_readback(
        native_status_env,
        redis_client,
        monkeypatch,
        "adgroup",
        "ENABLE",
        disabled_parent=True,
    )
