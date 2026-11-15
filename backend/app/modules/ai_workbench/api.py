"""HTTP contracts for tenant AI optimizer configuration."""

from datetime import date, datetime
from typing import cast as typing_cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.core.context import TenantContext
from app.modules.ai_workbench import (
    daily_report,
    plans_service,
    proposals,
    reviews,
    service,
)
from app.modules.ai_workbench.build_handoff import BuildHandoff
from app.modules.ai_workbench.models import BuildOccurrence
from app.modules.ai_workbench.plan_schemas import (
    DailyPlanPage,
    DailyPlanPublic,
    DailyPlanRequest,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.ai_workbench.schemas import (
    OptimizerCreate,
    OptimizerPage,
    OptimizerPublic,
    OptimizerUpdate,
)

# 灰度阶段仅平台管理员开放，覆盖全部入口与知识 API，不能仅隐藏菜单。
router = APIRouter(
    prefix="/tenants/{tenant_id}",
    tags=["ai_workbench"],
    dependencies=[Depends(get_current_active_superuser)],
)


class RunPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    strategy_version_id: UUID | None = None


class ReviewResponse(BaseModel):
    task: dict
    proposals: list[dict]


def _context(user: CurrentUser, tenant_id: UUID) -> TenantContext:
    # 权限由 service 每次读写重新从租户成员关系核验；这里不信任客户端角色。
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role="")


@router.get("/ai-optimizers", response_model=OptimizerPage)
def list_optimizers(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> OptimizerPage:
    return service.list_optimizers(session, context=_context(user, tenant_id))


@router.post("/ai-optimizers", response_model=OptimizerPublic, status_code=201)
def create_optimizer(
    tenant_id: UUID,
    body: OptimizerCreate,
    session: SessionDep,
    user: CurrentUser,
) -> OptimizerPublic:
    result = service.create_optimizer(
        session, context=_context(user, tenant_id), body=body
    )
    session.commit()
    return result


@router.get("/ai-optimizers/{optimizer_id}", response_model=OptimizerPublic)
def get_optimizer(
    tenant_id: UUID, optimizer_id: UUID, session: SessionDep, user: CurrentUser
) -> OptimizerPublic:
    return service.get_optimizer(
        session, context=_context(user, tenant_id), optimizer_id=optimizer_id
    )


@router.patch("/ai-optimizers/{optimizer_id}", response_model=OptimizerPublic)
def update_optimizer(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: OptimizerUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> OptimizerPublic:
    result = service.update_optimizer(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        body=body,
    )
    session.commit()
    return result


@router.get(
    "/ai-optimizers/{optimizer_id}/daily-plans",
    response_model=DailyPlanPublic | DailyPlanPage,
)
def get_daily_plans(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    business_date: date | None = Query(default=None),
) -> DailyPlanPublic | DailyPlanPage:
    context = _context(user, tenant_id)
    if business_date is None:
        return plans_service.list_daily_plans(
            session, context=context, optimizer_id=optimizer_id
        )
    return plans_service.get_daily_plan(
        session, context=context, optimizer_id=optimizer_id, business_date=business_date
    )


@router.post(
    "/ai-optimizers/{optimizer_id}/daily-plans",
    response_model=DailyPlanPublic,
    status_code=201,
)
def create_daily_plan(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: DailyPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DailyPlanPublic:
    result = plans_service.create_daily_plan(
        session, context=_context(user, tenant_id), optimizer_id=optimizer_id, body=body
    )
    session.commit()
    return result


@router.patch(
    "/ai-optimizers/{optimizer_id}/daily-plans",
    response_model=DailyPlanPublic,
)
def update_daily_plan(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: DailyPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DailyPlanPublic:
    result = plans_service.update_daily_plan(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        body=body,
    )
    session.commit()
    return result


@router.post(
    "/ai-optimizers/{optimizer_id}/plans/{plan_id}/run",
    response_model=BuildOccurrence,
    status_code=202,
)
def run_daily_plan_row(
    tenant_id: UUID,
    optimizer_id: UUID,
    plan_id: UUID,
    body: RunPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> BuildOccurrence:
    """Create a dry local handoff; the durable outbox performs the build later."""
    context = _context(user, tenant_id)
    # The row itself carries optimizer ownership.  Resolve and check the path
    # optimizer before any outbox write so a copied URL cannot cross scopes.
    row = session.exec(
        select(AIDailyPlanRow).where(
            col(AIDailyPlanRow.tenant_id) == tenant_id,
            col(AIDailyPlanRow.id) == plan_id,
            col(AIDailyPlanRow.optimizer_id) == optimizer_id,
        )
    ).one_or_none()
    if row is None:
        from app.core.errors import DomainError

        raise DomainError("ai_daily_plan_row_not_found", "ai_daily_plan_row_not_found")
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        plan_id,
        body.request_id,
        strategy_version_id=body.strategy_version_id,
    )
    session.commit()
    return occurrence


@router.get("/ai-optimizers/{optimizer_id}/occurrences", response_model=list[dict])
def list_occurrences(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Return tenant/optimizer-scoped handoff and readback evidence for the workbench."""
    context = _context(user, tenant_id)
    service.get_optimizer(session, context=context, optimizer_id=optimizer_id)
    statement = (
        select(BuildOccurrence)
        .where(
            col(BuildOccurrence.tenant_id) == tenant_id,
            col(BuildOccurrence.optimizer_id) == optimizer_id,
        )
        .order_by(
            col(BuildOccurrence.scheduled_at).desc(), col(BuildOccurrence.id).desc()
        )
        .limit(limit)
    )
    if status:
        statement = statement.where(col(BuildOccurrence.status) == status)
    rows = session.exec(statement).all()
    return [row.model_dump(mode="json") for row in rows]


@router.get("/ai-optimizers/{optimizer_id}/daily-report")
def get_daily_report(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    business_date: date | None = Query(default=None),
) -> dict:
    """Return the read-only daily summary built from frozen workbench evidence."""
    if business_date is None:
        optimizer = service.get_optimizer(
            session, context=_context(user, tenant_id), optimizer_id=optimizer_id
        )
        business_date = datetime.now(ZoneInfo(optimizer.timezone)).date()
    return daily_report.build_daily_report(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        business_date=business_date,
    )


@router.post(
    "/ai-optimizers/{optimizer_id}/reviews",
    response_model=ReviewResponse,
    status_code=201,
)
def create_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: proposals.ReviewRequest,
    session: SessionDep,
    user: CurrentUser,
) -> ReviewResponse:
    context = _context(user, tenant_id)
    task = reviews.create_review_task(
        session,
        context=context,
        optimizer_id=optimizer_id,
        request_id=body.request_id,
        evidence_ids=body.evidence_ids,
        knowledge_refs=body.knowledge_refs,
        authorization_ttl_seconds=body.authorization_ttl_seconds,
        dsh_task_id=body.dsh_task_id,
        request_digest=__import__("hashlib")
        .sha256(
            __import__("json")
            .dumps(body.model_dump(mode="json", exclude={"request_id"}), sort_keys=True)
            .encode()
        )
        .hexdigest(),
    )
    rows: list[proposals.AIProposal] = []
    for index, change in enumerate(body.proposals):
        # One review request may contain several changes; derive stable child
        # request IDs so retries cannot create duplicate proposals.
        child_request = UUID(int=(body.request_id.int + index + 1) % (1 << 128))
        rows.append(
            proposals.create_proposal(
                session,
                context=context,
                optimizer_id=optimizer_id,
                review_task=task,
                change=change,
                request_id=child_request,
            )
        )
    proposals.publish_auto_proposals(
        session,
        context=context,
        optimizer_id=optimizer_id,
        review_task_id=task.id,
    )
    session.commit()
    return ReviewResponse(
        task={
            "id": task.id,
            "tenant_id": task.tenant_id,
            "optimizer_id": task.optimizer_id,
            "dsh_task_id": task.dsh_task_id,
            "status": task.status,
            "fence": task.fence,
            "restart_generation": task.restart_generation,
            "optimizer_version": task.optimizer_version,
            "authorization_expires_at": task.authorization_expires_at,
            "evidence": task.evidence,
            "result": task.result,
        },
        proposals=[proposals._public(row) for row in rows],
    )


@router.get("/ai-optimizers/{optimizer_id}/proposals")
def get_proposals(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    status: str | None = Query(default=None),
) -> list[dict]:
    return proposals.list_proposals(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        status=status,
    )


@router.post("/ai-optimizers/{optimizer_id}/proposals/{proposal_id}/accept")
def accept_proposal(
    tenant_id: UUID,
    optimizer_id: UUID,
    proposal_id: UUID,
    _body: proposals.DecisionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    row = proposals.accept_proposal(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        proposal_id=proposal_id,
        request_id=_body.request_id,
    )
    session.commit()
    return proposals._public(row)


@router.post("/ai-optimizers/{optimizer_id}/proposals/{proposal_id}/reject")
def reject_proposal(
    tenant_id: UUID,
    optimizer_id: UUID,
    proposal_id: UUID,
    body: proposals.DecisionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    row = proposals.reject_proposal(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        proposal_id=proposal_id,
        reason=body.reason,
        request_id=body.request_id,
    )
    session.commit()
    return proposals._public(row)


@router.get("/ai-optimizers/{optimizer_id}/reviews")
def list_reviews(
    tenant_id: UUID, optimizer_id: UUID, session: SessionDep, user: CurrentUser
) -> dict:
    from app.core.config import settings

    service.get_optimizer(
        session, context=_context(user, tenant_id), optimizer_id=optimizer_id
    )
    tasks = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            col(reviews.DSHReviewTask.tenant_id) == tenant_id,
            col(reviews.DSHReviewTask.optimizer_id) == optimizer_id,
        )
        .order_by(col(reviews.DSHReviewTask.created_at).desc())
        .limit(50)
    ).all()
    return {
        "runtime_ready": bool(settings.AI_DSH_SERVICE_TOKEN),
        "items": [
            {
                "id": task.id,
                "status": task.status,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "optimizer_version": task.optimizer_version,
                "result": task.result,
            }
            for task in tasks
        ],
    }


class ReviewControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/ai-optimizers/{optimizer_id}/reviews/{task_id}/cancel")
def cancel_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    _body: ReviewControlRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    from app.modules.ai_workbench import ai_tasks

    context = _context(user, tenant_id)
    task = reviews.get_review_task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id
    )
    task = ai_tasks.cancel_task(
        session,
        context=context,
        optimizer_id=optimizer_id,
        task_id=task_id,
        fence=task.fence,
    )
    session.commit()
    return {"id": task.id, "status": task.status}


@router.post("/ai-optimizers/{optimizer_id}/reviews/{task_id}/retry")
def retry_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    _body: ReviewControlRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    from app.modules.ai_workbench import ai_tasks

    context = _context(user, tenant_id)
    task = ai_tasks.restart_task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id
    )
    session.commit()
    return {"id": task.id, "status": task.status}


@router.get("/ai-optimizers/{optimizer_id}/reviews/{task_id}")
def get_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    task = reviews.get_review_task(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        task_id=task_id,
    )
    return {
        "id": task.id,
        "status": task.status,
        "result": task.result,
        "error_message": task.result.get("summary")
        if task.status == "FAILED"
        else None,
    }


@router.get("/ai-material-options")
def material_options(tenant_id: UUID, session: SessionDep, user: CurrentUser) -> dict:
    from app.core.errors import DomainError
    from app.modules.ai_workbench.orders import company_client

    try:
        client = company_client(session, context=_context(user, tenant_id))
    except DomainError as error:
        if error.code == "material_production_not_ready":
            return {"ready": False, "strategies": []}
        raise
    catalog = client.request("GET", "/api/strategy-catalog")
    return {
        "ready": True,
        "strategies": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "enabled": item.get("enabled") is True,
            }
            for item in catalog.get("strategies", [])
            if isinstance(item, dict)
        ],
    }


# 知识资产独立于优化师：公共版本跨租户复用，私有版本受成员权限约束。
from pydantic import Field  # noqa: E402
from sqlalchemy import or_  # noqa: E402
from sqlmodel import col  # noqa: E402

from app.modules.ai_workbench.knowledge import (  # noqa: E402
    KnowledgeInput,
    KnowledgeVersion,
    create_version,
    latest_query,
    promote_public,
)
from app.modules.tenants.permissions import require_tenant  # noqa: E402


@router.get("/ai-knowledge")
def list_ai_knowledge(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: str | None = None,
    key: str | None = Query(default=None, min_length=1, max_length=100),
) -> list[dict]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    query = latest_query().where(
        or_(
            col(KnowledgeVersion.scope) == "PUBLIC",
            col(KnowledgeVersion.tenant_id) == tenant_id,
        )
    )
    if bc_id:
        query = query.where(
            or_(
                col(KnowledgeVersion.bc_id).is_(None),
                col(KnowledgeVersion.bc_id) == bc_id,
            )
        )
    if key is not None:
        query = query.where(col(KnowledgeVersion.key) == key)
    rows = session.exec(
        query.order_by(col(KnowledgeVersion.created_at).desc()).limit(100)
    ).all()
    # 不返回公共知识的创建者/审核原文等内部身份信息。
    return [
        r.model_dump(mode="json", exclude={"created_by", "sharing_review", "tenant_id"})
        for r in rows
    ]


@router.post("/ai-knowledge", status_code=201)
def create_ai_knowledge(
    tenant_id: UUID, body: KnowledgeInput, session: SessionDep, user: CurrentUser
) -> dict:
    row = create_version(session, context=_context(user, tenant_id), body=body)
    session.commit()
    return {"id": str(row.id), "asset_id": str(row.asset_id), "version": row.version}


@router.post("/ai-knowledge/{asset_id}/versions", status_code=201)
def revise_ai_knowledge(
    tenant_id: UUID,
    asset_id: UUID,
    body: KnowledgeInput,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    row = create_version(
        session, context=_context(user, tenant_id), body=body, asset_id=asset_id
    )
    session.commit()
    return {"id": str(row.id), "asset_id": str(row.asset_id), "version": row.version}


class PublicPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=12000)
    review: str = Field(min_length=1, max_length=2000)


@router.post("/ai-knowledge/{version_id}/promote", status_code=201)
def share_ai_knowledge(
    tenant_id: UUID,
    version_id: UUID,
    body: PublicPromotion,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    row = promote_public(
        session,
        context=_context(user, tenant_id),
        version_id=version_id,
        body=body.body,
        review=body.review,
    )
    session.commit()
    return {"id": str(row.id), "asset_id": str(row.asset_id), "version": row.version}


class CandidateValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    target_id: UUID | None = None
    after: dict


@router.post("/ai-optimizers/{optimizer_id}/validate-proposal")
def validate_ai_proposal(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: CandidateValidation,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    from app.modules.ai_workbench.capabilities import validate_candidate

    return validate_candidate(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        kind=body.kind,
        target_id=body.target_id,
        after=body.after,
    )


class ConfirmExperience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_result: str = Field(min_length=1, max_length=2000)


@router.post(
    "/ai-optimizers/{optimizer_id}/reviews/{task_id}/experiences/{index}/confirm",
    status_code=201,
)
def confirm_ai_experience(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    index: int,
    body: ConfirmExperience,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    from app.core.errors import DomainError

    context = _context(user, tenant_id)
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="manage")
    task = reviews.get_review_task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id
    )
    candidates = task.result.get("experience_candidates", [])
    if task.status != "COMPLETE" or index < 0 or index >= len(candidates):
        raise DomainError("ai_experience_not_found", "没有可确认的经验候选")
    candidate = candidates[index]
    # 稳定标识使重复点击幂等；只进入原租户/原 BC，不能借确认升级成公共经验。
    key = f"review-{task_id}-{index}"
    # 同一候选确认串行，防止并发请求各自创建随机 asset_id 的重复知识。
    from sqlalchemy import text

    typing_cast(SQLAlchemySession, session).execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"ai-experience:{tenant_id}:{key}"},
    )
    existing = session.exec(
        latest_query().where(
            col(KnowledgeVersion.tenant_id) == tenant_id,
            col(KnowledgeVersion.key) == key,
        )
    ).first()
    if existing:
        return {
            "id": str(existing.id),
            "asset_id": str(existing.asset_id),
            "version": existing.version,
        }
    row = create_version(
        session,
        context=context,
        body=KnowledgeInput(
            key=key,
            scope="BC",
            bc_id=task.evidence["optimizer"]["bc_id"],
            title=candidate["title"],
            body=candidate["body"] + "\n人工验证：" + body.validation_result,
            source=f"review:{task_id}",
            source_verified=True,
            applicability=candidate["applicability"],
            evidence_ids=candidate["evidence_ids"],
        ),
    )
    session.commit()
    return {"id": str(row.id), "asset_id": str(row.asset_id), "version": row.version}
