"""一份共享授权连接多个 BC；每个 BC 独立同步和解绑。"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis import Redis
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, delete, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.bootstrap import (
    _observation,
    candidate_attempt,
    candidate_business_centers,
    directory_fresh,
)
from app.integrations.tiktok.mcp_auth.management import (
    connection_business_centers,
    current_observation,
    management_connection,
    verified_schemas,
)
from app.integrations.tiktok.mcp_auth.service import (
    load_registration,
    require_mcp_admin,
)
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    ConnectionToolObservation,
    McpAuthorizationAttempt,
)
from app.modules.accounts.models import (
    BCAccountAccess,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

register_dispatch_task("accounts.mcp_discover", "resources")


def _selected(bc_ids: list[str]) -> list[str]:
    if (
        type(bc_ids) is not list
        or not bc_ids
        or len(bc_ids) > 5000
        or any(
            type(value) is not str or not value.strip() or len(value) > 128
            for value in bc_ids
        )
        or len(set(bc_ids)) != len(bc_ids)
    ):
        raise DomainError("mcp_candidate_bc_unknown", "请选择有效且不重复的 BC")
    return sorted(bc_ids)


def _locked_connection(
    session: Session, *, context: TenantContext, connection_id: UUID
) -> TikTokConnection:
    require_mcp_admin(session, actor_id=context.actor_id, tenant_id=context.tenant_id)
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == connection_id,
            TikTokConnection.tenant_id == context.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        connection is None
        or connection.kind != "OFFICIAL_MCP"
        or connection.status == "DISABLED"
    ):
        raise DomainError("connection_unavailable", "当前租户 MCP 授权不可用")
    return connection


def _clear_access(
    session: Session, *, tenant_id: UUID, connection_id: UUID, bc_id: str | None = None
) -> None:
    statement = update(BCAccountAccess).where(
        col(BCAccountAccess.tenant_id) == tenant_id,
        col(BCAccountAccess.connection_id) == connection_id,
    )
    if bc_id is not None:
        statement = statement.where(col(BCAccountAccess.bc_id) == bc_id)
    SASession.execute(
        session,
        statement.values(
            in_bc=False,
            authorized=False,
            active=False,
            can_upload=False,
            can_build=False,
            permission_state="UNKNOWN",
        ),
    )


def _cancel_runs(
    session: Session, *, tenant_id: UUID, connection_id: UUID, bc_id: str | None = None
) -> None:
    query = select(DiscoveryRun).where(
        DiscoveryRun.tenant_id == tenant_id,
        DiscoveryRun.connection_id == connection_id,
        col(DiscoveryRun.status).in_(["RUNNING", "ADMISSION_WAIT"]),
    )
    if bc_id is not None:
        query = query.where(DiscoveryRun.bc_id == bc_id)
    for run in session.exec(query.with_for_update()).all():
        run.status, run.error_code = "CANCELLED", "discovery_stale"
        run.claim_id = run.claimed_until = None
        session.add(run)
    session.flush()


def _run_result(run: DiscoveryRun) -> dict[str, Any]:
    return {"bc_id": run.bc_id, "discovery_run_id": str(run.id), "status": run.status}


def _start_bc(
    session: Session,
    *,
    context: TenantContext,
    connection: TikTokConnection,
    observation: ConnectionToolObservation,
    bc_id: str,
    force: bool = False,
) -> DiscoveryRun:
    verified_schemas(observation)
    if (
        observation.call_evidence.get("authorization_revision")
        != connection.authorization_revision
    ):
        raise DomainError("discovery_stale", "授权目录证据已过期")
    visible = {
        item["bc_id"]: item
        for item in observation.call_evidence.get("business_centers", [])
    }
    if (not force and not directory_fresh(observation)) or bc_id not in visible:
        raise DomainError("mcp_candidate_bc_unknown", "请先刷新授权可见 BC 列表")
    binding = session.get(
        BCConnectionBinding,
        (context.tenant_id, bc_id, connection.id),
        populate_existing=True,
    )
    previous = session.exec(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.tenant_id == context.tenant_id,
            DiscoveryRun.connection_id == connection.id,
            DiscoveryRun.bc_id == bc_id,
            DiscoveryRun.authorization_revision == connection.authorization_revision,
        )
        .order_by(col(DiscoveryRun.created_at).desc())
    ).first()
    if binding is not None and binding.status != "DISABLED" and previous is not None:
        if previous.binding_revision == binding.revision and (
            previous.status in {"RUNNING", "ADMISSION_WAIT"}
            or (
                not force
                and binding.status == "ACTIVE"
                and previous.status == "COMPLETE"
            )
        ):
            return previous
    bc = session.get(TenantBC, (context.tenant_id, bc_id))
    if bc is None:
        # BC 归属与完整账户权限仍由独立发布核实，SYNCING 绑定不能执行广告操作。
        session.add(
            TenantBC(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                name=visible[bc_id].get("name", ""),
            )
        )
        session.flush()
    if binding is None:
        binding = BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            connection_id=connection.id,
            kind="OFFICIAL_MCP",
            status="SYNCING",
            authorization_revision=connection.authorization_revision,
            revision=0,
        )
    else:
        if binding.status == "DISABLED":
            binding.revision += 1
        binding.status = "SYNCING"
        binding.last_error_code = None
        binding.authorization_revision = connection.authorization_revision
    session.add(binding)
    # 同步期间保留旧快照；独立发布完整替换，失败不能改写上一版账户证据。
    _cancel_runs(
        session, tenant_id=context.tenant_id, connection_id=connection.id, bc_id=bc_id
    )
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        bc_id=bc_id,
        authorization_revision=connection.authorization_revision,
        binding_revision=binding.revision,
        credential_revision=connection.credential_revision,
        work={
            "stage": "MCP_VERIFY",
            "bc_id": bc_id,
            "observation_id": str(observation.id),
            "page": 1,
        },
    )
    session.add(run)
    session.flush()
    enqueue_after_commit(
        session,
        context=context,
        task_name="accounts.mcp_discover",
        task_key=f"mcp-discover:{run.id}:0",
        payload={"run_id": str(run.id), "revision": 0},
    )
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.mcp.binding.request",
            target_id=str(connection.id),
            details={"bc_id": bc_id, "run_id": str(run.id)},
        )
    )
    return run


def _accepted_replay(
    session: Session,
    *,
    context: TenantContext,
    attempt: McpAuthorizationAttempt,
    selected: list[str],
) -> tuple[UUID, list[dict[str, Any]]] | None:
    if attempt.status != "ACCEPTED":
        return None
    connection = _locked_connection(
        session, context=context, connection_id=attempt.connection_id
    )
    observation = _observation(session, context=context, attempt_id=attempt.id)
    if (
        observation is None
        or observation.call_evidence.get("selected_bc_ids") != selected
        or observation.call_evidence.get("authorization_revision")
        != connection.authorization_revision
    ):
        raise DomainError(
            "mcp_candidate_superseded", "该授权已确认，请从当前授权列表添加 BC"
        )
    runs = []
    for run_id in observation.call_evidence.get("accepted_run_ids", []):
        run = session.get(DiscoveryRun, UUID(run_id))
        if (
            run is None
            or run.tenant_id != context.tenant_id
            or run.connection_id != connection.id
        ):
            raise DomainError("discovery_not_found", "授权同步任务不存在")
        runs.append(_run_result(run))
    return connection.id, runs


def _publish_authorization(
    session: Session, *, context: TenantContext, attempt_id: UUID, selected: list[str]
) -> tuple[UUID, list[dict[str, Any]]]:
    attempt = session.get(McpAuthorizationAttempt, attempt_id, populate_existing=True)
    if attempt is None or attempt.tenant_id != context.tenant_id:
        raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
    connection = _locked_connection(
        session, context=context, connection_id=attempt.connection_id
    )
    session.refresh(attempt)
    replay = _accepted_replay(
        session, context=context, attempt=attempt, selected=selected
    )
    if replay is not None:
        return replay
    attempt = candidate_attempt(session, context=context, attempt_id=attempt_id)
    observation = _observation(session, context=context, attempt_id=attempt_id)
    if observation is None or not directory_fresh(observation):
        raise DomainError(
            "mcp_candidate_directory_incomplete", "请先完整读取当前候选 BC 目录"
        )
    verified_schemas(observation)
    visible = {item["bc_id"] for item in observation.call_evidence["business_centers"]}
    if not set(selected) <= visible:
        raise DomainError("mcp_candidate_bc_unknown", "请选择候选授权可见的 BC")
    profile = load_mcp_protocol()
    material = decrypt_credentials(
        tenant_id=context.tenant_id, ciphertext=attempt.candidate_ciphertext or ""
    )
    facts = observation.call_evidence.get("authorization_facts", {})
    try:
        scopes = json.loads(material["scopes"])
        observed_at = datetime.fromisoformat(facts["observed_at"])
        expires_at = datetime.fromisoformat(material["expires_at"])
        if (
            not isinstance(facts.get("subject_id"), str)
            or not facts["subject_id"].strip()
            or facts.get("evidence_source") != "MCP_USER_INFO_AND_TOKEN"
            or not 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 300
            or scopes != facts["scopes"]
            or "mcp:tt4b" not in scopes
            or material.get("issuer") != facts.get("issuer")
            or facts.get("issuer") != profile.issuer
            or material.get("resource") != facts.get("resource")
            or facts.get("resource") != profile.resource
            or material.get("client_id") != load_registration(profile).client_id
            or expires_at <= datetime.now(UTC)
        ):
            raise ValueError("unverified authorization")
    except KeyError, TypeError, ValueError:
        raise DomainError(
            "mcp_candidate_directory_incomplete", "候选主体、授权范围或完整目录尚未核实"
        ) from None
    finally:
        material.clear()
    previous_auth = session.exec(
        select(ConnectionAuthorization)
        .where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == connection.id,
        )
        .order_by(col(ConnectionAuthorization.authorization_revision).desc())
    ).first()
    if previous_auth is not None and previous_auth.upstream_subject not in {
        None,
        facts["subject_id"],
    }:
        raise DomainError(
            "mcp_authorization_mismatch", "请使用此授权原有的 TikTok 账号重新授权"
        )
    bindings = session.exec(
        select(BCConnectionBinding)
        .where(
            BCConnectionBinding.tenant_id == context.tenant_id,
            BCConnectionBinding.connection_id == connection.id,
        )
        .with_for_update()
    ).all()
    reconnect = {
        binding.bc_id
        for binding in bindings
        if binding.status != "DISABLED" and binding.bc_id in visible
    }
    _cancel_runs(session, tenant_id=context.tenant_id, connection_id=connection.id)
    _clear_access(session, tenant_id=context.tenant_id, connection_id=connection.id)
    connection.credential_ciphertext = attempt.candidate_ciphertext
    connection.credential_revision += 1
    connection.authorization_revision += 1
    connection.adapter_contract_revision = profile.schema_manifest_sha256
    connection.status = "ACTIVE"
    for binding in bindings:
        if binding.status != "DISABLED":
            binding.status = "SYNCING" if binding.bc_id in visible else "DISABLED"
            if binding.status == "DISABLED":
                binding.revision += 1
                session.exec(
                    delete(BCDefaultRoute).where(
                        col(BCDefaultRoute.tenant_id) == context.tenant_id,
                        col(BCDefaultRoute.bc_id) == binding.bc_id,
                        col(BCDefaultRoute.connection_id) == connection.id,
                    )
                )
            session.add(binding)
    session.add(
        ConnectionAuthorization(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            authorization_revision=connection.authorization_revision,
            upstream_subject=facts["subject_id"],
            issuer=facts["issuer"],
            resource=facts["resource"],
            scopes=scopes,
            permission_summary={
                "read_authorized": True,
                "upload_authorized": None,
                "build_authorized": None,
            },
            source="MCP_USER_INFO_AND_COMPLETE_BC_DIRECTORY",
            verified_at=datetime.now(UTC),
            access_token_expires_at=expires_at,
            previous_authorization_id=previous_auth.id if previous_auth else None,
            mcp_authorization_attempt_id=attempt.id,
        )
    )
    observation.call_evidence = {
        **observation.call_evidence,
        "authorization_revision": connection.authorization_revision,
        "selected_bc_ids": selected,
    }
    active_observation = ConnectionToolObservation(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        schema_digest=observation.schema_digest,
        expected_contract_revision=observation.expected_contract_revision,
        pagination_complete=True,
        tool_schemas=dict(observation.tool_schemas),
        call_evidence={**observation.call_evidence, "kind": "MANAGEMENT_DIRECTORY"},
        observed_at=observation.observed_at,
    )
    session.add_all([connection, observation, active_observation])
    session.flush()
    runs = [
        _start_bc(
            session,
            context=context,
            connection=connection,
            observation=active_observation,
            bc_id=bc_id,
        )
        for bc_id in sorted(set(selected) | reconnect)
    ]
    attempt.status, attempt.candidate_ciphertext, attempt.completed_at = (
        "ACCEPTED",
        None,
        datetime.now(UTC),
    )
    observation.call_evidence = {
        **observation.call_evidence,
        "accepted_run_ids": [str(run.id) for run in runs],
    }
    session.add_all([attempt, observation])
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.mcp.authorization.publish",
            target_id=str(connection.id),
            details={
                "attempt_id": str(attempt_id),
                "bc_ids": [run.bc_id for run in runs],
                "authorization_revision": connection.authorization_revision,
            },
        )
    )
    session.flush()
    return connection.id, [_run_result(run) for run in runs]


def bind_candidate_bcs(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    attempt_id: UUID,
    bc_ids: list[str],
    task_deadline: datetime,
) -> tuple[UUID, list[dict[str, Any]]]:
    selected = _selected(bc_ids)
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        require_mcp_admin(
            session, actor_id=context.actor_id, tenant_id=context.tenant_id
        )
        attempt = session.get(McpAuthorizationAttempt, attempt_id)
        if attempt is None or attempt.tenant_id != context.tenant_id:
            raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
        replay = _accepted_replay(
            session, context=context, attempt=attempt, selected=selected
        )
        if replay is not None:
            return replay
    try:
        candidate_business_centers(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            attempt_id=attempt_id,
            task_deadline=task_deadline,
        )
    except DomainError:
        # 同一确认并发时，另一请求可能已在准备目录期间发布；只重放已确认的同集合结果。
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            attempt = session.get(
                McpAuthorizationAttempt, attempt_id, populate_existing=True
            )
            if attempt is not None and attempt.tenant_id == context.tenant_id:
                replay = _accepted_replay(
                    session, context=context, attempt=attempt, selected=selected
                )
                if replay is not None:
                    return replay
        raise
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        result = _publish_authorization(
            session, context=context, attempt_id=attempt_id, selected=selected
        )
        session.commit()
        return result


def add_connection_bcs(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    connection_id: UUID,
    bc_ids: list[str],
    task_deadline: datetime,
) -> tuple[UUID, list[dict[str, Any]]]:
    selected = _selected(bc_ids)
    connection_business_centers(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        task_deadline=task_deadline,
    )
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        connection = _locked_connection(
            session, context=context, connection_id=connection_id
        )
        management_connection(session, context=context, connection_id=connection_id)
        observation = current_observation(
            session, context=context, connection=connection
        )
        if observation is None:
            raise DomainError(
                "mcp_candidate_directory_incomplete", "请先刷新授权可见 BC 列表"
            )
        runs = [
            _start_bc(
                session,
                context=context,
                connection=connection,
                observation=observation,
                bc_id=bc_id,
            )
            for bc_id in selected
        ]
        result = connection.id, [_run_result(run) for run in runs]
        session.commit()
        return result


def sync_connection_bc(
    session: Session, *, context: TenantContext, connection_id: UUID, bc_id: str
) -> UUID:
    _selected([bc_id])
    connection = _locked_connection(
        session, context=context, connection_id=connection_id
    )
    management_connection(session, context=context, connection_id=connection_id)
    binding = session.get(
        BCConnectionBinding,
        (context.tenant_id, bc_id, connection_id),
        populate_existing=True,
    )
    if binding is None or binding.status == "DISABLED":
        raise DomainError("connection_bc_mismatch", "请先接入当前 BC")
    observation = current_observation(session, context=context, connection=connection)
    if observation is None:
        raise DomainError(
            "mcp_candidate_directory_incomplete", "请先刷新授权可见 BC 列表"
        )
    return _start_bc(
        session,
        context=context,
        connection=connection,
        observation=observation,
        bc_id=bc_id,
        force=True,
    ).id


def disable_connection_bc(
    session: Session, *, context: TenantContext, connection_id: UUID, bc_id: str
) -> None:
    _selected([bc_id])
    connection = _locked_connection(
        session, context=context, connection_id=connection_id
    )
    binding = session.get(
        BCConnectionBinding,
        (context.tenant_id, bc_id, connection_id),
        populate_existing=True,
    )
    if binding is None or binding.status == "DISABLED":
        return
    binding.status = "DISABLED"
    binding.revision += 1
    session.add(binding)
    _clear_access(
        session, tenant_id=context.tenant_id, connection_id=connection.id, bc_id=bc_id
    )
    _cancel_runs(
        session, tenant_id=context.tenant_id, connection_id=connection.id, bc_id=bc_id
    )
    session.exec(
        delete(BCDefaultRoute).where(
            col(BCDefaultRoute.tenant_id) == context.tenant_id,
            col(BCDefaultRoute.bc_id) == bc_id,
            col(BCDefaultRoute.connection_id) == connection_id,
        )
    )
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.mcp.binding.disable",
            target_id=str(connection_id),
            details={"bc_id": bc_id},
        )
    )
    session.flush()


def disable_connection(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    task_deadline: datetime,
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if task_deadline.tzinfo is None or task_deadline <= datetime.now(UTC):
        raise DomainError("mcp_deadline_exceeded", "操作时限已过")
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == connection_id,
            TikTokConnection.tenant_id == context.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if connection is None:
        raise DomainError("connection_not_found", "当前租户连接不存在")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if connection.status == "DISABLED":
        return
    connection.status = "DISABLED"
    connection.authorization_revision += 1
    session.add(connection)
    attempts = session.exec(
        select(McpAuthorizationAttempt).where(
            McpAuthorizationAttempt.tenant_id == context.tenant_id,
            McpAuthorizationAttempt.connection_id == connection_id,
            col(McpAuthorizationAttempt.status).in_(
                ["PENDING", "CLAIMED", "CANDIDATE_READY"]
            ),
        )
    ).all()
    attempt_ids = {str(attempt.id) for attempt in attempts}
    for attempt in attempts:
        attempt.status = "CANCELLED"
        attempt.pkce_verifier_ciphertext = None
        attempt.candidate_ciphertext = None
        attempt.completed_at = datetime.now(UTC)
        session.add(attempt)
    runs = session.exec(
        select(DiscoveryRun).where(
            DiscoveryRun.tenant_id == context.tenant_id,
            DiscoveryRun.connection_id == connection_id,
            col(DiscoveryRun.status).in_(["RUNNING", "ADMISSION_WAIT"]),
        )
    ).all()
    run_ids = {str(run.id) for run in runs}
    for run in runs:
        run.status = "CANCELLED"
        run.error_code = "connection_unavailable"
        session.add(run)
    # 只取消仍可派发的消息，保留已替代/退役身份，并命中待派发部分索引。
    pending = session.exec(
        select(PendingDispatch)
        .where(
            PendingDispatch.tenant_id == context.tenant_id,
            col(PendingDispatch.published_at).is_(None),
            col(PendingDispatch.superseded_at).is_(None),
            col(PendingDispatch.retired_at).is_(None),
        )
        .with_for_update()
    ).all()
    cancelled = 0
    for dispatch in pending:
        payload = dispatch.payload
        if (
            payload.get("connection_id") == str(connection_id)
            or payload.get("attempt_id") in attempt_ids
            or payload.get("run_id") in run_ids
        ):
            session.delete(dispatch)
            cancelled += 1
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.connection.disable",
            target_id=str(connection_id),
            details={"cancelled_dispatches": cancelled},
        )
    )
    # 已发送 attempt 与回执保留；本地停用不对上游 grant 发起隐式撤销。
    session.flush()


def request_mcp_revocation(
    session: Session, *, context: TenantContext, connection_id: UUID
) -> UUID:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != context.tenant_id
        or connection.kind != "OFFICIAL_MCP"
    ):
        raise DomainError("connection_not_found", "当前租户 MCP 连接不存在")
    load_mcp_protocol().require_authorization_verified()
    # P0 只确认 revoke URL，没有 grant 隔离与撤销范围证明。不能把端点存在当作安全撤销授权。
    raise DomainError(
        "mcp_revocation_unsupported", "官方 MCP 撤销隔离语义尚未核实，请使用本地停用"
    )
