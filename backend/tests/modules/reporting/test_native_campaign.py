"""JB25: 合成平台回执；预期来自 AC。"""

from datetime import date

from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.reporting.test_jobs import _route

D = date(2026, 11, 13)
TEST_BC = "123456"


def test_native_daily_contract_and_hour_exclusion():
    requests = []

    def send(_endpoint, _advertiser, args):
        requests.append(args)
        return {}

    reader = SdkReportReader(send=send)
    reader.campaign_day(advertiser_ids=("acc-1",), start=D, end=D, page=1)
    assert requests[-1]["report_type"] == "BASIC"
    assert requests[-1]["service_type"] == "AUCTION"
    assert requests[-1]["dimensions"] == ["campaign_id", "stat_time_day"]
    assert "native_growth_ad_revenue_roas" in requests[-1]["metrics"]
    assert "native_growth_ad_revenue_roas_d0" in requests[-1]["metrics"]
    reader.campaign_hour(advertiser_ids=("acc-1",), day=D, page=1)
    assert "spend" in requests[-1]["metrics"]
    assert not any(k.startswith("native_growth") for k in requests[-1]["metrics"])


def test_two_page_campaign_worker_and_query(
    committed_sync_seed, monkeypatch, redis_client, policy
):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from sqlmodel import select

    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from app.modules.reporting.store_models import ParentReportDay
    from tests.modules.reporting.claiming import claim_jobs
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    calls = []
    import json
    from urllib.parse import parse_qs, urlsplit

    from urllib3.response import HTTPResponse

    from app.core.config import settings
    from app.core.credentials import encrypt_credentials
    from app.core.db import engine
    from app.modules.accounts.models import TikTokConnection
    from app.modules.reporting.job_tasks import run_job_step

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {"base": policy.model_dump() | {"lease_ms": 240000}},
    )

    def transport(_pool, _method, url, **kwargs):
        args = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        args.update(dict(kwargs.get("fields") or []))
        calls.append(args)
        page = int(args["page"])
        payload = {
            "list": [
                {
                    "dimensions": {"campaign_id": f"c{page}", "stat_time_day": str(D)},
                    "metrics": {
                        "campaign_name": f"Campaign {page}",
                        "spend": "50",
                        "native_growth_total_ad_impression_value": "40",
                        "native_growth_ad_revenue_value_d0": "30",
                        "native_growth_ad_revenue_roas": "0.801234567890123456789",
                        "native_growth_ad_revenue_roas_d0": "0.601",
                    },
                }
            ],
            "page_info": {
                "page": page,
                "page_size": 1,
                "total_page": 2,
                "total_number": 2,
            },
        }
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": payload, "request_id": "synthetic-jb25"}
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    with factory() as s:
        from app.modules.accounts.connection_models import BCConnectionBinding
        from app.modules.accounts.models import BCAccountAccess, TenantBC

        s.add(
            TenantBC(tenant_id=seed.tenant_id, bc_id=TEST_BC, name="Campaign Test BC")
        )
        s.flush()
        original = s.get(
            BCAccountAccess, (seed.tenant_id, "bc-a", "acc-1", seed.connection.id)
        )
        s.add(BCAccountAccess(**(original.model_dump() | {"bc_id": TEST_BC})))
        binding = s.get(
            BCConnectionBinding, (seed.tenant_id, "bc-a", seed.connection.id)
        )
        s.add(BCConnectionBinding(**(binding.model_dump() | {"bc_id": TEST_BC})))
        connection = s.get(TikTokConnection, seed.connection.id)
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=seed.tenant_id, value={"access_token": "synthetic-offline-jb25"}
        )
        s.add(connection)
        enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            bc_id=TEST_BC,
            kind="campaign_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed) | {"bc_id": TEST_BC},
            actor_id=seed.actor_id,
        )
        s.commit()
        job = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        s.commit()
    outcome = run_job_step(
        database_engine=engine,
        redis_client=redis_client,
        job_id=job.id,
        claim_token=job.claim_token,
    )
    with factory() as check:
        from app.modules.reporting.store_models import ReportSyncJob

        state = check.get(ReportSyncJob, job.id)
        assert outcome == "COMPLETE", (outcome, state.error_code, state.progress)

    assert len(calls) == 2
    with factory() as s:
        facts = s.exec(
            select(ParentReportDay).where(ParentReportDay.tenant_id == seed.tenant_id)
        ).all()
        assert len(facts) == 2
        from app.modules.reporting.store_models import ReportSyncState

        bucket = s.get(ReportSyncState, (seed.tenant_id, "acc-1", "campaign_day", D))
        assert bucket.active_contract == "native-growth-day-v1"
        assert len(bucket.publication_digest) == 64

        assert (
            facts[0].evidence["fields"]["platform_roas_total"]["value"]
            == "0.801234567890123456789"
        )
        result = query_ads(
            s,
            context=seed.context,
            bc_id=TEST_BC,
            filters=ReportingFilter(
                dimension="campaign",
                advertiser_ids=("acc-1",),
                start_date=D,
                end_date=D,
            ),
        )
        assert len(result.items) == 2
        for row in result.items:
            assert row.metric_buckets[0].values["revenue_total"] == Decimal("40")
            assert row.metric_buckets[0].values["roas_total"] == Decimal("0.8")
            assert row.metric_buckets[0].availability["revenue_total"] == "AVAILABLE"
            assert row.metric_buckets[0].provenance[
                "roas_total"
            ].field_state_counts == {"VALUE": 2}
            assert row.coverage["status"] == "COMPLETE"
    live_campaign_workspace(seed, factory)


def test_direct_authority_and_missing_states(session, sync_seed):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from uuid import uuid4

    import pytest

    from app.core.errors import DomainError
    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_campaign_day,
    )
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.sync_conftest import ad_row

    accounts = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
    now = datetime.now(UTC)
    # 子广告有历史正收益；父级直接缺项仍必须权威。
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="50", d0="999", total="999")],
    )

    def parent(metrics):
        return {
            "dimensions": {"campaign_id": "c1", "stat_time_day": str(D)},
            "metrics": metrics,
        }

    publish_campaign_day(
        session,
        route=FrozenTikTokRoute(**_route(sync_seed)),
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        requested_at=now,
        rows=[
            parent(
                {
                    "spend": "50",
                    "native_growth_total_ad_impression_value": "-",
                    "native_growth_ad_revenue_value_d0": "0",
                    "native_growth_ad_revenue_roas": None,
                }
            )
        ],
    )
    filters = ReportingFilter(
        dimension="campaign",
        advertiser_ids=("acc-1",),
        start_date=D,
        end_date=D,
        sort_by="revenue_total",
    )
    result = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters
    )
    vector = result.items[0].metric_buckets[0]
    assert vector.values["revenue_total"] is None
    assert vector.values["revenue_d0"] == Decimal("0")
    assert vector.provenance["revenue_total"].field_state_counts == {"DASH": 1}
    assert vector.provenance["revenue_d0"].field_state_counts == {"VALUE": 1}
    assert result.items[0].display["platform_roas_total_reason"] == "NULL"
    assert result.items[0].display["platform_roas_d0_reason"] == "ABSENT"
    assert (
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=filters.model_copy(update={"min_d0_roas": Decimal("1")}),
        ).items
        == ()
    )
    # 广告发布与 qem 整桶重建不能覆盖 parent；不下摊父级收入。
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="5", d0="-", total="-")],
    )
    from app.modules.reporting.read_models.projector import rebuild_pending
    from app.modules.reporting.read_models.rebuild import enqueue_fact_buckets

    enqueue_fact_buckets(
        session, tenant_id=sync_seed.tenant_id, advertiser_id="acc-1", start=D, end=D
    )
    rebuild_pending(session)
    again = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert again.items[0].metric_buckets[0].values["spend"] == Decimal("50")
    assert again.items[0].metric_buckets[0].values["revenue_total"] is None
    ad = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"dimension": "ad"}),
    )
    assert ad.items[0].metric_buckets[0].values["revenue_total"] is None
    for value in ("NaN", "Infinity", "bad", 0.1):
        with pytest.raises(DomainError):
            publish_campaign_day(
                session,
                route=FrozenTikTokRoute(**_route(sync_seed)),
                tenant_id=sync_seed.tenant_id,
                job_id=uuid4(),
                accounts=accounts,
                start=D,
                end=D,
                requested_at=now + timedelta(seconds=1),
                rows=[parent({"native_growth_total_ad_impression_value": value})],
            )
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        query_ads(session, context=sync_seed.context, bc_id="bc-b", filters=filters)

    second = {
        "dimensions": {"campaign_id": "c2", "stat_time_day": str(D)},
        "metrics": {
            "spend": "5",
            "native_growth_total_ad_impression_value": "1",
            "native_growth_ad_revenue_value_d0": "1",
        },
    }
    publish_campaign_day(
        session,
        route=FrozenTikTokRoute(**_route(sync_seed)),
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        requested_at=now + timedelta(seconds=2),
        rows=[
            parent(
                {
                    "spend": "50",
                    "native_growth_total_ad_impression_value": "-",
                    "native_growth_ad_revenue_value_d0": "0",
                }
            ),
            second,
        ],
    )
    ordered = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"sort_direction": "desc"}),
    )
    assert [r.display["remote_id"] for r in ordered.items] == ["c2", "c1"]
    assert [r.metric_buckets[0].values["revenue_total"] for r in ordered.items] == [
        Decimal("1"),
        None,
    ]
    bounded = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"min_d0_roas": Decimal("0.1")}),
    )
    assert [r.display["remote_id"] for r in bounded.items] == ["c2"]


def live_campaign_workspace(
    seed,
    factory,
    *,
    level="campaign",
    label="系列",
    name="Campaign 1",
    attempt_status="COMPLETE",
    refresh_status="",
    scope_demo=False,
    snapshot_refresh=None,
    material_extended=False,
):
    """真实 FastAPI/Vite/浏览器；唯一平台替身在 urllib3 transport。"""
    import json
    import os
    import socket
    import subprocess
    import threading
    from pathlib import Path

    import uvicorn
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_db
    from app.main import app
    from app.models import User
    from app.modules.accounts.connection_models import BCDefaultRoute

    with factory() as s:
        if s.get(BCDefaultRoute, (seed.tenant_id, TEST_BC)) is None:
            s.add(
                BCDefaultRoute(
                    tenant_id=seed.tenant_id,
                    bc_id=TEST_BC,
                    connection_id=seed.connection.id,
                    channel="OFFICIAL_API",
                    binding_revision=0,
                    revision=0,
                )
            )
        s.commit()
        user = s.get(User, seed.actor_id)

    def db():
        with factory() as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: user
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    import tempfile

    from app.modules.reporting.page_cache import client as redis_connection
    from app.modules.reporting.page_cache import refresh_page
    from app.modules.reporting.page_cache_tasks import plan_due

    barrier_directory = tempfile.TemporaryDirectory(prefix="jb29-live-")
    barrier = Path(barrier_directory.name) / "refresh"
    snapshot_errors = []

    def source_refresh():
        while not stop.wait(0.05):
            if barrier.exists():
                try:
                    outcome = snapshot_refresh()
                    barrier.with_suffix(".done").write_text(json.dumps(outcome))
                except Exception as error:
                    snapshot_errors.append(error)
                return

    if snapshot_refresh:
        from datetime import UTC, datetime

        from app.modules.ads.models import AdObject

        with factory() as s:
            for kind, parent in (
                ("campaign", None),
                ("adgroup", "campaign"),
                ("ad", "adgroup"),
            ):
                for i in (1, 2):
                    key = (seed.tenant_id, "acc-1", kind, f"{kind}{i}")
                    if s.get(AdObject, key) is None:
                        s.add(
                            AdObject(
                                tenant_id=seed.tenant_id,
                                advertiser_id="acc-1",
                                kind=kind,
                                remote_id=f"{kind}{i}",
                                parent_kind=parent,
                                parent_remote_id=f"{parent}{i}" if parent else None,
                                name=f"{kind.title()} {1 if kind == 'campaign' else i}",
                                ad_type="NATIVE_SERIES",
                                observed_at=datetime.now(UTC),
                                published_version=1,
                            )
                        )
            s.commit()
    stop = threading.Event()

    def cache_worker():
        with redis_connection() as redis:
            while not stop.wait(0.05):
                plan_due(redis, lambda key, token: refresh_page(redis, key, token))

    cache_thread = threading.Thread(target=cache_worker, daemon=True)
    cache_thread.start()
    source_thread = (
        threading.Thread(target=source_refresh, daemon=True)
        if snapshot_refresh
        else None
    )
    if source_thread:
        source_thread.start()
    try:
        if level != "material":
            with TestClient(app) as client:
                import time

                started = time.perf_counter()
                response = client.get(
                    f"/api/tenants/{seed.tenant_id}/ads",
                    params={
                        "bc_id": TEST_BC,
                        "dimension": level,
                        "force_refresh": bool(snapshot_refresh),
                        "start_date": str(D),
                        "end_date": str(D),
                        "advertiser_id": "acc-1",
                    },
                )
                assert response.status_code == 200, response.text
                sync_response = client.get(
                    f"/api/tenants/{seed.tenant_id}/ad-sync-status",
                    params={"bc_id": TEST_BC, "start_date": str(D), "end_date": str(D)},
                )
                assert sync_response.status_code == 200, sync_response.text
                layers = sync_response.json()["accounts"][0]["levels"]
                assert {item["level"] for item in layers} == {
                    "campaign",
                    "adgroup",
                    "ad",
                }
                selected = next(item for item in layers if item["level"] == level)
                assert selected["status"] == attempt_status
                assert selected["publication_version"] > 0
                assert (
                    selected["last_success_at"]
                    and selected["last_attempt_at"]
                    and selected["observed_at"]
                )
                first_ms = (time.perf_counter() - started) * 1000
                payload = response.json()
                if snapshot_refresh:
                    assert (
                        payload["items"][0]["display"]["report_refresh_status"]
                        == "COMPLETE"
                    ), payload["items"][0]["display"]["report_layer_status"]
                started = time.perf_counter()
                page1 = client.get(
                    f"/api/tenants/{seed.tenant_id}/ads",
                    params={
                        "bc_id": TEST_BC,
                        "dimension": level,
                        "start_date": str(D),
                        "end_date": str(D),
                        "advertiser_id": "acc-1",
                        "limit": 1,
                    },
                ).json()
                page2 = client.get(
                    f"/api/tenants/{seed.tenant_id}/ads",
                    params={
                        "bc_id": TEST_BC,
                        "dimension": level,
                        "start_date": str(D),
                        "end_date": str(D),
                        "advertiser_id": "acc-1",
                        "limit": 1,
                        "snapshot_id": page1["snapshot"]["snapshot_id"],
                        "cursor": page1["next_cursor"],
                    },
                )
                assert page2.status_code == 200
                assert (
                    page2.json()["items"][0]["row_key"] != page1["items"][0]["row_key"]
                )
                print(  # noqa: T201
                    f"JB25 HTTP first={first_ms:.2f}ms; limit1 first+next={(time.perf_counter() - started) * 1000:.2f}ms; SDK requests=2"
                )
                assert (
                    payload["items"][0]["display"]["platform_roas_total"]
                    == "0.801234567890123456789"
                )
                assert (
                    payload["items"][0]["metric_buckets"][0]["values"]["revenue_total"]
                    == "40"
                )
                catalog = client.get(
                    f"/api/tenants/{seed.tenant_id}/ads/metric-catalog"
                ).json()
                d0 = next(m for m in catalog if m["key"] == "revenue_d0")
                assert d0["contract"]["window"] == "CLICK_HOURS"
                assert d0["contract"]["click_hours"] == 24
                assert d0["contract"]["traffic_scope"] == "UNCONFIRMED"
                assert d0["levels"] == ["campaign", "adgroup", "ad"]
                assert d0["source_kind"] == "PLATFORM_DIRECT"
                roas = next(m for m in catalog if m["key"] == "roas_total")
                assert roas["contract"]["aggregation"] == "RATIO_OF_SUMS"
                assert roas["source_kind"] == "LOCAL_CALCULATION"
                assert roas["contract"]["numerator"] == "revenue_total"
                assert (
                    roas["platform_observation"]["aggregation"]
                    == "SINGLE_OBSERVATION_ONLY"
                )
                assert (
                    roas["platform_observation"]["field"]
                    == "native_growth_ad_revenue_roas"
                )
        thread.start()
        pending_exports = []
        if snapshot_refresh:
            import httpx

            from tests.modules.reporting.native_export_harness import capture

            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
                pending_exports = capture(http, seed)
        result = subprocess.run(
            [
                "bun",
                "run",
                "test",
                "--project=workspace",
                "tests/ads-reporting.spec.ts",
                "-g",
                "@material-instance-live"
                if level == "material"
                else "@native-campaign-live",
                "--reporter=line",
            ],
            cwd=Path(__file__).resolve().parents[4] / "frontend",
            env={
                **os.environ,
                "API_PROXY_TARGET": f"http://127.0.0.1:{port}",
                "NATIVE_CAMPAIGN_TENANT": str(seed.tenant_id),
                "NATIVE_LEVEL": level,
                "JB34_CAPTURE": str(barrier.with_suffix(".json")),
                "JB29_REFRESH_BARRIER": str(barrier) if snapshot_refresh else "",
                "JB28_SCOPE_DEMO": "1" if scope_demo else "",
                "NATIVE_LABEL": label,
                "NATIVE_NAME": name,
                "NATIVE_REFRESH_STATUS": refresh_status,
                "PLAYWRIGHT_PORT": "5206",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert not snapshot_errors, snapshot_errors
        assert result.returncode == 0, result.stdout + result.stderr
        if level == "material":
            captured = json.loads(barrier.with_suffix(".json").read_text())
            pending_exports = [(captured["export_id"], captured["page"])]
        if material_extended:
            import httpx

            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
                full = http.get(
                    f"/api/tenants/{seed.tenant_id}/ads",
                    params={
                        "bc_id": TEST_BC,
                        "dimension": "material",
                        "advertiser_id": "acc-1",
                        "start_date": str(D),
                        "end_date": str(D),
                        "force_refresh": True,
                    },
                )
                assert full.status_code == 200, full.text
                page = full.json()
                all_instances = [
                    i for r in page["items"] for i in r["material_instances"]
                ]
                assert {c for i in all_instances for c in i["creative_ids"]} == {
                    "cr1",
                    "cr2",
                    "carousel",
                    "conflict",
                    "unmapped",
                }
                assert sorted(i["row_kind"] for i in all_instances) == [
                    "COMPOSITE",
                    "INSTANCE",
                    "UNMAPPED_CREATIVE",
                    "UNMAPPED_CREATIVE",
                ]
                exact = next(i for i in all_instances if i["row_kind"] == "INSTANCE")
                assert exact["creative_ids"] == ["cr1", "cr2"]
                assert exact["metric_buckets"][0]["values"]["spend"] == "40"
                assert exact["field_state_counts"]["spend"]["VALUE"] == 2
                export = http.post(
                    f"/api/tenants/{seed.tenant_id}/report-exports",
                    params={"bc_id": TEST_BC},
                    json={
                        "snapshot_id": page["snapshot"]["snapshot_id"],
                        "idempotency_key": "jb35-full",
                    },
                )
                assert export.status_code == 200, export.text
                pending_exports.append((export.json()["id"], page))
        if pending_exports:
            from tests.modules.reporting.native_export_harness import complete

            complete(f"http://127.0.0.1:{port}", seed, factory, pending_exports)
    finally:
        stop.set()
        cache_thread.join(timeout=10)
        if source_thread:
            source_thread.join(timeout=10)
        barrier_directory.cleanup()
        server.should_exit = True
        if thread.ident:
            thread.join(timeout=10)
        sock.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_campaign_demand_rejects_foreign_scope_before_queue(session, sync_seed):
    import pytest
    from sqlalchemy import func
    from sqlmodel import select

    from app.core.errors import DomainError
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ParentReportDay, ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    before = session.exec(select(func.count()).select_from(ReportSyncJob)).one()
    with pytest.raises(DomainError):
        enqueue_job(
            session,
            tenant_id=sync_seed.tenant_id,
            actor_id=sync_seed.actor_id,
            bc_id="bc-b",
            kind="campaign_day",
            tier="manual",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(sync_seed) | {"bc_id": "bc-b"},
        )
    assert session.exec(select(func.count()).select_from(ReportSyncJob)).one() == before
    assert (
        session.exec(
            select(ParentReportDay).where(
                ParentReportDay.tenant_id == sync_seed.tenant_id
            )
        ).all()
        == []
    )


def test_campaign_tenant_bc_isolation_and_index(session, sync_seed):
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy import text

    from app.modules.reporting.publish import AccountMeta, publish_campaign_day
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    for seed, advertiser, amount in (
        (sync_seed, "acc-1", "40"),
        (sync_seed, "acc-2", "222"),
        (other, "acc-1", "777"),
    ):
        publish_campaign_day(
            session,
            route=FrozenTikTokRoute(**_route(seed)),
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={advertiser: AccountMeta("USD", "Asia/Hong_Kong")},
            start=D,
            end=D,
            requested_at=datetime.now(UTC),
            rows=[
                {
                    "dimensions": {"campaign_id": "same-id", "stat_time_day": str(D)},
                    "metrics": {
                        "spend": "50",
                        "native_growth_total_ad_impression_value": amount,
                    },
                }
            ],
        )
    filters = ReportingFilter(
        dimension="campaign", advertiser_ids=("acc-1",), start_date=D, end_date=D
    )
    for seed, bc, adv, expected in (
        (sync_seed, "bc-a", "acc-1", "40"),
        (sync_seed, "bc-b", "acc-2", "222"),
        (other, "bc-a", "acc-1", "777"),
    ):
        result = query_ads(
            session,
            context=seed.context,
            bc_id=bc,
            filters=filters.model_copy(update={"advertiser_ids": (adv,)}),
        )
        assert result.items[0].metric_buckets[0].values["revenue_total"] == Decimal(
            expected
        )
        if seed is sync_seed and adv == "acc-1":
            primary_snapshot = result.snapshot.snapshot_id
    import pytest
    from fastapi import HTTPException

    from app.modules.accounts.models import BCAccountAccess

    grant = session.get(
        BCAccountAccess, (sync_seed.tenant_id, "bc-a", "acc-1", sync_seed.connection.id)
    )
    grant.authorized = False
    session.add(grant)
    session.flush()
    with pytest.raises(HTTPException):
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=filters,
            snapshot_id=primary_snapshot,
        )

    # 小夹具下默认可选顺扫；关闭顺扫只证明索引能约束完整租户/账户/层/日期。
    session.connection().execute(text("SET LOCAL enable_seqscan=off"))
    plan = (
        session.connection()
        .execute(
            text(
                "EXPLAIN SELECT remote_id,spend,revenue_total FROM parent_report_day WHERE tenant_id=:tenant AND advertiser_id='acc-1' AND level='campaign' AND stat_date=:day"
            ),
            {"tenant": sync_seed.tenant_id, "day": D},
        )
        .scalars()
        .all()
    )
    assert any("ix_parent_report_period" in line for line in plan)
    print("JB25 parent index plan:", "\n".join(plan))  # noqa: T201


def test_complete_empty_campaign_and_legacy_ad(session, sync_seed):
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy import text

    from app.modules.ads.models import AdObject
    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_campaign_day,
    )
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.sync_conftest import ad_row

    accounts = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="campaign",
            remote_id="c1",
            ad_type="NATIVE_SERIES",
            name="Empty Campaign",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[ad_row("legacy", D, total="8")],
    )
    session.connection().execute(
        text("UPDATE ad_report_day SET evidence=NULL WHERE tenant_id=:tenant"),
        {"tenant": sync_seed.tenant_id},
    )
    publish_campaign_day(
        session,
        route=FrozenTikTokRoute(**_route(sync_seed)),
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        rows=[],
    )
    filters = ReportingFilter(
        dimension="campaign",
        advertiser_ids=("acc-1",),
        start_date=D,
        end_date=D,
        include_empty=True,
    )
    result = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters
    )
    assert result.items[0].metric_buckets[0].values["revenue_total"] is None
    assert (
        result.items[0].metric_buckets[0].provenance["revenue_total"].contract_revision
        == "native-growth-day-v1"
    )
    assert (
        result.items[0].metric_buckets[0].provenance["revenue_total"].reason
        == "NO_RETURNED_ROW"
    )
    ad = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"dimension": "ad", "include_empty": False}),
    )
    assert ad.items[0].metric_buckets[0].values["revenue_total"] == Decimal("8")
    assert (
        ad.items[0].metric_buckets[0].provenance["revenue_total"].contract_revision
        == "LEGACY_UNKNOWN"
    )


def test_account_restore_trigger_preserves_parent(session, sync_seed):
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.publish import AccountMeta, publish_campaign_day
    from app.modules.reporting.store_models import ParentReportDay

    # retained parent facts precede rediscovery, as after a local account removal.
    publish_campaign_day(
        session,
        route=FrozenTikTokRoute(**_route(sync_seed)),
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"restore-account": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        rows=[
            {
                "dimensions": {"campaign_id": "c1", "stat_time_day": str(D)},
                "metrics": {
                    "spend": "50",
                    "native_growth_total_ad_impression_value": "40",
                },
            }
        ],
    )
    session.add(
        AdvertiserAccount(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="restore-account",
            currency="USD",
            timezone="UTC",
        )
    )
    session.flush()  # real qem_advertiser_insert and account-report restore triggers
    fact = session.get(
        ParentReportDay, (sync_seed.tenant_id, "restore-account", "campaign", "c1", D)
    )
    assert fact.revenue_total == Decimal("40")
    assert fact.evidence["source_kind"] == "PLATFORM_DIRECT"


def test_ad_evidence_tracks_requested_fields_without_copying_money(session, sync_seed):
    """Spec R2/R10: 未请求不等于未返回，金额只由 Numeric 事实持有。"""
    from decimal import Decimal
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from app.modules.reporting.store_models import AdReportDay
    from tests.modules.reporting.sync_conftest import ad_row

    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=[ad_row("requested-fields", D, spend="50", total="8")],
    )
    fact = session.get(
        AdReportDay, (sync_seed.tenant_id, "acc-1", D, "requested-fields")
    )
    assert fact is not None
    assert fact.revenue_total == Decimal("8")
    assert "platform_roas_total" not in fact.evidence["field_states"]
    assert "platform_roas_d0" not in fact.evidence["field_states"]
    assert fact.evidence["platform_roas"] == {}
    assert fact.evidence["fields"]["revenue_total"]["status"] == "VALUE"
    assert "value" not in fact.evidence["fields"]["revenue_total"]
    assert "value" not in fact.evidence["fields"]["spend"]
