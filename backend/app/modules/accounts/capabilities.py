"""Durable, link-free BC role evidence. Each delivery reads 50 or publishes 100.

Public start owns no commit; workers close all DB transactions before SDK I/O.
Only complete distinct remote lists can establish capabilities. Local grants are
never invented from BC visibility or another connection's token.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.accounts import AuthorizationFacts
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import OPERABLE_REMOTE_STATUSES, usable_grants
from app.modules.accounts.capability_models import (
    CapabilityAsset,
    CapabilityJob,
    CapabilityPage,
    CapabilityRequest,
)
from app.modules.accounts.capability_schemas import CapabilityEvidence
from app.modules.accounts.directory_models import DirectoryRevision
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.tenants.permissions import require_tenant

TASK_NAME = "accounts.refresh_capabilities"
ENDPOINT = "/open_api/v1.3/bc/asset/get/"
REVISION = "dual-channel-authorization-roles-2026-09-12-v3"
HARD_LIMIT = 45
CLAIM_SECONDS = 60
REPAIR_SECONDS = 120
register_dispatch_task(TASK_NAME, "resources")


def _require_bounded_worker() -> None:
    process = current_process()
    request = getattr(current_task, "request", None)
    limits = getattr(request, "timelimit", None)
    hard = limits[0] if isinstance(limits, (tuple, list)) and limits else None
    if hard is None:
        hard = getattr(current_task, "time_limit", None)
    if (
        not request
        or getattr(request, "called_directly", True)
        or getattr(request, "is_eager", True)
        or not process.daemon
        or not process.name.startswith("ForkPoolWorker-")
        or isinstance(hard, bool)
        or not isinstance(hard, (int, float))
        or not 0 < hard <= HARD_LIMIT
    ):
        raise DomainError("capability_worker_unbounded", "能力重检需要有界后台任务")


def _connection(
    session: Session,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID,
    *,
    lock: bool,
) -> TikTokConnection:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    # One lock order everywhere: connection, job, dispatch/grants. No lock survives I/O.
    statement = select(TikTokConnection).where(
        TikTokConnection.tenant_id == context.tenant_id,
        TikTokConnection.id == connection_id,
    )
    if lock:
        statement = statement.with_for_update()
    conn = session.exec(statement.execution_options(populate_existing=True)).first()
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if conn is None or conn.status != "ACTIVE" or not conn.credential_ciphertext:
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    if bc is None or bc.ownership_conflict:
        raise DomainError("account_not_in_bc", "当前租户 BC 不可用")
    if (
        session.exec(
            usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
            .where(BCAccountAccess.connection_id == connection_id)
            .limit(1)
        ).first()
        is None
    ):
        raise DomainError(
            "capability_unavailable", "当前连接在此 BC 没有可核实的授权账户"
        )
    return conn


def _directory_basis(
    session: Session, context: TenantContext, bc_id: str, connection_id: UUID
) -> str | None:
    # Triggers fence every directory mutation in its own transaction. A pure
    # primary-key read replaces the former all-account aggregate for each scene.
    # Missing state fails closed; readers never lazily create or repair a fence.
    with session.no_autoflush:
        value = session.exec(
            select(DirectoryRevision.revision).where(
                DirectoryRevision.tenant_id == context.tenant_id,
                DirectoryRevision.bc_id == bc_id,
                DirectoryRevision.connection_id == connection_id,
            )
        ).first()
    if value is None:
        return None
    return sha256(
        json.dumps(
            [
                REVISION,
                settings.BC_CAPABILITY_MAX_AGE_SECONDS,
                str(context.tenant_id),
                bc_id,
                str(connection_id),
                value,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _scope_flags(facts: AuthorizationFacts) -> tuple[bool, bool, bool]:
    # 业务层只消费适配器授权事实，不解密 token，也不把角色/工具存在当作 scope。
    age = (datetime.now(UTC) - facts.observed_at).total_seconds()
    known = (
        age >= 0
        and facts.evidence_source != "UNKNOWN"
        # 上传与搭建分别核实，不能因为另一项尚未知而永久封锁已接入的动作。
        and (facts.upload_authorized is not None or facts.build_authorized is not None)
    )
    return (
        known,
        known and facts.build_authorized is True,
        known and facts.upload_authorized is True,
    )


def _route(job: CapabilityJob) -> FrozenTikTokRoute:
    if (
        job.channel not in {"OFFICIAL_API", "OFFICIAL_MCP"}
        or job.authorization_revision is None
        or not job.adapter_contract_revision
    ):
        raise DomainError("capability_stale", "历史任务缺少冻结授权依据，请重新重检")
    return FrozenTikTokRoute(
        tenant_id=job.tenant_id,
        bc_id=job.bc_id,
        connection_id=job.connection_id,
        channel="OFFICIAL_MCP" if job.channel == "OFFICIAL_MCP" else "OFFICIAL_API",
        authorization_revision=job.authorization_revision,
        binding_revision=job.binding_revision,
        adapter_contract_revision=job.adapter_contract_revision,
    )


def _queue(session: Session, job: CapabilityJob, *, delay: int = 0) -> None:
    now = datetime.now(UTC)
    job.due_at = now + timedelta(seconds=delay)
    job.repair_after = job.due_at + timedelta(seconds=REPAIR_SECONDS)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"capability:{job.id}:{job.revision}",
        payload={"job_id": str(job.id), "revision": job.revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch is not None
    dispatch.available_at = job.due_at
    session.add_all([job, dispatch])


def start_capability_refresh(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID,
    request_id: UUID,
) -> UUID:
    conn = _connection(session, context, bc_id, connection_id, lock=True)
    route = freeze_route(
        session, context=context, bc_id=bc_id, connection_id=connection_id
    )
    previous = session.get(CapabilityRequest, (context.tenant_id, bc_id, request_id))
    if previous:
        original = session.get(CapabilityJob, previous.job_id)
        assert original
        if original.connection_id != connection_id:
            raise DomainError("request_id_conflict", "同一请求不能更换授权连接")
        return original.id
    basis = _directory_basis(session, context, bc_id, connection_id)
    if basis is None:
        raise DomainError("capability_unavailable", "当前账户目录缺少可核实版本")
    from app.modules.accounts.runtime_directory import needs_directory_refresh

    current_facts_fresh = not needs_directory_refresh(session, route)
    existing = session.exec(
        select(CapabilityJob)
        .where(
            CapabilityJob.tenant_id == context.tenant_id,
            CapabilityJob.bc_id == bc_id,
            CapabilityJob.connection_id == connection_id,
            CapabilityJob.authorization_revision == conn.authorization_revision,
            CapabilityJob.binding_revision == route.binding_revision,
            CapabilityJob.channel == conn.kind,
            CapabilityJob.adapter_contract_revision == conn.adapter_contract_revision,
            CapabilityJob.directory_basis == basis,
            (col(CapabilityJob.status) == "PENDING")
            | (
                (col(CapabilityJob.status) == "COMPLETE")
                & (col(CapabilityJob.expires_at) > datetime.now(UTC))
                & current_facts_fresh
            ),
        )
        .order_by(col(CapabilityJob.created_at).desc())
        .limit(1)
    ).first()
    try:
        with session.begin_nested():
            if existing is None:
                existing = CapabilityJob(
                    tenant_id=context.tenant_id,
                    bc_id=bc_id,
                    connection_id=connection_id,
                    actor_id=context.actor_id,
                    credential_revision=conn.credential_revision,
                    channel=route.channel,
                    authorization_revision=route.authorization_revision,
                    binding_revision=route.binding_revision,
                    adapter_contract_revision=route.adapter_contract_revision,
                    directory_basis=basis,
                )
                session.add(existing)
                session.flush()
                _queue(session, existing)
            session.add(
                CapabilityRequest(
                    tenant_id=context.tenant_id,
                    bc_id=bc_id,
                    request_id=request_id,
                    job_id=existing.id,
                )
            )
            session.flush()
    except IntegrityError:
        # Competing connections can race the same request UUID. Roll back the
        # entire candidate job/outbox savepoint, then return the exact winner.
        previous = session.get(
            CapabilityRequest,
            (context.tenant_id, bc_id, request_id),
            populate_existing=True,
        )
        if previous is None:
            raise
        original = session.get(CapabilityJob, previous.job_id)
        assert original
        if original.connection_id != connection_id:
            raise DomainError(
                "request_id_conflict", "同一请求不能更换授权连接"
            ) from None
        return original.id

    return existing.id


def get_capability_job(
    session: Session, *, context: TenantContext, bc_id: str, job_id: UUID
) -> CapabilityJob:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    job = session.exec(
        select(CapabilityJob)
        .where(
            CapabilityJob.tenant_id == context.tenant_id,
            CapabilityJob.bc_id == bc_id,
            CapabilityJob.id == job_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise DomainError("capability_not_found", "当前租户 BC 能力任务不存在")
    return job


def _locked_job(
    session: Session, tenant_id: UUID, job_id: UUID
) -> CapabilityJob | None:
    # Load identity without locks before taking the consistent parent-first locks.
    identity = session.exec(
        select(CapabilityJob.connection_id).where(
            CapabilityJob.tenant_id == tenant_id, CapabilityJob.id == job_id
        )
    ).first()
    if identity is None:
        return None
    session.exec(
        select(TikTokConnection)
        .where(TikTokConnection.tenant_id == tenant_id, TikTokConnection.id == identity)
        .with_for_update()
    ).first()
    return session.exec(
        select(CapabilityJob)
        .where(CapabilityJob.tenant_id == tenant_id, CapabilityJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _validate(
    session: Session,
    job: CapabilityJob,
    context: TenantContext,
    *,
    allow_expired_roles: bool = False,
) -> TikTokConnection:
    if (
        job.status != "COMPLETE"
        and not allow_expired_roles
        and job.expires_at is not None
        and job.expires_at <= datetime.now(UTC)
    ):
        raise DomainError("capability_stale", "账户角色证据已过期，需要重新重检")
    conn = _connection(session, context, job.bc_id, job.connection_id, lock=False)
    verify_route(
        session,
        context=context,
        route=_route(job),
        advertiser_id=None,
        capability="read",
    )
    if (
        _directory_basis(session, context, job.bc_id, job.connection_id)
        != job.directory_basis
    ):
        raise DomainError("capability_stale", "授权或账户目录已改变，需要重新重检")
    return conn


def _restrict_permissions(
    session: Session,
    *,
    job: CapabilityJob,
    facts: AuthorizationFacts,
    rows: Sequence[tuple[str, str | None]],
) -> None:
    # 读取尚未完整时只能单调收紧，绝不提前授予、改变目录成员或借用另一连接。
    known, build, upload = _scope_flags(facts)
    scope = update(BCAccountAccess).where(
        col(BCAccountAccess.tenant_id) == job.tenant_id,
        col(BCAccountAccess.bc_id) == job.bc_id,
        col(BCAccountAccess.connection_id) == job.connection_id,
    )
    changes: dict[str, Any] = {}
    if not build:
        changes["can_build"] = False
    if not upload:
        changes["can_upload"] = False
    if not known:
        # UNKNOWN 是本地缺少证明，不能记录成提供方明确撤权。
        changes["permission_state"] = "UNKNOWN"
    if changes:
        SASession.execute(
            session,
            scope.where(
                or_(
                    col(BCAccountAccess.can_build).is_(True),
                    col(BCAccountAccess.can_upload).is_(True),
                )
            ).values(**changes),
        )
    restricted = [identity for identity, role in rows if role == "ANALYST"]
    if restricted:
        SASession.execute(
            session,
            scope.where(col(BCAccountAccess.advertiser_id).in_(restricted)).values(
                can_build=False, can_upload=False
            ),
        )


def _publish(session: Session, job: CapabilityJob, context: TenantContext) -> None:
    now = datetime.now(UTC)
    if job.expires_at is None or job.expires_at <= now:
        raise DomainError("capability_stale", "完整账户角色证据已过期")
    # Join exact connection-local grants and their complete role facts. No N+1 lookup.
    statement = (
        select(BCAccountAccess, AdvertiserAccount, CapabilityAsset.role)
        .join(
            AdvertiserAccount,
            (col(AdvertiserAccount.tenant_id) == BCAccountAccess.tenant_id)
            & (col(AdvertiserAccount.advertiser_id) == BCAccountAccess.advertiser_id),
        )
        .outerjoin(
            CapabilityAsset,
            (col(CapabilityAsset.job_id) == job.id)
            & (col(CapabilityAsset.advertiser_id) == BCAccountAccess.advertiser_id),
        )
        .where(
            BCAccountAccess.tenant_id == context.tenant_id,
            BCAccountAccess.bc_id == job.bc_id,
            BCAccountAccess.connection_id == job.connection_id,
        )
    )
    if job.publish_after is not None:
        statement = statement.where(BCAccountAccess.advertiser_id > job.publish_after)
    rows = session.exec(
        statement.order_by(col(BCAccountAccess.advertiser_id))
        .limit(100)
        .with_for_update(of=BCAccountAccess)
        .execution_options(populate_existing=True)
    ).all()
    for grant, account, role in rows:
        eligible = (
            grant.in_bc
            and grant.authorized
            and grant.active
            and not account.ownership_conflict
            and bool(account.currency.strip())
            and bool(account.timezone.strip())
        )
        known = job.scope_known and role is not None and eligible
        operate = (
            known
            and role in {"ADMIN", "OPERATOR"}
            and account.remote_status in OPERABLE_REMOTE_STATUSES
        )
        grant.permission_state = "VERIFIED" if known else "UNKNOWN"
        grant.can_build = bool(operate and job.scope_build)
        grant.can_upload = bool(operate and job.scope_upload)
        grant.checked_at = now
        session.add(grant)
    job.published_count += len(rows)
    if rows:
        job.publish_after = rows[-1][0].advertiser_id
    if len(rows) < 100:
        job.status, job.phase, job.completed_at = "COMPLETE", "DONE", now
        job.error_code = None if job.scope_known else "capability_scope_unknown"
    else:
        job.revision += 1
        _queue(session, job)
    session.add(job)


def _fail(session: Session, job: CapabilityJob, error: Exception) -> None:
    code = (
        error.code
        if isinstance(error, DomainError)
        else "capability_remote_unavailable"
    )
    if code == "unsupported_account_schema":
        code = "capability_response_unverified"
    if code in {
        "capability_stale",
        "route_authorization_changed",
        "route_contract_changed",
    }:
        status = "STALE"
    elif code in {
        "action_forbidden",
        "tenant_forbidden",
        "connection_unavailable",
        "account_not_in_bc",
        "capability_unavailable",
        "credential_invalid",
        "mcp_contract_changed",
        "mcp_tool_unavailable",
        "mcp_refresh_unknown",
        "mcp_refresh_reauth_required",
    }:
        status = "BLOCKED"
    elif code in {"capability_response_unverified", "tiktok_response_error"}:
        status = "FAILED"
    else:
        # GET transport/admission failures are safe to retry; same business job.
        code = (
            code
            if code
            in {
                "admission_deferred",
                "admission_unavailable",
                "admission_policy_invalid",
                "admission_unconfigured",
                "mcp_refresh_pending",
                "gateway_credentials_changed",
            }
            else "capability_remote_unavailable"
        )
        status = "PENDING"
    job.claim_token = job.claimed_until = None
    job.error_code, job.status = code, status
    if status == "PENDING":
        job.failure_count += 1
        job.revision += 1
        _queue(session, job, delay=min(300, 2 ** min(job.failure_count, 8)))
    session.add(job)


def process_capability(
    *,
    database_engine: Any,
    redis_client: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    _require_bounded_worker()
    if (
        set(payload) != {"job_id", "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "能力任务参数无效")
    try:
        job_id = UUID(payload["job_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "能力任务标识无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    nonce = uuid4()
    with Session(database_engine) as session, session.begin():
        job = _locked_job(session, tenant_id, job_id)
        if job is None or job.actor_id != actor_id:
            return
        now = datetime.now(UTC)
        if (
            job.status != "PENDING"
            or job.revision != payload["revision"]
            or job.due_at > now
            or (job.claimed_until and job.claimed_until > now)
        ):
            return
        try:
            with session.begin_nested():
                from app.modules.accounts.runtime_directory import (
                    ensure_runtime_directory,
                    needs_directory_refresh,
                )

                refresh_required = needs_directory_refresh(session, _route(job))
                _validate(session, job, context, allow_expired_roles=refresh_required)
                if refresh_required:
                    ensure_runtime_directory(session, job=job, context=context)
                    return
                if job.phase == "PUBLISH":
                    _publish(session, job, context)
                    return
                job.claim_token = nonce
                job.claimed_until = now + timedelta(seconds=CLAIM_SECONDS)
                session.add(job)
                session.flush()
        except Exception as error:
            _fail(session, job, error)
            return
        page, connection_id, bc_id = job.next_page, job.connection_id, job.bc_id
        route = _route(job)
    deadline = datetime.now(UTC) + timedelta(seconds=30)

    def before_request() -> None:
        # 初始化、协议目录与每次业务HTTP前均重检原操作者和claim，短事务不跨网络。
        with bounded_session(database_engine, task_deadline=deadline) as session:
            current = _locked_job(session, tenant_id, job_id)
            if (
                current is None
                or current.actor_id != actor_id
                or current.status != "PENDING"
                or current.claim_token != nonce
                or current.revision != payload["revision"]
                or current.claimed_until is None
                or current.claimed_until <= datetime.now(UTC)
                or _route(current) != route
            ):
                raise DomainError("capability_stale", "原能力任务或请求claim已失效")
            _validate(session, current, context)

    try:
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=before_request,
        ) as gateway:
            facts = gateway.accounts.authorization_facts()
            result = gateway.accounts.roles(bc_id=bc_id, page=page, page_size=50)
        rows = [(item.advertiser_id, item.role) for item in result.items]
        total_pages, total_count = result.total_pages, result.total_number
        # 未返回精确总数或未知角色无法构成完整角色证据，旧授权快照保持不变。
        if total_count is None or any(role is None for _, role in rows):
            raise DomainError(
                "capability_response_unverified", "账户角色完整性尚未核实"
            )
        with Session(database_engine) as session, session.begin():
            job = _locked_job(session, tenant_id, job_id)
            if (
                job is None
                or job.actor_id != actor_id
                or job.connection_id != connection_id
                or job.bc_id != bc_id
                or job.status != "PENDING"
                or job.claim_token != nonce
                or job.revision != payload["revision"]
                or job.claimed_until is None
                or job.claimed_until <= datetime.now(UTC)
            ):
                return
            _validate(session, job, context)
            if job.total_pages is not None and (
                job.total_pages != total_pages or job.total_count != total_count
            ):
                raise DomainError(
                    "capability_response_unverified", "账户角色分页总数已变化"
                )
            job.total_pages, job.total_count = total_pages, total_count
            if (
                rows
                and session.exec(
                    select(CapabilityAsset.advertiser_id)
                    .where(
                        CapabilityAsset.job_id == job.id,
                        col(CapabilityAsset.advertiser_id).in_(
                            [aid for aid, _ in rows]
                        ),
                    )
                    .limit(1)
                ).first()
                is not None
            ):
                raise DomainError(
                    "capability_response_unverified", "账户角色分页包含重复记录"
                )
            _restrict_permissions(session, job=job, facts=facts, rows=rows)
            observed_at = datetime.now(UTC)
            if page == 1:
                job.expires_at = observed_at + timedelta(
                    seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS
                )
            session.add(
                CapabilityPage(
                    tenant_id=tenant_id,
                    bc_id=bc_id,
                    job_id=job.id,
                    page=page,
                    row_count=len(rows),
                    observed_at=observed_at,
                )
            )
            session.flush()
            for aid, role in rows:
                # 整页已经拒绝未知角色；这里不能把 None 写为有效角色证据。
                assert role is not None
                session.add(
                    CapabilityAsset(
                        tenant_id=tenant_id,
                        bc_id=bc_id,
                        job_id=job.id,
                        page=page,
                        advertiser_id=aid,
                        role=role,
                    )
                )
            session.flush()
            job.seen_count += len(rows)
            job.next_page += 1
            job.claim_token = job.claimed_until = None
            job.failure_count = 0
            job.error_code = None
            if page >= max(1, total_pages):
                if job.seen_count != total_count:
                    raise DomainError(
                        "capability_response_unverified", "账户角色分页未完整读取"
                    )
                job.scope_known, job.scope_build, job.scope_upload = _scope_flags(facts)
                job.phase = "PUBLISH"
            job.revision += 1
            _queue(session, job)
    except Exception as error:
        with Session(database_engine) as session, session.begin():
            job = _locked_job(session, tenant_id, job_id)
            if (
                job
                and job.actor_id == actor_id
                and job.claim_token == nonce
                and job.revision == payload["revision"]
            ):
                _fail(session, job, error)


def repair_capabilities(*, database_engine: Any, limit: int = 100) -> int:
    """Repair exact current transport identities; preserve unsent broker backoff."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session:
        identities = session.exec(
            select(CapabilityJob.tenant_id, CapabilityJob.id)
            .where(CapabilityJob.status == "PENDING", CapabilityJob.repair_after <= now)
            .order_by(col(CapabilityJob.repair_after), col(CapabilityJob.id))
            .limit(limit)
        ).all()
    repaired = 0
    for tenant_id, job_id in identities:
        with Session(database_engine) as session, session.begin():
            job = _locked_job(session, tenant_id, job_id)
            if (
                not job
                or job.status != "PENDING"
                or job.repair_after > now
                or (job.claimed_until and job.claimed_until > now)
            ):
                continue
            job.claim_token = job.claimed_until = None
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
                or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
                or dispatch.task_key != f"capability:{job.id}:{job.revision}"
            ):
                job.status, job.error_code = "FAILED", "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                dispatch.published_at = None
                dispatch.available_at = max(now, job.due_at)
                session.add(dispatch)
            job.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(job)
            repaired += 1
    return repaired


def get_capability_evidence(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    connection_id: UUID,
) -> CapabilityEvidence | None:
    """Pure local evidence read; never queues, decrypts, locks, flushes or grants."""
    with session.no_autoflush:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="read",
        )
        conn = session.get(TikTokConnection, connection_id, populate_existing=True)
        if (
            conn is None
            or conn.tenant_id != context.tenant_id
            or conn.status != "ACTIVE"
        ):
            return None
        grant = session.exec(
            usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
            .where(
                BCAccountAccess.advertiser_id == advertiser_id,
                BCAccountAccess.connection_id == connection_id,
            )
            .execution_options(populate_existing=True)
        ).first()
        if grant is None:
            return None
        statement = (
            select(CapabilityJob, CapabilityAsset.page, CapabilityAsset.role)
            .outerjoin(
                CapabilityAsset,
                (col(CapabilityAsset.job_id) == CapabilityJob.id)
                & (col(CapabilityAsset.advertiser_id) == advertiser_id),
            )
            .where(
                CapabilityJob.tenant_id == context.tenant_id,
                CapabilityJob.bc_id == bc_id,
                CapabilityJob.connection_id == connection_id,
                CapabilityJob.authorization_revision == conn.authorization_revision,
                CapabilityJob.channel == conn.kind,
                CapabilityJob.adapter_contract_revision
                == conn.adapter_contract_revision,
                CapabilityJob.status == "COMPLETE",
                CapabilityJob.phase == "DONE",
            )
            .order_by(col(CapabilityJob.completed_at).desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        row = session.exec(statement).first()
        if row is None:
            return None
        job, page, role = row
        if (
            _directory_basis(session, context, bc_id, connection_id)
            != job.directory_basis
        ):
            return None
        account = session.get(
            AdvertiserAccount,
            (context.tenant_id, advertiser_id),
            populate_existing=True,
        )
        proof_page = session.get(CapabilityPage, (job.id, page or 1))
        assert account and proof_page and job.expires_at
        operable = (
            job.scope_known
            and grant.permission_state == "VERIFIED"
            and role in {"ADMIN", "OPERATOR"}
            and account.remote_status in OPERABLE_REMOTE_STATUSES
            and bool(account.currency.strip())
            and bool(account.timezone.strip())
        )
        return CapabilityEvidence(
            job_id=job.id,
            evidence_ids=(job.id,),
            page_number=page,
            observed_at=proof_page.observed_at,
            expires_at=job.expires_at,
            scope_verified=job.scope_known,
            can_build=bool(operable and job.scope_build and grant.can_build),
            can_upload=bool(operable and job.scope_upload and grant.can_upload),
            source_revision=REVISION,
            basis_digest=job.directory_basis,
        )
