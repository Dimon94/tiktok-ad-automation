"""官方场景事实逐页提交，同应用链接共享冻结连接下的结果。"""

import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import array
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.scenes import ScenePage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.capabilities import (
    get_capability_evidence,
    start_capability_refresh,
)
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.models import TikTokConnection
from app.modules.tenants.permissions import require_tenant

from .image_composition_schemas import MediaKind
from .scene import (
    CLAIM_SECONDS,
    HARD_LIMIT,
    SCENE_CONTRACT_REVISION,
    _account_scope,
    _merge,
    _require_bounded_worker,
    _scope,
    read_scene_context,
)
from .scene_job_models import SceneJob, SceneJobPage
from .scene_reads import (
    SceneProgress,
    SceneRead,
    advance_scene_cursor,
    open_scene_reads,
    plan_scene_reads,
)
from .scene_schemas import CREATIVE_COMPILER_REVISION, ScenePreparation, SceneResource

TASK_NAME = "builds.refresh_scene"
REPAIR_SECONDS = 120
register_dispatch_task(TASK_NAME, "resources")
logger = logging.getLogger(__name__)


def _queue(session: Session, job: SceneJob, *, delay: int = 0) -> None:
    now = datetime.now(UTC)
    job.due_at = now + timedelta(seconds=delay)
    job.repair_after = job.due_at + timedelta(seconds=REPAIR_SECONDS)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"scene:{job.id}:{job.revision}",
        payload={"job_id": str(job.id), "revision": job.revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch
    dispatch.available_at = job.due_at
    session.add_all([job, dispatch])


def ensure_scene_preparation(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    link_id: UUID,
    route: FrozenTikTokRoute,
    selected_identity: dict[str, Any] | None = None,
    media_kind: MediaKind = "VIDEO",
) -> ScenePreparation:
    """Local enqueue/reuse only; caller commits. No SDK or credential decryption."""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    try:
        scope = _scope(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            link_id=link_id,
            route=route,
            lock=True,
            media_kind=media_kind,
        )
    except DomainError as error:
        return ScenePreparation(None, "blocked", error.code)
    now = datetime.now(UTC)
    job = session.exec(
        select(SceneJob)
        .where(
            SceneJob.tenant_id == context.tenant_id,
            SceneJob.scope_basis == scope["basis"],
        )
        .order_by(col(SceneJob.created_at).desc(), col(SceneJob.id).desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if job is not None and job.status == "PENDING":
        try:
            require_tenant(
                session, actor_id=job.actor_id, tenant_id=job.tenant_id, action="build"
            )
        except DomainError:
            job.status, job.error_code = "BLOCKED", "action_forbidden"
            session.add(job)
            session.flush()
            job = None
        else:
            return ScenePreparation(job.id, "queued")
    if (
        job is not None
        and job.status == "COMPLETE"
        and job.expires_at
        and job.expires_at > now
    ):
        if not scope["minis_id"]:
            from .mini_selection import match_catalog_link

            if match_catalog_link(session, context=context, link_id=link_id, job=job):
                return ensure_scene_preparation(
                    session,
                    context=context,
                    bc_id=bc_id,
                    advertiser_id=advertiser_id,
                    link_id=link_id,
                    route=route,
                    selected_identity=selected_identity,
                    media_kind=media_kind,
                )
            return ScenePreparation(job.id, "blocked", "minis_selection_required")
        result = read_scene_context(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            link_id=link_id,
            route=route,
            selected_identity=selected_identity,
            media_kind=media_kind,
        )
        if result.supported:
            return ScenePreparation(job.id, "ready")
        # A renewed BC proof is a dependency; it does not require repeating all
        # account asset GETs. The consumer can enqueue that proof without writes here.
        if set(result.reason_codes) <= {
            "account_scope_unverified",
            "account_build_unverified",
        }:
            proof = get_capability_evidence(
                session,
                context=context,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                connection_id=job.connection_id,
            )
            if proof is None:
                start_capability_refresh(
                    session,
                    context=context,
                    bc_id=bc_id,
                    connection_id=job.connection_id,
                    request_id=uuid4(),
                )
                return ScenePreparation(job.id, "queued", "account_scope_unverified")
        return ScenePreparation(job.id, "blocked", result.reason_codes[0])
    if job is not None and job.status in {"BLOCKED", "FAILED"}:
        # A current authorized caller can recover a job stopped by local authority
        # loss. Unknown scope, malformed evidence and exhausted reads stay terminal;
        # they need changed credentials/contracts or an explicit reviewed retry.
        restored = job.status in {"BLOCKED", "FAILED"} and job.error_code in {
            "action_forbidden",
            "tenant_forbidden",
            "connection_unavailable",
            "account_access_denied",
            "account_build_unverified",
            "scene_refresh_failed",
            "tiktok_local_resources_unavailable",
            "tiktok_call_deadline_exceeded",
        }
        proof = (
            get_capability_evidence(
                session,
                context=context,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                connection_id=job.connection_id,
            )
            if restored
            else None
        )
        if not restored or proof is not None and not proof.can_build:
            return ScenePreparation(
                job.id, "blocked", job.error_code or "scene_refresh_failed"
            )
        if job.error_code in {
            "scene_refresh_failed",
            "tiktok_local_resources_unavailable",
            "tiktok_call_deadline_exceeded",
        }:
            job.status = "PENDING"
            # 从真正失败的资源及页码恢复，不能跳到无关的历史预算查询。
            job.error_code = None
            # 显式重新准备开启新的有界重试周期，保留已保存资源及分页。
            job.failure_count = 0
            job.revision += 1
            session.add(job)
            _queue(session, job)
            return ScenePreparation(job.id, "queued")
        # Missing/expired proof is bootstrapped by the new generation before any
        # asset GET. A current explicit negative proof never triggers this retry.
    job = SceneJob(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        connection_id=scope["connection"].id,
        credential_revision=scope["connection"].credential_revision,
        frozen_route=route.model_dump(mode="json"),
        resource="capabilities" if scope["minis_id"] else "minis",
        minis_id=scope["minis_id"],
        scope_basis=scope["basis"],
        media_kind=media_kind,
        compiler_contract_revision=CREATIVE_COMPILER_REVISION,
    )
    from .mini_selection import reuse_minis_catalog

    reuse_minis_catalog(session, context=context, job=job, route=route)
    session.add(job)
    session.flush()
    if job.status == "COMPLETE":
        return ScenePreparation(job.id, "ready")
    _queue(session, job)
    return ScenePreparation(job.id, "queued")


def load_scene_route(job: SceneJob) -> FrozenTikTokRoute:
    """只读取任务自己的历史；缺失/错配路由不能补成今日默认授权。"""
    try:
        route = FrozenTikTokRoute.model_validate(job.frozen_route)
    except ValidationError:
        raise DomainError(
            "scene_route_missing", "场景缺少可核实的冻结连接，请重新准备"
        ) from None
    if (route.tenant_id, route.bc_id, route.connection_id) != (
        job.tenant_id,
        job.bc_id,
        job.connection_id,
    ):
        raise DomainError("scene_route_missing", "场景冻结连接与任务归属不一致")
    return route


def _scope_for_job(
    session: Session, context: TenantContext, job: SceneJob
) -> dict[str, Any]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    scope = _account_scope(
        session,
        context=context,
        bc_id=job.bc_id,
        advertiser_id=job.advertiser_id,
        minis_id=job.minis_id,
        route=load_scene_route(job),
        media_kind=job.media_kind,
    )
    if (
        job.compiler_contract_revision != CREATIVE_COMPILER_REVISION
        or scope["basis"] != job.scope_basis
    ):
        raise DomainError("scene_refresh_stale", "场景授权或应用已更新")
    return scope


def _lock_job(
    session: Session,
    tenant_id: UUID,
    identity: UUID,
    *,
    shared_connection: bool = False,
) -> SceneJob | None:
    found = session.exec(
        select(SceneJob).where(SceneJob.id == identity, SceneJob.tenant_id == tenant_id)
    ).first()
    if found is None:
        return None
    # 先连接后任务；纯校验/回写只共享锁定授权，让不同账户并行。
    # 领取阶段可能引导能力刷新，仍保留独占锁，避免共享锁升级死锁。
    session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == found.connection_id,
            TikTokConnection.tenant_id == tenant_id,
        )
        .with_for_update(read=shared_connection)
    ).first()
    return session.exec(
        select(SceneJob)
        .where(SceneJob.id == identity, SceneJob.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _capability_ready(session: Session, context: TenantContext, job: SceneJob) -> bool:
    proof = get_capability_evidence(
        session,
        context=context,
        bc_id=job.bc_id,
        advertiser_id=job.advertiser_id,
        connection_id=job.connection_id,
    )
    if proof is not None:
        job.capability_job_id = proof.job_id
        if not proof.scope_verified or not proof.can_build:
            job.status = "BLOCKED"
            job.error_code = (
                "account_scope_unverified"
                if not proof.scope_verified
                else "account_build_unverified"
            )
            return False
        return True
    previous = (
        session.get(CapabilityJob, job.capability_job_id)
        if job.capability_job_id
        else None
    )
    if previous is not None and previous.status in {"BLOCKED", "FAILED"}:
        if previous.status == "BLOCKED" and previous.error_code in {
            "action_forbidden",
            "tenant_forbidden",
            "connection_unavailable",
            "account_access_denied",
        }:
            # Current actor/scope was already reloaded by _scope_for_job. A shared
            # proof worker stopped by earlier local authority loss may be replaced.
            previous = None
        else:
            job.status, job.error_code = (
                "BLOCKED",
                previous.error_code or "account_scope_unverified",
            )
            return False
    if previous is None or previous.status != "PENDING":
        job.capability_job_id = start_capability_refresh(
            session,
            context=context,
            bc_id=job.bc_id,
            connection_id=job.connection_id,
            request_id=uuid4(),
        )
    return False


def _payload(payload: dict[str, Any]) -> tuple[UUID, int]:
    if (
        set(payload) != {"job_id", "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "场景准备任务参数无效")
    try:
        return UUID(payload["job_id"]), payload["revision"]
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "场景准备任务标识无效") from None


def _claimed_scene(
    session: Session, context: TenantContext, identity: UUID, token: UUID, revision: int
) -> SceneJob | None:
    current = _lock_job(session, context.tenant_id, identity, shared_connection=True)
    if (
        current is None
        or current.actor_id != context.actor_id
        or current.status != "PENDING"
        or current.revision != revision
        or current.claim_token != token
        or current.claimed_until is None
        or current.claimed_until <= datetime.now(UTC)
    ):
        return None
    _scope_for_job(session, context, current)
    proof = get_capability_evidence(
        session,
        context=context,
        bc_id=current.bc_id,
        advertiser_id=current.advertiser_id,
        connection_id=current.connection_id,
    )
    if proof is None or not proof.can_build:
        raise DomainError("account_build_unverified", "账户能力证据需要重新核实")
    if current.expires_at and current.expires_at <= datetime.now(UTC):
        raise DomainError("scene_evidence_expired", "场景证据已过期")
    return current


def _save_scene_page(
    session: Session,
    current: SceneJob,
    read: SceneRead,
    response: ScenePage,
    observed: datetime,
) -> None:
    facts = response.facts.model_dump(mode="json")
    merged = _merge(
        read.previous,
        facts,
        resource=read.resource,
        first=read.page == 1,
        last=response.last,
    )
    if read.page > 1 and facts.get("item_id_hashes"):
        repeated = session.exec(
            select(SceneJobPage.id)
            .where(
                SceneJobPage.tenant_id == current.tenant_id,
                SceneJobPage.job_id == current.id,
                SceneJobPage.resource == read.resource,
                SceneJobPage.page < read.page,
                col(SceneJobPage.facts)["item_id_hashes"].op("?|")(
                    array(facts["item_id_hashes"])
                ),
            )
            .limit(1)
        ).first()
        if repeated is not None:
            raise DomainError("scene_pagination_changed", "远端列表包含重复记录")
    session.add(
        SceneJobPage(
            tenant_id=current.tenant_id,
            job_id=current.id,
            resource=read.resource,
            page=read.page,
            endpoint=read.operation,
            request_id=response.evidence.request_id,
            mcp_request_id=response.evidence.mcp_request_id,
            remote_task_id=response.evidence.remote_task_id,
            source_revision=SCENE_CONTRACT_REVISION,
            scope_basis=current.scope_basis,
            facts=facts,
            observed_at=observed,
        )
    )
    current.facts = {**current.facts, read.resource: merged}
    # 并行返回顺序不代表采集顺序，有效期取最早观察且不能延长复用证据期限。
    current.first_observed_at = (
        min(current.first_observed_at, observed)
        if current.first_observed_at
        else observed
    )
    expires = current.first_observed_at + timedelta(
        seconds=settings.SCENE_MAX_AGE_SECONDS
    )
    current.expires_at = (
        min(current.expires_at, expires) if current.expires_at else expires
    )
    advance_scene_cursor(session, current)


def process_scene_job(
    *,
    database_engine: Any,
    redis_client: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
    task_deadline: datetime | None = None,
    max_resources: int = 1,
    turn_started: float | None = None,
) -> SceneProgress | None:
    _require_bounded_worker()
    identity, revision = _payload(payload)
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    token, now = uuid4(), datetime.now(UTC)
    started = monotonic() if turn_started is None else turn_started
    deadline = now + timedelta(seconds=HARD_LIMIT - 5)
    if task_deadline is not None:
        deadline = min(deadline, task_deadline)
    with (
        bounded_session(database_engine, task_deadline=deadline) as session,
        session.begin(),
    ):
        job = _lock_job(session, tenant_id, identity)
        if job is None or job.actor_id != actor_id:
            raise DomainError("resource_not_found", "当前场景准备任务不存在")
        if (
            job.status != "PENDING"
            or job.revision != revision
            or job.due_at > now
            or job.claimed_until
            and job.claimed_until > now
        ):
            return None
        try:
            _scope_for_job(session, context, job)
            if not _capability_ready(session, context, job):
                if job.status == "PENDING":
                    job.revision += 1
                    _queue(session, job, delay=5)
                return None
            if job.expires_at and job.expires_at <= now:
                job.status, job.error_code = "STALE", "scene_evidence_expired"
                return None
            advance_scene_cursor(session, job)
            reads = plan_scene_reads(session, job, limit=max_resources)
            for read in reads:
                if admission_policy(read.operation).lease_ms <= (HARD_LIMIT + 5) * 1000:
                    raise DomainError(
                        "admission_policy_invalid", "场景调用租约短于工作进程硬限"
                    )
            route = load_scene_route(job)
        except DomainError as error:
            job.status = (
                "STALE"
                if error.code
                in {
                    "scene_refresh_stale",
                    "scene_route_missing",
                    "route_authorization_changed",
                    "route_contract_changed",
                }
                else "BLOCKED"
            )
            job.error_code = error.code
            return None
        job.claim_token, job.claimed_until = (
            token,
            now + timedelta(seconds=CLAIM_SECONDS),
        )
        advertiser_id, minis_id = job.advertiser_id, job.minis_id

    def before_request() -> None:
        # 每个线程只在短事务中核验同一全局 claim，不持锁等待 HTTP。
        with (
            bounded_session(database_engine, task_deadline=deadline) as session,
            session.begin(),
        ):
            if _claimed_scene(session, context, identity, token, revision) is None:
                raise DomainError("scene_refresh_stale", "场景任务领取已失效")

    resource: SceneResource = reads[0].resource if reads else "identity"
    read_count = 0
    errors: list[tuple[SceneResource, Exception]] = []
    try:
        with open_scene_reads(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            advertiser_id=advertiser_id,
            minis_id=minis_id,
            deadline=deadline,
            before_request=before_request,
            reads=reads,
            can_start=lambda: (
                monotonic() - started < 8.0
                and (deadline - datetime.now(UTC)).total_seconds() >= 5
            ),
        ) as outcomes:
            for outcome in outcomes:
                read_count += 1
                read, response = outcome.read, outcome.result
                resource = read.resource
                if isinstance(response, Exception):
                    errors.append((resource, response))
                    continue
                with (
                    bounded_session(database_engine, task_deadline=deadline) as session,
                    session.begin(),
                ):
                    current = _claimed_scene(
                        session, context, identity, token, revision
                    )
                    if current is None:
                        return None
                    try:
                        _save_scene_page(
                            session, current, read, response, outcome.observed_at
                        )
                    except DomainError as error:
                        # 单页分页证据不完整不抹掉其它独立资源已经提交的成功页。
                        errors.append((resource, error))
        with (
            bounded_session(database_engine, task_deadline=deadline) as session,
            session.begin(),
        ):
            current = _claimed_scene(session, context, identity, token, revision)
            if current is None:
                return None
            if errors:
                resource, first_error = errors[0]
                raise first_error
            advance_scene_cursor(session, current)
            current.claim_token, current.claimed_until, current.error_code = (
                None,
                None,
                None,
            )
            current.failure_count = 0
            current.revision += 1
            if current.resource == "done":
                current.status, current.completed_at = "COMPLETE", datetime.now(UTC)
            else:
                _queue(session, current)
                return SceneProgress(current.revision, read_count)
    except Exception as error:
        if not isinstance(error, AccountAdmissionDeferred):
            logger.exception("scene refresh failed for resource=%s", resource)
        code = error.code if isinstance(error, DomainError) else "scene_refresh_failed"
        allowed = {
            "scene_response_unverified",
            "scene_pagination_changed",
            "scene_refresh_stale",
            "scene_evidence_expired",
            "action_forbidden",
            "tenant_forbidden",
            "account_access_denied",
            "account_build_unverified",
            "connection_unavailable",
            "scene_link_unavailable",
            "admission_deferred",
            "admission_unavailable",
            "scene_refresh_failed",
            "scene_route_missing",
            "route_authorization_changed",
            "route_contract_changed",
            "connection_bc_mismatch",
            "connection_channel_mismatch",
            "route_evidence_stale",
            "mcp_refresh_pending",
            "mcp_refresh_unknown",
            "mcp_refresh_reauth_required",
            "tiktok_call_deadline_exceeded",
            "tiktok_local_resources_unavailable",
        }
        safe = code if code in allowed else "scene_refresh_failed"
        with (
            bounded_session(database_engine, task_deadline=deadline) as session,
            session.begin(),
        ):
            current = _lock_job(session, tenant_id, identity)
            if (
                current is None
                or current.revision != revision
                or current.claim_token != token
            ):
                return None
            current.claim_token, current.claimed_until = None, None
            current.error_code = safe
            current.revision += 1
            if isinstance(error, AccountAdmissionDeferred):
                # 并发准入排队不代表远端失败，不能耗尽重试次数；释放 Worker 槽位。
                _queue(
                    session,
                    current,
                    delay=max(1, min(60, (error.retry_after_ms + 999) // 1000)),
                )
                return None
            current.failure_count += 1
            if safe in {
                "scene_refresh_stale",
                "scene_evidence_expired",
                "scene_route_missing",
                "route_authorization_changed",
                "route_contract_changed",
            }:
                current.status = "STALE"
            elif safe in {"scene_response_unverified", "scene_pagination_changed"}:
                current.status = "FAILED"
            elif safe not in {
                "admission_deferred",
                "admission_unavailable",
                "scene_refresh_failed",
                "mcp_refresh_pending",
                "tiktok_local_resources_unavailable",
                "tiktok_call_deadline_exceeded",
            }:
                current.status = "BLOCKED"
            elif current.failure_count >= 5:
                current.status = "FAILED"
            else:
                _queue(session, current, delay=min(60, 2**current.failure_count))
    return None


def process_scene_turn(
    *,
    database_engine: Any,
    redis_client: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    """独立资源并行，分页依赖串行；整轮仍受五页、八秒启动及四十秒期限约束。"""
    started = monotonic()
    deadline = datetime.now(UTC) + timedelta(seconds=HARD_LIMIT - 5)
    current_payload = payload
    remaining = 5
    while remaining > 0:
        progress = process_scene_job(
            database_engine=database_engine,
            redis_client=redis_client,
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload=current_payload,
            task_deadline=deadline,
            max_resources=remaining,
            turn_started=started,
        )
        if (
            progress is None
            or progress.pages == 0
            or monotonic() - started >= 8.0
            or (deadline - datetime.now(UTC)).total_seconds() < 5
        ):
            return
        remaining -= progress.pages
        current_payload = {"job_id": payload["job_id"], "revision": progress.revision}


def repair_scene_jobs(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair batch must be between 1 and 100")
    now, count = datetime.now(UTC), 0
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(SceneJob)
            .where(SceneJob.status == "PENDING", SceneJob.repair_after <= now)
            .order_by(col(SceneJob.repair_after), col(SceneJob.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for job in rows:
            if job.claimed_until and job.claimed_until > now:
                job.repair_after = job.claimed_until + timedelta(seconds=REPAIR_SECONDS)
                continue
            dispatch = session.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == job.dispatch_id)
                .with_for_update()
            ).first()
            if dispatch is None:
                _queue(session, job)
            elif (
                dispatch.tenant_id != job.tenant_id
                or dispatch.actor_id != job.actor_id
                or dispatch.task_name != TASK_NAME
                or dispatch.task_key != f"scene:{job.id}:{job.revision}"
                or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
            ):
                job.status, job.error_code = "BLOCKED", "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = (
                    None,
                    max(now, job.due_at),
                )
            # Unpublished dispatches retain the broker's original identity/backoff.
            job.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            count += 1
    return count
