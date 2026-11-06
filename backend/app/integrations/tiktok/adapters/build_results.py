"""创建回执只读取实际 ID/状态；不从请求或成功文案补值。"""

import re

from app.integrations.tiktok.contracts.builds import BuildKind, CreatedObject
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
    safe_identifier,
)

ID_KEYS = {
    "CAMPAIGN": "campaign_id",
    "ADGROUP": "adgroup_id",
    "AD": "smart_plus_ad_id",
    "CTA": "creative_portfolio_id",
}


def created_result(*, kind: BuildKind, response: McpBusinessResponse) -> CreatedObject:
    data = response.data
    value = data.get(ID_KEYS[kind]) if isinstance(data, dict) else None
    # 回执 ID 会进入内部审计与公开投影；拒绝 URL/控制字符，不保存伪装为 ID 的原文。
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value) is None
    ):
        raise RemoteCallError(
            "create_result_unknown", effect="UNKNOWN", evidence=response.evidence
        )
    status = data.get("operation_status") if isinstance(data, dict) else None
    return CreatedObject(
        kind=kind,
        remote_id=value,
        operation_status=safe_identifier(status),
        evidence=response.evidence,
    )


_SDK_NO_EFFECT_REJECTIONS = {
    # TikTok 对 Smart+ 广告组返回 40002 时表示请求参数不被接受，
    # 该请求不会生成广告组；必须把它交给有界业务拒绝重试，而不是伪装成
    # “创建结果未知”再排队做永远无法确认的远端回读。
    "ADGROUP": frozenset({40002}),
    "AD": frozenset({40002, 51002}),
}


def known_no_effect_rejection(kind: str, remote_code: object) -> bool:
    """Return whether a saved business code proves that no object was created."""
    return type(remote_code) is int and remote_code in _SDK_NO_EFFECT_REJECTIONS.get(
        kind, ()
    )


def sdk_creation_envelope(
    raw: object, *, kind: BuildKind | None = None
) -> McpBusinessResponse:
    code = raw.get("code") if isinstance(raw, dict) else None
    evidence = (
        CallEvidence(
            request_id=safe_identifier(raw.get("request_id")),
            # 只保留严格整数业务码及脱敏文案；是否确认拒绝由下方分类决定。
            remote_code=code if type(code) is int and code != 0 else None,
            remote_message=raw.get("message")
            if type(code) is int and code != 0
            else None,
        )
        if isinstance(raw, dict)
        else CallEvidence()
    )
    if (
        isinstance(raw, dict)
        and type(raw.get("code")) is int
        and raw["code"] in _SDK_NO_EFFECT_REJECTIONS.get(kind or "", ())
    ):
        raise RemoteCallError(
            "tiktok_business_error", effect="REJECTED_NO_EFFECT", evidence=evidence
        )
    if (
        not isinstance(raw, dict)
        or type(raw.get("code")) is not int
        or raw["code"] != 0
        or not isinstance(raw.get("data"), dict)
    ):
        raise RemoteCallError(
            "create_result_unknown", effect="UNKNOWN", evidence=evidence
        )
    return McpBusinessResponse(data=raw["data"], evidence=evidence)
