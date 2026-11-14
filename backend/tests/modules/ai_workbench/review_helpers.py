"""旧领域回归显式驱动生产准备步骤；HTTP 创建测试保持异步合同。"""

from app.modules.ai_workbench import reviews
from app.modules.ai_workbench.review_preparation import prepare_step
from app.modules.ai_workbench.review_store import ReviewSource, scoped


def minimal_report():
    """非展示专项用例也通过正式结构化结果合同，避免被格式错误提前拦截。"""
    return {
        "headline": "继续观察",
        "verdict": "本轮不调整规则。",
        "sections": [
            {"title": "证据限制", "body": "观察窗口不足。", "evidence_ids": []}
        ],
        "rule_decision": {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "需要完整观察窗口。",
        },
    }


def prepare_task(session, task):
    for _ in range(4 * (len(task.evidence["optimizer"]["advertiser_ids"]) + 1)):
        if task.status != "PREPARING":
            return task
        progress = task.evidence["preparation"]
        prepare_step(
            session,
            tenant_id=task.tenant_id,
            actor_id=task.created_by,
            payload={
                "task_id": str(task.id),
                **{
                    key: progress[key]
                    for key in ("cursor", "base_complete", "generation")
                },
            },
        )
    raise AssertionError("preparation did not finish")


def create_prepared_review_task(session, **kwargs):
    return prepare_task(session, reviews.create_review_task(session, **kwargs))


def source_index(session, task):
    return [
        {
            "id": str(row.id),
            "category": row.category,
            "sha256": row.digest,
            **row.source_metadata,
        }
        for row in session.exec(scoped(ReviewSource, task)).all()
    ]


def source_store(session, task):
    return {
        str(row.id): {
            "category": row.category,
            "digest": row.digest,
            "payload": row.payload,
        }
        for row in session.exec(scoped(ReviewSource, task)).all()
    }
