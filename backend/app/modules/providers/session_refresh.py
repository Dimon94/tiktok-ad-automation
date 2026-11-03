"""Bounded session-only renewal driven by existing durable provider deliveries.

Each advance performs at most one HTTP call after committing its connection-level
lease. It never retries the caller's business request. The previous application
proof generation survives only while discovered authority/configuration matches.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant

from .adapters.contract import failure
from .adapters.duiba import DuibaClient
from .adapters.gangganhao import GangganhaoClient
from .adapters.jiashu import JiashuClient
from .adapters.minglue import MinglueClient
from .adapters.registry import credential_fields
from .adapters.rongliang import RongliangClient
from .adapters.wangyan import WangyanClient
from .models import ProviderApplication, ProviderConnection, ProviderSessionRefresh

REFRESHING = "provider_session_refreshing"
LEASE_SECONDS = 60  # existing prefork task hard limit is 45 seconds
MAX_ATTEMPTS = 3
COOLDOWN_SECONDS = 600


def _locked(
    session: Session, context: TenantContext, identity: UUID
) -> ProviderConnection:
    from .connections import _connection

    return _connection(session, context, identity, lock=True)


def mark_expired(
    *,
    database_engine: Any,
    context: TenantContext,
    connection_id: UUID,
    version: int,
    verification: UUID | None,
    ciphertext: str,
) -> None:
    """A late error from the old session cannot invalidate a newer login."""
    with Session(database_engine) as session, session.begin():
        row = _locked(session, context, connection_id)
        if (
            row.credential_version != version
            or row.verification_token != verification
            or row.encrypted_credentials != ciphertext
            or row.status != "active"
        ):
            return
        row.status, row.error_code = "reauth_required", REFRESHING
        refresh = session.get(ProviderSessionRefresh, connection_id)
        if refresh is not None and refresh.phase == "complete":
            refresh.phase, refresh.attempts, refresh.option_index = "login", 0, 0
            refresh.candidate_ciphertext, refresh.applications = None, []
            refresh.claim_token, refresh.claimed_until, refresh.due_at = (
                None,
                None,
                None,
            )


def _publish(
    session: Session, row: ProviderConnection, refresh: ProviderSessionRefresh
) -> None:
    apps = session.exec(
        select(ProviderApplication).where(
            ProviderApplication.tenant_id == row.tenant_id,
            ProviderApplication.connection_id == row.id,
        )
    ).all()
    known = {app.external_id: app for app in apps}
    observed = {item["external_id"]: item for item in refresh.applications}
    changed = False
    for key, old in known.items():
        # Compare only previously usable applications; historical removed apps do
        # not invalidate unchanged sessions forever.
        if old.channel_config.get("verification_token") != str(row.verification_token):
            continue
        item = observed.get(key)
        old_config = {
            k: v for k, v in old.channel_config.items() if k != "verification_token"
        }
        if (
            item is None
            or old_config != item["channel_config"]
            or (
                item["tiktok_minis_id"] is not None
                and old.tiktok_minis_id != item["tiktok_minis_id"]
            )
        ):
            changed = True
    verification = uuid4() if changed else row.verification_token
    for key, item in observed.items():
        app = known.get(key)
        if app is None:
            app = ProviderApplication(
                tenant_id=row.tenant_id,
                connection_id=row.id,
                external_id=key,
                name=item["name"],
            )
            session.add(app)
        app.name = item["name"]
        app.channel_config = {
            **item["channel_config"],
            "verification_token": str(verification),
        }
        if item["tiktok_minis_id"] is not None:
            app.tiktok_minis_id = item["tiktok_minis_id"]
    assert refresh.candidate_ciphertext is not None
    row.encrypted_credentials = refresh.candidate_ciphertext
    row.verification_token = verification
    row.status, row.error_code, row.verified_at = "active", None, datetime.now(UTC)
    refresh.phase, refresh.candidate_ciphertext = "complete", None
    refresh.applications = []
    refresh.unavailable_since, refresh.cooldown_rounds = None, 0


def advance_session_refresh(
    *,
    database_engine: Any,
    context: TenantContext,
    connection_id: UUID,
    action: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Advance once, then yield the original task to its durable continuation."""
    now, claim = datetime.now(UTC), uuid4()
    with Session(database_engine) as session, session.begin():
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=action,
        )
        row = _locked(session, context, connection_id)
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=action,
        )
        if row.status == "active":
            return
        if row.status != "reauth_required" or row.verification_token is None:
            raise failure(
                row.error_code
                if row.error_code
                in {"provider_auth_failed", "provider_session_refresh_failed"}
                else "connection_unavailable"
            )
        refresh = session.get(ProviderSessionRefresh, connection_id)
        if refresh is None:
            refresh = ProviderSessionRefresh(
                connection_id=connection_id,
                tenant_id=context.tenant_id,
                credential_version=row.credential_version,
                verification_token=row.verification_token,
                window_started_at=now,
            )
            session.add(refresh)
        if (
            refresh.credential_version != row.credential_version
            or refresh.verification_token != row.verification_token
        ):
            # Only a successfully verified replacement may start another generation.
            refresh.credential_version, refresh.verification_token = (
                row.credential_version,
                row.verification_token,
            )
            refresh.phase, refresh.attempts, refresh.login_attempts = "login", 0, 0
            refresh.option_index, refresh.applications, refresh.candidate_ciphertext = (
                0,
                [],
                None,
            )
            refresh.claim_token, refresh.claimed_until, refresh.due_at = (
                None,
                None,
                None,
            )
            refresh.window_started_at = now
        if refresh.phase == "failed":
            raise failure(row.error_code or "provider_session_refresh_failed")
        if refresh.claimed_until and refresh.claimed_until > now:
            raise failure(REFRESHING, retryable=True)
        if refresh.due_at and refresh.due_at > now:
            raise failure(REFRESHING, retryable=True)
        if refresh.window_started_at < now - timedelta(minutes=10):
            refresh.login_attempts, refresh.window_started_at = 0, now
        if refresh.attempts >= MAX_ATTEMPTS or (
            refresh.phase == "login" and refresh.login_attempts >= MAX_ATTEMPTS
        ):
            # A finite burst ends in persisted cooldown, not a credential error.
            # The original item wakes at due_at; no worker or connection is held.
            refresh.due_at = now + timedelta(seconds=COOLDOWN_SECONDS)
            refresh.cooldown_rounds += 1
            if refresh.unavailable_since is None:
                refresh.unavailable_since = now
            refresh.attempts, refresh.login_attempts = 0, 0
            refresh.window_started_at = now
            row.error_code = REFRESHING
            exhausted = True
        else:
            exhausted = False
            refresh.claim_token, refresh.claimed_until = (
                claim,
                now + timedelta(seconds=LEASE_SECONDS),
            )
            refresh.attempts += 1
            if refresh.phase == "login":
                refresh.login_attempts += 1
            phase, index, kind = refresh.phase, refresh.option_index, row.kind
            version, verification = row.credential_version, row.verification_token
            ciphertext = (
                row.encrypted_credentials
                if phase == "login"
                else refresh.candidate_ciphertext
            )
            if ciphertext is None:
                raise failure("provider_state_invalid")
            credentials = decrypt_credentials(
                tenant_id=context.tenant_id, ciphertext=ciphertext
            )
            applications = [dict(item) for item in refresh.applications]
    if exhausted:
        raise failure(REFRESHING, retryable=True)
    try:
        if kind not in {"jiashu", "wangyan", "duiba", "gangganhao", "rongliang", "minglue"}:
            raise failure("provider_unavailable", retryable=True)
        with httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False, timeout=30
        ) as http:
            if phase == "login":
                try:
                    fields = credential_fields(kind)
                except DomainError:
                    raise failure("provider_request_invalid") from None
                if any(not credentials.get(k) for k in fields):
                    raise failure("provider_auth_failed")
                try:
                    if kind == "jiashu":
                        client = JiashuClient.login(
                            http,
                            username=credentials["username"],
                            password=credentials["password"],
                        )
                        credentials["session"] = client.session
                    elif kind == "minglue":
                        minglue = MinglueClient.login(http, account=credentials["account"], password=credentials["password"])
                        credentials["token"] = minglue.token
                    elif kind == "duiba":
                        duiba = DuibaClient.login(
                            http,
                            account=credentials["account"],
                            password=credentials["password"],
                        )
                        credentials["token"] = duiba.token
                    elif kind == "gangganhao":
                        ggh = GangganhaoClient.login(
                            http,
                            portal_id=credentials["portal_id"],
                            username=credentials["username"],
                            password=credentials["password"],
                        )
                        credentials["token"] = ggh.token
                    elif kind == "rongliang":
                        rongliang = RongliangClient.login(
                            http,
                            email=credentials["email"],
                            password=credentials["password"],
                            use_curl=True,
                        )
                        credentials["token"] = rongliang.token
                    else:
                        other = WangyanClient.login(
                            http,
                            email=credentials["email"],
                            password=credentials["password"],
                        )
                        credentials["token"] = other.token
                except DomainError as error:
                    if error.code in {"provider_session_expired", "provider_rejected"}:
                        raise failure("provider_auth_failed") from None
                    raise
                candidate = encrypt_credentials(
                    tenant_id=context.tenant_id, value=credentials
                )
            elif kind == "jiashu":
                client = JiashuClient(
                    http, session=credentials["session"], application_id=""
                )
                if phase == "applications":
                    applications = client.application_list()
                else:
                    applications[index]["channel_config"] = client.application_options(
                        applications[index]["external_id"]
                    )
            elif kind == "minglue":
                applications = MinglueClient(http, token=credentials["token"]).discover_applications()
            elif kind == "duiba":
                applications = DuibaClient(
                    http, token=credentials["token"]
                ).discover_applications()
            elif kind == "gangganhao":
                applications = GangganhaoClient(
                    http, token=credentials["token"]
                ).discover_applications()
            elif kind == "rongliang":
                applications = RongliangClient.login(
                    http,
                    email=credentials["email"],
                    password=credentials["password"],
                    use_curl=True,
                ).discover_applications()
            else:
                applications = WangyanClient(
                    http, token=credentials["token"], application_id=""
                ).discover_applications()
        with Session(database_engine) as session, session.begin():
            row = _locked(session, context, connection_id)
            refresh = session.get(ProviderSessionRefresh, connection_id)
            if (
                refresh is None
                or refresh.claim_token != claim
                or row.credential_version != version
                or row.verification_token != verification
                or row.status != "reauth_required"
            ):
                raise failure("provider_credentials_changed")
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action=action,
            )
            if phase == "login":
                refresh.candidate_ciphertext, refresh.phase = candidate, "applications"
            else:
                refresh.applications = applications
                if phase == "options":
                    refresh.option_index += 1
                if kind in {"wangyan", "duiba", "gangganhao", "rongliang", "minglue"} or (
                    phase == "options" and refresh.option_index == len(applications)
                ):
                    _publish(session, row, refresh)
                else:
                    refresh.phase = "options"
            (
                refresh.claim_token,
                refresh.claimed_until,
                refresh.due_at,
                refresh.attempts,
            ) = None, None, None, 0
    except Exception as error:
        code = (
            error.code
            if isinstance(error, DomainError)
            else "provider_unavailable"
            if isinstance(error, SoftTimeLimitExceeded)
            else "provider_state_invalid"
        )
        with Session(database_engine) as session, session.begin():
            row = _locked(session, context, connection_id)
            refresh = session.get(ProviderSessionRefresh, connection_id)
            if (
                refresh is not None
                and refresh.claim_token == claim
                and row.credential_version == version
                and row.verification_token == verification
                and row.status == "reauth_required"
            ):
                refresh.claim_token, refresh.claimed_until = None, None
                if code == "provider_session_expired" and phase != "login":
                    # A candidate can expire between durable discovery pages.
                    # Restart only authentication, retaining the login burst cap.
                    refresh.phase, refresh.attempts, refresh.option_index = (
                        "login",
                        0,
                        0,
                    )
                    refresh.candidate_ciphertext, refresh.applications = None, []
                    refresh.due_at = datetime.now(UTC) + timedelta(seconds=5)
                elif code in {
                    "provider_unavailable",
                    "tenant_forbidden",
                    "action_forbidden",
                }:
                    if (
                        code == "provider_unavailable"
                        and refresh.unavailable_since is None
                    ):
                        refresh.unavailable_since = datetime.now(UTC)
                    refresh.due_at = datetime.now(UTC) + timedelta(
                        seconds=5 * 2**refresh.attempts
                    )
                else:
                    refresh.phase, refresh.candidate_ciphertext = "failed", None
                    row.status, row.error_code = "error", code
        if code == "provider_unavailable" or (
            code == "provider_session_expired" and phase != "login"
        ):
            raise failure(REFRESHING, retryable=True) from None
        raise failure(code) from None
    raise failure(REFRESHING, retryable=True)
