"""一次处理一个账户，事务内保存进度及下一步；Beat 修复丢失的投递。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded
from fastapi import HTTPException
from sqlalchemy import DateTime, cast, or_, text
from sqlmodel import select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench import reviews
from app.modules.ai_workbench.review_store import archive_source, source_payloads

TASK_NAME = "ai_workbench.prepare_review"
register_dispatch_task(TASK_NAME, "ads-reporting")


def enqueue_preparation(session, *, task, repair=False):
    progress = dict(task.evidence["preparation"])
    if repair:
        progress["generation"] += 1
    progress["queued_at"] = datetime.now(UTC).isoformat()
    task.evidence = {**task.evidence, "preparation": progress}
    enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=task.tenant_id, actor_id=task.created_by, role=""
        ),
        task_name=TASK_NAME,
        task_key=f"ai-review-prepare:{task.id}:{progress['cursor']}:{int(progress['base_complete'])}:{progress['generation']}",
        payload={
            "task_id": str(task.id),
            "cursor": progress["cursor"],
            "base_complete": progress["base_complete"],
            "generation": progress["generation"],
        },
    )


def repair_preparation(session, *, now, limit=100):
    queued_at = reviews.DSHReviewTask.evidence["preparation"]["queued_at"].astext
    tasks = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            reviews.DSHReviewTask.status == "PREPARING",
            or_(
                queued_at.is_(None),
                cast(queued_at, DateTime(timezone=True)) < now - timedelta(minutes=5),
            ),
        )
        .order_by(reviews.DSHReviewTask.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    repaired = 0
    for task in tasks:
        queued = task.evidence.get("preparation", {}).get("queued_at")
        if not queued or datetime.fromisoformat(queued) < now - timedelta(minutes=5):
            enqueue_preparation(session, task=task, repair=True)
            repaired += 1
    return repaired


def _empty_pack(task):
    return {
        "contract": "ai-review-data-v2",
        "cutoff": task.evidence["captured_at"],
        "accounts": [],
        "reporting": [],
        "business": [],
        "operation_events": [],
        "missing_is_zero": False,
        "accounts_truncated": False,
        "excluded_accounts": [],
        "causal_incremental_profit": None,
        "window_days": 7,
    }


def _daily_backtest_inputs(session, *, task, advertiser_id, cutoff, version):
    """冻结规则回测专用的逐账户逐日事实；覆盖不足保持缺口状态，绝不补零。"""
    start = (cutoff.date() - timedelta(days=35))
    rows = session.execute(text("""
        SELECT stat_date, campaign_id, campaign_name, currency, timezone,
               spend, revenue_d0, revenue_d3, revenue_d7, revenue_d14,
               revenue_d30, revenue_total
        FROM campaign_report_day
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser
          AND stat_date >= :start AND stat_date < :cutoff
        ORDER BY stat_date, campaign_id
    """), {"tenant": task.tenant_id, "advertiser": advertiser_id,
           "start": start, "cutoff": cutoff.date()}).mappings().all()
    state = session.execute(text("""
        SELECT stat_date, status, revision, observed_at, last_success_at
        FROM account_report_day_state
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser
          AND stat_date >= :start AND stat_date < :cutoff
        ORDER BY stat_date
    """), {"tenant": task.tenant_id, "advertiser": advertiser_id,
           "start": start, "cutoff": cutoff.date()}).mappings().all()
    by_date = {r["stat_date"].isoformat(): r for r in state}
    dates = sorted({r["stat_date"].isoformat() for r in rows})
    expected = [(start + timedelta(days=i)).isoformat() for i in range((cutoff.date()-start).days)]
    missing = [d for d in expected if d not in by_date or by_date[d]["status"] != "COMPLETE"]
    return {
        "advertiser_id": advertiser_id, "bc_id": version.bc_id,
        "currency": next((r["currency"] for r in rows if r["currency"]), None),
        "timezone": next((r["timezone"] for r in rows if r["timezone"]), None),
        "start_date": start.isoformat(), "end_date": (cutoff.date()-timedelta(days=1)).isoformat(),
        "coverage": {"expected_days": len(expected), "observed_days": len(dates),
                      "missing_days": missing, "complete": not missing,
                      "states": [{**dict(v), "stat_date": v["stat_date"].isoformat(),
                                   "observed_at": v["observed_at"].isoformat() if v["observed_at"] else None,
                                   "last_success_at": v["last_success_at"].isoformat() if v["last_success_at"] else None} for v in state]},
        "rows": [{k: (str(v) if getattr(v, "as_tuple", None) else v) for k, v in
                   {**dict(r), "stat_date": r["stat_date"].isoformat()}.items()} for r in rows],
        "provenance": {"source": "campaign_report_day", "state_source": "account_report_day_state",
                        "cutoff": cutoff.isoformat(), "full_population": True},
    }


def prepare_step(session, *, tenant_id, actor_id, payload):
    task = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            reviews.DSHReviewTask.tenant_id == tenant_id,
            reviews.DSHReviewTask.created_by == actor_id,
            reviews.DSHReviewTask.id == UUID(payload["task_id"]),
        )
        .with_for_update()
    ).one_or_none()
    if task is None or task.status != "PREPARING" or task.cancellation_requested:
        return
    progress = dict(task.evidence["preparation"])
    if any(
        payload.get(k) != progress[k] for k in ("cursor", "base_complete", "generation")
    ):
        return
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="")
    try:
        reviews.authorize(session, context, write=True)
        optimizer, version = reviews.load_optimizer(session, context, task.optimizer_id)
        from app.modules.reporting.filters import authorized_grants

        grants = authorized_grants(
            session,
            context=context,
            bc_id=version.bc_id,
            advertiser_ids=tuple(version.advertiser_ids),
        )
        if (
            not optimizer.active
            or version.number != task.optimizer_version
            or {g.advertiser_id for g in grants}
            != set(task.evidence["optimizer"]["advertiser_ids"])
        ):
            raise DomainError("ai_evidence_scope_revoked", "复盘范围或授权已改变")
    except DomainError, HTTPException:
        task.status = "CANCELLED"
        task.cancellation_requested = True
        task.finished_at = datetime.now(UTC)
        task.result = {"summary": "准备期间范围、版本或授权已改变，请重新发起复盘"}
        session.flush()
        return
    cutoff = datetime.fromisoformat(task.evidence["captured_at"])
    captured = datetime.now(UTC)
    from app.modules.ai_workbench.review_reading import CATEGORIES

    if not progress["base_complete"]:
        request = next(source_payloads(session, task, "request"))
        frozen = reviews.frozen_evidence(
            session,
            context=context,
            optimizer=optimizer,
            version=version,
            cutoff=cutoff,
            data_pack=_empty_pack(task),
            evidence_ids=tuple(UUID(i) for i in request["evidence_ids"]),
            knowledge_refs=request["knowledge_refs"],
        )
        # 固定配置及方法提示保留在清单；业务正文全部独立保存。
        initial = {
            k: frozen[k]
            for k in ("captured_at", "capabilities", "knowledge", "knowledge_selection")
        }
        for category in CATEGORIES:
            archive_source(
                session,
                task=task,
                category=category,
                payload=frozen.get(category, {}),
                captured_at=captured,
                metadata={
                    "truncated": frozen.get("execution_truncated", {}).get(
                        {
                            "plans": "plans",
                            "build_occurrences": "builds",
                            "rule_evidence": "rules",
                        }.get(category),
                        False,
                    )
                },
                identity=f"base:{category}",
            )
        task.evidence = {
            **task.evidence,
            **initial,
            "data_pack": {
                k: v
                for k, v in _empty_pack(task).items()
                if k not in {"accounts", "reporting", "business", "operation_events"}
            },
        }
        progress["base_complete"] = True
    elif progress["cursor"] < progress["total_accounts"]:
        advertiser_id = task.evidence["optimizer"]["advertiser_ids"][progress["cursor"]]
        from app.modules.ai_workbench.attribution import build_attribution_pack
        from app.modules.ai_workbench.learning import freeze_history
        from app.modules.ai_workbench.review_data import build_data_pack

        try:
            with session.begin_nested():
                pack = build_data_pack(
                    session,
                    context=context,
                    version=version,
                    cutoff=cutoff,
                    observed_cutoff=captured,
                    advertiser_ids=(advertiser_id,),
                )
                archive_source(
                    session, task=task, category="rule_backtest_inputs",
                    payload=_daily_backtest_inputs(session, task=task, advertiser_id=advertiser_id,
                                                   cutoff=cutoff, version=version),
                    captured_at=captured,
                    identity=f"daily-backtest:{advertiser_id}",
                )
                for report in pack["reporting"]:
                    archive_source(
                        session,
                        task=task,
                        category="reporting",
                        payload=report,
                        captured_at=captured,
                        identity=f"report:{advertiser_id}:{report['dimension']}:{report['maturity_horizon']}",
                    )
                for name in ("business", "operation_events"):
                    for item in pack[name]:
                        archive_source(
                            session,
                            task=task,
                            category="data_pack_" + name,
                            payload=item,
                            captured_at=captured,
                        )
                # 归因映射仍校验整个执行范围，只为本账户读取冻结报表。
                attribution = build_attribution_pack(
                    session,
                    context=context,
                    optimizer_id=optimizer.id,
                    version=version,
                    cutoff=cutoff,
                    observed_cutoff=captured,
                    account_windows=pack["accounts"],
                )
                archive_source(
                    session,
                    task=task,
                    category="attribution",
                    payload={**attribution, "advertiser_id": advertiser_id},
                    captured_at=captured,
                )
                history = freeze_history(
                    session,
                    context=context,
                    optimizer_id=optimizer.id,
                    version=version,
                    data_pack=pack,
                )
                archive_source(
                    session,
                    task=task,
                    category="history",
                    payload={**history, "advertiser_id": advertiser_id},
                    captured_at=captured,
                )
                archive_source(
                    session,
                    task=task,
                    category="preparation",
                    payload={
                        "advertiser_id": advertiser_id,
                        "status": "COMPLETE",
                        "accounts": pack["accounts"],
                        "captured_at": captured.isoformat(),
                    },
                    captured_at=captured,
                )
            progress["completed_accounts"] += 1
        except SoftTimeLimitExceeded:
            # 保留未完成游标；整个事务回滚，由 Beat 恢复，不能当成已完成账户。
            raise
        except Exception as error:
            # 瞬态失败最多重试三次；错误正文可能包含连接串，只记录安全类型和业务码。
            attempts = progress.get("attempts", 0) + 1
            progress["last_error"] = (
                getattr(error, "code", None) or type(error).__name__
            )
            progress["attempts"] = attempts
            if attempts < 3:
                task.evidence = {**task.evidence, "preparation": progress}
                enqueue_preparation(session, task=task, repair=True)
                session.flush()
                return
            archive_source(
                session,
                task=task,
                category="preparation",
                payload={
                    "advertiser_id": advertiser_id,
                    "status": "FAILED",
                    "reason": progress["last_error"],
                    "attempts": attempts,
                    "captured_at": captured.isoformat(),
                },
                captured_at=captured,
            )
            progress["failed_accounts"] += 1
        progress["attempts"] = 0
        progress["cursor"] += 1
    task.evidence = {**task.evidence, "preparation": progress}
    if progress["base_complete"] and progress["cursor"] == progress["total_accounts"]:
        task.status = "PENDING"
        task.evidence = {
            **task.evidence,
            "preparation": {**progress, "finished_at": datetime.now(UTC).isoformat()},
        }
    else:
        enqueue_preparation(session, task=task)
    session.flush()


def record_step_failure(session, *, tenant_id, actor_id, payload, error_code):
    """Worker 整个事务回滚后再登记重试；覆盖基础冻结失败与软超时。"""
    task = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            reviews.DSHReviewTask.tenant_id == tenant_id,
            reviews.DSHReviewTask.created_by == actor_id,
            reviews.DSHReviewTask.id == UUID(payload["task_id"]),
        )
        .with_for_update()
    ).one_or_none()
    if task is None or task.status != "PREPARING" or task.cancellation_requested:
        return
    progress = dict(task.evidence["preparation"])
    if any(
        payload.get(k) != progress[k] for k in ("cursor", "base_complete", "generation")
    ):
        return
    progress["attempts"] = progress.get("attempts", 0) + 1
    progress["last_error"] = error_code[:100]
    if progress["attempts"] >= 3:
        if not progress["base_complete"]:
            task.status = "FAILED"
            task.finished_at = datetime.now(UTC)
            task.result = {
                "summary": "基础复盘证据准备失败，请稍后重试",
                "error_code": error_code[:100],
            }
        else:
            aid = task.evidence["optimizer"]["advertiser_ids"][progress["cursor"]]
            archive_source(
                session,
                task=task,
                category="preparation",
                payload={
                    "advertiser_id": aid,
                    "status": "FAILED",
                    "reason": error_code[:100],
                    "attempts": progress["attempts"],
                },
                captured_at=datetime.now(UTC),
            )
            progress["failed_accounts"] += 1
            progress["cursor"] += 1
            progress["attempts"] = 0
            if progress["cursor"] == progress["total_accounts"]:
                task.status = "PENDING"
    task.evidence = {**task.evidence, "preparation": progress}
    if task.status == "PREPARING":
        enqueue_preparation(session, task=task, repair=True)
    session.flush()
