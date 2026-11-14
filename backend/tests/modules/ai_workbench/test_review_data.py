from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import select

from app.modules.ai_workbench import service
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.reporting.query_models import QuerySnapshot
from tests.modules.ai_workbench.review_helpers import (
    create_prepared_review_task,
    source_index,
)
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)


def test_review_actively_builds_local_data_without_user_query(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    assert not session.exec(
        select(QuerySnapshot).where(QuerySnapshot.tenant_id == context.tenant_id)
    ).all()
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    pack = task.evidence["data_pack"]
    assert pack["contract"] == "ai-review-data-v2"
    assert pack["cutoff"]
    assert "accounts" not in pack and "reporting" not in pack
    assert task.status == "PENDING"
    assert task.evidence["preparation"]["completed_accounts"] == 1
    assert task.evidence["preparation"]["failed_accounts"] == 0
    assert {
        r["dimension"]
        for r in source_index(session, task)
        if r["category"] == "reporting"
    } == {"account", "campaign", "drama", "material"}
    assert pack["causal_incremental_profit"] is None
    assert pack["missing_is_zero"] is False
    frozen = task.evidence.copy()
    same = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=task.request_id
    )
    assert same.evidence == frozen


def test_account_window_uses_local_complete_days_and_maturity():
    from app.modules.ai_workbench.review_data import account_window

    cutoff = datetime(2026, 11, 13, 2, tzinfo=UTC)
    asia = account_window("Asia/Hong_Kong", cutoff)
    america = account_window("America/Los_Angeles", cutoff)
    assert asia["complete_through"] == "2026-11-12"
    assert america["complete_through"] == "2026-11-11"
    assert asia["mature_through"]["d7"] == "2026-11-05"
    assert account_window("", cutoff)["status"] == "TIMEZONE_UNAVAILABLE"


def test_pack_uses_published_metrics_and_preserves_old_values(session, context):
    from datetime import date

    from app.modules.ai_workbench.review_data import build_data_pack
    from app.modules.ai_workbench.reviews import load_optimizer
    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    _, version = load_optimizer(session, context, optimizer.id)
    day = date(2026, 11, 12)

    def publish(spend):
        publish_ad_day(
            session,
            tenant_id=context.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=day,
            end=day,
            requested_at=datetime(2026, 11, 13, 11, tzinfo=UTC),
            rows=[
                ad_row(
                    "ad-one",
                    day,
                    campaign="campaign-one",
                    adv="acc-1",
                    spend=spend,
                    d0="0",
                    total="0",
                )
            ],
        )

    publish("10")
    pack = build_data_pack(
        session,
        context=context,
        version=version,
        cutoff=datetime(2026, 11, 13, 12, tzinfo=UTC),
    )
    campaign = next(r for r in pack["reporting"] if r["dimension"] == "campaign")
    bucket = campaign["rows"][0]["metric_buckets"][0]
    assert bucket["currency"] == "USD"
    assert bucket["values"]["spend"] == "10"
    assert bucket["values"]["revenue_d0"] == "0"
    publish("20")
    assert bucket["values"]["spend"] == "10"
