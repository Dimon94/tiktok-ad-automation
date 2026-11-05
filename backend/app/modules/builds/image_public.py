"""浏览器公共响应。内部音乐意图与临时试听地址不能随创意快照泄露。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .image_composition_schemas import (
    CreativeUnit,
    ImageCandidate,
    ImageRule,
    ImageRulePatch,
    MusicStatus,
)


class PublicMusicSnapshot(BaseModel):
    music_id: str | None = None
    display_name: str | None = None
    author: str | None = None
    duration: float | None = None
    selection_source: str | None = None
    images_changed: bool | None = None


class ImageUnitPublic(CreativeUnit):
    drama_id: UUID
    drama_title: str | None = None
    music_snapshot: PublicMusicSnapshot = Field(default_factory=PublicMusicSnapshot)


class ImageCandidatePublic(ImageCandidate):
    drama_id: UUID
    cross_drama_allowed: bool = False


class ImageRevisionPublic(BaseModel):
    revision: int


class CandidateAdopted(ImageRevisionPublic):
    snapshot_id: UUID


class CandidateAdded(CandidateAdopted):
    added: int


class CandidateProgress(BaseModel):
    snapshot_id: UUID
    revision: int
    status: Literal["PENDING", "READY", "FAILED", "CANCELLED"]
    generation: int
    processed: int
    candidate_count: int
    completed_drama_count: int
    total_drama_count: int
    added: int | None
    removed: int | None
    error_code: str | None
    cutoff: datetime


class CompositionRunPublic(BaseModel):
    run_id: UUID
    plan_id: UUID
    generation: int
    status: Literal["PENDING", "APPLIED", "FAILED", "CANCELLED"]
    processed: int
    total: int
    cursor: int
    error_code: str | None


class CompositionStatePublic(ImageRevisionPublic):
    preview_ready: bool = False
    preview_reason_codes: list[str] = Field(default_factory=list)
    active_generation: int
    applied_rule_revision: int | None
    snapshot_id: UUID | None
    rule: ImageRule
    drama_patches: dict[UUID, ImageRulePatch]
    candidate_build: CandidateProgress | None
    run: CompositionRunPublic | None
    music_counts: dict[str, int]


class DifferenceMaterial(BaseModel):
    material_id: UUID
    file_name: str


class ImageDifference(BaseModel):
    music_title: str | None = None
    drama_title: str | None = None
    drama_titles: list[str] = Field(default_factory=list)
    unit_position: int | None = None
    materials: list[DifferenceMaterial] = Field(default_factory=list)
    composition_protected: bool = False
    music_protected: bool = False
    drama_id: UUID | None = None
    drama_ids: list[UUID] = Field(default_factory=list)
    code: str | None = None
    reason: str | None = None
    unit_id: UUID | None = None
    material_id: UUID | None = None
    material_ids: list[UUID] = Field(default_factory=list)


class DramaDifferenceSummary(BaseModel):
    drama_id: UUID
    drama_title: str = "剧目名称未记录"
    rule_digest: str
    candidate_digest: str
    retained_ids_count: int
    removed_ids_count: int
    excluded_images_count: int
    warnings_count: int
    protection_losses_count: int
    conflicts_count: int


class CompositionPlanPublic(BaseModel):
    plan_id: UUID
    base_revision: int
    input_digest: str
    unit_count: int
    added_unit_count: int
    removed_unit_count: int
    retained_unit_count: int
    protected_unit_count: int
    music_counts: dict[str, int]
    image_reference_count: int
    distinct_image_count: int
    reused_image_count: int
    conflicts: list[ImageDifference]
    conflict_count: int
    dramas: list[DramaDifferenceSummary]
    mode: Literal["PRESERVE_MANUAL", "REPLACE_ALL"]


class CreativeEditPublic(ImageRevisionPublic):
    affected_units: list[UUID]
    pending_music_ids: list[UUID]


class MusicItemPublic(BaseModel):
    music_id: str
    display_name: str
    author: str | None
    duration: float | None
    preview_url: str | None


class MusicPagePublic(BaseModel):
    items: list[MusicItemPublic]
    next_cursor: str | None
    truncated: bool


class MusicRequestPublic(ImageRevisionPublic):
    job_ids: list[UUID]
    music_revision: int
    request_generation: int
    status: MusicStatus


class MusicJobPublic(BaseModel):
    job_id: UUID
    generation: int
    status: str
    processed: int
    total: int
    attempts: int
    error_code: str | None


class MusicJobError(BaseModel):
    job_id: UUID
    error_code: str | None


class MusicPreparationError(BaseModel):
    run_id: UUID
    error_code: str | None


class MusicSummaryPublic(BaseModel):
    active_generation: int
    jobs: dict[str, int]
    preparations: dict[str, int]
    preparation_errors: list[MusicPreparationError]
    recent_errors: list[MusicJobError]


class CarouselPresetPublic(BaseModel):
    id: UUID
    tenant_id: UUID
    source_bc_id: str
    source_draft_id: UUID
    source_unit_id: UUID
    source_drama_id: UUID
    name: str
    version: int
    content_digest: str
    ordered_material_ids: list[UUID]
    music_id: str | None
    music_status: MusicStatus
    music_snapshot: PublicMusicSnapshot
    created_at: datetime


class CarouselPresetPage(BaseModel):
    items: list[CarouselPresetPublic]
    total: int
    offset: int
    limit: int


class CarouselPresetLoaded(ImageRevisionPublic):
    unit: ImageUnitPublic


class ImageDramaSummary(BaseModel):
    drama_id: UUID
    link_id: UUID
    title: str
    material_identifier: str = ""
    first_line: int
    material_state: str
    matched_count: int
    candidate_count: int
    excluded_count: int
    unused_count: int
    unit_count: int
    ready_count: int
    selection_count: int
    pending_count: int
    failed_count: int
    protected_count: int
    reference_count: int
    used_image_count: int


class MusicBatchProgress(BaseModel):
    run_id: UUID
    revision: int
    drama_ids: list[UUID]
    status: Literal["PENDING", "READY", "FAILED", "CANCELLED"]
    processed: int
    retried: int
    skipped_protected: int
    skipped_recovered: int
    error_code: str | None
