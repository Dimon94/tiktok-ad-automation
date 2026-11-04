"""Official SDK request scope; callers admit *each* generated method invocation.

OAuth runs in a deadline-bounded child. Resource/build workers must set a Celery
prefork hard time limit shorter than their admission lease minus cleanup margin.
Socket timeouts alone do not bound DNS, response streaming, or the whole task.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from errno import ECONNREFUSED, ECONNRESET, EHOSTUNREACH, ENETUNREACH, ETIMEDOUT
from typing import Any, cast
from uuid import UUID

import business_api_client  # type: ignore[import-untyped]
import business_api_client.tiktok_business.tiktok_exceptions as sdk_errors  # type: ignore[import-untyped]
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from business_api_client.rest import ApiException  # type: ignore[import-untyped]
from redis import Redis
from sqlmodel import Session
from urllib3.exceptions import HTTPError, NewConnectionError
from urllib3.exceptions import TimeoutError as UrllibTimeoutError
from urllib3.util.retry import Retry

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.transport_diagnostics import install_transport_tracking
from app.jobs.admission import AdmissionPolicy, admit_call, admitted_scope, release_call
from app.modules.accounts.models import TikTokConnection
from app.modules.tenants.permissions import require_tenant

SDK_SCOPE_INTERRUPTS = (SystemExit, KeyboardInterrupt, SoftTimeLimitExceeded)


def connection_setup_failure_code(client: Any, error: BaseException) -> str | None:
    """只认本SDK客户端在HTTP请求前亲自观察到的异常，不按错误名字猜副作用。"""
    state = getattr(client, "_tiktok_connection_setup_state", None)
    marker = state.get("marker") if isinstance(state, dict) else None
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (
            marker is not None
            and getattr(current, "_tiktok_before_http", None) is marker
        ):
            transient = isinstance(
                current, (TimeoutError, UrllibTimeoutError, NewConnectionError)
            ) or (
                isinstance(current, OSError)
                and current.errno
                in {ECONNREFUSED, ECONNRESET, EHOSTUNREACH, ENETUNREACH, ETIMEDOUT}
            )
            return (
                "tiktok_connection_not_sent"
                if transient
                else "tiktok_connection_setup_failed"
            )
        for cause in (
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
        ):
            if isinstance(cause, BaseException):
                pending.append(cause)
    return None


def begin_connection_setup_tracking(client: Any) -> None:
    """每次创建独立nonce，异常上下文里的旧失败不能证明本次没有发送。"""
    state = getattr(client, "_tiktok_connection_setup_state", None)
    if isinstance(state, dict):
        state["marker"] = object()
        state["transport_trace"] = None


class AccountAdmissionDeferred(DomainError):
    def __init__(
        self,
        retry_after_ms: int,
        *,
        source: str = "local",
        reason: str | None = None,
        shared_throttle_probe: bool = False,
    ):
        super().__init__("admission_deferred", "调用额度暂不可用", retryable=True)
        self.retry_after_ms = retry_after_ms
        self.admission_source = source
        self.admission_reason = reason
        self.shared_throttle_probe = shared_throttle_probe


def checked_data(response: object) -> dict[str, Any]:
    # The pinned sync ApiClient checks nonzero codes then returns this envelope,
    # dropping code/message; do not expect its generated InlineResponse200 here.
    if isinstance(response, dict):
        if set(response) != {"data", "request_id"} or not isinstance(
            response["data"], dict
        ):
            raise DomainError("tiktok_response_error", "TikTok 返回不支持的数据结构")
        return cast(dict[str, Any], response["data"])
    to_dict = getattr(response, "to_dict", None)
    raw = to_dict() if callable(to_dict) else None
    if (
        not isinstance(raw, dict)
        or type(raw.get("code")) is not int
        or raw["code"] != 0
        or not isinstance(raw.get("data"), dict)
    ):
        raise DomainError("tiktok_response_error", "TikTok 返回错误或不支持的数据结构")
    return cast(dict[str, Any], raw["data"])


@contextmanager
def official_client(
    *, access_token: str | None = None
) -> Iterator[business_api_client.ApiClient]:
    """Fresh SDK client, no automatic retry/redirect, and no wire logging."""
    configuration = business_api_client.Configuration()
    configuration.debug = False
    # The pinned REST client logs full bodies independently of debug, and urllib3
    # logs URLs containing OAuth query credentials. Disable those exact loggers.
    for name in (
        "business_api_client.rest",
        "urllib3.connectionpool",
        "urllib3.poolmanager",
        "urllib3.util.retry",
    ):
        logging.getLogger(name).disabled = True
    client = business_api_client.ApiClient(configuration=configuration)
    install_transport_tracking(client)
    if access_token is not None:
        client.default_headers["Access-Token"] = access_token
    client.rest_client.pool_manager.connection_pool_kw["retries"] = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0,
        status=0,
        other=0,
        raise_on_redirect=True,
    )
    try:
        yield client
    finally:
        client.default_headers.pop("Access-Token", None)
        client.last_response = None
        cleanup_interrupt: SoftTimeLimitExceeded | None = None
        try:
            client.rest_client.pool_manager.clear()
        except SoftTimeLimitExceeded as error:
            # Finishing a task while its SDK thread still runs cancels the task's
            # hard deadline. Cleanup must finish or be ended by the hard kill.
            cleanup_interrupt = error
        finally:
            while True:
                try:
                    client.pool.close()
                    client.pool.join()
                    break
                except SoftTimeLimitExceeded as error:
                    cleanup_interrupt = error
                    continue
                except Exception:
                    # An unjoinable owned SDK pool must not survive as a thread
                    # in a worker that starts accepting other tasks.
                    raise SystemExit("sdk_cleanup_incomplete") from None
        if cleanup_interrupt is not None:
            raise cleanup_interrupt


@contextmanager
def sdk_client(
    session: Session, *, context: TenantContext, connection_id: UUID
) -> Iterator[business_api_client.ApiClient]:
    settings.require_tiktok_app()
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != context.tenant_id
        or connection.status != "ACTIVE"
        or not connection.credential_ciphertext
    ):
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    token = decrypt_credentials(
        tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
    ).get("access_token")
    if not token:
        raise DomainError("credential_invalid", "凭据缺少访问令牌")
    with official_client(access_token=token) as client:
        try:
            yield client
        except ApiException, sdk_errors.TiktokSDKError, HTTPError:
            raise DomainError("tiktok_response_error", "TikTok 请求未成功") from None


@contextmanager
def admitted_account_call(
    redis_client: Redis,
    *,
    context: TenantContext,
    endpoint: str,
    advertiser_id: str,
    policy: AdmissionPolicy,
) -> Iterator[None]:
    settings.require_tiktok_app()
    physical = None
    if "sdk_level" in settings.TIKTOK_CALL_POLICIES:
        from app.integrations.tiktok.platform_quotas import sdk_endpoint
        from app.jobs.admission import admission_policy

        try:
            physical = sdk_endpoint(endpoint)
        except ValueError:
            raise DomainError(
                "admission_policy_invalid", "SDK物理接口额度尚未登记"
            ) from None
        policy = admission_policy(
            endpoint, channel="OFFICIAL_API", physical_endpoint=physical
        )
    with admitted_scope(
        redis_client,
        app_scope=settings.TIKTOK_APP_ID,
        endpoint=physical or endpoint,
        tenant_id=context.tenant_id,
        advertiser_id=advertiser_id,
        policy=policy,
        denied_error=AccountAdmissionDeferred,
        admit=admit_call,
        release=release_call,
        rate_group=physical,
    ):
        yield
