import pytest
from sqlalchemy import text


def _truncate_test_database() -> None:
    """Reset the dedicated API test database after the Engine-path test commits."""
    from app.core.db import engine

    with engine.begin() as connection:
        table_names = (
            connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename NOT IN "
                    # 迁移种入的不可变文案池属于共享基线；清空会让后续策略测试失去引用。
                    "('alembic_version', 'copy_pool_version', 'copy_entry')"
                )
            )
            .scalars()
            .all()
        )
        if table_names:
            quoted = ", ".join(
                f'"public"."{name.replace(chr(34), chr(34) * 2)}"'
                for name in table_names
            )
            connection.execute(
                text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
            )


def test_reporting_routes_have_stable_operation_ids():
    from app.main import app

    reporting = []
    for _path, methods in app.openapi()["paths"].items():
        for _method, route in methods.items():
            if route.get("tags") == ["ads_reporting"]:
                reporting.append(route)
    assert reporting
    operation_ids = [route["operationId"] for route in reporting]
    assert all(
        operation_id.startswith("ads_reporting-") for operation_id in operation_ids
    )
    assert len(operation_ids) == len(set(operation_ids))


def test_query_snapshot_survives_request_session_rollback(report_case, client):
    """GET creates a durable local snapshot consumed by a later request."""
    from app.api.deps import get_current_user

    # 与 client 使用同一应用实例；其他用例会 reload app.main 生成新对象。
    app = client.app
    from app.models import User

    user = report_case.session.get(User, report_case.context.actor_id)
    assert user is not None
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        path = f"/api/tenants/{report_case.context.tenant_id}/ads"
        params = {
            "bc_id": report_case.bc_id,
            "dimension": "campaign",
            "start_date": "2026-09-30",
            "end_date": "2026-09-30",
        }
        report_case.seed_campaign("P-API", "report-account", 1, 2)
        first = client.get(path, params=params)
        assert first.status_code == 200, first.text
        snapshot_id = first.json()["snapshot"]["snapshot_id"]
        second = client.get(path, params=params | {"snapshot_id": snapshot_id})
        assert second.status_code == 200, second.text
        assert second.json()["snapshot"]["snapshot_id"] == snapshot_id
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


def test_reporting_write_routes_are_viewer_denied_and_operator_gated(
    report_case, client
):
    """HTTP role gates run before report/BC mutations, for every write route."""
    from app.api.deps import get_current_user

    # 与 client 使用同一应用实例；其他用例会 reload app.main 生成新对象。
    app = client.app
    from app.models import User
    from app.modules.tenants.models import TenantMembership

    user = report_case.session.get(User, report_case.context.actor_id)
    membership = report_case.session.get(
        TenantMembership, (report_case.context.tenant_id, report_case.context.actor_id)
    )
    assert user is not None and membership is not None
    base = f"/api/tenants/{report_case.context.tenant_id}"
    snapshot_id = "00000000-0000-4000-8000-000000000001"
    view_id = "00000000-0000-4000-8000-000000000002"
    write_requests = (
        (
            "post",
            f"{base}/ad-selections",
            {"snapshot_id": snapshot_id, "mode": "EXPLICIT"},
        ),
        (
            "post",
            f"{base}/report-views",
            {
                "name": "权限测试视图",
                "filters": {
                    "dimension": "campaign",
                    "start_date": "2026-09-30",
                    "end_date": "2026-09-30",
                },
                "columns": ["name", "status"],
            },
        ),
        ("patch", f"{base}/report-views/{view_id}", {"name": "更新视图"}),
        ("delete", f"{base}/report-views/{view_id}", None),
        (
            "post",
            f"{base}/report-exports",
            {"snapshot_id": snapshot_id, "idempotency_key": "permission-test"},
        ),
        (
            "post",
            f"{base}/ad-sync-refresh",
            {"advertiser_ids": []},
        ),
    )
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        membership.role = "viewer"
        report_case.session.flush()
        readable = client.get(
            f"{base}/report-views", params={"bc_id": report_case.bc_id}
        )
        assert readable.status_code == 200, readable.text
        for method, path, body in write_requests:
            # TestClient.delete 不接受 json；统一走 request，DELETE 不带请求体。
            response = client.request(
                method.upper(),
                path,
                params={"bc_id": report_case.bc_id},
                json=body,
            )
            assert response.status_code == 403, (method, path, response.text)

        membership.role = "operator"
        report_case.session.flush()
        for method, path, body in write_requests:
            # TestClient.delete 不接受 json；统一走 request，DELETE 不带请求体。
            response = client.request(
                method.upper(),
                path,
                params={"bc_id": report_case.bc_id},
                json=body,
            )
            assert response.status_code != 403, (method, path, response.text)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


@pytest.mark.filterwarnings("ignore:transaction already deassociated from connection")
def test_engine_api_cursor_and_selection_cross_request(
    report_case, client, monkeypatch, redis_client
):
    """The production Engine path commits the snapshot before the next request."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4

    from redis import Redis

    from app.modules.reporting import page_cache
    from app.modules.reporting.page_cache_tasks import plan_due

    monkeypatch.setattr(page_cache, "PREFIX", "test:report-api:" + uuid4().hex)
    monkeypatch.setattr(
        page_cache, "client", lambda: Redis.from_url(os.environ["TEST_REDIS_URL"])
    )
    registered = Event()
    original_watch = page_cache.watch

    def watch(*args, **kwargs):
        original_watch(*args, **kwargs)
        registered.set()

    monkeypatch.setattr(page_cache, "watch", watch)
    from app.api.deps import get_current_user, get_db

    # 与 client 使用同一应用实例；其他用例会 reload app.main 生成新对象。
    app = client.app
    from app.models import User

    user = report_case.session.get(User, report_case.context.actor_id)
    assert user is not None
    report_case.seed_campaign("P-Engine-A", "report-account", 1, 2)
    report_case.seed_campaign("P-Engine-B", "report-account", 1, 2)
    # The fixture uses an outer transaction plus savepoints. Commit that outer
    # connection explicitly so SessionDep's independent Engine connection can
    # observe the seed rows; this test owns a unique tenant.
    report_case.session.get_bind().commit()
    old_db = app.dependency_overrides.pop(get_db, None)
    old_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        path = f"/api/tenants/{report_case.context.tenant_id}/ads"
        params = {
            "bc_id": report_case.bc_id,
            "dimension": "campaign",
            "start_date": "2026-09-30",
            "end_date": "2026-09-30",
            "limit": "1",
        }
        # 首屏由报表 Worker 构建；用真实规划/构建函数消费本次 HTTP 登记的任务。
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(client.get, path, params=params)
            assert registered.wait(5)
            assert (
                plan_due(
                    redis_client,
                    lambda key, token: page_cache.refresh_page(
                        redis_client, key, token
                    ),
                )
                == 1
            )
            first = response.result(timeout=5)
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["next_cursor"]
        second = client.get(
            path,
            params=params
            | {
                "snapshot_id": payload["snapshot"]["snapshot_id"],
                "cursor": payload["next_cursor"],
            },
        )
        assert second.status_code == 200, second.text
        selected = client.post(
            f"/api/tenants/{report_case.context.tenant_id}/ad-selections",
            params={"bc_id": report_case.bc_id},
            json={
                "snapshot_id": payload["snapshot"]["snapshot_id"],
                "mode": "ALL_MATCHING",
            },
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["refs"]
    finally:
        keys = list(redis_client.scan_iter(page_cache.PREFIX + "*"))
        if keys:
            redis_client.delete(*keys)
        if old_db is not None:
            app.dependency_overrides[get_db] = old_db
        if old_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = old_user
        # SessionDep intentionally uses an independent Engine connection. The
        # committed seed must be removed so later tests retain transaction isolation.
        report_case.session.close()
        _truncate_test_database()
