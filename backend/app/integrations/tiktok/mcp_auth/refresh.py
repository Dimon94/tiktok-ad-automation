"""持久化刷新：旧令牌只消费一次，完整候选先落库，再按授权连续关系发布。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import Engine, or_
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.admission import quota_scope
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.service import (
    _credentials,
    load_registration,
    require_mcp_admin,
)
from app.integrations.tiktok.mcp_auth.transport import exchange_token
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy, admitted_scope
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
    McpAuthorizationAttempt,
    McpRefreshAttempt,
)
from app.modules.accounts.models import TikTokConnection
from app.modules.tenants.models import AuditEvent, Tenant
from app.modules.tenants.permissions import require_tenant

CLEANUP_SECONDS = 5
WORK_SECONDS = 35
CLAIM_SECONDS = 45
ACTIVE_STATES = (
    "PENDING",
    "CLAIMED",
    "REQUEST_ARMED",
    "CANDIDATE_READY",
    "OUTCOME_UNKNOWN",
)
TERMINAL_STATES = ("PUBLISHED", "OUTCOME_UNKNOWN", "REJECTED", "SUPERSEDED")


def _locked(
    session: Session, attempt_id: UUID
) -> tuple[TikTokConnection, McpRefreshAttempt]:
    attempt = session.get(McpRefreshAttempt, attempt_id)
    if attempt is None:
        raise DomainError("mcp_refresh_unavailable", "刷新记录不可用")
    # 所有阶段保持连接、attempt 的锁顺序，不跨 HTTP 持有事务。
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == attempt.connection_id,
            TikTokConnection.tenant_id == attempt.tenant_id,
        )
        .with_for_update()
    ).one()
    session.refresh(attempt, with_for_update=True)
    return connection, attempt


def _current(
    session: Session,
    connection: TikTokConnection,
    attempt: McpRefreshAttempt,
    *,
    recovering: bool = False,
) -> bool:
    tenant = session.get(Tenant, connection.tenant_id)
    newer_authorization = session.exec(
        select(McpAuthorizationAttempt.id).where(
            McpAuthorizationAttempt.tenant_id == connection.tenant_id,
            McpAuthorizationAttempt.connection_id == connection.id,
            # 未兑换的授权入口过期后已不能回调，不应永久阻断原连接续期。
            # 已兑换、持有候选或结果未知的尝试仍须围栏，不能仅凭时间忽略。
            or_(
                (col(McpAuthorizationAttempt.status) == "PENDING")
                & (col(McpAuthorizationAttempt.expires_at) > datetime.now(UTC)),
                col(McpAuthorizationAttempt.status).in_(
                    ("CLAIMED", "CANDIDATE_READY", "RESULT_UNKNOWN")
                ),
            ),
        )
    ).first()
    prior_send = session.exec(
        select(McpRefreshAttempt.id).where(
            McpRefreshAttempt.tenant_id == connection.tenant_id,
            McpRefreshAttempt.connection_id == connection.id,
            McpRefreshAttempt.base_credential_revision
            == attempt.base_credential_revision,
            McpRefreshAttempt.id != attempt.id,
            col(McpRefreshAttempt.request_armed_at).is_not(None),
        )
    ).first()
    return bool(
        not prior_send
        and not newer_authorization
        and tenant
        and tenant.active
        and connection.kind == "OFFICIAL_MCP"
        and connection.status == ("REAUTH_REQUIRED" if recovering else "ACTIVE")
        and (not recovering or attempt.error_code == "mcp_refresh_reauth_required")
        and (connection.credential_revision, connection.authorization_revision)
        == (
            attempt.base_credential_revision,
            attempt.base_authorization_revision + int(recovering),
        )
    )


def _finish(attempt: McpRefreshAttempt, status: str, code: str | None = None) -> str:
    attempt.status = status
    attempt.error_code = code
    attempt.completed_at = datetime.now(UTC)
    return status


def _require_actor(
    session: Session, *, context: TenantContext, attempt: McpRefreshAttempt
) -> None:
    if attempt.tenant_id != context.tenant_id:
        raise DomainError("mcp_refresh_unavailable", "刷新记录不可用")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )


def _queue_refresh(
    session: Session,
    *,
    context: TenantContext,
    attempt: McpRefreshAttempt,
    due: datetime | None = None,
) -> None:
    """调用方持有 connection 锁；每个当前操作者至多保留一个未发布投递。"""
    existing = session.exec(
        select(PendingDispatch)
        .where(
            PendingDispatch.tenant_id == context.tenant_id,
            PendingDispatch.actor_id == context.actor_id,
            PendingDispatch.task_name == "accounts.refresh_mcp",
            PendingDispatch.payload["attempt_id"].as_string() == str(attempt.id),
            col(PendingDispatch.published_at).is_(None),
            col(PendingDispatch.superseded_at).is_(None),
            col(PendingDispatch.retired_at).is_(None),
        )
        .order_by(col(PendingDispatch.available_at))
        # publisher 在 send_task 后才提交 published_at；等待它的行锁并重判，不能把原消息误认成恢复。
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if due is None:
        due = datetime.now(UTC)
        if attempt.status in ("CLAIMED", "REQUEST_ARMED") and attempt.claimed_until:
            due = max(due, attempt.claimed_until + timedelta(seconds=1))
    if existing is not None:
        existing.available_at = min(existing.available_at, due)
        session.add(existing)
        return
    dispatch_id = enqueue_after_commit(
        session,
        context=context,
        task_name="accounts.refresh_mcp",
        task_key=f"mcp-refresh:{attempt.id}:{uuid4()}",
        payload={"attempt_id": str(attempt.id)},
    )
    dispatch = session.get(PendingDispatch, dispatch_id)
    assert dispatch is not None
    dispatch.available_at = due
    session.add(dispatch)


def ensure_mcp_credentials(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    connection_id: UUID,
    task_deadline: datetime,
) -> None:
    """仅预检有效期及排队；不在业务线程刷新或返回 token。"""
    _ = redis_client  # 预检只排队；真正刷新由 worker 使用 Redis 准入。
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="read",
        )
        connection = session.exec(
            select(TikTokConnection)
            .where(
                TikTokConnection.id == connection_id,
                TikTokConnection.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).first()
        if connection is None or connection.kind != "OFFICIAL_MCP":
            raise DomainError("mcp_refresh_unavailable", "MCP 连接不可用")
        if connection.status != "ACTIVE" or not connection.credential_ciphertext:
            raise DomainError("mcp_refresh_reauth_required", "MCP 连接需要重新授权")
        attempt = session.exec(
            select(McpRefreshAttempt)
            .where(
                McpRefreshAttempt.tenant_id == context.tenant_id,
                McpRefreshAttempt.connection_id == connection_id,
                McpRefreshAttempt.base_credential_revision
                == connection.credential_revision,
                or_(
                    col(McpRefreshAttempt.status).in_(ACTIVE_STATES),
                    col(McpRefreshAttempt.request_armed_at).is_not(None),
                ),
            )
            .order_by(
                col(McpRefreshAttempt.request_armed_at).desc().nulls_last(),
                col(McpRefreshAttempt.created_at).desc(),
            )
        ).first()
        # 当前公开协议未证明刷新发送后的旧 access token 有效性；只恢复可证明的持久状态。
        if attempt and attempt.status in ("OUTCOME_UNKNOWN", "SUPERSEDED", "REJECTED"):
            raise DomainError("mcp_refresh_unknown", "MCP 令牌更新尚未确认")
        if attempt and attempt.status in ("REQUEST_ARMED", "CANDIDATE_READY"):
            _queue_refresh(session, context=context, attempt=attempt)
            session.commit()
            raise DomainError(
                "mcp_refresh_pending", "MCP 令牌更新尚未确认", retryable=True
            )
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        try:
            expiry = datetime.fromisoformat(material["expires_at"])
            usable = expiry >= task_deadline + timedelta(seconds=CLEANUP_SECONDS)
        except KeyError, ValueError, TypeError:
            usable = False
        if usable:
            return
        if not material.get("refresh_token"):
            raise DomainError(
                "mcp_refresh_reauth_required", "MCP 令牌有效期不足，请重新授权"
            )
        if attempt is None:
            attempt = McpRefreshAttempt(
                tenant_id=context.tenant_id,
                connection_id=connection_id,
                base_credential_revision=connection.credential_revision,
                base_authorization_revision=connection.authorization_revision,
            )
            session.add(attempt)
            session.flush()
        _queue_refresh(session, context=context, attempt=attempt)
        session.commit()
    raise DomainError(
        "mcp_refresh_pending", "MCP 令牌正在更新，请稍后重试", retryable=True
    )


def _publish(
    *,
    database_engine: Engine,
    context: TenantContext,
    attempt_id: UUID,
    task_deadline: datetime,
    recovering: bool = False,
) -> str:
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        connection, attempt = _locked(session, attempt_id)
        _require_actor(session, context=context, attempt=attempt)
        if recovering:
            require_mcp_admin(
                session, actor_id=context.actor_id, tenant_id=context.tenant_id
            )
        if attempt.status != "CANDIDATE_READY":
            return attempt.status
        if not _current(session, connection, attempt, recovering=recovering):
            if recovering:
                raise DomainError(
                    "mcp_refresh_unavailable", "原连接或授权已变化，不能恢复此回执"
                )
            result = _finish(attempt, "SUPERSEDED")
        else:
            profile = load_mcp_protocol()
            old = decrypt_credentials(
                tenant_id=attempt.tenant_id,
                ciphertext=connection.credential_ciphertext or "",
            )
            receipt = decrypt_credentials(
                tenant_id=attempt.tenant_id,
                ciphertext=attempt.candidate_ciphertext or "",
            )
            data = json.loads(receipt["response"])
            authorization = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.tenant_id == attempt.tenant_id,
                    ConnectionAuthorization.connection_id == connection.id,
                    ConnectionAuthorization.authorization_revision
                    == (
                        attempt.base_authorization_revision
                        if recovering
                        else connection.authorization_revision
                    ),
                )
            ).first()
            invalid = False
            try:
                candidate = _credentials(data, profile=profile)
                # RFC 6749 §6：省略 scope 继承原授权；不用 token 字节或虚构 grant ID 证明连续性。
                if "scope" not in data:
                    candidate["scopes"] = old["scopes"]
                if "refresh_token" not in candidate:
                    candidate["refresh_token"] = old["refresh_token"]
                candidate["client_id"] = receipt["client_id"]
                candidate["received_at"] = receipt["received_at"]
                if "expires_in" in data:
                    candidate["expires_at"] = (
                        datetime.fromisoformat(receipt["received_at"])
                        + timedelta(seconds=data["expires_in"])
                    ).isoformat()
                old_scopes = sorted(json.loads(old["scopes"]))
                new_scopes = sorted(json.loads(candidate["scopes"]))
                # 未审查的身份/角色扩展不能被丢弃后视为例行刷新。
                fields = {
                    "access_token",
                    "refresh_token",
                    "token_type",
                    "scope",
                    "expires_in",
                    "refresh_token_expires_in",
                    "issuer",
                    "iss",
                    "resource",
                    "aud",
                }
                refresh_expiry = data.get("refresh_token_expires_in")
                # TikTok 的刷新令牌寿命是秒数元数据，不代表授权主体或范围改变。
                invalid = (
                    (
                        "refresh_token_expires_in" in data
                        and (
                            type(refresh_expiry) is not int
                            or not 0 < refresh_expiry <= 10 * 365 * 86400
                        )
                    )
                    or bool(set(data) - fields)
                    or not (
                        receipt.get("attempt_id") == str(attempt.id)
                        and receipt.get("connection_id") == str(connection.id)
                        and receipt.get("base_credential_revision")
                        == str(attempt.base_credential_revision)
                        and receipt.get("base_authorization_revision")
                        == str(attempt.base_authorization_revision)
                        and receipt.get("claim_id") == str(attempt.claim_id)
                        and "expires_at" in candidate
                        and datetime.fromisoformat(candidate["expires_at"])
                        > datetime.now(UTC) + timedelta(seconds=CLEANUP_SECONDS)
                        and authorization
                        and authorization.issuer == old.get("issuer") == profile.issuer
                        and authorization.resource
                        == old.get("resource")
                        == profile.resource
                        and sorted(authorization.scopes) == old_scopes == new_scopes
                        and old.get("client_id")
                        == receipt["client_id"]
                        == load_registration(profile).client_id
                    )
                )
            except DomainError, KeyError, TypeError, ValueError:
                invalid = True
            if invalid:
                if recovering:
                    raise DomainError(
                        "mcp_refresh_reauth_required",
                        "回执未通过完整校验，仍需重新授权",
                    )
                # 缩权、未知身份与扩权均即时封闭旧操作；完整候选保留供管理员重新核验。
                connection.status = "REAUTH_REQUIRED"
                connection.authorization_revision += 1
                attempt.error_code = "mcp_refresh_reauth_required"
                result = "CANDIDATE_READY"
            else:
                if recovering:
                    assert authorization is not None
                    # 管理员显式复核已保存回执；保留已递增的版本，旧冻结任务仍失效。
                    # 权限事实及观测时间原样继承，新准备仍须通过正常目录时效核对。
                    authorization = ConnectionAuthorization(
                        **authorization.model_dump(
                            exclude={
                                "id",
                                "created_at",
                                "authorization_revision",
                                "previous_authorization_id",
                                "source",
                            }
                        ),
                        authorization_revision=connection.authorization_revision,
                        previous_authorization_id=authorization.id,
                        source="MCP_REFRESH_RECEIPT_RECOVERY",
                    )
                    session.add(authorization)
                    for binding in session.exec(
                        select(BCConnectionBinding).where(
                            BCConnectionBinding.tenant_id == context.tenant_id,
                            BCConnectionBinding.connection_id == connection.id,
                            BCConnectionBinding.status == "ACTIVE",
                            BCConnectionBinding.authorization_revision
                            == attempt.base_authorization_revision,
                        )
                    ).all():
                        binding.authorization_revision = (
                            connection.authorization_revision
                        )
                        session.add(binding)
                    connection.status = "ACTIVE"
                    session.add(
                        AuditEvent(
                            tenant_id=context.tenant_id,
                            actor_id=context.actor_id,
                            action="mcp_refresh_receipt_recovered",
                            target_id=str(attempt.id),
                            details={
                                "connection_id": str(connection.id),
                                "authorization_revision": connection.authorization_revision,
                            },
                        )
                    )
                connection.credential_ciphertext = encrypt_credentials(
                    tenant_id=attempt.tenant_id, value=candidate
                )
                connection.credential_revision += 1
                # 授权摘要及未知远端 ID 原样保留；这里只更新材料有效期。
                assert authorization is not None
                authorization.access_token_expires_at = (
                    datetime.fromisoformat(candidate["expires_at"])
                    if "expires_at" in candidate
                    else None
                )
                result = _finish(attempt, "PUBLISHED")
        session.add_all([connection, attempt])
        session.commit()
        return result


def recover_mcp_refresh_receipt(
    *,
    database_engine: Engine,
    context: TenantContext,
    attempt_id: UUID,
) -> str:
    """管理员复核已持久化回执；不发 HTTP，不重放刷新令牌，不恢复旧任务代数。"""
    return _publish(
        database_engine=database_engine,
        context=context,
        attempt_id=attempt_id,
        task_deadline=datetime.now(UTC) + timedelta(seconds=WORK_SECONDS),
        recovering=True,
    )


def process_mcp_refresh(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    attempt_id: UUID,
) -> str:
    deadline = datetime.now(UTC) + timedelta(seconds=WORK_SECONDS)
    network_deadline = deadline - timedelta(seconds=CLEANUP_SECONDS)
    claim_id = uuid4()
    with bounded_session(database_engine, task_deadline=deadline) as session:
        connection, attempt = _locked(session, attempt_id)
        _require_actor(session, context=context, attempt=attempt)
        if attempt.status in TERMINAL_STATES:
            return attempt.status
        if not _current(session, connection, attempt):
            # 管理员待核验候选重投递时保留 receipt，不再次改变授权版本。
            if (
                attempt.status == "CANDIDATE_READY"
                and attempt.error_code == "mcp_refresh_reauth_required"
                and connection.status == "REAUTH_REQUIRED"
                and connection.credential_revision == attempt.base_credential_revision
                and connection.authorization_revision
                == attempt.base_authorization_revision + 1
            ):
                return attempt.status
            result = _finish(attempt, "SUPERSEDED")
            session.add(attempt)
            session.commit()
            return result
        candidate_ready = attempt.status == "CANDIDATE_READY"
        if not candidate_ready:
            if attempt.status == "REQUEST_ARMED":
                if attempt.claimed_until and attempt.claimed_until <= datetime.now(UTC):
                    _finish(attempt, "OUTCOME_UNKNOWN", "mcp_refresh_unknown")
                    session.add(attempt)
                    session.commit()
                return attempt.status
            if (
                attempt.status == "CLAIMED"
                and attempt.claimed_until
                and attempt.claimed_until > datetime.now(UTC)
            ):
                return attempt.status
            old = decrypt_credentials(
                tenant_id=attempt.tenant_id,
                ciphertext=connection.credential_ciphertext or "",
            )
            if not old.get("refresh_token"):
                result = _finish(attempt, "REJECTED", "mcp_refresh_reauth_required")
                session.add(attempt)
                session.commit()
                return result
            profile = load_mcp_protocol()
            registration = load_registration(profile)
            if (
                old.get("issuer") != profile.issuer
                or old.get("resource") != profile.resource
                or old.get("client_id") != registration.client_id
            ):
                raise DomainError("mcp_refresh_reauth_required", "MCP 授权范围无效")
            tenant_id = attempt.tenant_id
            connection_id = attempt.connection_id
            base_credential_revision = attempt.base_credential_revision
            base_authorization_revision = attempt.base_authorization_revision
            attempt.claim_id = claim_id
            attempt.claimed_until = datetime.now(UTC) + timedelta(seconds=CLAIM_SECONDS)
            attempt.status = "CLAIMED"
            session.add(attempt)
            session.commit()
    if candidate_ready:
        return _publish(
            database_engine=database_engine,
            context=context,
            attempt_id=attempt_id,
            task_deadline=deadline,
        )

    def received(data: dict[str, Any]) -> None:
        ciphertext = encrypt_credentials(
            tenant_id=tenant_id,
            value={
                "response": json.dumps(data, allow_nan=False),
                "attempt_id": str(attempt_id),
                "connection_id": str(connection_id),
                "base_credential_revision": str(base_credential_revision),
                "base_authorization_revision": str(base_authorization_revision),
                "claim_id": str(claim_id),
                "client_id": registration.client_id,
                "received_at": datetime.now(UTC).isoformat(),
            },
        )
        with bounded_session(database_engine, task_deadline=deadline) as session:
            _, row = _locked(session, attempt_id)
            # 即便停用/重授权已发生，先保存本 attempt 收到的候选，发布阶段再围栏。
            if row.claim_id != claim_id or row.status not in (
                "REQUEST_ARMED",
                "OUTCOME_UNKNOWN",
            ):
                raise DomainError("mcp_refresh_unavailable", "刷新所有权已变化")
            row.candidate_ciphertext = ciphertext
            row.status = "CANDIDATE_READY"
            session.add(row)
            session.commit()

    try:
        policy = admission_policy("auth_refresh")
        if policy.lease_ms <= WORK_SECONDS * 1000 + 1000:
            raise DomainError(
                "admission_policy_invalid", "刷新租约必须覆盖完整调用期限"
            )
        scope = quota_scope(
            channel="OFFICIAL_MCP",
            app_id=None,
            verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
        )
        with bounded_redis(redis_client, task_deadline=deadline) as bounded:
            with admitted_scope(
                bounded,
                app_scope=scope,
                endpoint="auth_refresh",
                tenant_id=tenant_id,
                advertiser_id="",
                policy=policy,
                denied_error=AccountAdmissionDeferred,
            ):
                with bounded_session(
                    database_engine, task_deadline=deadline
                ) as session:
                    connection, row = _locked(session, attempt_id)
                    _require_actor(session, context=context, attempt=row)
                    if row.claim_id != claim_id or row.status != "CLAIMED":
                        return row.status
                    if not _current(session, connection, row):
                        result = _finish(row, "SUPERSEDED")
                        session.add(row)
                        session.commit()
                        return result
                    row.status = "REQUEST_ARMED"
                    row.request_armed_at = datetime.now(UTC)
                    session.add(row)
                    session.commit()
                assert (
                    profile.token_endpoint is not None and profile.resource is not None
                )
                exchange_token(
                    endpoint=profile.token_endpoint,
                    form={
                        "grant_type": "refresh_token",
                        "refresh_token": old["refresh_token"],
                        "client_id": registration.client_id,
                        "resource": profile.resource,
                    },
                    task_deadline=network_deadline,
                    on_received=received,
                )
    except Exception:
        # 仅未发送的 CLAIMED 可以重调度；已发送而无 receipt 永远不重消费旧 refresh token。
        with bounded_session(database_engine, task_deadline=deadline) as session:
            _, row = _locked(session, attempt_id)
            if row.claim_id == claim_id:
                if row.status == "REQUEST_ARMED":
                    _finish(row, "OUTCOME_UNKNOWN", "mcp_refresh_unknown")
                elif row.status == "CLAIMED":
                    row.status = "PENDING"
                    row.claim_id = None
                    row.claimed_until = None
                session.add(row)
                session.commit()
            status = row.status
        if status != "CANDIDATE_READY":
            return status
    return _publish(
        database_engine=database_engine,
        context=context,
        attempt_id=attempt_id,
        task_deadline=deadline,
    )
