"""R1 的三个 P2：只读详情与字段证据不能冒认平台返回状态。"""

from datetime import UTC, datetime
from uuid import uuid4

from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ads.models import AdObject
from app.modules.reporting.detail import get_ad_detail
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import AdReportDay
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_native_scope_roas import D, publish, query


def test_creative_detail_accepts_authorized_snapshot(session, sync_seed):
    publish(session, sync_seed, ("10",), ("20",))
    page = query(session, sync_seed)
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="creative",
            remote_id="creative-1",
            name="Creative",
            ad_type="NATIVE_SERIES",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.commit()
    result = get_ad_detail(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        ref=EntityRef(sync_seed.tenant_id, "acc-1", "creative", "creative-1"),
        snapshot_id=page.snapshot.snapshot_id,
    )
    assert result.report_snapshot.snapshot_id == page.snapshot.snapshot_id
    assert result.report_result is None


def test_legacy_null_evidence_remains_unknown(session, sync_seed):
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="5", total="-", d0="-")],
    )
    fact = session.get(AdReportDay, (sync_seed.tenant_id, "acc-1", D, "a1"))
    fact.evidence = None
    session.add(fact)
    session.commit()
    page = query(session, sync_seed, "ad")
    assert page.items[0].metric_buckets[0].provenance[
        "revenue_total"
    ].field_state_counts == {"LEGACY_UNKNOWN": 1}


def test_unrequested_ratio_is_distinct_from_missing_row(session, sync_seed):
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="5", total="-", d0="-")],
    )
    row = query(session, sync_seed, "ad").items[0]
    assert row.display["platform_roas_total"] is None
    assert row.display["platform_roas_total_reason"] == "NOT_REQUESTED"
