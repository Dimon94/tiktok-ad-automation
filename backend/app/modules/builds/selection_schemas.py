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


SceneSelection = Annotated[
    MiniAppSelection | NativeSeriesSelection, Field(discriminator="kind")
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
