"""历史回补只覆盖当前拥有账户的既有广告总账日期，幂等成对排队。"""

from datetime import UTC, datetime

from sqlmodel import select

from app.modules.reporting import publish
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import _route


def test_backfill_pairs_sources_and_is_scoped_idempotent(session, sync_seed):
    from app.modules.reporting.creative_rebuild import enqueue_backfill
    from app.modules.reporting.planner import ensure_bc_plans

    now = datetime.now(UTC)
    day = now.date()
    publish.publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=sync_seed.actor_id,
        accounts={"acc-1": publish.AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[ad_row("ad1", day)],
        requested_at=now,
    )
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.frozen_route = _route(sync_seed)
    assert (
        enqueue_backfill(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            start=day,
            end=day,
            now=now,
        )
        == 2
    )
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {j.kind for j in jobs} == {
        "campaign_day",
        "adgroup_day",
        "ad_day",
        "creative_day",
    }
    assert all(j.advertiser_ids == ["acc-1"] for j in jobs)
    assert (
        enqueue_backfill(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            start=day,
            end=day,
            now=now,
        )
        == 0
    )


def _backfill_history(session, seed):
    from datetime import timedelta

    from app.modules.reporting.planner import ensure_bc_plans

    now = datetime.now(UTC)
    end = now.date() - timedelta(days=5)
    start = end - timedelta(days=20)
    publish.publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=seed.actor_id,
        accounts={"acc-1": publish.AccountMeta("USD", "UTC")},
        start=start,
        end=end,
        rows=[ad_row("ad1", start + timedelta(days=i)) for i in range(21)],
        requested_at=now,
    )
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.frozen_route = _route(seed)
    return now, start, end, plan


def test_backfill_adjacent_chunks_stay_bounded_across_enqueue_rounds(
    session, sync_seed
):
    from datetime import timedelta

    from app.modules.reporting.creative_rebuild import enqueue_backfill

    now, start, end, _ = _backfill_history(session, sync_seed)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "start": start,
        "end": end,
    }
    assert enqueue_backfill(session, **args, now=now) == 6
    assert enqueue_backfill(session, **args, now=now + timedelta(seconds=1)) == 0
    session.flush()
    session.expire_all()
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(jobs) == 12
    for kind in ("campaign_day", "adgroup_day", "ad_day", "creative_day"):
        chunks = sorted((j for j in jobs if j.kind == kind), key=lambda j: j.start_date)
        assert [(j.start_date, j.end_date) for j in chunks] == [
            (start + timedelta(days=i), start + timedelta(days=i + 6))
            for i in (0, 7, 14)
        ]
        assert all(j.progress["execution_window_days"] == 7 for j in chunks)


def test_backfill_reuses_existing_broad_jobs_without_rewriting_their_contract(
    session, sync_seed
):
    from app.modules.reporting.creative_rebuild import enqueue_backfill
    from app.modules.reporting.jobs import enqueue_job

    now, start, end, plan = _backfill_history(session, sync_seed)
    for kind in ("ad_day", "creative_day"):
        assert enqueue_job(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            kind=kind,
            tier="history",
            advertiser_ids=["acc-1"],
            start=start,
            end=end,
            frozen_route=plan.frozen_route,
            actor_id=plan.actor_id,
            requested_at=now,
        )
    assert (
        enqueue_backfill(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            start=start,
            end=end,
            now=now,
        )
        == 0
    )
    session.flush()
    session.expire_all()
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(jobs) == 4
    assert {j.kind for j in jobs} == {
        "campaign_day",
        "adgroup_day",
        "ad_day",
        "creative_day",
    }
    assert all((j.start_date, j.end_date) == (start, end) for j in jobs)
    assert all("execution_window_days" not in j.progress for j in jobs)


def _audit_fact(
    session,
    seed,
    key,
    *,
    currency="USD",
    timezone="UTC",
    spend="10",
    revenue="5",
    source_status="COMPLETE",
    has_null=False,
    day=None,
    advertiser_id="acc-1",
):
    from decimal import Decimal

    from app.modules.reporting.read_models.models import CreativeMaterialEntityDay
    from app.modules.reporting.store_models import AdReportDay, CreativeReportDay

    common = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": advertiser_id,
        "stat_date": day or datetime.now(UTC).date(),
        "currency": currency,
        "timezone": timezone,
        "fetched_at": datetime.now(UTC),
        "campaign_id": "campaign",
        "adgroup_id": key,
        "spend": Decimal(spend) if spend is not None else None,
        "revenue_total": Decimal(revenue) if revenue is not None else None,
    }
    session.add(AdReportDay(**common, ad_id=key, sync_job_id=seed.actor_id))
    session.add(CreativeReportDay(**common, creative_id=key, sync_job_id=seed.actor_id))
    for status in ("UNMAPPED", "RESIDUAL"):
        session.add(
            CreativeMaterialEntityDay(
                **common,
                fact_key=f"{status}:{key}",
                row_key=f"{status}:{key}",
                source_revision=1,
                material_type="UNKNOWN",
                attribution_status=status,
                source_status=source_status,
                spend_has_null=has_null,
            )
        )
    session.flush()


def test_audit_separates_currency_timezone_and_strict_unknown_amounts(
    session, sync_seed
):
    from datetime import timedelta
    from decimal import Decimal

    from app.modules.reporting.creative_rebuild import audit
    from app.modules.reporting.planner import ensure_bc_plans

    day = datetime.now(UTC).date()
    ensure_bc_plans(session, now=datetime.now(UTC))
    _audit_fact(session, sync_seed, "known")
    _audit_fact(
        session, sync_seed, "unknown", spend=None, revenue=None, source_status="PARTIAL"
    )
    _audit_fact(session, sync_seed, "cny", currency="CNY", spend="70")
    _audit_fact(session, sync_seed, "hongkong", timezone="Asia/Hong_Kong", spend="20")
    # 投影已有已知子和但带 NULL 标志时，不能当成完整金额。
    _audit_fact(
        session,
        sync_seed,
        "flagged",
        timezone="Asia/Tokyo",
        spend="8",
        has_null=True,
        source_status="PARTIAL",
    )
    _audit_fact(session, sync_seed, "outside", spend="999", day=day - timedelta(days=1))
    result = audit(
        session, tenant_id=sync_seed.tenant_id, bc_id="bc-a", start=day, end=day
    )
    groups = {(r["currency"], r["timezone"]): r for r in result["groups"]}
    assert len(groups) == 4
    assert result["dirty"] == 1
    usd = groups["USD", "UTC"]
    for prefix in ("ad", "creative", "unmapped", "residual"):
        assert usd[f"{prefix}_rows"] == 2
        for metric, partial in (("spend", Decimal("10")), ("revenue", Decimal("5"))):
            assert usd[f"{prefix}_{metric}"] is None
            assert usd[f"{prefix}_{metric}_unknown_rows"] == 1
            assert usd[f"{prefix}_{metric}_partial"] == partial
    assert usd["partial_rows"] == 2 and usd["stale_rows"] == 0
    assert usd["dirty_buckets"] == 1
    assert usd["ad_buckets"] == usd["creative_buckets"] == 1
    assert groups["CNY", "UTC"]["ad_spend"] == Decimal("70")
    assert groups["USD", "Asia/Hong_Kong"]["ad_spend"] == Decimal("20")
    flagged = groups["USD", "Asia/Tokyo"]
    assert flagged["ad_spend"] == Decimal("8")
    assert flagged["residual_spend"] is None
    assert flagged["residual_spend_unknown_rows"] == 1
    assert flagged["residual_spend_partial"] == Decimal("8")


def test_audit_empty_scope_has_no_invented_zero_totals(session, sync_seed):
    from app.modules.reporting.creative_rebuild import audit
    from app.modules.reporting.planner import ensure_bc_plans

    now = datetime.now(UTC)
    ensure_bc_plans(session, now=now)
    assert audit(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        start=now.date(),
        end=now.date(),
    ) == {"groups": [], "dirty": 0}


def test_audit_distinguishes_zero_unknown_stale_and_missing_sources(session, sync_seed):
    from decimal import Decimal

    from sqlalchemy import text

    from app.modules.reporting.creative_rebuild import audit
    from app.modules.reporting.planner import ensure_bc_plans

    now = datetime.now(UTC)
    ensure_bc_plans(session, now=now)
    _audit_fact(session, sync_seed, "zero", spend="0", revenue="0")
    _audit_fact(
        session,
        sync_seed,
        "stale",
        currency="CNY",
        spend=None,
        revenue=None,
        source_status="STALE",
    )
    params = {"tenant": sync_seed.tenant_id}
    session.connection().execute(
        text("""
        DELETE FROM creative_report_day WHERE tenant_id=:tenant AND currency='USD'
    """),
        params,
    )
    result = audit(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        start=now.date(),
        end=now.date(),
    )
    groups = {row["currency"]: row for row in result["groups"]}
    usd = groups["USD"]
    assert usd["ad_spend"] == usd["residual_spend"] == Decimal("0")
    assert usd["ad_spend_unknown_rows"] == usd["residual_spend_unknown_rows"] == 0
    assert usd["creative_rows"] == usd["creative_buckets"] == 0
    assert usd["creative_spend"] is None and usd["creative_spend_partial"] is None
    cny = groups["CNY"]
    assert cny["stale_rows"] == 2 and cny["partial_rows"] == 0
    assert cny["residual_spend"] is None and cny["residual_spend_partial"] is None
    assert cny["residual_spend_unknown_rows"] == 1


def test_audit_keeps_dirty_empty_buckets_and_excludes_other_bc(session, sync_seed):
    from sqlalchemy import text

    from app.modules.reporting.creative_rebuild import audit
    from app.modules.reporting.planner import ensure_bc_plans
    from app.modules.reporting.store_models import AdSyncAccount

    now = datetime.now(UTC)
    ensure_bc_plans(session, now=now)
    ownership = session.get(AdSyncAccount, (sync_seed.tenant_id, "acc-2"))
    ownership.owner_bc_id = "bc-b"
    session.add(ownership)
    _audit_fact(session, sync_seed, "owned")
    _audit_fact(session, sync_seed, "other-bc", advertiser_id="acc-2", spend="999")
    for table in (
        "ad_report_day",
        "creative_report_day",
        "creative_material_entity_day",
    ):
        session.connection().execute(
            text(f"""
            DELETE FROM {table} WHERE tenant_id=:tenant AND advertiser_id='acc-1'
        """),
            {"tenant": sync_seed.tenant_id},
        )
    assert audit(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        start=now.date(),
        end=now.date(),
    ) == {"groups": [], "dirty": 1}
