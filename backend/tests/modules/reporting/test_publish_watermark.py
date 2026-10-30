"""真实 PostgreSQL 验证晚到回刷不会删除或降低较新发布的事实。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.reporting.publish import (
    AccountMeta,
    mark_failed,
    publish_account_reconciliation,
    publish_ad_day,
    publish_campaign_hour,
    publish_material_day,
)
from app.modules.reporting.store_models import (
    AccountReconciliationDay,
    AdReportDay,
    CampaignReportDay,
    CampaignReportHour,
    MaterialReportDay,
    ReportSyncState,
)
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)
HK = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
OLD = datetime(2026, 1, 1, tzinfo=UTC)


def args(tenant_id, **overrides):
    return {
        "tenant_id": tenant_id,
        "job_id": uuid4(),
        "accounts": HK,
        "start": D,
        "end": D,
    } | overrides


def state_values(session, tenant, kind="ad_day", adv="acc-1", day=D):
    state = session.get(ReportSyncState, (tenant, adv, kind, day))
    return (
        state.status,
        state.error_code,
        state.row_count,
        state.last_success_at,
        state.updated_at,
    )


@pytest.mark.parametrize("rows", [[], [ad_row("a1", D, spend="0.10")]])
def test_old_ad_publish_preserves_facts_rollups_state_and_reconciliation(
    session, sync_seed, rows
):
    tenant = sync_seed.tenant_id
    publish_ad_day(session, **args(tenant), rows=[ad_row("a1", D, spend="20")])
    publish_account_reconciliation(
        session, tenant_id=tenant, accounts=HK, start=D, end=D, rows=[]
    )
    state = state_values(session, tenant)
    before = session.get(CampaignReportDay, (tenant, "acc-1", D, "c1")).model_dump()
    reconciliation = session.get(
        AccountReconciliationDay, (tenant, "acc-1", D)
    ).model_dump()
    assert publish_ad_day(session, **args(tenant), rows=rows, requested_at=OLD) == 0
    session.expire_all()
    assert session.get(AdReportDay, (tenant, "acc-1", D, "a1")).spend == Decimal("20")
    assert (
        session.get(CampaignReportDay, (tenant, "acc-1", D, "c1")).model_dump()
        == before
    )
    assert (
        session.get(AccountReconciliationDay, (tenant, "acc-1", D)).model_dump()
        == reconciliation
    )
    assert state_values(session, tenant) == state


def test_old_multi_account_window_fills_only_unpublished_buckets(session, sync_seed):
    tenant, previous = sync_seed.tenant_id, D - timedelta(days=1)
    publish_ad_day(session, **args(tenant), rows=[ad_row("a1", D, spend="20")])
    before = state_values(session, tenant)
    accounts = HK | {"acc-2": AccountMeta("USD", "UTC")}
    assert (
        publish_ad_day(
            session,
            **args(tenant, start=previous, accounts=accounts),
            requested_at=OLD,
            rows=[
                ad_row("old", previous),
                ad_row("a1", D, spend="1"),
                ad_row("b1", D, adv="acc-2"),
            ],
        )
        == 2
    )
    session.expire_all()
    facts = session.exec(
        select(AdReportDay).where(AdReportDay.tenant_id == tenant)
    ).all()
    assert {(r.advertiser_id, r.stat_date, r.ad_id, r.spend) for r in facts} == {
        ("acc-1", D, "a1", Decimal("20")),
        ("acc-1", previous, "old", Decimal("1")),
        ("acc-2", D, "b1", Decimal("1")),
    }
    assert state_values(session, tenant) == before
    assert state_values(session, tenant, adv="acc-2", day=previous)[2] == 0


def test_new_request_can_accept_platform_downward_correction(session, sync_seed):
    tenant = sync_seed.tenant_id
    publish_ad_day(session, **args(tenant), rows=[ad_row("a1", D, spend="20")])
    requested_at = (
        session.connection().execute(text("SELECT clock_timestamp()")).scalar_one()
    )
    assert (
        publish_ad_day(
            session,
            **args(tenant),
            requested_at=requested_at,
            rows=[ad_row("a1", D, spend="2")],
        )
        == 1
    )
    session.expire_all()
    assert session.get(AdReportDay, (tenant, "acc-1", D, "a1")).spend == Decimal("2")


def test_old_request_cannot_undo_new_complete_empty_day(session, sync_seed):
    tenant = sync_seed.tenant_id
    publish_ad_day(session, **args(tenant), rows=[])
    before = state_values(session, tenant)
    assert (
        publish_ad_day(
            session, **args(tenant), rows=[ad_row("a1", D)], requested_at=OLD
        )
        == 0
    )
    assert state_values(session, tenant) == before
    assert (
        session.exec(select(AdReportDay).where(AdReportDay.tenant_id == tenant)).all()
        == []
    )


def test_campaign_shards_do_not_claim_whole_day_or_block_siblings(session, sync_seed):
    tenant, job = sync_seed.tenant_id, uuid4()
    for campaign in ("c1", "c2"):
        assert (
            publish_ad_day(
                session,
                **args(tenant, job_id=job),
                requested_at=OLD,
                campaign_ids=(campaign,),
                rows=[ad_row(campaign, D, campaign=campaign)],
            )
            == 1
        )
    assert session.get(ReportSyncState, (tenant, "acc-1", "ad_day", D)) is None
    publish_ad_day(session, **args(tenant), rows=[ad_row("new", D, spend="20")])
    before = state_values(session, tenant)
    assert (
        publish_ad_day(
            session, **args(tenant), requested_at=OLD, campaign_ids=("c1",), rows=[]
        )
        == 0
    )
    assert state_values(session, tenant) == before
    assert session.get(AdReportDay, (tenant, "acc-1", D, "new")) is not None


@pytest.mark.parametrize(
    "row,campaign_ids",
    [
        (ad_row("a", D, adv="outside"), ()),
        (ad_row("a", D + timedelta(days=1)), ()),
        (ad_row("a", D, campaign="outside"), ("c1",)),
    ],
)
def test_stale_ad_request_still_rejects_out_of_scope_rows(
    session, sync_seed, row, campaign_ids
):
    tenant = sync_seed.tenant_id
    publish_ad_day(session, **args(tenant), rows=[])
    with pytest.raises(DomainError):
        publish_ad_day(
            session,
            **args(tenant),
            requested_at=OLD,
            rows=[row],
            campaign_ids=campaign_ids,
        )


def material_row(day=D, spend="20", adv="acc-1"):
    return {
        "dimensions": {
            "stat_time_day": str(day),
            "main_material_id": "m1",
            "main_material_type": "VIDEO",
        },
        "metrics": {"spend": spend, "advertiser_id": adv},
    }


@pytest.mark.parametrize("rows", [[], [material_row(spend="1")]])
def test_material_watermark_keeps_new_facts_and_state(session, sync_seed, rows):
    tenant = sync_seed.tenant_id
    publish_material_day(session, **args(tenant), rows=[material_row()])
    before = state_values(session, tenant, "material_day")
    assert (
        publish_material_day(session, **args(tenant), requested_at=OLD, rows=rows) == 0
    )
    session.expire_all()
    assert session.get(MaterialReportDay, (tenant, "acc-1", D, "m1")).spend == Decimal(
        "20"
    )
    assert state_values(session, tenant, "material_day") == before
    assert (
        publish_material_day(session, **args(tenant), rows=[material_row(spend="2")])
        == 1
    )
    session.expire_all()
    assert session.get(MaterialReportDay, (tenant, "acc-1", D, "m1")).spend == Decimal(
        "2"
    )


@pytest.mark.parametrize(
    "rows", [[], [{"dimensions": {"stat_time_day": str(D)}, "metrics": {"spend": "1"}}]]
)
def test_account_watermark_keeps_amount_timestamp_and_state(session, sync_seed, rows):
    tenant = sync_seed.tenant_id
    kwargs = {"tenant_id": tenant, "accounts": HK, "start": D, "end": D}
    publish_ad_day(session, **args(tenant), rows=[ad_row("a", D, spend="19")])
    assert (
        publish_account_reconciliation(
            session,
            **kwargs,
            rows=[
                {"dimensions": {"stat_time_day": str(D)}, "metrics": {"spend": "20"}}
            ],
        )
        == 1
    )
    before = session.get(AccountReconciliationDay, (tenant, "acc-1", D)).model_dump()
    state = state_values(session, tenant, "account_day")
    assert (
        publish_account_reconciliation(session, **kwargs, requested_at=OLD, rows=rows)
        == 0
    )
    session.expire_all()
    assert (
        session.get(AccountReconciliationDay, (tenant, "acc-1", D)).model_dump()
        == before
    )
    assert state_values(session, tenant, "account_day") == state


def test_hourly_old_empty_response_preserves_each_accounts_local_day(
    session, sync_seed
):
    tenant = sync_seed.tenant_id
    kwargs = {"tenant_id": tenant, "job_id": uuid4(), "accounts": HK, "day": D}
    row = {
        "dimensions": {"stat_time_hour": f"{D} 10:00:00", "campaign_id": "c1"},
        "metrics": {"spend": "20"},
    }
    publish_campaign_hour(session, **kwargs, rows=[row])
    state = state_values(session, tenant, "campaign_hour")
    assert publish_campaign_hour(session, **kwargs, rows=[], requested_at=OLD) == 0
    assert state_values(session, tenant, "campaign_hour") == state
    assert session.get(
        CampaignReportHour,
        (tenant, "acc-1", datetime(2026, 10, 5, 2, tzinfo=UTC), "c1"),
    ).spend == Decimal("20")


@pytest.mark.parametrize(
    "publisher,row",
    [
        (publish_material_day, material_row(D + timedelta(days=1))),
        (
            publish_account_reconciliation,
            {"dimensions": {"stat_time_day": "2026-10-06"}},
        ),
    ],
)
def test_stale_simple_publish_still_validates_range(session, sync_seed, publisher, row):
    kwargs = args(sync_seed.tenant_id)
    if publisher is publish_account_reconciliation:
        kwargs.pop("job_id")
    publisher(session, **kwargs, rows=[])
    with pytest.raises(DomainError):
        publisher(session, **kwargs, rows=[row], requested_at=OLD)


def test_concurrent_old_publish_waits_then_preserves_new_commit(committed_sync_seed):
    seed, factory = committed_sync_seed
    waiting = Event()
    with factory() as newest:
        publish_ad_day(
            newest, **args(seed.tenant_id), rows=[ad_row("a1", D, spend="20")]
        )

        def old_publish():
            with factory() as older:
                waiting.set()
                result = publish_ad_day(
                    older, **args(seed.tenant_id), requested_at=OLD, rows=[]
                )
                older.commit()
                return result

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_publish)
            assert waiting.wait(5)
            newest.commit()
            assert future.result(timeout=10) == 0
    with factory() as check:
        assert check.get(
            AdReportDay, (seed.tenant_id, "acc-1", D, "a1")
        ).spend == Decimal("20")


def test_old_failure_preserves_new_success_but_marks_missing_day_failed(
    session, sync_seed
):
    tenant, previous = sync_seed.tenant_id, D - timedelta(days=1)
    publish_ad_day(session, **args(tenant), rows=[ad_row("a1", D)])
    state = state_values(session, tenant)
    mark_failed(
        session,
        tenant_id=tenant,
        advertiser_ids=["acc-1"],
        kind="ad_day",
        start=previous,
        end=D,
        error_code="report_timeout",
        requested_at=OLD,
    )
    assert state_values(session, tenant) == state
    assert state_values(session, tenant, day=previous)[:3] == (
        "FAILED",
        "report_timeout",
        0,
    )


@pytest.mark.parametrize(
    "publisher,model,kind",
    [
        (publish_material_day, MaterialReportDay, "material_day"),
        (publish_account_reconciliation, AccountReconciliationDay, "account_day"),
    ],
)
def test_simple_old_window_fills_missing_dates_and_accounts_only(
    session, sync_seed, publisher, model, kind
):
    tenant, previous = sync_seed.tenant_id, D - timedelta(days=1)
    kwargs = args(tenant)
    if publisher is publish_account_reconciliation:
        kwargs.pop("job_id")
    publisher(session, **kwargs, rows=[material_row()])
    before = state_values(session, tenant, kind)
    assert publisher(
        session,
        **(
            kwargs
            | {"start": previous, "accounts": HK | {"acc-2": AccountMeta("USD", "UTC")}}
        ),
        requested_at=OLD,
        rows=[
            material_row(previous),
            material_row(spend="1"),
            material_row(adv="acc-2"),
        ],
    ) == (3 if publisher is publish_account_reconciliation else 2)
    session.expire_all()
    key = (
        (tenant, "acc-1", D)
        if publisher is publish_account_reconciliation
        else (tenant, "acc-1", D, "m1")
    )
    row = session.get(model, key)
    assert (
        row.account_spend if publisher is publish_account_reconciliation else row.spend
    ) == Decimal("20")
    assert state_values(session, tenant, kind) == before
    assert state_values(session, tenant, kind, day=previous)[0] == "COMPLETE"
    assert state_values(session, tenant, kind, adv="acc-2")[0] == "COMPLETE"
    assert publisher(session, **kwargs, rows=[material_row(spend="2")]) == 1
    session.expire_all()
    row = session.get(model, key)
    assert (
        row.account_spend if publisher is publish_account_reconciliation else row.spend
    ) == Decimal("2")


@pytest.mark.parametrize(
    "publisher,kind",
    [
        (publish_ad_day, "ad_day"),
        (publish_material_day, "material_day"),
        (publish_account_reconciliation, "account_day"),
    ],
)
def test_later_observation_replaces_earlier_snapshot_even_when_both_finish_late(
    session, sync_seed, publisher, kind
):
    """两次读取均早于首次提交；按观测先后而非完成时间选择新快照。"""
    tenant = sync_seed.tenant_id
    kwargs = args(tenant)
    if kind == "account_day":
        kwargs.pop("job_id")
    old_rows = (
        [ad_row("a1", D, spend="20")]
        if kind == "ad_day"
        else [material_row(spend="20")]
    )
    new_rows = (
        [ad_row("a1", D, spend="2")] if kind == "ad_day" else [material_row(spend="2")]
    )
    assert publisher(session, **kwargs, rows=old_rows, requested_at=OLD) == 1
    assert (
        publisher(
            session, **kwargs, rows=new_rows, requested_at=OLD + timedelta(seconds=1)
        )
        == 1
    )
    assert publisher(session, **kwargs, rows=old_rows, requested_at=OLD) == 0
