"""Tenant-bound encrypted connections and independently owned provider sessions.

Verification owns short database transactions and never holds one over HTTP.
Session renewal is driven by bounded durable business continuations; no cross-
account fallback or implicit replay of a provider write is permitted.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

from .adapters.contract import ProviderClient, ProviderSession, failure
from .adapters.duiba import DuibaClient
from .adapters.gangganhao import GangganhaoClient
from .adapters.jiashu import JiashuClient
from .adapters.minglue import MinglueClient
from .adapters.registry import adapter_for_kind, credential_fields
from .adapters.rongliang import RongliangClient
from .adapters.wangyan import WangyanClient
from .models import ProviderApplication, ProviderConnection


def _connection(
    session: Session, context: TenantContext, connection_id: UUID, *, lock: bool = False
) -> ProviderConnection:
    statement = (
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == context.tenant_id,
            ProviderConnection.id == connection_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    row = session.exec(statement).one_or_none()
    if row is None:
        raise failure("resource_not_found")
    return row


def save_connection(
    session: Session,
    *,
    context: TenantContext,
    kind: str,
    display_name: str,
    credentials: dict[str, str],
    connection_id: UUID | None = None,
) -> ProviderConnection:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    try:
        fields = credential_fields(kind)
    except DomainError:
        raise failure("provider_request_invalid") from None
    if (
        kind not in {"jiashu", "wangyan", "duiba", "gangganhao", "rongliang", "minglue"}
        or not isinstance(credentials, dict)
        or set(credentials) != fields
        or any(
            not isinstance(v, str) or not v or len(v) > 16384
            for v in credentials.values()
        )
        or not display_name.strip()
        or len(display_name) > 255
    ):
        raise failure("provider_request_invalid")
    ciphertext = encrypt_credentials(tenant_id=context.tenant_id, value=credentials)
    if connection_id:
        row = _connection(session, context, connection_id, lock=True)
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="manage",
        )
        if row.status == "disabled" or row.kind != kind:
            raise failure("connection_unavailable")
        row.encrypted_credentials = ciphertext
        row.credential_version += 1
        row.status = "pending"
        row.display_name = display_name.strip()
        row.verification_token = None
        row.verifying_started_at = None
        row.error_code = None
    else:
        row = ProviderConnection(
            tenant_id=context.tenant_id,
            kind=kind,
            display_name=display_name.strip(),
            encrypted_credentials=ciphertext,
        )
        session.add(row)
        session.flush()
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="provider.connection.save",
            target_id=str(row.id),
        )
    )
    session.flush()
    return row


def verify_connection(
    *,
    database_engine: Any,
    context: TenantContext,
    connection_id: UUID,
    transport: httpx.BaseTransport | None = None,
) -> None:
    claim, now = uuid4(), datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="manage",
        )
        row = _connection(session, context, connection_id, lock=True)
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="manage",
        )
        if row.status == "disabled" or row.kind == "other":
            raise failure("connection_unavailable")
        if (
            row.status == "verifying"
            and row.verifying_started_at
            and row.verifying_started_at > now - timedelta(minutes=5)
        ):
            raise failure("provider_verification_in_progress")
        if not row.encrypted_credentials:
            raise failure("connection_unavailable")
        credentials = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=row.encrypted_credentials
        )
        kind, version = row.kind, row.credential_version
        row.status, row.verification_token, row.verifying_started_at = (
            "verifying",
            claim,
            now,
        )
        row.error_code = None
    logged_in = False
    try:
        if kind not in {"jiashu", "wangyan", "duiba", "gangganhao", "rongliang", "minglue"}:
            raise failure("provider_unavailable", retryable=True)
        with httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False, timeout=30
        ) as http:
            if kind == "jiashu":
                client = JiashuClient.login(
                    http,
                    username=credentials["username"],
                    password=credentials["password"],
                )
                logged_in = True
                applications = client.discover_applications()
                credentials["session"] = client.session
            elif kind == "minglue":
                minglue = MinglueClient.login(http, account=credentials["account"], password=credentials["password"])
                logged_in = True
                applications = minglue.discover_applications()
                credentials["token"] = minglue.token
            elif kind == "duiba":
                duiba = DuibaClient.login(
                    http,
                    account=credentials["account"],
                    password=credentials["password"],
                )
                logged_in = True
                applications = duiba.discover_applications()
                credentials["token"] = duiba.token
            elif kind == "gangganhao":
                ggh = GangganhaoClient.login(
                    http,
                    portal_id=credentials["portal_id"],
                    username=credentials["username"],
                    password=credentials["password"],
                )
                logged_in = True
                applications = ggh.discover_applications()
                credentials["token"] = ggh.token
            elif kind == "rongliang":
                rongliang = RongliangClient.login(
                    http,
                    email=credentials["email"],
                    password=credentials["password"],
                    use_curl=True,
                )
                logged_in = True
                applications = rongliang.discover_applications()
                credentials["token"] = rongliang.token
            else:
                other = WangyanClient.login(
                    http, email=credentials["email"], password=credentials["password"]
                )
                logged_in = True
                applications = other.discover_applications()
                credentials["token"] = other.token
        with Session(database_engine) as session, session.begin():
            row = _connection(session, context, connection_id, lock=True)
            if (
                row.verification_token != claim
                or row.credential_version != version
                or row.status != "verifying"
            ):
                raise failure("version_conflict")
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="manage",
            )
            for item in applications:
                application = session.exec(
                    select(ProviderApplication).where(
                        ProviderApplication.tenant_id == context.tenant_id,
                        ProviderApplication.connection_id == connection_id,
                        ProviderApplication.external_id == item["external_id"],
                    )
                ).one_or_none()
                if application is None:
                    application = ProviderApplication(
                        tenant_id=context.tenant_id,
                        connection_id=connection_id,
                        external_id=item["external_id"],
                        name=item["name"],
                    )
                    session.add(application)
                application.name = item["name"]
                application.channel_config = {
                    **item["channel_config"],
                    "verification_token": str(claim),
                }
                # 版权方未提供 Minis ID 时保留管理员配置的关联；包名不冒充 Minis ID。
                if item["tiktok_minis_id"] is not None:
                    application.tiktok_minis_id = item["tiktok_minis_id"]
            row.encrypted_credentials = encrypt_credentials(
                tenant_id=context.tenant_id, value=credentials
            )
            row.status, row.verified_at = "active", datetime.now(UTC)
            row.verifying_started_at = None
            row.error_code = None
            # Keep the successful generation token: unseen historical apps remain
            # stored for references but cannot pass the session-opening check.
            session.add(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    action="provider.connection.verify",
                    target_id=str(connection_id),
                )
            )
    except Exception as error:
        code = error.code if isinstance(error, DomainError) else "provider_unavailable"
        if code in {"provider_session_expired", "provider_rejected"} and not logged_in:
            code = "provider_auth_failed"
        with Session(database_engine) as session, session.begin():
            row = _connection(session, context, connection_id, lock=True)
            if (
                row.verification_token == claim
                and row.credential_version == version
                and row.status == "verifying"
            ):
                row.status = "error"
                row.error_code = code
                row.verification_token = None
                row.verifying_started_at = None
        if isinstance(error, DomainError):
            raise failure(code, retryable=error.retryable) from None
        raise failure("provider_unavailable", retryable=True) from None


@contextmanager
def open_provider_session(
    *,
    database_engine: Any,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    action: str = "read",
    transport: httpx.BaseTransport | None = None,
) -> Iterator[ProviderSession]:
    if action not in {"read", "provider_write"}:
        raise failure("provider_request_invalid")
    from .session_refresh import advance_session_refresh, mark_expired

    # Release this snapshot before the refresh driver opens its short claim txn.
    with Session(database_engine) as snapshot:
        require_tenant(
            snapshot,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=action,
        )
        pending = (
            _connection(snapshot, context, connection_id).status == "reauth_required"
        )
    if pending:
        advance_session_refresh(
            database_engine=database_engine,
            context=context,
            connection_id=connection_id,
            action=action,
            transport=transport,
        )
    with Session(database_engine) as session:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=action,
        )
        row = _connection(session, context, connection_id)
        if row.status != "active" or row.verification_token is None:
            raise failure("connection_unavailable")
        app = session.exec(
            select(ProviderApplication).where(
                ProviderApplication.tenant_id == context.tenant_id,
                ProviderApplication.connection_id == connection_id,
                ProviderApplication.external_id == application_id,
            )
        ).one_or_none()
        if app is None or app.channel_config.get("verification_token") != str(
            row.verification_token
        ):
            raise failure("provider_application_forbidden")
        if not row.encrypted_credentials:
            raise failure("connection_unavailable")
        credentials = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=row.encrypted_credentials
        )
        kind, version, claim = row.kind, row.credential_version, row.verification_token
        ciphertext = row.encrypted_credentials
        prefix = app.channel_config.get("channel_prefix", "")
    try:
        with httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False, timeout=30
        ) as http:
            client: ProviderClient
            if kind not in {"jiashu", "wangyan", "duiba", "gangganhao", "rongliang", "minglue"}:
                raise failure("provider_unavailable", retryable=True)
            if kind in {"duiba", "gangganhao", "rongliang", "minglue"}:
                adapter = adapter_for_kind(
                    kind, http, credentials, application_id=application_id, channel_config=app.channel_config
                )
                if not isinstance(adapter, (DuibaClient, GangganhaoClient, RongliangClient, MinglueClient)):
                    raise failure("provider_state_invalid")
                client = adapter
                if isinstance(client, GangganhaoClient):
                    client.delivery_mode = str(app.channel_config.get("delivery_mode", "iaa"))
            elif kind == "jiashu":
                client = JiashuClient(
                    http,
                    session=credentials["session"],
                    application_id=application_id,
                    channel_prefix=prefix,
                )
            else:
                client = WangyanClient(
                    http, token=credentials["token"], application_id=application_id
                )
            yield ProviderSession(
                connection_id=connection_id,
                application_id=application_id,
                http=http,
                client=client,
            )
    except DomainError as error:
        if error.code == "provider_session_expired":
            mark_expired(
                database_engine=database_engine,
                context=context,
                connection_id=connection_id,
                version=version,
                verification=claim,
                ciphertext=ciphertext,
            )
            raise failure("provider_session_refreshing", retryable=True) from None
        raise
