"""JB26: Adgroup two-page direct report, spec 40/50 independent expectation."""

from datetime import date

from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy

D = date(2026, 11, 13)
TEST_BC = "123456"


def test_two_page_adgroup_worker_and_query(
    committed_sync_seed, monkeypatch, redis_client, policy
):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from sqlmodel import select

    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from app.modules.reporting.store_models import ParentReportDay
    from tests.modules.reporting.claiming import claim_jobs

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
        level = {
            "AUCTION_CAMPAIGN": "campaign",
            "AUCTION_ADGROUP": "adgroup",
            "AUCTION_AD": "ad",
        }[args["data_level"]]
        dimension = "ad_id_v2" if level == "ad" else f"{level}_id"
        payload = {
            "list": [
                {
                    "dimensions": {
                        dimension: f"{level}{page}",
                        "stat_time_day": str(D),
                    },
                    "metrics": {
                        f"{level}_name": f"{level.title()} {page}",
                        "campaign_id": f"campaign{page}",
                        "adgroup_id": f"adgroup{page}",
                        "spend": "50",
                        "native_growth_total_ad_impression_value": "-"
                        if level == "ad"
                        else "40",
                        "native_growth_ad_revenue_value_d0": "-"
                        if level == "ad"
                        else "30",
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

        s.add(TenantBC(tenant_id=seed.tenant_id, bc_id=TEST_BC, name="Adgroup Test BC"))
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
        s.commit()
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_db
    from app.main import app
    from app.models import User
    from app.modules.accounts.connection_models import BCDefaultRoute
    from app.modules.accounts.models import DiscoveryRun
    from app.modules.reporting import api as report_api

    with factory() as s:
        s.add(
            DiscoveryRun(
                tenant_id=seed.tenant_id,
                actor_id=seed.actor_id,
                connection_id=seed.connection.id,
                bc_id=TEST_BC,
                status="COMPLETE",
                completed_at=datetime.now(UTC),
            )
        )
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
        with factory() as s:
            yield s

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 13, 12, tzinfo=UTC)

    overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with monkeypatch.context() as clock:
            clock.setattr(report_api, "datetime", FrozenDateTime)
            with TestClient(app) as client:
                response = client.post(
                    f"/api/tenants/{seed.tenant_id}/ad-sync-refresh",
                    params={"bc_id": TEST_BC},
                    json={"advertiser_ids": ["acc-1"]},
                )
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "QUEUED"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
    with factory() as s:
        claimed = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=4)
        claimed = [
            j for j in claimed if j.kind in {"ad_day", "campaign_day", "adgroup_day"}
        ]
        assert {j.kind for j in claimed} == {"ad_day", "campaign_day", "adgroup_day"}
        s.commit()
    for job in claimed:
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
    assert len(calls) == 6
    assert sorted(call["data_level"] for call in calls) == sorted(
        ["AUCTION_CAMPAIGN"] * 2 + ["AUCTION_ADGROUP"] * 2 + ["AUCTION_AD"] * 2
    )
    for call in (c for c in calls if c["data_level"] == "AUCTION_ADGROUP"):
        assert json.loads(call["dimensions"]) == ["adgroup_id", "stat_time_day"]
        assert "native_growth_ad_revenue_roas" in json.loads(call["metrics"])
    with factory() as s:
        facts = s.exec(
            select(ParentReportDay).where(ParentReportDay.tenant_id == seed.tenant_id)
        ).all()
        assert len(facts) == 4
        from app.modules.reporting.store_models import ReportSyncState

        bucket = s.get(ReportSyncState, (seed.tenant_id, "acc-1", "adgroup_day", D))
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
                dimension="adgroup",
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

    from tests.modules.reporting.test_native_campaign import live_campaign_workspace

    live_campaign_workspace(
        seed, factory, level="adgroup", label="广告组", name="Adgroup 1"
    )


def test_three_levels_independent_and_direct_survives_writers(session, sync_seed):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from uuid import uuid4

    from sqlmodel import select

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_parent_day,
    )
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.read_models.projector import rebuild_pending
    from app.modules.reporting.read_models.rebuild import enqueue_fact_buckets
    from app.modules.reporting.schemas import ReportingFilter
    from app.modules.reporting.store_models import ParentReportDay, ReportSyncState
    from tests.modules.reporting.sync_conftest import ad_row
    from tests.modules.reporting.test_jobs import _route

    accounts = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
    now = datetime.now(UTC)
    route = FrozenTikTokRoute(**_route(sync_seed))

    def parent(level, total, at):
        publish_parent_day(
            session,
            level=level,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts=accounts,
            start=D,
            end=D,
            requested_at=at,
            route=route,
            rows=[
                {
                    "dimensions": {f"{level}_id": "same-id", "stat_time_day": str(D)},
                    "metrics": {
                        "spend": "50",
                        "native_growth_total_ad_impression_value": total,
                    },
                }
            ],
        )

    def query(level):
        return query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension=level, advertiser_ids=("acc-1",), start_date=D, end_date=D
            ),
        )

    parent("campaign", "40", now)
    assert (
        session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", "adgroup_day", D))
        is None
    )
    assert (
        session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", D))
        is None
    )
    assert query("campaign").items[0].metric_buckets[0].values[
        "revenue_total"
    ] == Decimal("40")
    parent("adgroup", "40", now)
    for total in ("-", "999"):
        publish_ad_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts=accounts,
            start=D,
            end=D,
            rows=[
                ad_row(
                    "a1",
                    D,
                    campaign="same-id",
                    adgroup="same-id",
                    spend="5",
                    total=total,
                )
            ],
        )
        enqueue_fact_buckets(
            session,
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            start=D,
            end=D,
        )
        rebuild_pending(session)
        for level in ("campaign", "adgroup"):
            row = query(level).items[0]
            assert row.metric_buckets[0].values["revenue_total"] == Decimal("40")
            assert row.metric_buckets[0].values["spend"] == Decimal("50")
            assert row.display["report_source"] == "PLATFORM_DIRECT"
    assert query("ad").items[0].display["platform_roas_total_reason"] == "NOT_REQUESTED"

    from app.modules.reporting.publish import mark_failed

    mark_failed(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1"],
        kind="adgroup_day",
        start=D,
        end=D,
        error_code="synthetic_page_failure",
        requested_at=now + timedelta(seconds=1),
    )
    stale = query("adgroup").items[0]
    assert stale.metric_buckets[0].values["revenue_total"] == Decimal("40")
    assert "STALE" in stale.display["report_layer_status"]
    assert "synthetic_page_failure" in stale.display["report_layer_status"]
    assert query("campaign").items[0].coverage["status"] == "COMPLETE"
    for level in ("campaign", "adgroup"):
        parent(level, "-", now + timedelta(seconds=2))
        row = query(level).items[0]
        assert row.metric_buckets[0].values["revenue_total"] is None
        assert row.metric_buckets[0].provenance["revenue_total"].field_state_counts == {
            "DASH": 1
        }
    before = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", D)
    ).publication_version
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[ad_row("a1", D, campaign="same-id", adgroup="same-id", total="1")],
        campaign_ids=("same-id",),
        requested_at=now + timedelta(seconds=3),
    )
    assert (
        session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", D)
        ).publication_version
        == before
    )
    assert (
        len(
            session.exec(
                select(ParentReportDay).where(
                    ParentReportDay.tenant_id == sync_seed.tenant_id
                )
            ).all()
        )
        == 2
    )
    from app.modules.reporting.publish import publish_adgroup_day

    campaign_version = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "campaign_day", D)
    ).publication_version
    publish_adgroup_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[],
        requested_at=now + timedelta(seconds=4),
        route=route,
    )
    assert query("adgroup").items == ()
    empty = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "adgroup_day", D)
    )
    assert empty.status == "COMPLETE" and empty.row_count == 0
    assert (
        empty.active_contract == "native-growth-day-v1"
        and len(empty.publication_digest) == 64
    )
    assert (
        session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-1", "campaign_day", D)
        ).publication_version
        == campaign_version
    )


def test_account_restore_trigger_preserves_parent(session, sync_seed):
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.publish import AccountMeta, publish_adgroup_day
    from app.modules.reporting.store_models import ParentReportDay
    from tests.modules.reporting.test_jobs import _route

    # retained parent facts precede rediscovery, as after a local account removal.
    publish_adgroup_day(
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
                "dimensions": {"adgroup_id": "c1", "stat_time_day": str(D)},
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
        ParentReportDay, (sync_seed.tenant_id, "restore-account", "adgroup", "c1", D)
    )
    assert fact.revenue_total == Decimal("40")
    assert fact.evidence["source_kind"] == "PLATFORM_DIRECT"


def test_adgroup_publication_rolls_back_fact_evidence_and_state(session, sync_seed):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from uuid import uuid4

    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.reporting.publish import AccountMeta, publish_adgroup_day
    from app.modules.reporting.store_models import ParentReportDay, ReportSyncState
    from tests.modules.reporting.test_jobs import _route

    def publish(value, at):
        publish_adgroup_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=D,
            end=D,
            requested_at=at,
            route=FrozenTikTokRoute(**_route(sync_seed)),
            rows=[
                {
                    "dimensions": {"adgroup_id": "g1", "stat_time_day": str(D)},
                    "metrics": {
                        "spend": "50",
                        "native_growth_total_ad_impression_value": value,
                    },
                }
            ],
        )

    now = datetime.now(UTC)
    publish("40", now)
    session.flush()
    key = (sync_seed.tenant_id, "acc-1", "adgroup_day", D)
    before = session.get(ReportSyncState, key).model_dump()
    session.connection().execute(
        text("""CREATE FUNCTION pg_temp.reject_adgroup_state() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NEW.report_kind='adgroup_day' THEN RAISE EXCEPTION 'JB26 atomicity probe'; END IF; RETURN NEW; END $$""")
    )
    session.connection().execute(
        text(
            "CREATE TRIGGER jb26_reject_state BEFORE UPDATE ON report_sync_state FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_adgroup_state()"
        )
    )
    try:
        with pytest.raises(DBAPIError), session.begin_nested():
            publish("999", now + timedelta(seconds=1))
        session.expire_all()
        fact = session.get(
            ParentReportDay, (sync_seed.tenant_id, "acc-1", "adgroup", "g1", D)
        )
        assert fact.revenue_total == Decimal("40")
        assert fact.evidence["publication_version"] == before["publication_version"]
        assert session.get(ReportSyncState, key).model_dump() == before
    finally:
        session.connection().execute(
            text("DROP TRIGGER jb26_reject_state ON report_sync_state")
        )


def test_adgroup_demand_rejects_foreign_scope_before_queue(session, sync_seed):
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
            kind="adgroup_day",
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


def test_three_kind_requirements_merge_and_reject_parent_subset(session, sync_seed):
    from datetime import UTC, datetime

    import pytest
    from sqlmodel import select

    from app.core.errors import DomainError
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    now = datetime.now(UTC)
    kwargs = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "tier": "hot",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "requested_at": now,
    }
    enqueue_job(session, kind="ad_day", **kwargs)
    enqueue_job(session, kind="ad_day", **kwargs)
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert sorted(j.kind for j in jobs) == ["ad_day", "adgroup_day", "campaign_day"]
    for kind in ("campaign_day", "adgroup_day"):
        with pytest.raises(DomainError):
            enqueue_job(session, kind=kind, campaign_ids=("subset",), **kwargs)
    assert (
        len(
            session.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.tenant_id == sync_seed.tenant_id
                )
            ).all()
        )
        == 3
    )
