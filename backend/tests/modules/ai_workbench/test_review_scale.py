"""真实 PostgreSQL：大范围清单、独立归档、后台续接与全量聚合。"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.ai_workbench import reviews, service
from app.modules.ai_workbench.review_analysis import analyze_for_task
from app.modules.ai_workbench.review_preparation import prepare_step, repair_preparation
from app.modules.ai_workbench.review_reading import (
    list_notes,
    query_for_task,
    read_for_task,
    write_note,
)
from app.modules.ai_workbench.review_store import (
    ReviewArtifact,
    ReviewSource,
    archive_source,
    delivered_evidence_ids,
    scoped,
)
from app.modules.ai_workbench.schemas import OptimizerCreate
from tests.modules.ai_workbench.review_helpers import prepare_task
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)


def create_task(session, context, count=1):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    _, version = reviews.load_optimizer(session, context, optimizer.id)
    ids = ["acc-1", *[f"acc-{i}" for i in range(2, count + 1)]]
    if count > 1:
        connection = (
            session.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == context.tenant_id
                )
            )
            .one()
            .connection_id
        )
        session.add_all(
            [
                AdvertiserAccount(
                    tenant_id=context.tenant_id,
                    advertiser_id=aid,
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
                for aid in ids[1:]
            ]
        )
        session.flush()
        session.add_all(
            [
                BCAccountAccess(
                    tenant_id=context.tenant_id,
                    bc_id="bc-a",
                    advertiser_id=aid,
                    connection_id=connection,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_build=True,
                    permission_state="VERIFIED",
                )
                for aid in ids[1:]
            ]
        )
        version.advertiser_ids = ids
        session.flush()
    return reviews.create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )


def step(session, task):
    progress = task.evidence["preparation"]
    payload = {
        "task_id": str(task.id),
        **{k: progress[k] for k in ("cursor", "base_complete", "generation")},
    }
    prepare_step(
        session, tenant_id=task.tenant_id, actor_id=task.created_by, payload=payload
    )
    return payload


def report(
    aid,
    spend="0.10",
    revenue="0.30",
    currency="USD",
    timezone="UTC",
    mature="AVAILABLE",
    padding="",
):
    return {
        "advertiser_id": aid,
        "dimension": "campaign",
        "maturity_horizon": "d0",
        "status": "COMPLETE",
        "window": {"start_date": "2026-11-06", "end_date": "2026-11-12"},
        "total": 1,
        "rows_truncated": False,
        "maturity_evidence": {"status": mature},
        "rows": [
            {
                "row_key": aid + ":campaign",
                "display": {"name": padding},
                "metric_buckets": [
                    {
                        "currency": currency,
                        "timezone": timezone,
                        "attribution": "D0",
                        "optimization_goal": None,
                        "values": {
                            "spend": spend,
                            "revenue_d0": revenue,
                            "roas_d0": "999",
                        },
                        "availability": {
                            "spend": "AVAILABLE",
                            "revenue_d0": "AVAILABLE",
                            "roas_d0": "AVAILABLE",
                        },
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize("count", [50, 100, 500, 1000, 1001])
def test_initial_scope_and_full_scan_do_not_depend_on_archive_capacity(
    session, context, count
):
    task = create_task(session, context, count)
    assert task.status == "PREPARING"
    assert len(task.evidence["optimizer"]["advertiser_ids"]) == count
    assert "evidence_index" not in task.evidence and not task.evidence_store
    assert "data_pack" not in task.evidence
    before = json.dumps(task.evidence, sort_keys=True)
    # 发布过的数据量远超过旧512KB总预算，仍只归档一次；不需要伪造1000个实时报表。
    sources = []
    for aid in task.evidence["optimizer"]["advertiser_ids"]:
        payload = report(aid, padding="fixture" * 180)
        sources.append(
            ReviewSource(
                task_id=task.id,
                id=uuid5(task.id, aid),
                tenant_id=context.tenant_id,
                optimizer_id=task.optimizer_id,
                category="reporting",
                advertiser_id=aid,
                dimension="campaign",
                maturity_horizon="d0",
                digest=reviews._digest(payload),
                payload=payload,
                source_metadata={"advertiser_id": aid},
                captured_at=datetime.now(UTC),
            )
        )
    session.add_all(sources)
    session.flush()
    result = analyze_for_task(
        session,
        task=task,
        dimension="campaign",
        maturity_horizon="d0",
        offset=count - 1,
        limit=1,
    )
    assert result["total"] == count and result["next_offset"] is None
    assert result["coverage"]["frozen_accounts"] == count
    # totals独立分页；offset0展示全量分母而不是TopN之和。
    result = analyze_for_task(
        session, task=task, dimension="campaign", maturity_horizon="d0", limit=1
    )
    assert result["totals"][0]["spend"] == str(Decimal("0.10") * count)
    assert result["totals"][0]["roas"] == "3"
    assert result["source_reference_count"] == count
    assert result["id"] in delivered_evidence_ids(session, task)
    assert (
        query_for_task(
            session, task=task, category="reporting", advertiser_id=f"acc-{count}"
        )["total"]
        == 1
    )
    assert json.dumps(task.evidence, sort_keys=True) == before
    artifact = session.get(ReviewArtifact, (task.id, UUID(result["id"])))
    assert len(artifact.payload["source_refs"]) == count


def test_receipts_exceed_old_total_budget_without_mutating_task_and_notes_are_not_evidence(
    session, context
):
    task = create_task(session, context)
    payload = [{"id": str(uuid4()), "text": "z" * 500} for _ in range(1100)]
    source = archive_source(
        session,
        task=task,
        category="plans",
        payload=payload,
        captured_at=datetime.now(UTC),
    )
    initial = json.dumps(task.evidence, sort_keys=True)
    ids = set()
    for offset in range(1100):
        page = read_for_task(session, task=task, evidence_id=source.id, offset=offset)
        ids.add(page["id"])
    assert len(ids) == 1100 and delivered_evidence_ids(session, task) == ids
    assert json.dumps(task.evidence, sort_keys=True) == initial
    assert source.id not in delivered_evidence_ids(session, task)
    note = write_note(session, task=task, key="progress", body="未经核验的工作草稿")
    assert note["id"] not in delivered_evidence_ids(session, task)
    updated = write_note(session, task=task, key="progress", body="下一步继续核查")
    assert list_notes(session, task=task)["items"] == [updated]
    assert (
        len(
            session.exec(
                scoped(ReviewArtifact, task).where(ReviewArtifact.kind == "note")
            ).all()
        )
        == 2
    )


def test_exact_money_axes_maturity_thresholds_and_scoped_citations(
    session, context, other_context
):
    task = create_task(session, context, 5)
    for aid, spend, revenue, currency, zone, mature in (
        ("acc-1", "0.10", "0.30", "USD", "UTC", "AVAILABLE"),
        ("acc-2", "0.20", "0.10", "USD", "UTC", "AVAILABLE"),
        ("acc-3", "99", "99", "EUR", "UTC", "AVAILABLE"),
        ("acc-4", "99", "99", "USD", "Asia/Shanghai", "AVAILABLE"),
        ("acc-5", "999", "999", "USD", "UTC", "STALE"),
    ):
        archive_source(
            session,
            task=task,
            category="reporting",
            payload=report(aid, spend, revenue, currency, zone, mature),
            captured_at=datetime.now(UTC),
        )
    result = analyze_for_task(
        session,
        task=task,
        dimension="campaign",
        maturity_horizon="d0",
        sort_by="profit",
        descending=False,
        max_roas="1",
    )
    assert result["total"] == 3
    group = next(
        g
        for g in result["totals"]
        if g["currency"] == "USD"
        and g["timezone"] == "UTC"
        and g["maturity_status"] == "AVAILABLE"
    )
    assert group["spend"] == "0.30" and group["revenue_d0"] == "0.40"
    assert group["roas"] == str(Decimal("0.40") / Decimal("0.30"))
    assert group["filtered_spend"] == "0.20"
    assert result["items"][0]["advertiser_id"] == "acc-2"
    assert result["coverage"]["excluded_accounts"] == 1
    foreign = create_task(session, other_context)
    assert result["id"] not in delivered_evidence_ids(session, foreign)
    with pytest.raises(HTTPException):
        analyze_for_task(
            session,
            task=task,
            dimension="campaign",
            maturity_horizon="d7",
            sort_by="revenue_d0",
        )
    with pytest.raises(HTTPException):
        analyze_for_task(
            session,
            task=task,
            dimension="campaign",
            maturity_horizon="d0",
            advertiser_id="outside",
        )


def test_preparation_is_bounded_idempotent_and_repairs_lost_dispatch(session, context):
    task = create_task(session, context, 2)
    original = step(session, task)
    assert (
        task.status == "PREPARING"
        and task.evidence["preparation"]["completed_accounts"] == 0
    )
    source_count = session.exec(
        select(func.count())
        .select_from(ReviewSource)
        .where(ReviewSource.task_id == task.id)
    ).one()
    prepare_step(
        session, tenant_id=task.tenant_id, actor_id=task.created_by, payload=original
    )
    assert (
        session.exec(
            select(func.count())
            .select_from(ReviewSource)
            .where(ReviewSource.task_id == task.id)
        ).one()
        == source_count
    )
    assert (
        repair_preparation(session, now=datetime.now(UTC) + timedelta(minutes=6)) == 1
    )
    step(session, task)
    assert (
        task.status == "PREPARING"
        and task.evidence["preparation"]["completed_accounts"] == 1
    )
    step(session, task)
    assert (
        task.status == "PENDING"
        and task.evidence["preparation"]["completed_accounts"] == 2
    )
    assert task.evidence["preparation"]["failed_accounts"] == 0
    assert query_for_task(session, task=task, category="reporting")["total"] == 24
    assert session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "ai_workbench.prepare_review",
            PendingDispatch.tenant_id == context.tenant_id,
        )
    ).all()


def test_preparation_retries_failure_and_revocation_stops_work(
    session, context, monkeypatch
):
    from app.modules.ai_workbench import review_data

    task = create_task(session, context)
    step(session, task)

    def unavailable(*_args, **_kwargs):
        raise DomainError(
            "snapshot_temporary_failure", "do not persist private error details"
        )

    monkeypatch.setattr(review_data, "build_data_pack", unavailable)
    step(session, task)
    assert (
        task.evidence["preparation"]["attempts"] == 1
        and task.evidence["preparation"]["cursor"] == 0
    )
    step(session, task)
    step(session, task)
    assert (
        task.status == "PENDING"
        and task.evidence["preparation"]["failed_accounts"] == 1
    )
    failures = query_for_task(session, task=task, category="preparation")
    page = read_for_task(
        session, task=task, evidence_id=UUID(failures["items"][0]["id"])
    )
    assert page["rows"][0]["reason"] == "snapshot_temporary_failure"
    other = reviews.create_review_task(
        session, context=context, optimizer_id=task.optimizer_id, request_id=uuid4()
    )
    grant = session.exec(
        select(BCAccountAccess).where(BCAccountAccess.tenant_id == context.tenant_id)
    ).one()
    grant.authorized = False
    session.flush()
    step(session, other)
    assert other.status == "CANCELLED"


def test_two_accounts_freeze_real_published_reports_after_request_without_cross_currency_mix(
    session, context, monkeypatch
):
    from datetime import date

    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from tests.modules.ai_workbench.test_review_maturity import CUTOFF, _publish
    from tests.modules.reporting.sync_conftest import ad_row

    monkeypatch.setattr(reviews, "_now", lambda: CUTOFF)
    task = create_task(session, context, 2)
    # 实际后台捕获比请求晚；完整当地日窗口仍固定在请求日。
    _publish(session, context, observed=CUTOFF + timedelta(minutes=10))
    start, end = date(2026, 10, 30), date(2026, 11, 12)
    rows = []
    for offset in range(14):
        row = ad_row(
            "ad-second",
            start + timedelta(days=offset),
            adv="acc-2",
            campaign="campaign-second",
            spend="20",
            d0="2",
            total="999",
        )
        row["metrics"]["native_growth_ad_revenue_value_d6"] = "14"
        rows.append(row)
    publish_ad_day(
        session,
        tenant_id=context.tenant_id,
        job_id=uuid4(),
        accounts={"acc-2": AccountMeta("EUR", "UTC")},
        start=start,
        end=end,
        rows=rows,
        requested_at=CUTOFF + timedelta(minutes=20),
    )
    prepare_task(session, task)
    assert (
        task.evidence["preparation"]["completed_accounts"] == 2
        and task.evidence["preparation"]["failed_accounts"] == 0
    )
    result = analyze_for_task(
        session, task=task, dimension="campaign", maturity_horizon="d7", sort_by="roas"
    )
    totals = {g["currency"]: g for g in result["totals"]}
    assert totals["USD"]["spend"] == "70" and totals["USD"]["revenue_d7"] == "49"
    assert totals["EUR"]["spend"] == "140" and totals["EUR"]["revenue_d7"] == "98"
    assert all(g["roas"] == "0.7" for g in totals.values())
    sources = query_for_task(
        session,
        task=task,
        category="reporting",
        advertiser_id="acc-2",
        dimension="campaign",
        maturity_horizon="d7",
    )
    page = read_for_task(
        session, task=task, evidence_id=UUID(sources["items"][0]["id"])
    )
    assert page["metadata"]["requested_at"] == CUTOFF.isoformat()
    assert datetime.fromisoformat(page["captured_at"]) > CUTOFF
    assert page["metadata"]["filters"]["start_date"] == "2026-10-30"
    assert (
        page["rows"][0]["metric_buckets"][0] == result["items"][1]["metric_bucket"]
        or page["rows"][0]["metric_buckets"][0] == result["items"][0]["metric_bucket"]
    )
    for row in rows:
        row["metrics"]["spend"] = "999"
    publish_ad_day(
        session,
        tenant_id=context.tenant_id,
        job_id=uuid4(),
        accounts={"acc-2": AccountMeta("EUR", "UTC")},
        start=start,
        end=end,
        rows=rows,
        requested_at=datetime.now(UTC),
    )
    assert (
        analyze_for_task(
            session,
            task=task,
            dimension="campaign",
            maturity_horizon="d7",
            sort_by="roas",
        )
        == result
    )


def test_worker_rollback_failure_budget_and_retry_restore_preparing(session, context):
    from app.modules.ai_workbench.ai_tasks import restart_task
    from app.modules.ai_workbench.review_preparation import record_step_failure

    task = create_task(session, context)
    for attempt in range(3):
        progress = task.evidence["preparation"]
        payload = {
            "task_id": str(task.id),
            **{k: progress[k] for k in ("cursor", "base_complete", "generation")},
        }
        record_step_failure(
            session,
            tenant_id=task.tenant_id,
            actor_id=task.created_by,
            payload=payload,
            error_code="SoftTimeLimitExceeded",
        )
        assert task.evidence["preparation"]["attempts"] == attempt + 1
    assert task.status == "FAILED"
    restarted = restart_task(
        session, context=context, optimizer_id=task.optimizer_id, task_id=task.id
    )
    assert (
        restarted.status == "PREPARING"
        and restarted.evidence["preparation"]["attempts"] == 0
    )
    prepare_task(session, restarted)
    assert restarted.status == "PENDING"


def test_repair_due_filter_prevents_fresh_tasks_starving_lost_dispatch(
    session, context
):
    first = create_task(session, context)
    second = reviews.create_review_task(
        session, context=context, optimizer_id=first.optimizer_id, request_id=uuid4()
    )
    second.evidence = {
        **second.evidence,
        "preparation": {
            **second.evidence["preparation"],
            "queued_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
        },
    }
    session.flush()
    assert repair_preparation(session, now=datetime.now(UTC), limit=1) == 1
    assert first.evidence["preparation"]["generation"] == 0
    assert second.evidence["preparation"]["generation"] == 1


def test_catalog_exposes_account_business_and_coverage_percentages(session, context):
    from app.modules.ai_workbench.review_store import source_catalog

    task = create_task(session, context, 50)
    for i, aid in enumerate(task.evidence["optimizer"]["advertiser_ids"]):
        archive_source(
            session,
            task=task,
            category="reporting",
            payload=report(aid, mature="AVAILABLE" if i < 15 else "STALE"),
            captured_at=datetime.now(UTC),
        )
    archive_source(
        session,
        task=task,
        category="business",
        payload={"rows": []},
        captured_at=datetime.now(UTC),
    )
    archive_source(
        session,
        task=task,
        category="data_pack_business",
        payload={
            "advertiser_id": "acc-1",
            "rows": [{"id": "settlement-a"}, {"id": "settlement-b"}],
        },
        captured_at=datetime.now(UTC),
    )
    catalog = {row["category"]: row for row in source_catalog(session, task)}
    assert catalog["business"]["row_count"] == 0
    assert catalog["data_pack_business"]["row_count"] == 2
    assert catalog["data_pack_business"]["account_count"] == 1
    result = analyze_for_task(
        session, task=task, dimension="campaign", maturity_horizon="d0"
    )
    assert result["coverage"]["available_accounts_percent"] == "30"
    assert result["coverage"]["excluded_accounts_percent"] == "70"


def test_catalog_does_not_mislabel_nested_business_as_empty(session, context):
    from app.modules.ai_workbench.review_store import source_catalog

    task = create_task(session, context)
    payloads = {
        "business": {"runs": [{"id": "run"}], "publications": [{"id": "published"}]},
        "history": {"reviews": [{"id": "review"}], "proposals": []},
        "operation_logs": {
            "coverage": {"complete": True},
            "exports": [{"id": "export"}],
        },
    }
    for category, payload in payloads.items():
        archive_source(
            session,
            task=task,
            category=category,
            payload=payload,
            captured_at=datetime.now(UTC),
        )
    catalog = {row["category"]: row for row in source_catalog(session, task)}
    for category in payloads:
        assert catalog[category]["source_count"] == 1
        assert catalog[category]["row_count"] is None
        assert catalog[category]["row_count_status"] == "UNKNOWN_READ_SOURCE"
