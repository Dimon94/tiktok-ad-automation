"""Stable provider identities and results shared with strategies/builds.

application_id is always the provider's external string ID. It is neither the
ProviderApplication row UUID nor the separately discovered TikTok Minis ID.
"""

import hashlib
import json
import math
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProviderKind = Literal["wangyan", "jiashu", "duiba", "gangganhao", "rongliang", "minglue"]
LinkResultStatus = Literal[
    "pending",
    "needs_resolution",
    "blocked_auth",
    "config_conflict",
    "retryable_error",
    "result_unknown",
    "failed",
    "ready",
]


class DramaCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_drama_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    language: str | None = None
    display_drama_id: str | None = None
    # 剧单返回的免费集数、总集数，只用于逐剧计算取链参数，不写入剧目表。
    free_episode_count: int | None = Field(default=None, ge=0)
    episode_count: int | None = Field(default=None, ge=1)


class ResolvedLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: UUID
    line_no: int = Field(gt=0, strict=True)
    raw_input: str
    provider_kind: str = Field(min_length=1)
    connection_id: UUID
    application_id: str = Field(min_length=1)
    drama_id: UUID | None = None
    external_drama_id: str | None = None
    title: str | None = None
    language: str | None = None
    display_drama_id: str | None = None
    link_id: UUID | None = None
    url: str | None = None
    protected_base: str | None = None
    tiktok_minis_id: str | None = None
    status: LinkResultStatus
    candidates: list[DramaCandidate] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    existing_config: dict[str, str | int | bool | None] | None = None
    requested_config: dict[str, str | int | bool | None] | None = None

    @model_validator(mode="after")
    def require_ready_fields(self) -> Self:
        if self.status == "ready":
            required = (
                self.drama_id,
                self.external_drama_id,
                self.title,
                self.link_id,
                self.url,
                self.protected_base,
            )
            if any(value is None for value in required):
                raise ValueError("ready 链接缺少身份、URL 或归因信息")
            if not all(
                value and value.strip()
                for value in (self.external_drama_id, self.title, self.url)
            ):
                raise ValueError("ready 链接身份与 URL 不能为空")
        return self


def _validate_json(value: object) -> None:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("推广配置必须使用字符串键")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("推广配置数值必须有限")
    elif value is not None and not isinstance(value, str | int | bool):
        raise ValueError("推广配置必须是 JSON 数据")


def link_reuse_key(
    tenant_id: UUID,
    connection_id: UUID,
    application_id: str,
    external_drama_id: str,
    config: dict[str, Any],
) -> str:
    """Hash exact intent, canonicalizing object order without guessing semantics.

    Adapters normalize protocol-equivalent configuration before calling this. Do
    not merge different episode/channel values, numeric strings, or array order.
    """
    if not isinstance(config, dict):
        raise ValueError("推广配置必须是 JSON 对象")
    if (
        not isinstance(application_id, str)
        or not application_id.strip()
        or not isinstance(external_drama_id, str)
        or not external_drama_id.strip()
    ):
        raise ValueError("应用和剧目必须使用非空字符串 ID")
    _validate_json(config)
    encoded = json.dumps(
        [str(tenant_id), str(connection_id), application_id, external_drama_id, config],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProviderConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ProviderKind
    display_name: str = Field(min_length=1, max_length=255)
    credentials: dict[str, str] = Field(repr=False)


class ProviderConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    credentials: dict[str, str] | None = Field(default=None, repr=False)
    status: Literal["disabled"] | None = None


class ProviderConnectionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    display_name: str
    status: str
    verified_at: datetime | None = None
    error_code: str | None = None


class ProviderApplicationPublic(BaseModel):
    external_id: str
    name: str
    tiktok_minis_id: str | None = None
    available: bool
    link_defaults: dict[str, Any] = Field(default_factory=dict)


class LinkDefaultsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link_defaults: dict[str, Any]


class ProviderLinkPublic(BaseModel):
    source: str = "provider"
    link_id: UUID
    drama_id: UUID
    external_drama_id: str
    display_drama_id: str | None = None
    title: str
    language: str | None
    provider_kind: str
    connection_id: UUID
    connection_name: str
    connection_status: str
    application_id: str
    application_name: str
    tiktok_minis_id: str | None
    status: str
    version: int
    url: str | None
    protected_base: str | None
    verified_at: datetime | None
    config: dict[str, str | int | bool | None]
    config_display_incomplete: bool


class PreparationSummary(BaseModel):
    task_id: UUID
    connection_id: UUID
    connection_name: str
    provider_kind: str
    application_id: str
    application_name: str
    status: str
    config: dict[str, str | int | bool | None]
    config_display_incomplete: bool
    total_count: int
    ready_count: int
    pending_count: int
    exception_count: int
    counts: dict[str, int]


def display_config(value: object) -> dict[str, str | int | bool | None]:
    """Only known scalar business settings; never expose raw provider config."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, raw in value.items():
        key = "episode" if key == "drama_num" else key
        if key in {"episode", "charge_level", "channel_prefix", "chapter_index"} and (
            raw is None
            or type(raw) in {int, bool}
            or isinstance(raw, str)
            and len(raw) <= 1000
        ):
            result[key] = raw
    return result


class LinkPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    connection_id: UUID
    application_id: str = Field(min_length=1, max_length=255)
    lines: list[str] = Field(min_length=1, max_length=1000)
    config: dict[str, Any] = Field(default_factory=dict)


class PreparationAccepted(BaseModel):
    task_id: UUID


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_drama_id: str = Field(min_length=1, max_length=255)
