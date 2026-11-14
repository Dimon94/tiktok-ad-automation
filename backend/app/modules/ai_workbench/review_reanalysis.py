"""人工模式下用同一份冻结事实重新分析，不改写原复盘或继承旧引用权限。"""

from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.ai_workbench import reviews
from app.modules.ai_workbench.review_store import ReviewSource, scoped
from app.modules.reporting.filters import authorized_grants


def _configuration(value):
    # 规则/账户集合没有顺序语义；配置、版本、停用状态等其余内容必须完全相同。
    result = {
        key: deepcopy(value.get(key)) for key in ("optimizer", "strategy", "rules")
    }
    if not isinstance(result["optimizer"], dict) or not isinstance(
        result["rules"], list
    ):
        return None
    accounts = result["optimizer"].get("advertiser_ids")
    if not isinstance(accounts, list) or any(not isinstance(a, str) for a in accounts):
        return None
    if any(not isinstance(rule, dict) or "id" not in rule for rule in result["rules"]):
        return None
    accounts.sort()
    result["rules"].sort(key=lambda rule: str(rule["id"]))
    return result


def _validate_archive(session, original, accounts):
    from app.modules.ai_workbench.review_reading import CATEGORIES

    # 仅投影小元数据，不为完整性核验将大报表正文载入内存。
    rows = session.execute(
        scoped(ReviewSource, original).with_only_columns(
            ReviewSource.category,
            ReviewSource.advertiser_id,
            ReviewSource.dimension,
            ReviewSource.maturity_horizon,
            ReviewSource.payload["status"].astext,
        )
    ).all()
    counts = Counter(
        (category, aid, dimension, horizon)
        for category, aid, dimension, horizon, _ in rows
    )
    preparations = [
        (aid, status)
        for category, aid, _, _, status in rows
        if category == "preparation"
    ]
    valid = (
        len(preparations) == len(accounts)
        and {aid for aid, _ in preparations} == accounts
        and all(status == "COMPLETE" for _, status in preparations)
        and all(aid is None or aid in accounts for _, aid, _, _, _ in rows)
        and all(counts[(category, None, None, None)] == 1 for category in CATEGORIES)
        and all(
            counts[("reporting", aid, dim, horizon)] == 1
            for aid in accounts
            for dim in ("account", "campaign", "drama", "material")
            for horizon in ("d0", "d3", "d7")
        )
        and all(
            counts[(category, aid, None, None)] == 1
            for aid in accounts
            for category in ("rule_backtest_inputs", "attribution", "history")
        )
    )
    archived_catalog = original.evidence.get("source_catalog")
    if archived_catalog:
        current_counts = Counter(row[0] for row in rows)
        valid = valid and current_counts == {
            item["category"]: item["source_count"] for item in archived_catalog
        }
    if not valid:
        raise DomainError(
            "ai_review_reanalysis_unavailable", "原冻结归档不完整，请重新复盘"
        )


def reanalyze_review_task(
    session,
    *,
    context,
    optimizer_id: UUID,
    source_task_id: UUID,
    request_id: UUID,
    authorization_ttl_seconds: int = 900,
):
    reviews.authorize(session, context, write=True)
    original = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            reviews.DSHReviewTask.tenant_id == context.tenant_id,
            reviews.DSHReviewTask.optimizer_id == optimizer_id,
            reviews.DSHReviewTask.id == source_task_id,
        )
        .with_for_update()
    ).one_or_none()
    if original is None:
        raise DomainError("ai_review_not_found", "找不到本投手的原复盘")
    optimizer, version = reviews.load_optimizer(session, context, optimizer_id)
    current = reviews.frozen_configuration(
        session,
        optimizer=optimizer,
        version=version,
        context=context,
    )
    digest = reviews._digest(
        {
            "reanalyze_from": str(source_task_id),
            "authorization_ttl_seconds": authorization_ttl_seconds,
        }
    )
    existing = session.exec(
        select(reviews.DSHReviewTask).where(
            reviews.DSHReviewTask.tenant_id == context.tenant_id,
            reviews.DSHReviewTask.request_id == request_id,
        )
    ).one_or_none()
    progress = original.evidence.get("preparation", {})
    allowed = {
        g.advertiser_id
        for g in authorized_grants(
            session,
            context=context,
            bc_id=version.bc_id,
            advertiser_ids=tuple(version.advertiser_ids),
        )
    }
    if (
        not optimizer.active
        or version.optimization_mode != "MANUAL"
        or original.status != "COMPLETE"
        or original.evidence.get("reading_contract") != "on-demand-v2"
        or progress.get("completed_accounts") != len(version.advertiser_ids)
        or progress.get("failed_accounts") != 0
        or not progress.get("base_complete")
        or not original.evidence.get("captured_at")
        or allowed != set(version.advertiser_ids)
        or _configuration(current) != _configuration(original.evidence)
    ):
        raise DomainError(
            "ai_review_reanalysis_unavailable",
            "仅可在人工模式及原范围、授权、配置均未改变时重分析完整的已完成复盘；否则请发起新复盘",
        )
    if existing is not None:
        if existing.optimizer_id != optimizer_id or existing.request_digest != digest:
            raise DomainError("idempotency_conflict", "重分析请求内容不同")
        return existing
    _validate_archive(session, original, set(version.advertiser_ids))
    task = reviews.create_review_task(
        session,
        context=context,
        optimizer_id=optimizer_id,
        request_id=request_id,
        authorization_ttl_seconds=authorization_ttl_seconds,
        request_digest=digest,
    )
    # 仅复制事实来源：不复制旧结果、笔记、计算产物、交付凭证、fence或执行授权。
    # 复用来源UUID可以保留冻结对象内的标识；所有读取/凭证仍受新task_id严格隔离。
    copied = session.execute(
        text("""
        WITH copied AS (
            INSERT INTO ai_review_source
                (task_id,id,tenant_id,optimizer_id,category,advertiser_id,dimension,
                 maturity_horizon,digest,payload,source_metadata,captured_at)
            SELECT :new_task,id,tenant_id,optimizer_id,category,advertiser_id,dimension,
                maturity_horizon,digest,payload,
                source_metadata || jsonb_build_object('reused_from_task_id',CAST(:old_task AS text)),captured_at
            FROM ai_review_source
            WHERE task_id=:old_task AND tenant_id=:tenant AND optimizer_id=:optimizer
                AND category<>'request'
            RETURNING 1
        ) SELECT count(*) FROM copied
    """),
        {
            "new_task": task.id,
            "old_task": original.id,
            "tenant": context.tenant_id,
            "optimizer": optimizer_id,
        },
    ).scalar_one()
    now = datetime.now(UTC)
    evidence = deepcopy(original.evidence)
    evidence.pop("methods", None)
    evidence.update(
        requested_at=now.isoformat(),
        reanalysis={
            "source_task_id": str(original.id),
            "sources": copied,
            "fact_cutoff": original.evidence["captured_at"],
            "fresh_data": False,
        },
    )
    task.evidence = evidence
    task.status = "PENDING"
    session.flush()
    # create_review_task投递的准备消息仍按正常outbox发送；PENDING使其安全空操作，
    # 不删除队列/消息，也不将原任务或历史事实改成新时点。
    return task
