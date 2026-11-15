"""JB-4: independent 50/NULL/0 expectations, real immutable PG evidence."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdObject
from app.modules.business.evidence import auto_is_current
from app.modules.operation_logs.evidence import build_evidence, frozen_sources
from app.modules.operation_logs.models import OperationCoverage, OperationExport
from app.modules.operation_logs.parser import parse_export, utc_window
from app.modules.operation_logs.store import publish_export
from tests.modules.operation_logs.test_daily_evidence import anchors
from tests.modules.operation_logs.test_parser import export

DAY = date(2026, 10, 1)
ANCHOR = datetime(2026, 9, 30, 12, tzinfo=UTC)


def saved_day(
    session,
    case,
    day,
    *,
    account="report-account",
    timezone="UTC",
    content=None,
    observed=None,
    tail_end=None,
):
    content = export() if content is None else content
    job = SimpleNamespace(
        id=uuid4(),
        tenant_id=case.context.tenant_id,
        bc_id=case.bc_id,
        advertiser_ids=[account],
        frozen_route={},
        start_date=day,
        end_date=day,
        progress={"operation_log": {"task_id": "synthetic-jb6"}},
    )
    lower, upper = utc_window(day, day, timezone)
    publish_export(
        session,
        job=job,
        content_digest=sha256(content.encode()).hexdigest(),
        receipt=SimpleNamespace(
            key="private/jb6-synthetic", size=len(content.encode())
        ),
        events=parse_export(content),
        start_at=lower,
        end_at=tail_end or upper,
        timezone=timezone,
        observed_at=observed or datetime(2026, 11, 12, tzinfo=UTC),
    )
    session.flush()
    return job.id


def freeze(session, case, day=DAY, accounts=None):
    return frozen_sources(
        session, case.context.tenant_id, accounts or ["report-account"], day, day
    )


def evidence(session, case, frozen, day=DAY, timezone="UTC", spend=100, events=300):
    return build_evidence(
        session,
        tenant_id=case.context.tenant_id,
        advertiser_id="report-account",
        campaign_id="campaign-test",
        day=day,
        timezone=timezone,
        frozen=frozen,
        spend=Decimal(spend),
        revenue=Decimal(60),
        events=Decimal(events),
    )


def test_prestart_independent_exports_restore_approved_fifty(session, report_case):
    anchors(session, report_case, ANCHOR)
    early = saved_day(session, report_case, date(2026, 9, 30))
    target = saved_day(session, report_case, DAY)
    frozen = freeze(session, report_case)
    actual = evidence(session, report_case, frozen)
    assert actual.data["amount"] == "50", actual.data
    assert actual.data["status"] == "ESTIMATED_PENDING"
    assert actual.data["bid"] == "1.2"
    assert frozen["exports"] == sorted([str(early), str(target)])
    assert set(frozen["days"]) == {
        "report-account/2026-09-30",
        "report-account/2026-10-01",
    }
    assert actual.data["source_start"] == actual.data["source_end"] == "2026-10-01"


@pytest.mark.parametrize(
    "day,anchor,zone,early,hours",
    [
        (
            date(2027, 1, 1),
            datetime(2026, 12, 31, 12, tzinfo=UTC),
            "UTC",
            date(2026, 12, 31),
            24,
        ),
        (
            DAY,
            datetime(2026, 10, 1, 2, tzinfo=UTC),
            "America/New_York",
            date(2026, 9, 30),
            24,
        ),
        (
            date(2026, 3, 9),
            datetime(2026, 3, 8, 12, tzinfo=UTC),
            "America/New_York",
            date(2026, 3, 8),
            23,
        ),
        (
            date(2026, 11, 2),
            datetime(2026, 11, 1, 12, tzinfo=UTC),
            "America/New_York",
            date(2026, 11, 1),
            25,
        ),
    ],
)
def test_local_boundaries_use_real_utc_footprint(
    session, report_case, day, anchor, zone, early, hours
):
    account = session.get(
        AdvertiserAccount, (report_case.context.tenant_id, "report-account")
    )
    account.timezone = zone
    session.add(account)
    anchors(session, report_case, anchor)
    early_id = saved_day(session, report_case, early, timezone=zone)
    saved_day(session, report_case, day, timezone=zone)
    # An unrelated older stored export must not become a historical backfill.
    saved_day(session, report_case, early - timedelta(days=1), timezone=zone)
    frozen = freeze(session, report_case, day)
    assert set(frozen["days"]) == {f"report-account/{early}", f"report-account/{day}"}
    stored = session.get(OperationExport, early_id)
    assert (stored.end_at - stored.start_at).total_seconds() == hours * 3600
    assert evidence(session, report_case, frozen, day, zone).data["amount"] == "50"


def test_account_ranges_signatures_and_later_selected_anchor(session, report_case):
    case = report_case
    anchors(session, case, ANCHOR)
    saved_day(session, case, date(2026, 9, 30))
    saved_day(session, case, DAY)
    saved_day(session, case, date(2026, 10, 3))
    session.add(
        AdvertiserAccount(
            tenant_id=case.context.tenant_id,
            advertiser_id="second",
            currency="USD",
            timezone="Asia/Tokyo",
        )
    )
    session.flush()
    for obj in session.exec(
        select(AdObject).where(AdObject.tenant_id == case.context.tenant_id)
    ).all():
        session.add(
            AdObject(
                **{
                    **obj.model_dump(),
                    "advertiser_id": "second",
                    "observed_at": datetime(2026, 10, 3, 12, tzinfo=UTC),
                }
            )
        )
    session.flush()
    for d in (date(2026, 9, 30), DAY, date(2026, 10, 2), date(2026, 10, 3)):
        saved_day(session, case, d, account="second", timezone="Asia/Tokyo")
    first, second = freeze(session, case), freeze(session, case, accounts=["second"])
    combined = freeze(session, case, accounts=["second", "report-account"])
    assert combined["days"] == first["days"] | second["days"]
    assert (
        combined["account_signatures"]
        == first["account_signatures"] | second["account_signatures"]
    )
    assert set(first["days"]) == {
        "report-account/2026-09-30",
        "report-account/2026-10-01",
    }
    assert set(second["days"]) == {
        "second/2026-10-01",
        "second/2026-10-02",
        "second/2026-10-03",
    }
    # Existing selection prefers the first post-window observation, not the old one.
    for obj in session.exec(
        select(AdObject).where(
            AdObject.tenant_id == case.context.tenant_id,
            AdObject.advertiser_id == "report-account",
        )
    ).all():
        obj.observed_at = datetime(2026, 10, 2, tzinfo=UTC)
        obj.published_version += 1
        session.add(obj)
    session.flush()
    later = freeze(session, case)
    assert set(later["days"]) == {"report-account/2026-10-01"}
    assert evidence(session, case, later).data["amount"] == "50"


def test_frozen_gap_stays_unknown_and_early_correction_invalidates_current(
    session, report_case
):
    case = report_case
    anchors(session, case, ANCHOR)
    saved_day(session, case, DAY)
    frozen = freeze(session, case)
    old = evidence(session, case, frozen)
    assert old.data["status"] == "UNKNOWN" and old.data["amount"] is None
    assert auto_is_current(session, old)
    saved_day(session, case, date(2026, 9, 30))
    assert not auto_is_current(session, old)
    repeated = evidence(session, case, frozen)
    assert (repeated.id, repeated.digest, repeated.data) == (
        old.id,
        old.digest,
        old.data,
    )
    fresh = freeze(session, case)
    assert fresh["signature"] != frozen["signature"]
    assert evidence(session, case, fresh).data["amount"] == "50"
    # Storage identity/revision churn retains provenance, not semantic revision.
    key = (case.context.tenant_id, "report-account", date(2026, 9, 30))
    previous = session.get(OperationCoverage, key).export_id
    new_id = saved_day(
        session, case, date(2026, 9, 30), observed=datetime(2026, 11, 12, 1, tzinfo=UTC)
    )
    coverage = session.get(OperationCoverage, key)
    coverage.revision += 1
    session.add(coverage)
    session.flush()
    same = freeze(session, case)
    assert new_id != previous
    assert same["signature"] == fresh["signature"]
    assert (
        same["days"]["report-account/2026-09-30"]["revision"]
        != fresh["days"]["report-account/2026-09-30"]["revision"]
    )
    # A real early event changes semantic content, even outside the business day.
    saved_day(
        session,
        case,
        date(2026, 9, 30),
        content=export(
            records=[
                (
                    "2026-09-30 13:00",
                    "Campaign",
                    "campaign-test",
                    [
                        {
                            "action": "Change",
                            "name": "Name",
                            "before_after": [{"before": "a", "after": "b"}],
                        }
                    ],
                )
            ]
        ),
        observed=datetime(2026, 11, 12, 2, tzinfo=UTC),
    )
    corrected = freeze(session, case)
    assert corrected["signature"] != fresh["signature"]
    assert corrected["account_signatures"] != fresh["account_signatures"]
    assert evidence(session, case, fresh).data["amount"] == "50"


def test_other_tenant_cannot_fill_gap_and_known_zero_survives(
    session, report_case, other_context
):
    anchors(session, report_case, ANCHOR)
    saved_day(session, report_case, DAY)
    before = freeze(session, report_case)
    other = SimpleNamespace(context=other_context, bc_id="bc-other")
    anchors(session, other, ANCHOR)
    for obj in session.exec(
        select(AdObject).where(
            AdObject.tenant_id == other_context.tenant_id, AdObject.kind == "adgroup"
        )
    ).all():
        obj.configuration = {**obj.configuration, "roas_bid": "9.9"}
        obj.published_version += 1
        session.add(obj)
    saved_day(session, other, date(2026, 9, 30))
    assert freeze(session, report_case) == before
    assert set(before["days"]) == {"report-account/2026-10-01"}
    assert evidence(session, report_case, before).data["amount"] is None
    assert evidence(session, report_case, before, spend=0).data["amount"] == "0"
    assert evidence(session, report_case, before, events=299).data["amount"] == "0"


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("anchor", "CONFIGURATION_ANCHOR_MISSING"),
        ("gap", "LOG_CHAIN_INCOMPLETE"),
        ("corrupt", "BID_VALUE_UNKNOWN"),
        ("ambiguous", "BID_CHAIN_AMBIGUOUS"),
    ],
)
def test_real_missing_or_ambiguous_evidence_remains_unknown(
    session, report_case, defect, reason
):
    if defect != "anchor":
        anchors(
            session,
            report_case,
            ANCHOR - timedelta(days=1) if defect == "gap" else ANCHOR,
        )
    saved_day(
        session,
        report_case,
        ANCHOR.date() - timedelta(days=1) if defect == "gap" else ANCHOR.date(),
    )
    records = []
    if defect in {"corrupt", "ambiguous"}:
        detail = {
            "action": "Change",
            "name": "minimum ROAS",
            "before_after": [
                {"before": "1.2", "after": "broken" if defect == "corrupt" else "1.2"}
            ],
        }
        records = [("2026-10-01 12:00", "Ad group", "group-test", [detail])] * (
            2 if defect == "ambiguous" else 1
        )
    saved_day(session, report_case, DAY, content=export(records=records))
    actual = evidence(session, report_case, freeze(session, report_case))
    assert actual.data["status"] == "UNKNOWN" and actual.data["amount"] is None
    assert reason in actual.data["reasons"]


def test_eastern_local_anchor_extends_right_boundary(session, report_case):
    zone = "Asia/Tokyo"
    account = session.get(
        AdvertiserAccount, (report_case.context.tenant_id, "report-account")
    )
    account.timezone = zone
    session.add(account)
    anchors(session, report_case, datetime(2026, 10, 1, 20, tzinfo=UTC))
    saved_day(session, report_case, DAY, timezone=zone)
    saved_day(session, report_case, date(2026, 10, 2), timezone=zone)
    frozen = freeze(session, report_case)
    assert set(frozen["days"]) == {
        "report-account/2026-10-01",
        "report-account/2026-10-02",
    }
    assert evidence(session, report_case, frozen, timezone=zone).data["amount"] == "50"
