import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Any, Literal

from app.core.errors import DomainError


def safe_identifier(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        else None
    )


def safe_remote_message(value: object) -> str | None:
    """保留平台参数说明；凭据、签名链接和控制字符不能进入回执或页面。"""
    if not isinstance(value, str) or len(value) > 8192:
        return None
    message = "".join(
        " "
        if char.isspace()
        else ""
        if unicodedata.category(char).startswith("C")
        else char
        for char in value
    )
    message = re.sub(r"(?i)\b(?:https?|s3)://[^\s<>\"']+", "[REDACTED]", message)
    message = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"',;]+", "[REDACTED]", message)
    message = re.sub(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b", "[REDACTED]", message)
    message = re.sub(r"(?i)\b(?:set-cookie|cookie)\s*:\s*.*", "[REDACTED]", message)
    message = re.sub(
        r"""(?ix)\b(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|
        app[_-]?secret|password|passwd|signature|x-amz-[\w-]+|authorization|
        api[_-]?key|cookie|token|secret)\b["']?\s*[:=]\s*
        (?:"[^"]*"|'[^']*'|[^\s,;}\]]+)""",
        "[REDACTED]",
        message,
    )
    message = " ".join(message.split())
    if not message.replace("[REDACTED]", "").strip(" \"',;:{}[]"):
        return None
    return message[:1000]


@dataclass(frozen=True)
class CallEvidence:
    request_id: str | None = None
    mcp_request_id: str | None = None
    remote_task_id: str | None = None
    # 错误文案在最早的回执边界脱敏，不能把原始响应或凭据带入异常。
    remote_code: int | None = None
    remote_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "remote_message", safe_remote_message(self.remote_message)
        )


def exact_json_number(value: Decimal) -> int | float | None:
    """把精确十进制金额转换成 TikTok 要求的 JSON 数字。

    TikTok 的 budget、roas_bid 等金额字段只接受 JSON 数字，传字符串会被平台以
    40002 参数错误拒绝。转换为浮点后必须能原样还原，否则返回 None，由调用方
    在发送前拒绝，不能把舍入后的值当作用户确认的金额发出去。
    """
    number = float(value)
    if not isfinite(number) or Decimal(str(number)) != value:
        return None
    return int(value) if value == value.to_integral() else number


RemoteEffect = Literal["NOT_SENT", "UNKNOWN", "REJECTED_NO_EFFECT"]

# 只有明确未发送的传输故障可重试；权限、参数、契约变化不属于临时网络错误。
TRANSIENT_NOT_SENT = frozenset(
    {
        "mcp_call_failed",
        "mcp_request_failed",
        "mcp_session_open_failed",
        "mcp_session_unavailable",
        # 本地总期限在真实请求发送前耗尽，有持久 NOT_SENT 证据时与连接瞬断
        # 使用同一三次预算；已发送或结果未知仍不会进入此集合。
        "tiktok_call_deadline_exceeded",
        # 仅官方SDK客户端在HTTPS校验阶段捕获的瞬态故障；HTTP正文尚未发送。
        "tiktok_connection_not_sent",
    }
)


class RemoteCallError(DomainError):
    def __init__(
        self,
        code: str,
        *,
        effect: RemoteEffect,
        evidence: CallEvidence,
        retry_after_ms: int | None = None,
        transport_diagnostics: dict[str, Any] | None = None,
    ):
        # 上游原文可能包含凭据和签名 URL，异常仅保留应用定义的提示和关联证据。
        super().__init__(code, "TikTok 调用未取得可确认结果", retryable=False)
        self.effect = effect
        self.evidence = evidence
        self.retry_after_ms = retry_after_ms
        self.transport_diagnostics = transport_diagnostics


@dataclass(frozen=True)
class McpBusinessResponse:
    """适配器内部回执；业务层须继续转换为对应业务组的类型。"""

    data: dict[str, Any] | list[dict[str, Any]]
    evidence: CallEvidence
