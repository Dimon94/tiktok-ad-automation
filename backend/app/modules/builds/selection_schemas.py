"""目的地选择的互斥输入；Mini 的实际链接仍由既有 owner 保存。"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SelectionIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_type: Literal["TT_USER", "BC_AUTH_TT"]
    identity_id: str = Field(min_length=1, max_length=255)
    identity_authorized_bc_id: str | None = Field(
        default=None, min_length=1, max_length=255
    )

    @model_validator(mode="after")
    def valid_authority(self):
        if (self.identity_type == "BC_AUTH_TT") != (
            self.identity_authorized_bc_id is not None
        ):
            raise ValueError("BC_AUTH_TT 必须携带授权 BC，TT_USER 不携带授权 BC")
        return self


class MiniAppSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["MINI_APP"] = "MINI_APP"
    creative_identity: SelectionIdentity | None = None


class NativeVideoSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: UUID
    cover_material_id: UUID | None = None


class NativeSeriesSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["NATIVE_SERIES_DIRECT_VIDEO"]
    advertiser_id: str = Field(min_length=1, max_length=128)
    native_series_id: str | None = Field(default=None, min_length=1, max_length=255)
    video_sources: list[NativeVideoSource] = Field(
        default_factory=list, max_length=1000
    )
    series_identity: SelectionIdentity | None = None
    creative_identity: SelectionIdentity | None = None
    regions: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = Field(
        min_length=1, max_length=250
    )

    @model_validator(mode="after")
    def unique_regions(self):
        if len(self.regions) != len(set(self.regions)):
            raise ValueError("地区不能重复")
        return self


# 资源边界：每份计划最多1000素材，单次准备最多20项；20计划限制任务扇出。
# 这是实现预算，不是平台或经营上限。
NATIVE_PLAN_LIMIT = 20
LEGACY_PLAN_KEY = UUID("00000000-0000-4000-8000-000000000001")


class NativeSeriesPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_key: UUID
    selection: NativeSeriesSelection


class NativeSeriesPlanSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["NATIVE_SERIES_DIRECT_VIDEO"]
    plans: list[NativeSeriesPlan] = Field(min_length=1, max_length=NATIVE_PLAN_LIMIT)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy(cls, value):
        if isinstance(value, dict) and "plans" not in value:
            return {
                "kind": value.get("kind"),
                "plans": [{"plan_key": LEGACY_PLAN_KEY, "selection": value}],
            }
        return value

    @model_validator(mode="after")
    def unique_keys(self):
        if len({plan.plan_key for plan in self.plans}) != len(self.plans):
            raise ValueError("计划键不能重复")
        return self

    @property
    def advertiser_ids(self):
        return list(dict.fromkeys(plan.selection.advertiser_id for plan in self.plans))


SceneSelection = Annotated[
    MiniAppSelection | NativeSeriesPlanSet, Field(discriminator="kind")
]


class NativeMaterialMapping(BaseModel):
    position: int
    material_id: UUID
    cover_material_id: UUID | None = None
    video_id: str
    image_id: str
    video_evidence_id: UUID
    cover_evidence_id: UUID
    video_task_id: UUID | None = None
    cover_task_id: UUID | None = None
    preparation_id: UUID


class NativeCheckpoint(BaseModel):
    state: Literal["PENDING", "BLOCKED", "UNVERIFIED", "READY"]
    reason_codes: list[str]
    draft_revision: int
    scope_basis: str
    route: dict[str, Any]
    selection: NativeSeriesSelection
    evidence_ids: list[UUID] = Field(default_factory=list)
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    materials_state: Literal["PENDING", "BLOCKED", "UNVERIFIED", "READY"] = "PENDING"
    materials_ready: bool = False
    materials: list[NativeMaterialMapping] = Field(default_factory=list)
    material_issues: list[dict[str, Any]] = Field(default_factory=list)


class ReadyNativePlan(BaseModel):
    state: Literal["READY"]
    plan_key: UUID
    checkpoint: NativeCheckpoint


class UnavailableNativePlan(BaseModel):
    state: Literal["PENDING", "BLOCKED", "UNVERIFIED"]
    plan_key: UUID
    selection: NativeSeriesSelection
    reason_codes: list[str]


class NativeSeriesBatchCheckpoint(BaseModel):
    draft_revision: int
    plans: list[
        Annotated[ReadyNativePlan | UnavailableNativePlan, Field(discriminator="state")]
    ]
