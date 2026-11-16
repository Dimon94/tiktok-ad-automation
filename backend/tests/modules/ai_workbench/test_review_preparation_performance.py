"""真实 PostgreSQL：直接冻结与页面同口径，账户批次可续接且查询成本显著降低。"""

from time import perf_counter

from sqlalchemy import event

from app.modules.ai_workbench.review_data import build_data_pack
from app.modules.ai_workbench.review_preparation import ACCOUNT_BATCH_SIZE
from app.modules.reporting.queries import freeze_review_page
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.ai_workbench.test_review_maturity import CUTOFF, _publish, _version
from tests.modules.ai_workbench.test_review_scale import create_task, step


def _measured(session, call):
    counts = []

    def count(*_args):
        counts.append(1)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", count)
    start = perf_counter()
    try:
        result = call()
    finally:
        elapsed = perf_counter() - start
        event.remove(connection, "before_cursor_execute", count)
    return result, len(counts), elapsed


def _assert_financial_parity(actual, expected):
    for key in (
        "status",
        "filters",
        "publication_versions",
        "coverage",
        "metrics_observed_at",
        "total",
        "rows_truncated",
    ):
        assert actual[key] == expected[key], key
    assert actual["projection_contract"] == "financial-review-v2"
    assert len(actual["rows"]) == len(expected["rows"])
    for a, e in zip(actual["rows"], expected["rows"], strict=True):
        assert a["row_key"] == e["row_key"]

        # 分析范围不包含可执行 refs，scope_digest 不等于页面动作范围摘要；其余来源与金额完全相同。
        def financial(value):
            if isinstance(value, dict):
                return {
                    k: financial(v)
                    for k, v in value.items()
                    if k not in {"scope_digest", "evidence_ref"}
                }
            if isinstance(value, list):
                return [financial(v) for v in value]
            return value

        from tests.modules.reporting.evidence_helpers import archived_records

        assert financial(actual["summary"]) == financial(expected["summary"])
        for left, right in zip(
            actual["summary"]["buckets"], expected["summary"]["buckets"], strict=True
        ):
            assert archived_records(actual, left) == archived_records(expected, right)
        assert archived_records(actual, a["metric_buckets"][0]) == archived_records(
            expected, e["metric_buckets"][0]
        )
        assert financial(a["metric_buckets"]) == financial(e["metric_buckets"]), a[
            "row_key"
        ]
        assert a["display"]["name"] == e["display"]["name"]
        if e["coverage"].get("status") == "INCOMPLETE":
            assert a["coverage"]["status"] == "INCOMPLETE"
        assert a["coverage"]["action_evidence"] == "NOT_INCLUDED"
        assert not a.get("refs") and not a.get("material_uses")


def test_direct_freeze_parity_and_query_budget(session, context):
    version = _version(session, context)
    _publish(session, context)
    pack, direct_count, direct_seconds = _measured(
        session,
        lambda: build_data_pack(
            session, context=context, version=version, cutoff=CUTOFF
        ),
    )

    def baseline():
        return [
            freeze_review_page(
                session,
                context=context,
                bc_id=version.bc_id,
                filters=ReportingFilter.model_validate(report["filters"]),
                cutoff=CUTOFF,
                freeze_all=True,
            )
            for report in pack["reporting"]
        ]

    old, old_count, old_seconds = _measured(session, baseline)
    for actual, expected in zip(pack["reporting"], old, strict=True):
        _assert_financial_parity(actual, expected)
    print(  # noqa: T201
        f"REPORT_BENCH direct={direct_count} SQL/{direct_seconds:.3f}s baseline={old_count} SQL/{old_seconds:.3f}s"
    )
    assert direct_count < old_count * 0.60


def test_batches_checkpoint_and_ignore_old_dispatch(session, context):
    task = create_task(session, context, ACCOUNT_BATCH_SIZE + 1)
    step(session, task)
    old = step(session, task)
    assert task.status == "PREPARING"
    assert task.evidence["preparation"]["completed_accounts"] == ACCOUNT_BATCH_SIZE
    from app.modules.ai_workbench.review_preparation import prepare_step

    prepare_step(
        session, tenant_id=task.tenant_id, actor_id=task.created_by, payload=old
    )
    assert task.evidence["preparation"]["cursor"] == ACCOUNT_BATCH_SIZE
    step(session, task)
    assert task.status == "PENDING"
    assert task.evidence["preparation"]["completed_accounts"] == ACCOUNT_BATCH_SIZE + 1
    assert task.evidence["preparation"]["failed_accounts"] == 0


def test_material_full_population_parity_with_unmapped_and_unallocated(
    session, context
):
    from datetime import date
    from uuid import uuid4

    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_creative_day,
    )
    from tests.modules.reporting.sync_conftest import ad_row
    from tests.modules.reporting.test_creative_reporting import creative_row

    version = _version(session, context)
    day = date(2026, 11, 12)
    publish_ad_day(
        session,
        tenant_id=context.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[ad_row("a", day, campaign="c", spend="1000")],
        requested_at=CUTOFF,
    )
    rows = []
    for i in range(61):
        row = creative_row(f"creative-{i}", "10", "3")
        row["dimensions"]["stat_time_day"] = day.isoformat()
        rows.append(row)
    publish_creative_day(
        session,
        tenant_id=context.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=rows,
        requested_at=CUTOFF,
    )
    from datetime import UTC, datetime

    from tests.modules.reporting.query_helpers import finish_projections

    finish_projections(session)
    observed_cutoff = datetime.now(UTC)
    pack = build_data_pack(
        session,
        context=context,
        version=version,
        cutoff=CUTOFF,
        observed_cutoff=observed_cutoff,
    )
    report = next(
        r
        for r in pack["reporting"]
        if r["dimension"] == "material" and r["maturity_horizon"] == "d0"
    )
    baseline = freeze_review_page(
        session,
        context=context,
        bc_id=version.bc_id,
        filters=ReportingFilter.model_validate(report["filters"]),
        cutoff=observed_cutoff,
        freeze_all=True,
    )
    assert report["total"] > 50
    _assert_financial_parity(report, baseline)


def test_batch_retry_keeps_successful_predecessor_checkpoint(
    session, context, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.ai_workbench import review_data
    from app.modules.ai_workbench.review_store import ReviewSource, scoped

    task = create_task(session, context, 3)
    step(session, task)
    real = review_data.build_data_pack

    def fail_second(*args, **kwargs):
        if kwargs["advertiser_ids"] == ("acc-2",):
            raise DomainError("temporary_report", "retry")
        return real(*args, **kwargs)

    monkeypatch.setattr(review_data, "build_data_pack", fail_second)
    step(session, task)
    assert task.evidence["preparation"]["cursor"] == 1
    assert task.evidence["preparation"]["completed_accounts"] == 1
    assert task.evidence["preparation"]["attempts"] == 1
    first = [
        (s.id, s.digest)
        for s in session.exec(
            scoped(ReviewSource, task).where(ReviewSource.advertiser_id == "acc-1")
        )
    ]
    monkeypatch.setattr(review_data, "build_data_pack", real)
    step(session, task)
    assert task.status == "PENDING"
    assert task.evidence["preparation"]["completed_accounts"] == 3
    assert first == [
        (s.id, s.digest)
        for s in session.exec(
            scoped(ReviewSource, task).where(ReviewSource.advertiser_id == "acc-1")
        )
    ]


def test_publication_change_during_freeze_is_retryable(session, context, monkeypatch):
    import pytest
    from sqlmodel import select

    from app.core.errors import DomainError
    from app.modules.ai_workbench import review_reporting
    from app.modules.reporting.store_models import ReportSyncState

    version = _version(session, context)
    _publish(session, context)
    real = review_reporting.freeze_report
    changed = False

    def interleave(*args, **kwargs):
        nonlocal changed
        result = real(*args, **kwargs)
        if not changed:
            state = session.exec(
                select(ReportSyncState).where(
                    ReportSyncState.tenant_id == context.tenant_id,
                    ReportSyncState.report_kind == "ad_day",
                )
            ).first()
            state.publication_version += 1
            session.flush()
            changed = True
        return result

    monkeypatch.setattr(review_reporting, "freeze_report", interleave)
    with pytest.raises(DomainError, match="冻结期间报表已更新"):
        build_data_pack(session, context=context, version=version, cutoff=CUTOFF)


def test_preparation_batch_scale(session, context):
    import os

    from sqlalchemy import func

    from app.modules.ai_workbench.review_store import ReviewSource, scoped

    count = int(os.environ.get("TKADA_REVIEW_BENCHMARK_ACCOUNTS", "32"))
    task = create_task(session, context, count)
    calls = 0

    def prepare():
        nonlocal calls
        while task.status == "PREPARING":
            step(session, task)
            calls += 1

    _, query_count, elapsed = _measured(session, prepare)
    assert task.status == "PENDING"
    assert task.evidence["preparation"]["completed_accounts"] == count
    assert task.evidence["preparation"]["failed_accounts"] == 0
    reporting = session.exec(
        scoped(ReviewSource, task)
        .where(ReviewSource.category == "reporting")
        .with_only_columns(func.count())
    ).one()
    assert reporting == count * 12
    print(  # noqa: T201
        f"PREPARATION_BENCH accounts={count} dispatches={calls} SQL={query_count} elapsed={elapsed:.3f}s"
    )


def test_daily_inputs_follow_frozen_account_local_day(session, context):
    from datetime import UTC, datetime

    from app.modules.ai_workbench.review_data import account_window
    from app.modules.ai_workbench.review_preparation import _daily_backtest_inputs
    from app.modules.ai_workbench.reviews import load_optimizer

    task = create_task(session, context)
    _, version = load_optimizer(session, context, task.optimizer_id)
    cutoff = datetime(2026, 11, 13, 2, tzinfo=UTC)
    window = account_window("America/Los_Angeles", cutoff)
    inputs = _daily_backtest_inputs(
        session,
        task=task,
        advertiser_id="acc-1",
        cutoff=cutoff,
        version=version,
        account=window,
    )
    assert inputs["end_date"] == "2026-11-11"
    assert inputs["start_date"] == "2026-10-08"
    assert inputs["coverage"]["expected_days"] == 35
    assert len(inputs["coverage"]["missing_days"]) == 35
    assert inputs["rows"] == []


def test_soft_timeout_keeps_completed_accounts_and_retries_actual_account(
    session, context, monkeypatch
):
    from billiard.exceptions import SoftTimeLimitExceeded

    from app.modules.ai_workbench import review_data
    from app.modules.ai_workbench.review_store import source_payloads

    task = create_task(session, context, 3)
    step(session, task)
    actual = review_data.build_data_pack
    failing = task.evidence["optimizer"]["advertiser_ids"][1]

    def interrupted(*args, **kwargs):
        if kwargs["advertiser_ids"] == (failing,):
            raise SoftTimeLimitExceeded()
        return actual(*args, **kwargs)

    monkeypatch.setattr(review_data, "build_data_pack", interrupted)
    for attempt in (1, 2, 3):
        step(session, task)
        progress = task.evidence["preparation"]
        assert progress["completed_accounts"] == 1
        assert progress["cursor"] == (2 if attempt == 3 else 1)
    failures = [
        p
        for p in source_payloads(session, task, "preparation")
        if p.get("status") == "FAILED"
    ]
    assert [p["advertiser_id"] for p in failures] == [failing]
    step(session, task)
    assert task.status == "PENDING"
    assert task.evidence["preparation"]["completed_accounts"] == 2
    assert task.evidence["preparation"]["failed_accounts"] == 1


def test_outer_batch_timeout_retries_serially_without_failing_first_account(
    session, context
):
    from app.modules.ai_workbench.review_preparation import record_step_failure

    task = create_task(session, context, 3)
    payload = step(session, task)
    # 模拟账户保存点外整批回滚后的独立失败登记，不将批首账户归为错误对象。
    payload = {
        **payload,
        **{
            k: task.evidence["preparation"][k]
            for k in ("cursor", "base_complete", "generation")
        },
    }
    record_step_failure(
        session,
        tenant_id=task.tenant_id,
        actor_id=task.created_by,
        payload=payload,
        error_code="SoftTimeLimitExceeded",
    )
    assert task.evidence["preparation"]["batch_size"] == 1
    assert task.evidence["preparation"]["attempts"] == 0
    assert task.evidence["preparation"]["failed_accounts"] == 0
    step(session, task)
    assert task.evidence["preparation"]["cursor"] == 1
    assert task.evidence["preparation"]["completed_accounts"] == 1


def test_daily_material_coverage_requires_current_derived_projection(session, context):
    from datetime import UTC, datetime, timedelta

    from app.modules.ai_workbench.review_preparation import _daily_backtest_inputs
    from app.modules.ai_workbench.reviews import load_optimizer
    from app.modules.reporting.read_models.models import ReadBucketState

    task = create_task(session, context)
    _, version = load_optimizer(session, context, task.optimizer_id)
    cutoff = datetime.now(UTC)
    day = cutoff.date() - timedelta(days=1)
    state = ReadBucketState(
        tenant_id=context.tenant_id,
        advertiser_id="acc-1",
        kind="creative_material",
        bucket=day,
        source_revision=2,
        derived_revision=1,
        derived_status="COMPLETE",
        derived_observed_at=cutoff,
    )
    session.add(state)
    session.flush()

    def read():
        return _daily_backtest_inputs(
            session, task=task, advertiser_id="acc-1", cutoff=cutoff, version=version
        )

    assert day.isoformat() in read()["material_coverage"]["missing_days"]
    state.derived_revision = 2
    session.flush()
    assert day.isoformat() not in read()["material_coverage"]["missing_days"]


def test_full_account_freeze_retries_if_older_backtest_source_changes(
    session, context, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.modules.ai_workbench import review_preparation
    from app.modules.ai_workbench.review_store import source_payloads
    from app.modules.reporting.read_models.models import ReadBucketState

    task = create_task(session, context)
    step(session, task)
    original = review_preparation._daily_backtest_inputs

    def interleave(*args, **kwargs):
        result = original(*args, **kwargs)
        # 在数据包校验后改变35日窗口内、14日报表窗外的投影来源。
        session.add(
            ReadBucketState(
                tenant_id=context.tenant_id,
                advertiser_id="acc-1",
                kind="creative_material",
                bucket=datetime.now(UTC).date() - timedelta(days=30),
                source_revision=1,
            )
        )
        session.flush()
        return result

    monkeypatch.setattr(review_preparation, "_daily_backtest_inputs", interleave)
    step(session, task)
    assert task.evidence["preparation"]["completed_accounts"] == 0
    assert task.evidence["preparation"]["cursor"] == 0
    assert task.evidence["preparation"]["last_error"] == "ai_review_publication_changed"
    assert list(source_payloads(session, task, "reporting")) == []
    assert not [
        p
        for p in source_payloads(session, task, "rule_backtest_inputs")
        if p.get("advertiser_id")
    ]


def test_native_selected_layers_and_actual_publication_cutoff(
    session, context, monkeypatch
):
    from datetime import date, timedelta
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_parent_day

    version = _version(session, context)
    _publish(session, context)
    day = date(2026, 11, 12)
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.reporting.filters import authorized_grants

    grant = authorized_grants(
        session, context=context, bc_id=version.bc_id, advertiser_ids=("acc-1",)
    )[0]
    route = FrozenTikTokRoute(
        tenant_id=context.tenant_id,
        bc_id=version.bc_id,
        connection_id=grant.connection_id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
    )

    def parent(observed):
        publish_parent_day(
            session,
            tenant_id=context.tenant_id,
            job_id=uuid4(),
            level="campaign",
            route=route,
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=day,
            end=day,
            requested_at=observed,
            rows=[
                {
                    "dimensions": {
                        "campaign_id": "campaign-one",
                        "stat_time_day": str(day),
                    },
                    "metrics": {
                        "campaign_name": "嘉书-剧campaign-one-备注",
                        "spend": "50",
                        "native_growth_ad_revenue_value_d0": "20",
                    },
                }
            ],
        )

    parent(CUTOFF)
    pack = build_data_pack(session, context=context, version=version, cutoff=CUTOFF)
    report = next(
        r
        for r in pack["reporting"]
        if r["dimension"] == "campaign" and r["maturity_horizon"] == "d0"
    )
    values = {
        b["source_kind"]: b["values"]["spend"] for b in report["summary"]["buckets"]
    }
    assert values == {"PLATFORM_DIRECT": "50", "CHILD_DERIVED": "60"}
    assert all(b["level"] == "campaign" for b in report["summary"]["buckets"])
    baseline = freeze_review_page(
        session,
        context=context,
        bc_id=version.bc_id,
        filters=ReportingFilter.model_validate(report["filters"]),
        cutoff=CUTOFF,
        freeze_all=True,
    )
    _assert_financial_parity(report, baseline)
    from app.modules.ai_workbench import reviews
    from app.modules.ai_workbench.review_analysis import analyze_for_task
    from tests.modules.ai_workbench.review_helpers import create_prepared_review_task

    monkeypatch.setattr(reviews, "_now", lambda: CUTOFF)
    task = create_prepared_review_task(
        session, context=context, optimizer_id=version.optimizer_id, request_id=uuid4()
    )
    analysis = analyze_for_task(
        session, task=task, dimension="campaign", maturity_horizon="d0"
    )
    assert {item["source_kind"] for item in analysis["totals"]} == {
        "PLATFORM_DIRECT",
        "CHILD_DERIVED",
    }
    assert {item["level"] for item in analysis["totals"]} == {"campaign"}
    parent(CUTOFF + timedelta(seconds=1))
    late = build_data_pack(session, context=context, version=version, cutoff=CUTOFF)
    assert all(
        r["status"] == "UNAVAILABLE" and r["reason"] == "publication_after_cutoff"
        for r in late["reporting"]
        if r["dimension"] in {"account", "campaign", "drama"}
        and r["maturity_horizon"] == "d0"
    )
