"""每个物理 SDK 请求独立准入；硬任务期限由工厂/worker 负责。"""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import business_api_client as sdk
from business_api_client.rest import ApiException
from business_api_client.tiktok_business.tiktok_exceptions import TiktokSDKError
from urllib3.exceptions import HTTPError, TimeoutError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import (
    AuthorizationFacts,
    CandidateReadContext,
    RuntimeReadContext,
)
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.integrations.tiktok.contracts.discovery import ObservedAuthorization
from app.integrations.tiktok.discovery_read import DiscoveryReadAdapter
from app.integrations.tiktok.sdk import AccountAdmissionDeferred, checked_data

RequestScope = Callable[[str | None, str, datetime], AbstractContextManager[None]]


def remaining(deadline: datetime) -> float:
    if (
        not isinstance(deadline, datetime)
        or deadline.tzinfo is None
        or deadline.utcoffset() is None
    ):
        raise DomainError("read_deadline_invalid", "读取期限无效")
    value = (deadline - datetime.now(UTC)).total_seconds()
    if value <= 0:
        raise DomainError("read_deadline_exceeded", "读取期限已到")
    return value


@contextmanager
def _strict_sdk_envelope(
    client: Any, *, exact: dict[str, Any] | None = None, operation: str | None = None
) -> Iterator[None]:
    # 固定 SDK 会把 code 经 int() 转换，再丢掉 code/message；必须在其
    # deserialize 入口检查原始 JSON，不能把已经转换的模型当原始证据。
    original = client.deserialize
    missing = object()
    instance_original = client.__dict__.get("deserialize", missing)

    def deserialize(response: Any, response_type: Any) -> Any:
        try:
            if len(response.data) > 8 * 1024 * 1024:
                raise ValueError("oversized provider response")
            raw = json.loads(response.data)
        except ValueError, TypeError, AttributeError, RecursionError:
            raise DomainError(
                "tiktok_response_error", "TikTok 返回不支持的数据结构"
            ) from None
        if (
            type(raw) is not dict
            or type(raw.get("code")) is not int
            or (raw["code"] == 0 and type(raw.get("data")) is not dict)
        ):
            raise DomainError("tiktok_response_error", "TikTok 返回不支持的数据结构")
        if raw["code"] == 40100:
            # 明确的配额类别保留平台要求；未知类别不冒充 QPM。
            message = str(raw.get("message") or "").lower()
            getheaders = getattr(response, "getheaders", None)
            headers = (
                getheaders()
                if callable(getheaders)
                else getattr(response, "headers", None)
            )
            retry_after = _provider_retry_after_ms(headers)
            if "qpd" in message or "per day" in message:
                now = datetime.now(UTC)
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=5, microsecond=0
                )
                delay, reason = int((tomorrow - now).total_seconds() * 1000), "qpd"
            elif "qpm" in message or "per minute" in message:
                delay, reason = 300_000, "qpm"
            elif "qps" in message or "per second" in message:
                delay, reason = 1_000, "qps"
            elif retry_after is not None:
                delay, reason = retry_after, "retry_after"
            else:
                # 报表未知类型交共享探测轮退避；不能先写五分钟冷却再让任务五秒重试。
                # 非报表继续保守等待，不把未知类型冒充平台QPS。
                delay = (
                    5_000 if operation and operation.startswith("reports.") else 300_000
                )
                reason = "40100"
            raise AccountAdmissionDeferred(
                max(delay, retry_after or 0),
                source="provider",
                reason=reason,
                shared_throttle_probe=bool(
                    reason == "40100" and operation and operation.startswith("reports.")
                ),
            )
        if (
            operation == "scene.get_identity_video"
            and raw["code"] == 40000
            and "unable to fetch a valid item" in str(raw.get("message") or "").lower()
        ):
            raise DomainError(
                "tiktok_identity_item_unavailable", "平台未提供该帖子内容"
            )
        if (
            exact is not None
            and raw["code"] != 0
            and any(
                marker in str(raw.get("message") or "").lower()
                for marker in ("permission", "权限")
            )
        ):
            # 只保留权限分类，不把平台原文（可能含账户信息）写入任务或页面。
            raise DomainError("report_permission_denied", "报表权限不足")
        if exact is not None and raw["code"] == 40002:
            raise DomainError("report_query_invalid", "报表参数被平台拒绝")
        if exact is not None and raw["code"] == 0:
            # 只对显式选择精确数值的读取保留原始十进制文本，不能从 SDK float 反推。
            exact["data"] = json.loads(response.data, parse_float=str)["data"]
        # 整数非零业务码仍交由原 SDK 抛错，再由 invoke 的既有分支脱敏。
        return original(response, response_type)

    client.deserialize = deserialize
    try:
        yield
    finally:
        # 只包装本次同步发送；成功、失败与任务中断均恢复工厂拥有的客户端。
        if instance_original is missing:
            del client.deserialize
        else:
            client.deserialize = instance_original


# 平台未给出 Retry-After 时的初始等待。
_THROTTLE_DEFAULT_MS = 5_000


def _provider_retry_after_ms(headers: object) -> int | None:
    """有效的平台等待指令优先；同时接受秒数和 HTTP 日期，不擅自缩短。"""
    getter = getattr(headers, "get", None)
    value = (
        (getter("Retry-After") or getter("retry-after")) if callable(getter) else None
    )
    if value is None:
        return None
    try:
        seconds = int(str(value).strip())
        if seconds < 0:
            return None
        return max(1_000, seconds * 1_000)
    except ValueError:
        try:
            target = parsedate_to_datetime(str(value))
            return max(1_000, int((target - datetime.now(UTC)).total_seconds() * 1000))
        except ValueError, TypeError, OverflowError:
            return None


class OfficialReadRequests:
    def __init__(self, client: Any, *, request_scope: RequestScope, deadline: datetime):
        remaining(deadline)
        self.client = client
        self._scope = request_scope
        self._deadline = deadline

    def invoke(
        self,
        operation: str,
        advertiser_id: str | None,
        send: Callable[[tuple[float, float]], object],
        *,
        exact_numbers: bool = False,
    ) -> McpBusinessResponse:
        remaining(self._deadline)
        with self._scope(advertiser_id, operation, self._deadline):
            budget = remaining(self._deadline)
            try:
                exact: dict[str, Any] = {}
                with _strict_sdk_envelope(
                    self.client,
                    exact=exact if exact_numbers else None,
                    operation=operation,
                ):
                    response = send((min(5.0, budget), min(30.0, budget)))
            except ApiException as error:
                # 只读 GET 被平台限流时请求未被执行，必须释放 Worker 并进入既有的
                # 额度延后/退避重试路径；不能当作终态错误让任务直接失败。
                if error.status == 429:
                    retry_after = _provider_retry_after_ms(error.headers)
                    raise AccountAdmissionDeferred(
                        retry_after
                        if retry_after is not None
                        else _THROTTLE_DEFAULT_MS,
                        source="provider",
                        reason="retry_after" if retry_after is not None else "http_429",
                    ) from None
                raise DomainError(
                    "tiktok_response_error", "TikTok 请求未成功"
                ) from None
            except TimeoutError:
                code = (
                    "report_timeout"
                    if operation.startswith("reports.")
                    else "tiktok_response_error"
                )
                raise DomainError(code, "TikTok 读取超时") from None
            except TiktokSDKError, HTTPError:
                raise DomainError(
                    "tiktok_response_error", "TikTok 请求未成功"
                ) from None
            remaining(self._deadline)
            data = checked_data(response)
            if exact_numbers:
                if "data" not in exact:
                    raise DomainError("tiktok_response_error", "缺少原始数值回执")
                data = exact["data"]
            request_id = (
                response.get("request_id")
                if isinstance(response, dict)
                else getattr(response, "request_id", None)
            )
            if type(request_id) is not str or len(request_id) > 128:
                request_id = None
            return McpBusinessResponse(data, CallEvidence(request_id=request_id))


class OfficialAccountsGateway(DiscoveryReadAdapter):
    def __init__(
        self,
        client: Any,
        *,
        context: RuntimeReadContext | CandidateReadContext,
        authorization: AuthorizationFacts,
        app_id: str,
        secret: str,
        request_scope: RequestScope,
        deadline: datetime,
        observation_authorization: AuthorizationFacts | None = None,
    ):
        super().__init__(context=context, authorization=authorization)
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )
        self._app_id = app_id
        self._secret = secret
        self._observation_authorization = observation_authorization

    def observe_authorization(self) -> ObservedAuthorization:
        # 固定 SDK 未暴露主体读取合同；只保留实际 OAuth scope，主体与grant不臆造。
        return ObservedAuthorization(
            facts=self._observation_authorization or self.authorization_facts(),
            evidence=CallEvidence(),
        )

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        client = self._requests.client
        token = client.default_headers["Access-Token"]

        def send(timeout: tuple[float, float]) -> object:
            kwargs = {**arguments, "_request_timeout": timeout}
            if operation == "accounts.list_bcs":
                return sdk.BCApi(client).bc_get(token, **kwargs)
            if operation == "accounts.list_bc_assets":
                return sdk.BCApi(client).bc_asset_get(access_token=token, **kwargs)
            if operation == "accounts.list_authorized_advertisers":
                return sdk.AuthenticationApi(client).oauth2_advertiser_get(
                    self._app_id, self._secret, token, **kwargs
                )
            if operation == "accounts.get_advertisers":
                return sdk.AccountManagementApi(client).advertiser_info(
                    access_token=token, **kwargs
                )
            raise DomainError("read_operation_invalid", "读取操作无效")

        return self._requests.invoke(operation, None, send)
