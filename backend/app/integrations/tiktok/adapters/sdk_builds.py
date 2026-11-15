"""工厂拥有的官方 SDK 广告创建与只读适配；逐次授权/准入复用已有请求边界。"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import business_api_client as sdk  # type: ignore[import-untyped]
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.build_results import (
    created_result,
    observed_cta,
    sdk_creation_envelope,
)
from app.integrations.tiktok.contracts.builds import (
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    CreatedObject,
    CreateIntent,
    CtaCreate,
)
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.group_isolation import (
    FrozenGroupIsolation,
    optimizer_rule_arguments,
    require_isolation_target,
)
from app.integrations.tiktok.official.accounts import (
    OfficialReadRequests,
    RequestScope,
    _strict_sdk_envelope,
    remaining,
)
from app.integrations.tiktok.sdk import (
    begin_connection_setup_tracking,
    connection_setup_failure_code,
)
from app.integrations.tiktok.transport_diagnostics import transport_snapshot
from app.modules.builds.readback_compare import parse_page, parse_status
from app.modules.builds.request_compiler import (
    create_arguments,
    read_arguments,
    status_arguments,
)

logger = logging.getLogger(__name__)


def _rejection_hints(raw: object) -> dict[str, list[str]]:
    """仅输出预定义标签；不保存平台原文、账户值、令牌或签名链接。"""
    message = raw.get("message") if isinstance(raw, dict) else None
    message = message[:4096].lower() if isinstance(message, str) else ""
    # 创建接口在 SDK 反序列化时也会抛错；错误码本身不足以定位拒绝字段。
    parameters = (
        "advertiser_id",
        "campaign_id",
        "adgroup_id",
        "schedule_start_time",
        "schedule_end_time",
        "schedule_type",
        "operation_status",
        "minis_id",
        "promotion_type",
        "optimization_goal",
        "optimization_event",
        "bid_type",
        "deep_bid_type",
        "roas_bid",
        "budget",
        "budget_mode",
        "targeting_spec",
        "location_ids",
        "placements",
        "placement_type",
        "identity_id",
        "identity_type",
        "image_id",
        "image_ids",
        "music_id",
        "ad_format",
        "creative_list",
        "creative_info",
        "creative_material_mode",
    )
    reasons = (
        "current time",
        "later than",
        "earlier than",
        "past",
        "future",
        "required",
        "invalid",
        "not supported",
        "not allowed",
        "not exist",
        "permission",
        "minimum",
        "maximum",
        "start time",
        "end time",
        "carousel",
        "image",
        "video",
        "disabled",
        "enabled",
    )
    return {
        "parameters": [
            word for word in parameters if re.search(rf"\b{word}\b", message)
        ],
        "reasons": [word for word in reasons if re.search(rf"\b{word}\b", message)],
    }


def _log_rejection(raw: object, *, kind: str) -> None:
    if isinstance(raw, dict) and type(raw.get("code")) is int and raw["code"] != 0:
        logger.error(
            "official TikTok create rejected: kind=%s code=%s hints=%s",
            kind,
            raw["code"],
            _rejection_hints(raw),
        )


class ApiBuildOperations:
    def read_cta_portfolio(self, *, advertiser_id: str, portfolio_id: str) -> CtaCreate:
        response = self._read(
            "build.get_cta_portfolio",
            advertiser_id,
            {"advertiser_id": advertiser_id, "creative_portfolio_id": portfolio_id},
        )
        return observed_cta(
            response=response, advertiser_id=advertiser_id, portfolio_id=portfolio_id
        )

    def __init__(
        self,
        client: Any,
        *,
        request_scope: RequestScope,
        deadline: datetime,
        isolation: FrozenGroupIsolation | None = None,
        before_disable: Callable[[], None] | None = None,
    ):
        self._request_scope, self._deadline = request_scope, deadline
        self._isolation = isolation
        self._before_disable = before_disable
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )

    def disable_adgroup(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> McpBusinessResponse:
        if self._before_disable is not None:
            self._before_disable()
        require_isolation_target(
            self._isolation, advertiser_id=advertiser_id, adgroup_id=adgroup_id
        )
        client = self._requests.client
        sent = False
        try:
            remaining(self._deadline)
            with self._request_scope(
                advertiser_id, "build.disable_adgroup", self._deadline
            ):
                budget = remaining(self._deadline)
                sent = True
                sdk.AdgroupApi(client).smart_plus_adgroup_status_update(
                    client.default_headers["Access-Token"],
                    body={
                        "advertiser_id": advertiser_id,
                        "adgroup_ids": [adgroup_id],
                        "operation_status": "DISABLE",
                    },
                    async_req=True,
                    _request_timeout=(min(5.0, budget), min(30.0, budget)),
                ).get()
                return sdk_creation_envelope(
                    json.loads(cast(HTTPResponse, client.last_response).data)
                )
        except RemoteCallError:
            raise
        except Exception:
            if not sent:
                raise
            # 停用失败也不能认为未发送；只允许后续读取状态，禁止盲重试。
            raise RemoteCallError(
                "group_isolation_result_unknown",
                effect="UNKNOWN",
                evidence=CallEvidence(),
            ) from None

    def list_optimizer_rules(
        self, *, advertiser_id: str, page: int = 1
    ) -> McpBusinessResponse:
        require_isolation_target(self._isolation, advertiser_id=advertiser_id)
        return self._read(
            "build.list_optimizer_rules",
            advertiser_id,
            optimizer_rule_arguments(advertiser_id=advertiser_id, page=page),
        )

    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject:
        operation, arguments = create_arguments(
            attempt_id=attempt_id, intent=intent, channel="OFFICIAL_API"
        )
        client = self._requests.client
        begin_connection_setup_tracking(client)
        methods = {
            "CAMPAIGN": sdk.CampaignCreationApi(client).smart_plus_campaign_create,
            "ADGROUP": sdk.AdgroupApi(client).smart_plus_adgroup_create,
            "AD": sdk.AdApi(client).smart_plus_ad_create,
            "CTA": sdk.CreativeManagementApi(client).creative_portfolio_create,
        }
        invoked = False
        try:
            remaining(self._deadline)
            with self._request_scope(intent.advertiser_id, operation, self._deadline):
                budget = remaining(self._deadline)
                # async 官方入口保留业务 envelope；不使用会丢弃 code 的同步便捷层。
                # 不另设 future timeout，禁止请求线程仍在运行时释放共享准入。
                # SDK客户端可复用于同一任务；清空旧正文，异常时只能引用本次响应。
                client.last_response = None
                invoked = True
                with _strict_sdk_envelope(client, operation=operation):
                    methods[intent.kind](
                        client.default_headers["Access-Token"],
                        body=arguments,
                        async_req=True,
                        _request_timeout=(min(5.0, budget), min(30.0, budget)),
                    ).get()
                raw = json.loads(cast(HTTPResponse, client.last_response).data)
                _log_rejection(raw, kind=intent.kind)
                response = sdk_creation_envelope(raw, kind=intent.kind)
                return created_result(kind=intent.kind, response=response)
        except RemoteCallError as error:
            error.transport_diagnostics = transport_snapshot(
                client, include_runtime=True
            )
            raise
        except Exception as error:
            setup_code = connection_setup_failure_code(client, error)
            if setup_code is not None:
                logger.warning(
                    "official TikTok create blocked before HTTP: kind=%s code=%s",
                    intent.kind,
                    setup_code,
                )
                raise RemoteCallError(
                    setup_code,
                    effect="NOT_SENT",
                    evidence=CallEvidence(),
                    transport_diagnostics=transport_snapshot(
                        client, include_runtime=True
                    ),
                ) from None
            if not invoked:
                raise
            # 40100必须先穿透物理scope，把QPM/QPD等待写入共享冷却，再保留
            # 本次拒绝证据。创建仍按既有白名单判断副作用，不能因限流放行重发。
            raw = None
            try:
                body = cast(HTTPResponse, client.last_response).data
                if len(body) <= 8 * 1024 * 1024:
                    raw = json.loads(body)
            except ValueError, TypeError, AttributeError, RecursionError:
                pass
            # 严格校验拒绝布尔/字符串code，但安全request_id仍属于本次响应。
            if type(raw) is dict and (
                type(raw.get("code")) is not int or raw["code"] != 0
            ):
                _log_rejection(raw, kind=intent.kind)
                try:
                    sdk_creation_envelope(raw, kind=intent.kind)
                except RemoteCallError as rejected:
                    rejected.retry_after_ms = getattr(error, "retry_after_ms", None)
                    rejected.transport_diagnostics = transport_snapshot(
                        client, include_runtime=True
                    )
                    raise rejected from None
            # SDK 异常文本可能包含请求信息，只记录类型，禁止输出异常原文。
            logger.error(
                "official TikTok create failed without confirmed receipt: kind=%s error_type=%s",
                intent.kind,
                type(error).__name__,
            )
            # 已进入SDK调用且没有本次HTTP前失败的确证，不能按异常类型放行重发。
            raise RemoteCallError(
                "create_result_unknown",
                effect="UNKNOWN",
                evidence=CallEvidence(),
                transport_diagnostics=transport_snapshot(client, include_runtime=True),
            ) from None

    def _read(
        self, operation: str, advertiser_id: str, arguments: dict[str, object]
    ) -> McpBusinessResponse:
        client = self._requests.client
        methods = {
            "build.get_campaigns": sdk.CampaignCreationApi(
                client
            ).smart_plus_campaign_get,
            "build.get_adgroups": sdk.AdgroupApi(client).smart_plus_adgroup_get,
            "build.get_ads": sdk.AdApi(client).smart_plus_ad_get,
            "build.get_cta_portfolio": sdk.CreativeManagementApi(
                client
            ).creative_portfolio_get,
            "build.get_regular_adgroups": sdk.AdgroupApi(client).adgroup_get,
            "build.list_optimizer_rules": sdk.AutomatedRulesApi(
                client
            ).optimizer_rule_list,
        }

        def send(timeout: tuple[float, float]) -> object:
            return methods[operation](
                access_token=client.default_headers["Access-Token"],
                _request_timeout=timeout,
                **arguments,
            )

        try:
            # ROAS/预算从原始 JSON 保留十进制文本，不能先经 SDK float
            # 再被严格金额契约误判缺失；复用官方读取器已有的精确数值路径。
            return self._requests.invoke(
                operation, advertiser_id, send, exact_numbers=True
            )
        except DomainError as error:
            if error.code not in {"tiktok_response_error", "read_deadline_exceeded"}:
                raise
            raise RemoteCallError(
                "build_readback_unverified", effect="UNKNOWN", evidence=CallEvidence()
            ) from None

    def read_page(self, *, query: BuildReadQuery) -> BuildPage:
        operation, arguments = read_arguments(query)
        response = self._read(operation, query.intent.advertiser_id, arguments)
        return parse_page(query=query, response=response)

    def read_adgroup_status(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> AdGroupStatus:
        arguments = status_arguments(advertiser_id=advertiser_id, adgroup_id=adgroup_id)
        response = self._read("build.get_regular_adgroups", advertiser_id, arguments)
        return parse_status(
            advertiser_id=advertiser_id, adgroup_id=adgroup_id, response=response
        )
