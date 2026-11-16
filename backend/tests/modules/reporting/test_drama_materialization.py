"""剧行只创建首成员展示对象，指标仍与逐成员完整补全结果一致。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlmodel import select

from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.reporting import dimension_query as dq
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.row_set import SnapshotMember, entity_ref
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_dimension_query import D, _seed


def test_drama_first_display_and_full_member_metrics_are_identical(
    session, sync_seed, monkeypatch
):
    _seed(session, sync_seed)
    day2 = D + timedelta(days=1)
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-2": AccountMeta("EUR", "UTC")},
        start=day2,
        end=day2,
        rows=[
            ad_row("b1", day2, campaign="c9", spend="3", d0="1", total="2", adv="acc-2")
        ],
    )
    specs = [
        ("acc-1", "c1", "DISABLE", "首版权方", 3),
        ("acc-2", "c9", "ENABLE", "第二版权方", 5),
        ("acc-2", "empty", "DELETE", "第二版权方", 7),
        ("acc-1", "missing", None, "首版权方", None),
    ]
    refs = []
    for adv, campaign, status, provider, version in specs:
        if version is not None:
            session.add(
                AdObject(
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id=adv,
                    kind="campaign",
                    remote_id=campaign,
                    name=f"目录-{campaign}",
                    ad_type="SMART_PLUS",
                    operation_status=status,
                    observed_at=datetime.now(UTC),
                    published_version=version,
                )
            )
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=campaign,
                raw_name="同剧",
                provider_label=provider,
                drama_name="共同剧",
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
                name_revision=1,
            )
        )
        refs.append(
            {
                "tenant_id": str(sync_seed.tenant_id),
                "advertiser_id": adv,
                "kind": "campaign",
                "remote_id": campaign,
            }
        )
    session.flush()
    filters = ReportingFilter(
        dimension="drama", start_date=D, end_date=day2, include_empty=True
    )
    key = '["drama", "共同剧"]'
    # 每次只补全一个成员，作为优化前“每成员完整ReportRow再合并”的独立基准。
    singles = [
        dq.materialize_rows(
            session,
            tenant_id=sync_seed.tenant_id,
            filters=filters,
            members=[SnapshotMember(key, (ref,), ())],
            advertisers=["acc-1", "acc-2"],
        )[0]
        for ref in refs
    ]
    states = session.exec(
        select(ReportSyncState).where(ReportSyncState.tenant_id == sync_seed.tenant_id)
    ).all()
    coverage = dq._coverage(states, advertisers={"acc-1", "acc-2"}, filters=filters)
    expected = singles[0].model_copy(
        update={
            "refs": tuple(entity_ref(ref) for ref in refs),
            "membership_digest": dq.ref_digest(tuple(entity_ref(ref) for ref in refs)),
            "metric_buckets": dq.merge_metric_buckets(
                [
                    (
                        row.metric_buckets,
                        row.display["advertiser_id"]
                        in coverage["incomplete_advertiser_ids"],
                    )
                    for row in singles
                ],
                strict_denominators=True,
            ),
            "directory_versions": {
                k: v for row in singles for k, v in row.directory_versions.items()
            },
            "coverage": coverage,
            "display": dq._drama_status_display(
                singles[0].display,
                [spec[2] for spec in specs],
                [spec[3] for spec in specs],
            ),
        }
    )
    calls = []
    original = dq._member_row

    def counted(**kwargs):
        calls.append((kwargs["adv"], kwargs["rid"]))
        return original(**kwargs)

    monkeypatch.setattr(dq, "_member_row", counted)
    actual = dq.materialize_rows(
        session,
        tenant_id=sync_seed.tenant_id,
        filters=filters,
        members=[SnapshotMember(key, tuple(refs), ())],
        advertisers=["acc-1", "acc-2"],
    )[0]
    # 聚合后的报告解释应来自所有成员，不能复制首成员的已解释provenance/display。
    assert actual.model_dump(
        exclude={"display", "metric_buckets", "source_evidence"}
    ) == expected.model_dump(exclude={"display", "metric_buckets", "source_evidence"})
    expected_buckets = {dq.bucket_key(v): v for v in expected.metric_buckets}
    assert {dq.bucket_key(v) for v in actual.metric_buckets} == set(expected_buckets)
    for actual_bucket in actual.metric_buckets:
        expected_bucket = expected_buckets[dq.bucket_key(actual_bucket)]
        assert actual_bucket.model_dump(
            exclude={"provenance"}
        ) == expected_bucket.model_dump(exclude={"provenance"})
        selected = [
            o
            for row in singles
            for vector in row.metric_buckets
            if dq.bucket_key(vector) == dq.bucket_key(actual_bucket)
            for o in row.source_evidence
            if (o["currency"], o["level"], o["source_kind"], o["contract_revision"])
            == dq.bucket_key(vector)
        ]
        import json

        assert sorted(
            json.dumps(o, sort_keys=True)
            for o in actual.source_evidence
            if (o["currency"], o["level"], o["source_kind"], o["contract_revision"])
            == dq.bucket_key(actual_bucket)
        ) == sorted(json.dumps(o, sort_keys=True) for o in selected)
    base_display = {
        k: v
        for k, v in expected.display.items()
        if not k.startswith(("report_", "platform_roas_"))
    }
    assert {k: actual.display[k] for k in base_display} == base_display
    assert actual.display["platform_roas_total_reason"] == "NON_ADDITIVE"
    assert actual.display["remote_id"] == "c1"
    assert actual.display["name"] == "共同剧"
    assert actual.display["provider"] == "首版权方、第二版权方"
    assert actual.display["operation_status"] == "ENABLE"
    assert actual.directory_versions == {"acc-1:c1": 3, "acc-2:c9": 5, "acc-2:empty": 7}
    buckets = {
        v.currency: v
        for v in actual.metric_buckets
        if v.contract_revision == "native-growth-day-v1"
    }
    assert buckets["USD"].values["spend"] == Decimal(17)
    assert buckets["USD"].values["revenue_d0"] == Decimal(6)
    assert buckets["USD"].timezone == "MIXED"
    assert buckets["USD"].availability["spend"] == "PARTIAL"
    assert buckets["EUR"].values["spend"] == Decimal(3)
    assert buckets["EUR"].availability["spend"] == "AVAILABLE"
    assert coverage["incomplete_advertiser_ids"] == ["acc-1"]
    assert calls == [("acc-1", "c1")]
