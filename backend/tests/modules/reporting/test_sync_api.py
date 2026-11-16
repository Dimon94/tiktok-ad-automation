from datetime import date

import pytest
from sqlmodel import select

from app.api.deps import get_current_user
from app.core.config import settings
from app.models import User
from app.modules.reporting.store_models import ReportSyncJob


@pytest.fixture
def sync_api(client, session, sync_seed):
    previous = client.app.dependency_overrides.get(get_current_user)
    client.app.dependency_overrides[get_current_user] = lambda: session.get(
        User, sync_seed.actor_id
    )
    try:
        yield client, f"/api/tenants/{sync_seed.tenant_id}"
    finally:
        if previous is None:
            client.app.dependency_overrides.pop(get_current_user, None)
        else:
            client.app.dependency_overrides[get_current_user] = previous


def test_disabled_refresh_is_visible_and_queues_nothing(
    sync_api, session, sync_seed, monkeypatch
):
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SYNC_DISABLED"
    assert not session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()


def test_refresh_inserts_priority_one_jobs_and_coalesces(
    sync_api, session, sync_seed, monkeypatch
):
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued"] >= 2 and response.json()["accounts"] == 2
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {j.priority for j in jobs} == {1}
    assert {j.kind for j in jobs} == {
        "campaign_day",
        "adgroup_day",
        "ad_day",
        "creative_day",
    }
    repeat = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert repeat.json()["queued"] == 0


def test_status_only_returns_current_bc_accounts(sync_api):
    client, path = sync_api
    response = client.get(
        f"{path}/ad-sync-status",
        params={"bc_id": "bc-b", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert response.status_code == 200, response.text
    assert [a["advertiser_id"] for a in response.json()["accounts"]] == ["acc-2"]
    assert response.json()["accounts"][0]["incomplete_dates"] == [
        date(2026, 10, 5).isoformat()
    ]


def test_foreign_account_cannot_be_refreshed(sync_api, monkeypatch):
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh",
        params={"bc_id": "bc-b"},
        json={"advertiser_ids": ["acc-1"]},
    )
    assert response.status_code == 403


def test_refresh_without_api_binding_is_visible(
    sync_api, session, sync_seed, monkeypatch
):
    from app.modules.accounts.connection_models import BCConnectionBinding

    client, path = sync_api
    session.delete(
        session.get(
            BCConnectionBinding, (sync_seed.tenant_id, "bc-a", sync_seed.connection.id)
        )
    )
    session.flush()
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "API_BINDING_REQUIRED"


def test_manual_request_does_not_create_other_tenant_plans(
    sync_api, session, monkeypatch
):
    from app.modules.reporting.store_models import AdSyncBc
    from tests.modules.reporting.sync_conftest import sync_seed as make_seed

    other = make_seed.__wrapped__(session)
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert session.get(AdSyncBc, (other.tenant_id, "bc-a")) is None


def test_status_exposes_budget_slowdown_and_material_publication(
    sync_api, session, sync_seed, redis_client, monkeypatch
):
    from datetime import UTC, datetime

    from app.modules.reporting import api
    from app.modules.reporting.planner import record_calls
    from app.modules.reporting.store_models import ReportSyncState

    client, path = sync_api
    monkeypatch.setattr(api.Redis, "from_url", lambda *a, **kw: redis_client)
    monkeypatch.setattr(settings, "TIKTOK_REPORT_DAILY_CALL_BUDGET", 100)
    record_calls(redis_client, datetime.now(UTC), 75)
    params = {"bc_id": "bc-a", "start_date": "2026-10-05", "end_date": "2026-10-05"}
    first = client.get(f"{path}/ad-sync-status", params=params).json()
    assert first["plan"]["slowed"] is True
    session.add(
        ReportSyncState(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            report_kind="creative_day",
            stat_date=date(2026, 10, 5),
            status="COMPLETE",
            last_success_at=datetime.now(UTC),
        )
    )
    session.flush()
    second = client.get(f"{path}/ad-sync-status", params=params).json()
    assert second["publication_version"] != first["publication_version"]


def test_balance_publication_advances_visible_status_version(
    sync_api, session, sync_seed
):
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.modules.reporting.models import AccountBalanceObservation

    client, path = sync_api
    params = {"bc_id": "bc-a", "start_date": "2026-10-05", "end_date": "2026-10-05"}
    first = client.get(f"{path}/ad-sync-status", params=params).json()[
        "publication_version"
    ]
    session.add(
        AccountBalanceObservation(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            amount=Decimal("8"),
            currency="USD",
            availability="AVAILABLE",
            balance_scope="ADVERTISER",
            scope_id="acc-1",
            observed_at=datetime.now(UTC),
        )
    )
    session.flush()
    assert (
        client.get(f"{path}/ad-sync-status", params=params).json()[
            "publication_version"
        ]
        != first
    )


def test_directory_publication_advances_status_version_without_new_metrics(
    sync_api, session, sync_seed
):
    from uuid import uuid4

    from app.modules.accounts.routing import freeze_route
    from app.modules.ads.sync_models import AdDirectoryRun

    client, path = sync_api
    params = {"bc_id": "bc-a", "start_date": "2026-10-05", "end_date": "2026-10-05"}

    def version():
        response = client.get(f"{path}/ad-sync-status", params=params)
        assert response.status_code == 200, response.text
        return response.json()["publication_version"]

    def directory(bc, account, published):
        route = freeze_route(
            session,
            context=sync_seed.context,
            bc_id=bc,
            connection_id=sync_seed.connection.id,
        )
        row = AdDirectoryRun(
            tenant_id=sync_seed.tenant_id,
            advertiser_id=account,
            actor_id=sync_seed.actor_id,
            bc_id=bc,
            connection_id=sync_seed.connection.id,
            channel="OFFICIAL_API",
            frozen_route=route.model_dump(mode="json"),
            partition_key=uuid4().hex * 2,
            query={},
            kind="campaign",
            ad_type="REGULAR",
            status="COMPLETE" if published else "RUNNING",
            published_version=published,
        )
        session.add(row)
        session.flush()
        return row

    before = version()
    # 同一账户同时属于两个 BC，另一 BC 的发布不能推进当前版本。
    directory("bc-b", "acc-2", 99)
    assert version() == before
    run = directory("bc-a", "acc-1", None)
    assert version() == before
    run.status = "COMPLETE"
    run.published_version = 100
    session.add(run)
    session.flush()
    assert version() != before


def test_status_directory_version_reads_one_row_per_account(
    sync_api, session, sync_seed, record_property
):
    """状态轮询不能随已完成目录运行历史增长；空快照仍须推进版本。"""
    from uuid import uuid4

    from sqlalchemy import event, text

    from app.modules.accounts.routing import freeze_route
    from app.modules.ads.sync_models import AdDirectoryRun

    route = freeze_route(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        connection_id=sync_seed.connection.id,
    )
    run = AdDirectoryRun(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        connection_id=sync_seed.connection.id,
        channel="OFFICIAL_API",
        frozen_route=route.model_dump(mode="json"),
        partition_key=uuid4().hex * 2,
        query={},
        kind="ad",
        ad_type="REGULAR",
        status="COMPLETE",
        published_version=1,
    )
    session.add(run)
    session.flush()
    # 不复制唯一自增序号；只有本地测试历史，未播种任何平台对象。
    columns = [
        c.name for c in AdDirectoryRun.__table__.columns if c.name != "request_sequence"
    ]
    session.execute(
        text(f"""
        INSERT INTO ad_directory_run ({",".join(columns)})
        SELECT {",".join("incoming." + c for c in columns)}
        FROM ad_directory_run r CROSS JOIN generate_series(2,25001) i
        CROSS JOIN LATERAL jsonb_populate_record(NULL::ad_directory_run,
          to_jsonb(r)||jsonb_build_object('id',gen_random_uuid(),
            'request_id',gen_random_uuid(),'published_version',i)) incoming
        WHERE r.id=:id
        """),
        {"id": run.id},
    )
    session.execute(text("ANALYZE ad_directory_run"))
    captured = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "ad_directory_run" in statement and "published_version" in statement:
            captured.append((statement, parameters))

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    client, path = sync_api
    try:
        response = client.get(
            f"{path}/ad-sync-status",
            params={
                "bc_id": "bc-a",
                "start_date": "2026-11-11",
                "end_date": "2026-11-11",
            },
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    statement, parameters = captured[0]
    assert connection.exec_driver_sql(statement, parameters).scalar_one() == 25001
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement,
        parameters,
    ).scalar_one()[0]

    def scans(node):
        own = 0
        if node.get("Relation Name") == "ad_directory_run":
            own = (
                node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
            ) * node.get("Actual Loops", 1)
        return own + sum(scans(child) for child in node.get("Plans", []))

    scanned = scans(plan["Plan"])
    record_property("directory_history_rows", 25001)
    record_property("directory_rows_scanned", scanned)
    record_property("directory_probe_ms", plan["Execution Time"])
    assert scanned <= 2, plan
