"""JB26: Adgroup two-page direct report, spec 40/50 independent expectation."""

from datetime import date

import pytest

from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy

D = date(2026, 11, 13)
TEST_BC = "123456"


@pytest.mark.parametrize("yield_quota", [False, True])
def test_two_page_adgroup_worker_and_query(
    committed_sync_seed, monkeypatch, redis_client, policy, yield_quota
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

    deferred = set()
    extend_campaign = False
    snapshot_refreshed = False

    def transport(_pool, _method, url, **kwargs):
        args = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        args.update(dict(kwargs.get("fields") or []))
        calls.append(args)
        page = int(args["page"])
        # 真实 SDK 发送期间，另一 PG 会话可取得任务行与账户发布锁。
        from sqlalchemy import text

        from app.modules.reporting.store_models import ReportSyncJob

        with factory() as probe:
            probe.exec(
                select(ReportSyncJob)
                .where(ReportSyncJob.tenant_id == seed.tenant_id)
                .with_for_update(nowait=True)
            ).all()
            for kind in ("campaign_day", "adgroup_day", "ad_day"):
                assert (
                    probe.connection()
                    .execute(
                        text(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(:scope,0))"
                        ),
                        {"scope": f"report:{seed.tenant_id}:acc-1:{kind}"},
                    )
                    .scalar_one()
                )
        level = {
            "AUCTION_CAMPAIGN": "campaign",
            "AUCTION_ADGROUP": "adgroup",
            "AUCTION_AD": "ad",
        }[args["data_level"]]
        # 真 SDK transport 边界让出；第一页 checkpoint 必须由原 Worker 续用。
        if yield_quota and page == 2 and level not in deferred:
            from app.integrations.tiktok.sdk import AccountAdmissionDeferred

            deferred.add(level)
            raise AccountAdmissionDeferred(1000)
        dimension = "ad_id_v2" if level == "ad" else f"{level}_id"
        payload = {
            "list": [
                {
                    "dimensions": {
                        dimension: f"{level}{page}",
                        "stat_time_day": str(D),
                    },
                    "metrics": {
                        f"{level}_name": f"{level.title()} {1 if level == 'campaign' else page}",
                        "campaign_id": f"campaign{page}",
                        "adgroup_id": f"adgroup{page}",
                        "spend": ("5" if page == 1 else "45")
                        if level == "ad"
                        else "50",
                        "native_growth_total_ad_impression_value": "-"
                        if level == "ad" or (level == "campaign" and page == 2)
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
        if extend_campaign and level == "campaign":
            payload["page_info"]["total_number"] = 52
            payload["page_info"]["page_size"] = 51
            if page == 1:
                payload["list"].extend(
                    {
                        "dimensions": {
                            "campaign_id": f"campaign{i}",
                            "stat_time_day": str(D),
                        },
                        "metrics": {
                            "campaign_name": f"Paging {i}",
                            "spend": "1",
                            "native_growth_total_ad_impression_value": "-",
                            "native_growth_ad_revenue_value_d0": "-",
                        },
                    }
                    for i in range(3, 53)
                )
        if snapshot_refreshed and level == "campaign":
            payload["list"][0]["metrics"]["native_growth_total_ad_impression_value"] = (
                "80"
            )
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
        outcome = (
            celery_delivery(seed, factory, job, redis_client, calls)
            if job.kind == "adgroup_day" and not yield_quota
            else run_job_step(
                database_engine=engine,
                redis_client=redis_client,
                job_id=job.id,
                claim_token=job.claim_token,
            )
        )
        if yield_quota:
            assert outcome == "WAITING"
            with factory() as s:
                from app.modules.reporting.store_models import (
                    ReportSyncJob,
                    ReportSyncPage,
                )

                pending = s.get(ReportSyncJob, job.id)
                observed = pending.progress["basic_paging"]["observed_at"]
                assert (
                    len(
                        s.exec(
                            select(ReportSyncPage).where(
                                ReportSyncPage.job_id == job.id
                            )
                        ).all()
                    )
                    == 1
                )
                resumed = claim_jobs(
                    s, now=datetime.now(UTC) + timedelta(seconds=5), limit=4
                )
                retry = next(j for j in resumed if j.id == job.id)
                token = retry.claim_token
                s.commit()
            outcome = run_job_step(
                database_engine=engine,
                redis_client=redis_client,
                job_id=job.id,
                claim_token=token,
            )
            with factory() as s:
                from app.modules.reporting.store_models import ReportSyncState

                bucket = s.get(ReportSyncState, (seed.tenant_id, "acc-1", job.kind, D))
                assert (
                    bucket.observed_at.isoformat()
                    == datetime.fromisoformat(observed)
                    .astimezone(bucket.observed_at.tzinfo)
                    .isoformat()
                )
                assert not s.exec(
                    select(ReportSyncPage).where(ReportSyncPage.job_id == job.id)
                ).all()
        with factory() as check:
            from app.modules.reporting.store_models import ReportSyncJob

            state = check.get(ReportSyncJob, job.id)
            assert outcome == "COMPLETE", (outcome, state.error_code, state.progress)
            assert state.request_count == (3 if yield_quota else 2)
    assert len(calls) == (9 if yield_quota else 6)
    assert sorted(call["data_level"] for call in calls) == sorted(
        ["AUCTION_CAMPAIGN"] * (3 if yield_quota else 2)
        + ["AUCTION_ADGROUP"] * (3 if yield_quota else 2)
        + ["AUCTION_AD"] * (3 if yield_quota else 2)
    )
    for data_level in ("AUCTION_CAMPAIGN", "AUCTION_ADGROUP", "AUCTION_AD"):
        same_level = [c for c in calls if c["data_level"] == data_level]
        assert [int(c["page"]) for c in same_level] == (
            [1, 2, 2] if yield_quota else [1, 2]
        )
        assert all(
            {k: v for k, v in c.items() if k != "page"}
            == {k: v for k, v in same_level[0].items() if k != "page"}
            for c in same_level
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

    if not yield_quota:
        from app.core.errors import DomainError
        from app.modules.reporting.jobs import enqueue_job
        from app.modules.reporting.store_models import ReportSyncJob, ReportSyncState

        old_transport = transport
        fail = True

        def failing_transport(pool, method, url, **kwargs):
            args = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            args.update(dict(kwargs.get("fields") or []))
            if (
                fail
                and args.get("data_level") == "AUCTION_ADGROUP"
                and int(args["page"]) == 2
            ):
                raise DomainError(
                    "tiktok_response_error", "synthetic second page failure"
                )
            return old_transport(pool, method, url, **kwargs)

        monkeypatch.setattr("urllib3.PoolManager.request", failing_transport)
        with factory() as s:
            before = {
                j.kind: s.get(
                    ReportSyncState, (seed.tenant_id, "acc-1", j.kind, D)
                ).model_dump()
                for j in claimed
            }
            original = s.get(
                ReportSyncJob, next(j.id for j in claimed if j.kind == "adgroup_day")
            )
            retry_id = enqueue_job(
                s,
                tenant_id=seed.tenant_id,
                bc_id=TEST_BC,
                actor_id=seed.actor_id,
                frozen_route=original.frozen_route,
                kind="adgroup_day",
                tier="manual",
                advertiser_ids=["acc-1"],
                start=D,
                end=D,
            )
            s.commit()
            retry = next(
                j
                for j in claim_jobs(
                    s, now=datetime.now(UTC) + timedelta(seconds=2), limit=4
                )
                if j.id == retry_id
            )
            token = retry.claim_token
            s.commit()
        assert (
            run_job_step(
                database_engine=engine,
                redis_client=redis_client,
                job_id=retry_id,
                claim_token=token,
            )
            == "WAITING"
        )
        with factory() as s:
            stale = s.get(ReportSyncState, (seed.tenant_id, "acc-1", "adgroup_day", D))
            assert stale.status == "STALE"
            assert stale.error_code == "tiktok_response_error"
            stale_query = query_ads(
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
            assert stale_query.items[0].display["report_refresh_status"] == "PARTIAL"
            assert (
                "tiktok_response_error"
                in stale_query.items[0].display["report_layer_status"]
            )
            assert stale.observed_at == before["adgroup_day"]["observed_at"]
            assert s.exec(
                select(ParentReportDay).where(
                    ParentReportDay.tenant_id == seed.tenant_id,
                    ParentReportDay.level == "adgroup",
                )
            ).first().revenue_total == Decimal("40")
            for kind in ("campaign_day", "ad_day"):
                assert (
                    s.get(
                        ReportSyncState, (seed.tenant_id, "acc-1", kind, D)
                    ).model_dump()
                    == before[kind]
                )
            from tests.modules.reporting.test_native_campaign import (
                live_campaign_workspace,
            )

            # 同一SDK transport/Worker链路追加足够跨真实页面的事实，不造浏览器响应。
            extend_campaign = True
            expanded_id = enqueue_job(
                s,
                tenant_id=seed.tenant_id,
                bc_id=TEST_BC,
                actor_id=seed.actor_id,
                frozen_route=original.frozen_route,
                kind="campaign_day",
                tier="manual",
                advertiser_ids=["acc-1"],
                start=D,
                end=D,
            )
            s.commit()
            expanded = next(
                j
                for j in claim_jobs(
                    s, now=datetime.now(UTC) + timedelta(seconds=2), limit=4
                )
                if j.id == expanded_id
            )
            expanded_token = expanded.claim_token
            s.commit()
            assert (
                run_job_step(
                    database_engine=engine,
                    redis_client=redis_client,
                    job_id=expanded_id,
                    claim_token=expanded_token,
                )
                == "COMPLETE"
            )
            s.expire_all()
            expanded_state = s.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "campaign_day", D)
            )
            assert expanded_state.row_count == 52
            assert (
                expanded_state.publication_version
                == before["campaign_day"]["publication_version"] + 1
            )
            # 后续只恢复Adgroup，隔离断言对照刚刚有意发布的新Campaign水位。
            before["campaign_day"] = expanded_state.model_dump()
            live_campaign_workspace(
                seed,
                factory,
                level="adgroup",
                label="广告组",
                name="Adgroup 1",
                attempt_status="STALE",
                refresh_status="PARTIAL",
                scope_demo=True,
            )
            retry = next(
                j
                for j in claim_jobs(
                    s, now=datetime.now(UTC) + timedelta(seconds=65), limit=4
                )
                if j.id == retry_id
            )
            token = retry.claim_token
            s.commit()
        fail = False
        assert (
            run_job_step(
                database_engine=engine,
                redis_client=redis_client,
                job_id=retry_id,
                claim_token=token,
            )
            == "COMPLETE"
        )
        with factory() as s:
            for kind in ("campaign_day", "ad_day"):
                assert (
                    s.get(
                        ReportSyncState, (seed.tenant_id, "acc-1", kind, D)
                    ).model_dump()
                    == before[kind]
                )
            assert (
                s.get(
                    ReportSyncState, (seed.tenant_id, "acc-1", "adgroup_day", D)
                ).publication_version
                == before["adgroup_day"]["publication_version"] + 1
            )

    with factory() as check:
        from app.modules.reporting.store_models import ReportSyncJob

        assert not check.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind.in_(["campaign_day", "adgroup_day", "ad_day"]),
                ReportSyncJob.status.in_(["QUEUED", "WAITING", "RUNNING"]),
            )
        ).all()
    from tests.modules.reporting.test_native_campaign import live_campaign_workspace

    def snapshot_refresh():
        nonlocal snapshot_refreshed
        from app.modules.reporting.jobs import enqueue_job
        from app.modules.reporting.store_models import ReportSyncState
        from tests.modules.reporting.test_jobs import _route

        with factory() as s:
            before = {
                kind: s.get(
                    ReportSyncState, (seed.tenant_id, "acc-1", kind, D)
                ).publication_version
                for kind in ("campaign_day", "adgroup_day", "ad_day")
            }
        snapshot_refreshed = True
        with factory() as s:
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
                requested_at=datetime.now(UTC),
            )
            s.commit()
            jobs = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=2), limit=4)
            s.commit()
        assert jobs
        for job in jobs:
            assert (
                run_job_step(
                    database_engine=engine,
                    redis_client=redis_client,
                    job_id=job.id,
                    claim_token=job.claim_token,
                )
                == "COMPLETE"
            )

        with factory() as s:
            after = {
                kind: s.get(
                    ReportSyncState, (seed.tenant_id, "acc-1", kind, D)
                ).publication_version
                for kind in ("campaign_day", "adgroup_day", "ad_day")
            }
        assert after["campaign_day"] > before["campaign_day"]
        assert after["adgroup_day"] == before["adgroup_day"]
        assert after["ad_day"] == before["ad_day"]
        return {"before": before, "after": after}

    live_campaign_workspace(
        seed,
        factory,
        level="adgroup",
        label="广告组",
        name="Adgroup 1",
        snapshot_refresh=snapshot_refresh if not yield_quota else None,
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


def celery_delivery(seed, factory, job, redis_client, calls):
    """Two independent Celery processes consume canonical dispatch on a private queue."""
    import multiprocessing
    import os
    from uuid import uuid4

    from kombu import Queue
    from sqlmodel import select

    from app.jobs.celery_app import celery_app
    from app.jobs.models import PendingDispatch
    from app.modules.sync_control.dispatch_envelope import dispatch_envelope

    queue = f"jb27-{uuid4().hex}"
    with factory() as s:
        record = next(
            r
            for r in s.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id
                )
            ).all()
            if r.payload.get("job_id") == str(job.id)
            and r.payload.get("claim_id") == str(job.claim_token)
        )
        kwargs = {
            "tenant_id": str(seed.tenant_id),
            "actor_id": str(seed.actor_id),
            "payload": record.payload,
            "dispatch": dispatch_envelope(record),
        }
    ctx = multiprocessing.get_context("fork")

    def worker(pipe, index):
        import urllib3
        from celery.signals import task_postrun, worker_ready

        from app.core.config import settings
        from app.core.db import engine

        engine.dispose(close=False)
        settings.REDIS_URL = os.environ["TEST_REDIS_URL"]
        celery_app.conf.update(
            broker_url=settings.REDIS_URL,
            task_queues=(*old_queues, Queue(queue, exchange=queue, routing_key=queue)),
            imports=(),
        )
        original = urllib3.PoolManager.request

        def transport(*args, **kw):
            before = len(calls)
            try:
                return original(*args, **kw)
            finally:
                for call in calls[before:]:
                    pipe.send(("read", call))

        urllib3.PoolManager.request = transport

        @worker_ready.connect(weak=False)
        def ready(**_kw):
            pipe.send(("ready", os.getpid()))

        @task_postrun.connect(weak=False)
        def done(**kw):
            pipe.send(("done", str(kw.get("retval"))))

        with open(os.devnull, "w") as sink:
            import contextlib

            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                celery_app.worker_main(
                    [
                        "worker",
                        "--pool=solo",
                        "--concurrency=1",
                        "-Q",
                        queue,
                        "--hostname",
                        f"jb27-{index}@localhost",
                        "--without-gossip",
                        "--without-mingle",
                        "--without-heartbeat",
                        "--loglevel=ERROR",
                    ]
                )

    processes, pipes = [], []
    old_broker = celery_app.conf.broker_url
    old_queues = celery_app.conf.task_queues
    try:
        for i in range(2):
            parent, child = ctx.Pipe()
            process = ctx.Process(target=worker, args=(child, i))
            process.start()
            processes.append(process)
            pipes.append(parent)
        for pipe in pipes:
            assert pipe.poll(15), "Celery worker did not become ready"
            assert pipe.recv()[0] == "ready"
        celery_app.conf.update(
            broker_url=os.environ["TEST_REDIS_URL"],
            task_queues=(*old_queues, Queue(queue, exchange=queue, routing_key=queue)),
        )
        # Duplicate broker delivery exercises the canonical consumer guard as well.
        for _ in range(2):
            celery_app.send_task("reporting.job_step", kwargs=kwargs, queue=queue)
        from multiprocessing.connection import wait

        done_count = 0
        while done_count < 2:
            ready = wait(pipes, timeout=20)
            assert ready, "Celery publication timed out"
            for pipe in ready:
                event, value = pipe.recv()
                if event == "read":
                    calls.append(value)
                if event == "done":
                    done_count += 1
        from app.modules.reporting.store_models import ReportSyncJob

        with factory() as s:
            assert s.get(ReportSyncJob, job.id).status == "COMPLETE"
    finally:
        celery_app.conf.update(broker_url=old_broker, task_queues=old_queues)
        for process in processes:
            process.terminate()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(5)
        for pipe in pipes:
            pipe.close()
        redis_client.delete(queue, f"_kombu.binding.{queue}")
    return "COMPLETE"
