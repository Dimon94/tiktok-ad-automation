"""按显式工具契约解码业务回执，不从服务消息推断成功或可重发。"""

import json
import math
import re
from dataclasses import replace
from typing import Any

from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.mcp.protocol import ToolContract
from mcp.types import CallToolResult, TextContent

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_INVALID = object()
# 官方已接入业务工具使用 JSON TextContent 承载同一 code/data envelope。
# 只扩展明确的操作/工具对；结构、错误码、重复键及双载体冲突校验保持不变。
_NATIVE_TEXT_TOOLS = {
    ("ads.get_campaigns", "campaign_get"),
    ("ads.get_ads", "ad_get"),
    ("finance.get_advertiser_balances", "advertiser_balance_get"),
    ("materials.share_assets", "creative_asset_share_get"),
    ("scene.list_identities", "identity_get"),
    ("scene.list_minis", "minis_get"),
    ("scene.recommend_ctas", "creative_cta_recommend_get"),
    ("build.create_cta_portfolio", "creative_portfolio_create"),
    ("build.get_cta_portfolio", "creative_portfolio_get"),
    ("build.list_cta_portfolios", "creative_portfolio_list_get"),
    ("scene.list_regions", "tool_region_get"),
    ("scene.check_vbo", "tool_vbo_status_check"),
    ("materials.upload_video_url", "file_video_ad_upload"),
    ("materials.get_videos", "file_video_ad_info_get"),
    ("materials.search_videos", "file_video_ad_search"),
    ("materials.get_suggested_covers", "file_video_suggestcover_get"),
    ("materials.upload_image_url", "file_image_ad_upload"),
    ("materials.get_images", "file_image_ad_info_get"),
    ("materials.search_images", "file_image_ad_search"),
    ("build.create_campaign", "smart_plus_campaign_create"),
    ("build.get_campaigns", "smart_plus_campaign_get"),
    ("build.create_adgroup", "smart_plus_adgroup_create"),
    ("build.get_adgroups", "smart_plus_adgroup_get"),
    ("build.create_ad", "smart_plus_ad_create"),
    ("build.get_ads", "smart_plus_ad_get"),
    ("build.get_regular_adgroups", "adgroup_get"),
}

# 只允许真实事故中已验证为“接口明确拒绝、没有创建对象”的广告创建业务码。
# 其他操作和其他非零码仍按 UNKNOWN 处理，不能仅凭非零 code 推断无副作用。
_NO_EFFECT_REJECTIONS = {
    ("build.create_ad", "smart_plus_ad_create"): frozenset({40002, 51002}),
}


def _identifier(value: Any) -> str | None:
    # 不把浮点 ID 转为字符串；精度一旦丢失便不能作为关联证据。
    if type(value) is int:
        try:
            value = str(value)
        except ValueError:
            return None
    if type(value) is str and _IDENTIFIER.fullmatch(value):
        return value
    return None


def _evidence(raw: Any) -> CallEvidence:
    if type(raw) is not dict:
        return CallEvidence()
    # 不递归搜寻 ID，也不猜测未知 _meta 字段的业务含义。
    return CallEvidence(
        request_id=_identifier(raw.get("request_id")),
        mcp_request_id=_identifier(raw.get("mcp_request_id")),
        remote_task_id=_identifier(raw.get("remote_task_id")),
    )


def _unknown(code: str, evidence: CallEvidence) -> RemoteCallError:
    return RemoteCallError(code, effect="UNKNOWN", evidence=evidence)


def _valid_json(value: Any, depth: int = 0) -> bool:
    # SDK 的 structured_content 为 Any；同样拒绝非 JSON 对象、非有限数和循环结构。
    if depth > 100:
        return False
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_valid_json(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _valid_json(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _parse_json(text: str) -> Any:
    # 不提取代码块或文字中的片段；json.loads 必须消费完整文本。
    try:
        value = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError, RecursionError:
        # 在捕获块外抛业务异常，避免异常链携带原始 JSON 或签名 URL。
        return _INVALID
    return value if _valid_json(value) else _INVALID


def _require_success(raw: Any, evidence: CallEvidence, contract: ToolContract) -> None:
    if (
        type(raw) is not dict
        or not _valid_json(raw)
        or type(raw.get("code")) is not int
    ):
        raise _unknown("mcp_response_invalid", evidence)
    if raw["code"] != 0:
        # 与官方 SDK 共用回执边界脱敏，保留可排查的字段说明而非原始响应。
        rejected = replace(
            evidence, remote_code=raw["code"], remote_message=raw.get("message")
        )
        if raw["code"] in _NO_EFFECT_REJECTIONS.get(
            (contract.operation, contract.tool_name), ()
        ):
            raise RemoteCallError(
                "mcp_business_error",
                effect="REJECTED_NO_EFFECT",
                evidence=rejected,
            )
        raise _unknown("mcp_business_error", rejected)
    data = raw.get("data")
    if contract.response_shape == "OBJECT":
        valid_data = type(data) is dict
        if (contract.operation, contract.tool_name) == (
            "materials.upload_video_url",
            "file_video_ad_upload",
        ):
            # 视频上传的官方 API 数据可为单条数组；多条结果不能认作本次文件。
            valid_data = valid_data or (
                type(data) is list and len(data) == 1 and type(data[0]) is dict
            )
    elif contract.response_shape == "OBJECT_LIST":
        valid_data = type(data) is list and all(type(item) is dict for item in data)
    else:
        valid_data = False
    if not valid_data:
        raise _unknown("mcp_response_invalid", evidence)


def _same_json(first: Any, second: Any) -> bool:
    # Python 的相等运算会把 True、1、1.0 混同；比较 JSON 表达以保留类型差异。
    try:
        return json.dumps(first, sort_keys=True, allow_nan=False) == json.dumps(
            second, sort_keys=True, allow_nan=False
        )
    except TypeError, ValueError, RecursionError:
        return False


def decode_mcp_result(
    result: CallToolResult, *, contract: ToolContract
) -> McpBusinessResponse:
    raw = result.structured_content
    text_json_allowed = (
        contract.text_json_envelope
        or (contract.operation, contract.tool_name) in _NATIVE_TEXT_TOOLS
    )
    evidence = _evidence(raw)
    text_receipts = []
    receipt_text = None
    for block in result.content:
        if not isinstance(block, TextContent):
            continue
        text = block.text.strip()
        if not text.startswith(("{", "[")):
            continue
        parsed = _parse_json(text)
        if parsed is _INVALID:
            raise _unknown("mcp_response_invalid", evidence)
        text_receipts.append(parsed)
        receipt_text = text

    if len(text_receipts) > 1:
        raise _unknown("mcp_response_ambiguous", evidence)

    if raw is None and text_json_allowed and len(text_receipts) == 1:
        evidence = _evidence(text_receipts[0])
    if result.is_error or result.result_type != "complete":
        raise _unknown("mcp_tool_error", evidence)

    if raw is not None:
        # 先验证双载体一致性，再分类成功或拒绝；矛盾回执不能进入
        # 只读业务拒绝的会话复用分支，也不能成为写入无副作用证据。
        if text_receipts and not _same_json(raw, text_receipts[0]):
            raise _unknown("mcp_response_ambiguous", evidence)
        _require_success(raw, evidence, contract)
    else:
        if not text_json_allowed or len(text_receipts) != 1:
            raise _unknown("mcp_response_invalid", evidence)
        raw = text_receipts[0]
        evidence = _evidence(raw)
        _require_success(raw, evidence, contract)

    data = raw["data"]
    if (
        contract.operation
        in {
            "ads.get_campaigns",
            "ads.get_ads",
            "finance.get_advertiser_balances",
        }
        and receipt_text is not None
    ):
        # 先完成双载体一致性与业务 envelope 校验，再从原文保留十进制数字。
        data = json.loads(receipt_text, parse_float=str)["data"]
    if (
        contract.operation
        in {"build.get_campaigns", "build.get_adgroups", "build.get_regular_adgroups"}
        and receipt_text is not None
    ):
        # 金额从已完整校验的原始 JSON 数字重新提取，不能由 float 转回十进制冒充精度。
        # 仅转换创建回读合同中的金额字段；不改变其他工具或其他字段的数据类型。
        exact = json.loads(receipt_text, parse_float=str)["data"]
        if (
            isinstance(data, dict)
            and isinstance(exact, dict)
            and isinstance(exact.get("list"), list)
        ):
            rows: list[dict[str, Any]] = []
            for row in exact["list"]:
                if not isinstance(row, dict):
                    rows = []
                    break
                rows.append(row)
            if len(rows) == len(data.get("list", [])):
                data = {
                    **data,
                    "list": [
                        {
                            **row,
                            **{
                                key: exact_row[key]
                                for key in (
                                    "budget",
                                    "roas_bid",
                                    "current_budget",
                                    "min_budget",
                                    "bid_price",
                                    "conversion_bid_price",
                                )
                                if key in exact_row
                            },
                        }
                        for row, exact_row in zip(
                            data.get("list", []), rows, strict=True
                        )
                    ],
                }
    return McpBusinessResponse(data=data, evidence=evidence)
