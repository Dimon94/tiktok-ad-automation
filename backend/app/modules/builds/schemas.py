from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.builds.image_composition_schemas import MediaKind
from app.modules.builds.structure import StructureOverride
from app.modules.builds.targeting_schemas import AudienceTargeting
from app.modules.providers.schemas import DramaCandidate

InputText = Annotated[str, Field(max_length=1000)]


class DeliveryConfig(BaseModel):
    """广告搭建阶段的投放排期；策略只保存预算和结构规则。"""

    model_config = ConfigDict(extra="forbid")
    schedule_mode: Literal["IMMEDIATE", "START_AT", "START_END"] = "IMMEDIATE"
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None

    @model_validator(mode="after")
    def validate_schedule(self) -> DeliveryConfig:
        def parse(value: str | None) -> datetime | None:
            if value is None:
                return None
            import re

            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
                raise ValueError("排期时间必须为 UTC YYYY-MM-DD HH:MM:SS")
            try:
                return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError as exc:
                raise ValueError("排期时间不是有效日期") from exc

        start, end = parse(self.schedule_start_time), parse(self.schedule_end_time)
        if self.schedule_mode == "IMMEDIATE" and (start or end):
            raise ValueError("立即开始不应填写排期时间")
        if self.schedule_mode in {"START_AT", "START_END"} and start is None:
            raise ValueError("指定时间开启必须填写开始时间")
        if self.schedule_mode == "START_AT" and end is not None:
            raise ValueError("指定时间开启不应填写结束时间")
        if self.schedule_mode == "START_END" and (
            start is None or end is None or end <= start
        ):
            raise ValueError("结束时间必须晚于开始时间")
        return self


class ManualLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int = Field(ge=1, le=1000, strict=True)
    # 补充信息可独立于链接；URL 非空才表示用户提供了手动推广链接。
    url: str = Field(default="", max_length=8192)
    material_identifier: str = Field(default="", max_length=1000)
    # 用户提供的版权方编号，可按展示 ID 关联已有剧目；不要求用户填写技术 ID。
    external_drama_id: str = Field(default="", max_length=255)
    protected_base: str = Field(default="", max_length=1000)


class ManualLinkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    link: ManualLinkInput


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_kind: MediaKind = "VIDEO"
    request_id: UUID
    bc_id: str = Field(min_length=1, max_length=128)
    strategy_version_id: UUID
    provider_connection_id: UUID | None = None
    execution_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    custom_provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    manual_links: list[ManualLinkInput] = Field(default_factory=list, max_length=1000)
    drama_lines: list[InputText] = Field(max_length=1000)
    account_lines: list[InputText] = Field(max_length=100_000)
    link_config: dict[str, Any] = Field(default_factory=dict)


class PatchDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID | None = None
    expected_revision: int = Field(gt=0, strict=True)
    strategy_version_id: UUID | None = None
    provider_connection_id: UUID | None = None
    execution_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    custom_provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    manual_links: list[ManualLinkInput] | None = Field(default=None, max_length=1000)
    drama_lines: list[InputText] | None = Field(default=None, max_length=1000)
    account_lines: list[InputText] | None = Field(default=None, max_length=100_000)
    link_config: dict[str, Any] | None = None


class DeliveryChange(BaseModel):
    """生成搭建预览前保存投放排期，不走会清空准备结果的通用草稿更新。"""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    delivery_config: DeliveryConfig


class DraftSaved(BaseModel):
    draft_id: UUID
    revision: int


class DraftPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class DraftPrepareAccepted(BaseModel):
    task_id: UUID
    revision: int


class DraftGroupEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID | None = None
    expected_revision: int = Field(gt=0, strict=True)
    groups: list[list[UUID]] = Field(max_length=100_000)


class DraftSummary(BaseModel):
    structure_override: StructureOverride = Field(default_factory=StructureOverride)
    media_kind: MediaKind = "VIDEO"
    targeting: AudienceTargeting = Field(default_factory=AudienceTargeting)
    targeting_override: AudienceTargeting | None = None
    draft_id: UUID
    revision: int
    bc_id: str
    status: Literal["DRAFT", "PREPARING", "READY", "BLOCKED"]
    strategy_version_id: UUID
    provider_connection_id: UUID
    execution_connection_id: UUID | None = None
    application_id: str
    link_config: dict[str, str | int | bool | None]
    delivery_config: DeliveryConfig = Field(default_factory=DeliveryConfig)
    custom_provider_name: str | None = None
    provider_kind: str = ""
    input_counts: dict[str, dict[str, int]]
    drama_count: int
    account_count: int
    task_id: UUID | None
    provider_task_id: UUID | None
    error_code: str | None
    preparation_phase: (
        Literal["accounts", "links", "materials", "scenes", "done"] | None
    ) = None
    created_at: datetime
    updated_at: datetime


class DraftListItem(BaseModel):
    media_kind: MediaKind = "VIDEO"
    draft_id: UUID
    bc_id: str
    revision: int
    status: Literal["DRAFT", "PREPARING", "READY", "BLOCKED"]
    drama_titles: list[str]
    drama_input_count: int
    account_input_count: int
    resolved_account_count: int
    strategy_label: str
    preview_id: UUID | None
    preview_status: Literal["BUILDING", "FROZEN", "OBSOLETE", "FAILED"] | None
    updated_at: datetime


class DraftDramaPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    drama_id: UUID
    link_id: UUID
    title: str
    first_line: int
    material_state: str
    matched_count: int


class DraftInputPreparation(BaseModel):
    """按输入行展示准备进度；未取得链接时不伪造可搭建剧目。"""

    link_status: str
    external_drama_id: str | None = None
    display_drama_id: str | None = None
    url: str | None = None
    protected_base: str | None = None
    provider_input_id: UUID | None = None
    candidates: list[DramaCandidate] = Field(default_factory=list)
    title: str | None = None
    reason_code: str | None = None
    drama: DraftDramaPublic | None = None


class DraftInputPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    line_no: int
    raw_text: str
    manual_link: dict[str, str | int] = Field(default_factory=dict)
    status: str
    reason_code: str | None
    duplicate_of: int | None
    advertiser_id: str | None
    drama_id: UUID | None
    provider_input_id: UUID | None
    candidates: list[dict[str, Any]]
    preparation: DraftInputPreparation | None = None


class DraftMaterialPublic(BaseModel):
    """VIDEO 当前单元成员视图：group_no 固定为 1，position 是原分组展平后的稳定顺序。"""

    material_id: UUID
    file_name: str
    source_bc_id: str
    content_key: str
    group_no: int
    position: int
    shared_with_other_drama: bool
