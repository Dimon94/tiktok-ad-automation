"""官方 SDK 广告管理写适配器。

只在官方生成 SDK 的边界发送；普通对象和 Smart+ 使用不同方法与最小 body，
授权、额度和冻结路由检查由 request_scope 在每次物理请求前完成。
"""

import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    RemoteCallError,
    exact_json_number,
    safe_identifier,
)
from app.integrations.tiktok.contracts.management import (
    MANAGEMENT_OPERATIONS,
    ManagementCommand,
    ManagementReceipt,
)
from app.integrations.tiktok.official.accounts import (
    RequestScope,
    _strict_sdk_envelope,
    remaining,
)

CapabilityCheck = Callable[[str, str, str], None]
ManagementScope = Callable[[str, str, str, datetime, str], Any]


def _smart_plus(command: ManagementCommand) -> bool:
    """只接受目录明确给出的广告类型；缺失或冲突必须闭合拒绝。

    campaign_type 是平台的普通/iOS 专属系列区分（目录恒为 REGULAR_CAMPAIGN 等），
    与是否 Smart+ 无关，不能作为类型标识，否则所有系列层修改都会被误拒。
    """
    marker_names = ("ad_type", "platform_ad_type", "is_smart_plus")

    def parse_markers(values: dict[str, Any]) -> bool | None:
        parsed: set[bool] = set()
        for name in marker_names:
            if name not in values:
                continue
            marker = values[name]
            if isinstance(marker, bool) and name == "is_smart_plus":
                parsed.add(marker)
            elif isinstance(marker, str):
                kind = marker.upper()
                if kind in {"SMART_PLUS", "UPGRADED_SMART_PLUS", "SMART+", "SMARTPLUS"}:
                    parsed.add(True)
                elif kind in {"REGULAR", "AUCTION", "NORMAL"}:
                    parsed.add(False)
                else:
                    raise DomainError(
                        "management_contract_unsupported", "广告类型未经目录核验"
                    )
            else:
                raise DomainError(
                    "management_contract_unsupported", "广告类型未经目录核验"
                )
        if len(parsed) > 1:
            raise DomainError("management_contract_unsupported", "广告类型标识冲突")
        return next(iter(parsed), None)

    original_type = parse_markers(command.original)
    desired_type = parse_markers(command.desired)
    if original_type is None:
        raise DomainError("management_contract_unsupported", "广告类型未经目录核验")
    if desired_type is not None and desired_type != original_type:
        raise DomainError("management_contract_unsupported", "广告类型标识冲突")
    return original_type


def _decimal(value: object, field: str) -> int | float:
    """冻结值以精确十进制文本保存，发送前转成平台要求的 JSON 数字。"""
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is str:
        try:
            parsed = Decimal(value)
        except Exception:
            raise DomainError(
                "management_request_invalid", f"{field} 数值无效"
            ) from None
    else:
        raise DomainError("management_request_invalid", f"{field} 必须使用精确十进制")
    if not parsed.is_finite() or parsed <= 0:
        raise DomainError("management_request_invalid", f"{field} 必须为正数")
    number = exact_json_number(parsed)
    if number is None:
        raise DomainError("management_request_invalid", f"{field} 无法精确发送")
    return number


def _request_id(raw: object) -> str | None:
    return safe_identifier(raw) if isinstance(raw, str) else None


def _receipt(raw: object) -> ManagementReceipt:
    if not isinstance(raw, dict) or type(raw.get("code")) is not int:
        raise RemoteCallError(
            "management_result_unknown", effect="UNKNOWN", evidence=CallEvidence()
        )
    evidence = CallEvidence(
        request_id=_request_id(raw.get("request_id")),
        remote_code=raw["code"] if raw["code"] != 0 else None,
    )
    if raw["code"] == 0 and isinstance(raw.get("data"), dict):
        return ManagementReceipt("ACCEPTED", evidence.request_id, False, evidence)
    if raw["code"] != 0:
        return ManagementReceipt("REJECTED", evidence.request_id, False, evidence)
    raise RemoteCallError(
        "management_result_unknown", effect="UNKNOWN", evidence=evidence
    )


def _management_envelope(client: Any, operation: str) -> Any:
    # 工厂生产的官方ApiClient始终具备deserialize。无该方法的纯传输测试替身
    # 仍由_receipt/batch_receipts校验原始JSON，不能用SDK转换后的code作证。
    if callable(getattr(client, "deserialize", None)):
        return _strict_sdk_envelope(client, operation=operation)
    return nullcontext()


def _sdk_rejection(
    client: Any, error: Exception, *, previous_response: object = None
) -> ManagementReceipt | None:
    """SDK可在返回业务拒绝后抛错；只恢复本次真实非零envelope的拒绝证据。

    调用方须在物理准入scope退出后调用，使40100的provider延期异常先穿过
    scope，落共享冷却。拒绝不证明可以重发写操作，故回执始终不可自动重试。
    """
    response = getattr(client, "last_response", None)
    if response is None or response is previous_response:
        return None
    try:
        if len(response.data) > 8 * 1024 * 1024:
            return None
        raw = json.loads(response.data)
    except ValueError, TypeError, AttributeError, RecursionError:
        return None
    if type(raw) is not dict or type(raw.get("code")) is not int or raw["code"] == 0:
        return None
    data = raw.get("data")
    # 有成员证据时仍交由批量解析，不能用顶层业务码覆盖混合/矛盾的成员结果。
    if isinstance(data, dict) and any(
        value
        for key, value in data.items()
        if key.endswith("_ids") or key == "error_list"
    ):
        return None
    receipt = _receipt(raw)
    provider = getattr(error, "admission_source", None) == "provider"
    return replace(
        receipt,
        reason="management_provider_rate_limited"
        if provider
        else "management_provider_rejected",
        retry_after_ms=getattr(error, "retry_after_ms", None),
    )


def _payload(command: ManagementCommand, *, smart_plus: bool) -> dict[str, Any]:
    ref = command.ref
    status = command.desired.get("status", command.desired.get("operation_status"))
    if command.field == "material_status":
        if not smart_plus:
            raise DomainError(
                "management_contract_unsupported", "普通广告不支持独立素材状态"
            )
        return {
            "advertiser_id": ref.advertiser_id,
            "smart_plus_ad_id": ref.remote_id,
            "ad_material_ids": [command.ad_material_id],
            "operation_status": status,
        }
    if command.field == "status":
        key = f"{ref.kind}_ids"
        if smart_plus and ref.kind == "ad":
            key = "smart_plus_ad_ids"
        return {
            "advertiser_id": ref.advertiser_id,
            key: [ref.remote_id],
            "operation_status": status,
        }
    if command.field == "name":
        # 系列更新接口按字段增量更新，改名只发送名称，避免回写其它旧配置。
        return {
            "advertiser_id": ref.advertiser_id,
            "campaign_id": ref.remote_id,
            "campaign_name": command.desired["campaign_name"],
        }
    if ref.kind not in {"campaign", "adgroup"}:
        raise DomainError(
            "management_contract_unsupported", "该对象不支持预算或 ROAS 修改"
        )
    if command.field == "roas" and ref.kind != "adgroup":
        raise DomainError("management_contract_unsupported", "广告系列不支持 ROAS 修改")
    payload: dict[str, Any] = {"advertiser_id": ref.advertiser_id}
    payload["campaign_id" if ref.kind == "campaign" else "adgroup_id"] = ref.remote_id
    source = {**command.original, **command.desired}
    if command.field == "roas":
        payload["roas_bid"] = _decimal(
            command.desired.get("roas_bid", command.desired.get("roas")), "roas"
        )
    else:
        payload["budget"] = _decimal(command.desired.get("budget"), "budget")
    if not smart_plus and ref.kind == "adgroup":
        # 普通组更新是整体替换合同，保留平台验证过的必填字段。
        for name in (
            "adgroup_name",
            "campaign_id",
            "bid_type",
            "billing_event",
            "optimization_goal",
            "promotion_type",
            "schedule_type",
            "schedule_start_time",
            "targeting_spec",
        ):
            if name in source:
                payload[name] = source[name]
    return payload


class SdkManagementOperations:
    def __init__(
        self,
        client: Any,
        *,
        request_scope: RequestScope,
        deadline: datetime,
        capability_check: CapabilityCheck | None = None,
        authorization_check: CapabilityCheck | None = None,
        require_capability: CapabilityCheck | None = None,
        management_scope: ManagementScope | None = None,
        before_send: Callable[[], None] | None = None,
    ):
        self._client = client
        self._scope = request_scope
        self._deadline = deadline
        self._check = capability_check or authorization_check or require_capability
        self._management_scope = management_scope
        self._before_send = before_send
        if self._check is None:
            raise DomainError(
                "management_permission_unverified", "管理请求缺少账户能力门禁"
            )
        if self._management_scope is None:
            raise DomainError(
                "management_permission_unverified", "管理请求缺少专用准入门禁"
            )

    def apply_batch(
        self, commands: tuple[ManagementCommand, ...]
    ) -> tuple[ManagementReceipt, ...]:
        from .management_batches import apply_sdk_batch

        return apply_sdk_batch(self, commands)

    def apply(self, command: ManagementCommand) -> ManagementReceipt:
        sent = False
        previous_response = getattr(self._client, "last_response", None)
        try:
            if not isinstance(command, ManagementCommand):
                raise TypeError("management command required")
            operation = f"management.{command.operation}"
            if operation not in {
                f"management.{name}" for name in MANAGEMENT_OPERATIONS
            }:
                raise DomainError("management_operation_invalid", "管理操作无效")
            check = self._check
            management_scope = self._management_scope
            assert check is not None and management_scope is not None
            check(command.ref.advertiser_id, command.operation, command.ref.kind)
            smart_plus = _smart_plus(command)
            payload = _payload(command, smart_plus=smart_plus)
            methods: dict[str, Any]
            if command.field == "material_status":
                methods = {
                    "smart": sdk.AdApi(
                        self._client
                    ).smart_plus_ad_material_status_update
                }
                method = methods["smart"]
            elif command.field == "status":
                if smart_plus:
                    method = {
                        "campaign": sdk.CampaignCreationApi(
                            self._client
                        ).smart_plus_campaign_status_update,
                        "adgroup": sdk.AdgroupApi(
                            self._client
                        ).smart_plus_adgroup_status_update,
                        "ad": sdk.AdApi(self._client).smart_plus_ad_status_update,
                    }[command.ref.kind]
                else:
                    method = {
                        "campaign": sdk.CampaignCreationApi(
                            self._client
                        ).campaign_status_update,
                        "adgroup": sdk.AdgroupApi(self._client).adgroup_status_update,
                        "ad": sdk.AdApi(self._client).ad_status_update,
                    }[command.ref.kind]
            elif smart_plus:
                method = (
                    sdk.CampaignCreationApi(self._client).smart_plus_campaign_update
                    if command.ref.kind == "campaign"
                    else sdk.AdgroupApi(self._client).smart_plus_adgroup_update
                )
            else:
                method = (
                    sdk.CampaignCreationApi(self._client).campaign_update
                    if command.ref.kind == "campaign"
                    else sdk.AdgroupApi(self._client).adgroup_update
                )
            remaining(self._deadline)
            from app.integrations.tiktok.platform_quotas import sdk_endpoint

            scope = management_scope(
                command.ref.advertiser_id,
                command.operation,
                command.ref.kind,
                self._deadline,
                sdk_endpoint(operation, kind=command.ref.kind, smart=smart_plus),
            )
            with scope:
                budget = remaining(self._deadline)
                if self._before_send is not None:
                    self._before_send()
                access_token = self._client.default_headers["Access-Token"]
                sent = True
                with _management_envelope(self._client, operation):
                    method(
                        access_token,
                        body=payload,
                        async_req=True,
                        _request_timeout=(min(5.0, budget), min(30.0, budget)),
                    ).get()
                raw = json.loads(self._client.last_response.data)
                return _receipt(raw)
        except Exception as error:
            if sent:
                rejected = _sdk_rejection(
                    self._client, error, previous_response=previous_response
                )
                if rejected is not None:
                    return rejected
            # 只有到达 SDK 方法调用边界才可能产生远端效果。准入/授权/原值校验
            # 都在此之前；异常文案不落库，仅传递应用错误码与明确的发送阶段。
            if (
                isinstance(error, RemoteCallError)
                and sent
                and error.effect == "UNKNOWN"
            ):
                raise
            code = (
                "management_result_unknown"
                if sent
                else error.code
                if isinstance(error, DomainError)
                else "management_preflight_failed"
            )
            raise RemoteCallError(
                code,
                effect="UNKNOWN" if sent else "NOT_SENT",
                evidence=CallEvidence(),
                retry_after_ms=getattr(error, "retry_after_ms", None),
            ) from None


# Naming follows the API/MCP adapter pair used by the existing build adapters.
ApiManagementOperations = SdkManagementOperations
