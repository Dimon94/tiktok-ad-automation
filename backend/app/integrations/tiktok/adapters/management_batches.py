"""平台管理合批：冻结命令分组、单次发送与逐成员回执。

只在一个任务的冻结路由内调用；任务/BC/连接代数由执行层隔离，本层再次
核对租户、账户、类型、动作与物理接口。批量成功必须有成员身份作证。
"""

import json
from collections import Counter
from dataclasses import replace
from typing import Any

import business_api_client as sdk  # type: ignore[import-untyped]
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.management import (
    ManagementCommand,
    ManagementReceipt,
)
from app.integrations.tiktok.official.accounts import remaining
from app.integrations.tiktok.platform_quotas import sdk_endpoint

from .sdk_management import (
    _decimal,
    _management_envelope,
    _payload,
    _request_id,
    _sdk_rejection,
    _smart_plus,
)

MAX_BATCH = 20


def _batch_budget(command: ManagementCommand) -> bool:
    return (
        command.field == "budget"
        and command.ref.kind == "adgroup"
        and (
            _smart_plus(command)
            or command.original.get("budget_mode") == "BUDGET_MODE_TOTAL"
        )
    )


def batch_endpoint(command: ManagementCommand) -> str:
    smart = _smart_plus(command)
    if _batch_budget(command):
        prefix = "smart_plus/" if smart else ""
        return f"POST /open_api/v1.3/{prefix}adgroup/budget/update/"
    return sdk_endpoint(
        f"management.{command.operation}", kind=command.ref.kind, smart=smart
    )


def _material_class(command: ManagementCommand) -> str:
    original = command.original
    # 缺少格式证据时只允许单条，不能把未知素材默认为视频混进轮播批次。
    if (
        original.get("is_carousel") is True
        or original.get("ad_format") == "CAROUSEL_ADS"
    ):
        return "CAROUSEL"
    kind = original.get("material_type")
    if kind in {"CAROUSEL", "CAROUSEL_ADS"}:
        return "CAROUSEL"
    if original.get("is_carousel") is False or kind in {
        "VIDEO",
        "TIKTOK_POST",
        "SPARK",
        "TIKTOK_ITEM",
    }:
        return "NON_CAROUSEL"
    return f"UNKNOWN:{command.ad_material_id}"


def _roas_scope(command: ManagementCommand) -> tuple[Any, ...] | None:
    """只用冻结目录给出的系列身份，不从组ID或其它命令猜共同祖先。"""
    if (
        command.field != "roas"
        or command.ref.kind != "adgroup"
        or not _smart_plus(command)
    ):
        return None
    campaign = command.original.get("campaign_id")
    if type(campaign) is not str or not campaign.strip() or len(campaign) > 255:
        return None
    return command.ref.tenant_id, command.ref.advertiser_id, campaign


def _roas_value(command: ManagementCommand) -> int | float:
    return _decimal(
        command.desired.get("roas_bid", command.desired.get("roas")), "roas"
    )


def roas_link_groups(
    commands: tuple[ManagementCommand, ...],
) -> tuple[tuple[ManagementCommand, ...], ...]:
    """返回同系列ROAS的owner（组首项）及所有联动审计成员。

    本函数不发请求、不丢弃联动项，也不限制审计组大小。执行层持久化
    整组逐项审计，适配器只发组首项的单adgroup_id请求；不能按20拆发。
    非Smart+ ROAS及缺少系列证据的命令始终各自一组。
    """
    groups: dict[tuple[Any, ...], list[ManagementCommand]] = {}
    values: dict[tuple[Any, ...], int | float] = {}
    identities: set[tuple[Any, ...]] = set()
    for index, command in enumerate(commands):
        identity = (command.ref, command.ad_material_id, command.field)
        if identity in identities:
            raise ValueError("duplicate management member")
        identities.add(identity)
        scope = _roas_scope(command)
        if scope is None:
            groups[("single", index)] = [command]
            continue
        value = _roas_value(command)
        if scope in values and values[scope] != value:
            raise DomainError(
                "management_roas_series_conflict", "同一Smart+系列的目标ROAS冲突"
            )
        values[scope] = value
        groups.setdefault(("series", *scope), []).append(command)
    return tuple(tuple(group) for group in groups.values())


def _linked_receipts(
    receipt: ManagementReceipt, commands: tuple[ManagementCommand, ...]
) -> tuple[ManagementReceipt, ...]:
    # 单组更新的官方同系列联动语义不是平台多ID批量；所有成员共享这一次
    # 请求的受理证据，观察值/实际生效仍由执行层分别回读，失败不伪装联动成功。
    if receipt.outcome == "ACCEPTED" and _roas_scope(commands[0]) is not None:
        receipt = replace(receipt, reason="series_roas_linked")
    return tuple(receipt for _ in commands)


def batch_key(command: ManagementCommand) -> tuple[Any, ...]:
    if not isinstance(command, ManagementCommand):
        raise ValueError("management command required")
    key = (
        command.ref.tenant_id,
        command.ref.advertiser_id,
        command.field,
        command.ref.kind,
        command.operation,
        _smart_plus(command),
        batch_endpoint(command),
    )
    if command.field in {"status", "material_status"}:
        key += (command.desired.get("status", command.desired.get("operation_status")),)
        if command.field == "material_status":
            key += (command.ref.remote_id, _material_class(command))
        return key
    if _batch_budget(command):
        return key
    roas_scope = _roas_scope(command)
    if roas_scope is not None:
        return (*key, "series", roas_scope[-1], _roas_value(command))
    # 系列预算/改名、ROAS及普通日预算保持单对象端点语义。
    return (*key, command.ref.remote_id)


def plan_batches(
    commands: tuple[ManagementCommand, ...],
) -> tuple[tuple[ManagementCommand, ...], ...]:
    # 先检查整份输入的系列冲突，不能先按最终值分组再分别覆盖同一系列。
    roas_link_groups(commands)
    groups: dict[tuple[Any, ...], list[ManagementCommand]] = {}
    identities: set[tuple[Any, ...]] = set()
    for command in commands:
        key = batch_key(command)
        identity = (command.ref, command.ad_material_id, command.field)
        if identity in identities:
            raise ValueError("duplicate management member")
        identities.add(identity)
        groups.setdefault(key, []).append(command)
    batches = []
    # 账户轮转由执行层负责；这里保持各组首次出现的确定性顺序。
    for group in groups.values():
        linked_roas = _roas_scope(group[0]) is not None
        if linked_roas:
            # 20上限约束平台ID数组，不限制单组ROAS更新所联动的审计对象数。
            # 整个系列保持一个物理请求，即使审计成员超过20也不能重复发更新。
            limit = len(group)
        else:
            limit = (
                MAX_BATCH
                if group[0].field in {"status", "material_status"}
                or _batch_budget(group[0])
                else 1
            )
        batches.extend(
            tuple(group[start : start + limit]) for start in range(0, len(group), limit)
        )
    return tuple(batches)


def _validate(commands: tuple[ManagementCommand, ...]) -> None:
    if type(commands) is not tuple or not commands:
        raise ValueError("management batch requires commands")
    if len(commands) > MAX_BATCH and _roas_scope(commands[0]) is None:
        raise ValueError("management platform batch exceeds twenty commands")
    planned = plan_batches(commands)
    if len(planned) != 1 or planned[0] != commands:
        raise ValueError("management batch mixes incompatible members")
    for command in commands:
        legacy_ids = command.desired.get("ad_material_ids")
        if legacy_ids is not None and legacy_ids != [command.ad_material_id]:
            raise ValueError("management member must identify exactly one material")


def _member_id(command: ManagementCommand) -> str:
    return (
        command.ad_material_id
        if command.field == "material_status"
        else command.ref.remote_id
    )  # type: ignore[return-value]


def _id_key(command: ManagementCommand) -> str:
    if command.field == "material_status":
        return "ad_material_id"
    return (
        "smart_plus_ad_id"
        if command.ref.kind == "ad" and _smart_plus(command)
        else f"{command.ref.kind}_id"
    )


def batch_payload(commands: tuple[ManagementCommand, ...]) -> dict[str, Any]:
    _validate(commands)
    first = commands[0]
    if _batch_budget(first):
        return {
            "advertiser_id": first.ref.advertiser_id,
            "budget": [
                {
                    "adgroup_id": item.ref.remote_id,
                    "budget": _decimal(item.desired.get("budget"), "budget"),
                }
                for item in commands
            ],
        }
    payload = _payload(first, smart_plus=_smart_plus(first))
    if first.field in {"status", "material_status"}:
        payload[f"{_id_key(first)}s"] = [_member_id(item) for item in commands]
    return payload


def batch_receipts(
    commands: tuple[ManagementCommand, ...],
    raw: object,
    *,
    evidence: CallEvidence | None = None,
) -> tuple[ManagementReceipt, ...]:
    """只认端点身份字段及 error_list；缺失、重复、矛盾不转成整批成功。"""
    evidence = evidence or CallEvidence()
    if type(raw) is dict:
        code = raw.get("code")
        evidence = CallEvidence(
            request_id=_request_id(raw.get("request_id")) or evidence.request_id,
            mcp_request_id=evidence.mcp_request_id,
            remote_code=code
            if type(code) is int and code != 0
            else evidence.remote_code,
        )
    else:
        code = None
    request_id = evidence.request_id or evidence.mcp_request_id
    unknown = ManagementReceipt(
        "UNKNOWN", request_id, False, evidence, "management_member_result_unknown"
    )
    if type(raw) is not dict or type(code) is not int:
        return tuple(unknown for _ in commands)
    data = raw.get("data")
    has_member_evidence = isinstance(data, dict) and any(
        value
        for key, value in data.items()
        if key.endswith("_ids") or key in {"error_list", f"{commands[0].ref.kind}_list"}
    )
    # 非零顶层业务码且无成员结果是整次请求的明确拒绝；失败响应无需成功data。
    # 有成员证据时仍逐项解析，避免把混合回执当成整批同一结果。
    if code != 0 and not has_member_evidence:
        rejected = ManagementReceipt(
            "REJECTED", request_id, False, evidence, "management_provider_rejected"
        )
        return tuple(rejected for _ in commands)
    if type(data) is not dict:
        return tuple(unknown for _ in commands)
    id_key = _id_key(commands[0])
    success_ids = data.get(f"{id_key}s", [])
    errors = data.get("error_list", [])
    if type(success_ids) is not list or type(errors) is not list:
        return tuple(unknown for _ in commands)
    # Smart+实际状态回执返回campaign_list等对象列表，而不是campaign_ids。
    # 必须同时证明成员身份与最终状态；缺字段/冲突仍保留UNKNOWN。
    status_rows = data.get(f"{commands[0].ref.kind}_list", [])
    if commands[0].field == "status" and status_rows:
        desired = commands[0].desired.get(
            "operation_status", commands[0].desired.get("status")
        )
        if type(status_rows) is not list or any(
            type(row) is not dict
            or type(row.get(id_key)) is not str
            or row.get("status", row.get("operation_status")) != desired
            for row in status_rows
        ):
            return tuple(unknown for _ in commands)
        success_ids = [*success_ids, *(row[id_key] for row in status_rows)]
    accepted: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    error_codes: dict[str, int] = {}
    expected = {_member_id(command) for command in commands}
    invalid = False
    for identity in success_ids:
        if type(identity) is not str or identity not in expected:
            invalid = True
        else:
            accepted[identity] += 1
    for error in errors:
        if (
            type(error) is not dict
            or type(error.get(id_key)) is not str
            or error[id_key] not in expected
        ):
            invalid = True
            continue
        identity = error[id_key]
        rejected[identity] += 1
        member_code = error.get("code", error.get("error_code"))
        if type(member_code) is int and member_code != 0:
            error_codes[identity] = member_code
    # 外来ID表明响应无法可信地绑定本次请求，整批等待核验。
    if invalid:
        return tuple(unknown for _ in commands)
    receipts = []
    for command in commands:
        identity = _member_id(command)
        if accepted[identity] == 1 and rejected[identity] == 0 and code == 0:
            receipts.append(ManagementReceipt("ACCEPTED", request_id, False, evidence))
        elif (
            rejected[identity] == 1
            and accepted[identity] == 0
            and identity in error_codes
        ):
            member_evidence = CallEvidence(
                request_id=evidence.request_id,
                mcp_request_id=evidence.mcp_request_id,
                remote_code=error_codes[identity],
            )
            receipts.append(
                ManagementReceipt(
                    "REJECTED",
                    request_id,
                    False,
                    member_evidence,
                    "management_member_rejected",
                )
            )
        else:
            receipts.append(unknown)
    return tuple(receipts)


def _failure(
    commands: tuple[ManagementCommand, ...], error: Exception, *, sent: bool
) -> tuple[ManagementReceipt, ...]:
    if isinstance(error, RemoteCallError):
        sent = sent or error.effect != "NOT_SENT"
        evidence = error.evidence
    else:
        evidence = CallEvidence()
    reason = (
        "management_result_unknown"
        if sent
        else getattr(error, "code", "management_preflight_failed")
    )
    if not sent and isinstance(error, SQLAlchemyError):
        receipt = ManagementReceipt(
            "NOT_SENT",
            evidence.request_id,
            True,
            evidence,
            "management_local_deferred",
            1000,
        )
        return tuple(receipt for _ in commands)
    retry_after = getattr(error, "retry_after_ms", None)
    receipt = ManagementReceipt(
        "UNKNOWN" if sent else "NOT_SENT",
        evidence.request_id or evidence.mcp_request_id,
        not sent and retry_after is not None,
        evidence,
        reason,
        retry_after,
    )
    return tuple(receipt for _ in commands)


def apply_sdk_batch(
    adapter: Any, commands: tuple[ManagementCommand, ...]
) -> tuple[ManagementReceipt, ...]:
    sent = False
    try:
        _validate(commands)
        first = commands[0]
        if first.field not in {"status", "material_status"} and not _batch_budget(
            first
        ):
            return _linked_receipts(adapter.apply(first), commands)
        adapter._check(first.ref.advertiser_id, first.operation, first.ref.kind)
        payload = batch_payload(commands)
        smart = _smart_plus(first)
        api_class = {
            "campaign": sdk.CampaignCreationApi,
            "adgroup": sdk.AdgroupApi,
            "ad": sdk.AdApi,
        }[first.ref.kind]
        method_name = ("smart_plus_" if smart else "") + first.ref.kind
        if first.field == "material_status":
            method_name += "_material_status_update"
        elif first.field == "status":
            method_name += "_status_update"
        else:
            method_name += "_budget_update"
        method = getattr(api_class(adapter._client), method_name, None)
        endpoint = batch_endpoint(first)
        # 固定 SDK 尚未生成预算方法：沿用官方 ApiClient 的已核实固定端点。
        # 普通组仅总预算进入此分支，日预算继续单对象即时更新，不改成次日预约。
        if method is None and not (
            _batch_budget(first)
            and callable(getattr(adapter._client, "call_api", None))
        ):
            raise DomainError(
                "management_contract_unsupported", "SDK 尚无已核验批量接口"
            )
        remaining(adapter._deadline)
        with adapter._management_scope(
            first.ref.advertiser_id,
            first.operation,
            first.ref.kind,
            adapter._deadline,
            endpoint,
        ):
            budget = remaining(adapter._deadline)
            if adapter._before_send is not None:
                adapter._before_send()
            token = adapter._client.default_headers["Access-Token"]
            timeout = (min(5.0, budget), min(30.0, budget))
            adapter._client.last_response = None
            sent = True
            with _management_envelope(adapter._client, f"management.{first.operation}"):
                if method is None:
                    adapter._client.call_api(
                        endpoint.removeprefix("POST "),
                        "POST",
                        {},
                        [],
                        {"Access-Token": token},
                        body=payload,
                        response_type="InlineResponse200",
                        auth_settings=[],
                        _return_http_data_only=True,
                        _request_timeout=timeout,
                    )
                else:
                    method(
                        token, body=payload, async_req=True, _request_timeout=timeout
                    ).get()
            # gateway为每批创建独立client，last_response不能在并发批次之间共享。
            raw = json.loads(adapter._client.last_response.data)
            return batch_receipts(commands, raw)
    except Exception as error:
        if sent:
            rejected = _sdk_rejection(adapter._client, error)
            if rejected is not None:
                return tuple(rejected for _ in commands)
        return _failure(commands, error, sent=sent)


def apply_mcp_batch(
    adapter: Any, commands: tuple[ManagementCommand, ...]
) -> tuple[ManagementReceipt, ...]:
    sent = False
    try:
        _validate(commands)
        first = commands[0]
        if first.field not in {"status", "material_status"} and not _batch_budget(
            first
        ):
            return _linked_receipts(adapter.apply(first), commands)
        adapter._check(first.ref.advertiser_id, first.operation, first.ref.kind)
        operation = f"management.{first.operation}"
        # MCP管理合同按实测工具名核验；同一operation不能把普通/Smart+或预算端点混用。
        expected_tool = (
            batch_endpoint(first)
            .removeprefix("POST /open_api/v1.3/")
            .strip("/")
            .replace("/", "_")
        )
        contract = getattr(adapter._client, "_contracts", {}).get(operation)
        if (
            not callable(getattr(adapter._client, "has_contract", None))
            or not adapter._client.has_contract(operation)
            or contract is None
            or contract.tool_name != expected_tool
        ):
            raise DomainError(
                "management_contract_unsupported", "MCP 批量管理工具合同尚未核验"
            )
        sent = True
        response = adapter._client.call(
            operation=operation,
            advertiser_id=first.ref.advertiser_id,
            arguments=batch_payload(commands),
            entity_kind=first.ref.kind,
        )
        return batch_receipts(
            commands, {"code": 0, "data": response.data}, evidence=response.evidence
        )
    except Exception as error:
        return _failure(
            commands,
            error,
            sent=(error.effect != "NOT_SENT")
            if isinstance(error, RemoteCallError)
            else sent,
        )
