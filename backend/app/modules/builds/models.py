"""Editable tenant drafts; no advertising objects are created by preparation."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.builds.image_composition_schemas import MediaKind


def draft_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "draft_id"], ["build_draft.tenant_id", "build_draft.id"]
    )


class BuildDraft(SQLModel, table=True):
    __tablename__ = "build_draft"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_build_draft_tenant_id"),
        UniqueConstraint("tenant_id", "id", "bc_id", name="uq_build_draft_bc"),
        UniqueConstraint("tenant_id", "request_id", name="uq_build_draft_request"),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "execution_connection_id"],
            [
                "bc_connection_binding.tenant_id",
                "bc_connection_binding.bc_id",
                "bc_connection_binding.connection_id",
            ],
            name="fk_build_draft_execution_connection",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "id", "bc_id", "media_kind", name="uq_build_draft_media_kind"
        ),
        CheckConstraint(
            "media_kind IN ('VIDEO','CAROUSEL')", name="ck_build_draft_media_kind"
        ),
        CheckConstraint(
            "active_image_generation >= 0 AND (applied_image_rule_revision IS NULL OR applied_image_rule_revision > 0)",
            name="ck_build_draft_image_versions",
        ),
        CheckConstraint(
            "structure_override IS NULL OR jsonb_typeof(structure_override) = 'object'",
            name="ck_build_draft_structure_object",
        ),
        CheckConstraint("revision > 0", name="ck_build_draft_revision"),
        CheckConstraint(
            "COALESCE((scene_selection->>'kind'='NATIVE_SERIES_DIRECT_VIDEO' AND strategy_version_id IS NOT NULL AND provider_connection_id IS NULL AND application_id IS NULL AND link_config='{}'::jsonb AND media_kind='VIDEO') OR ((scene_selection IS NULL OR scene_selection->>'kind'='MINI_APP') AND ((strategy_version_id IS NOT NULL AND provider_connection_id IS NOT NULL AND application_id IS NOT NULL) OR (link_config->>'source_mode'='CAMPAIGN_COPY' AND strategy_version_id IS NULL AND provider_connection_id IS NULL AND application_id IS NULL))),false)",
            name="ck_draft_source_references",
        ),
        CheckConstraint(
            "targeting_override IS NULL OR jsonb_typeof(targeting_override) = 'object'",
            name="ck_build_draft_targeting_object",
        ),
        CheckConstraint(
            "status IN ('DRAFT','PREPARING','READY','BLOCKED')",
            name="ck_build_draft_status",
        ),
        Index("ix_build_draft_tenant_page", "tenant_id", "bc_id", "created_at", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    # P2 图片直接使用创意单元；旧视频素材表由 P3 与规划器一起切换。
    media_kind: MediaKind = Field(
        default="VIDEO",
        sa_column=Column(String(16), nullable=False, server_default="VIDEO"),
    )
    active_image_generation: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    applied_image_rule_revision: int | None = None
    structure_override: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    scene_selection: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    revision: int = 1
    status: str = "DRAFT"
    strategy_version_id: UUID | None = None
    provider_connection_id: UUID | None = None
    # 可编辑偏好与后代冻结路由分开；NULL 仅在新准备时解析 BC 默认。
    execution_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, max_length=255)
    link_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # 排期属于本次广告搭建，不能随投放策略版本复用。
    delivery_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    targeting_override: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    identity_display_name: str | None = Field(default=None, max_length=255)
    identity_username: str | None = Field(default=None, max_length=255)
    created_by: UUID = Field(foreign_key="user.id")
    request_id: UUID
    request_digest: str = Field(max_length=64)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class DraftInput(SQLModel, table=True):
    __tablename__ = "draft_input"
    __table_args__ = (
        draft_reference(),
        UniqueConstraint(
            "tenant_id", "draft_id", "kind", "line_no", name="uq_draft_input_line"
        ),
        CheckConstraint(
            "kind IN ('drama','account') AND line_no > 0", name="ck_draft_input_line"
        ),
        CheckConstraint(
            "duplicate_of IS NULL OR duplicate_of > 0", name="ck_draft_input_duplicate"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_input_id"],
            ["link_preparation_item.tenant_id", "link_preparation_item.id"],
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    kind: str = Field(max_length=16)
    line_no: int
    raw_text: str = Field(max_length=1000)
    manual_link: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    status: str = Field(default="pending", max_length=32)
    reason_code: str | None = None
    duplicate_of: int | None = None
    advertiser_id: str | None = None
    drama_id: UUID | None = None
    provider_input_id: UUID | None = None
    candidates: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )


class DraftPreparation(SQLModel, table=True):
    __tablename__ = "draft_preparation"
    __table_args__ = (
        draft_reference(),
        UniqueConstraint(
            "tenant_id", "draft_id", "id", name="uq_draft_preparation_identity"
        ),
        UniqueConstraint(
            "tenant_id", "request_id", name="uq_draft_preparation_request"
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "draft_revision",
            name="uq_draft_preparation_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_task_id"],
            ["link_preparation.tenant_id", "link_preparation.id"],
        ),
        CheckConstraint(
            "draft_revision > 0 AND generation >= 0 AND account_after >= 0",
            name="ck_draft_preparation_progress",
        ),
        CheckConstraint(
            "status IN ('PENDING','READY','BLOCKED','OBSOLETE')",
            name="ck_draft_preparation_status",
        ),
        CheckConstraint(
            "phase IN ('accounts','links','materials','scenes','done')",
            name="ck_draft_preparation_phase",
        ),
        Index("ix_draft_preparation_repair", "status", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    draft_revision: int
    actor_id: UUID = Field(foreign_key="user.id")
    request_id: UUID
    provider_task_id: UUID | None = None
    phase: str = "accounts"
    status: str = "PENDING"
    account_after: int = 0
    link_cursor: str | None = None
    pending_links: bool = False
    generation: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    due_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    repair_after: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    error_code: str | None = None


class DraftPreparationRequest(SQLModel, table=True):
    __tablename__ = "draft_preparation_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_preparation.tenant_id",
                "draft_preparation.draft_id",
                "draft_preparation.id",
            ],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    request_id: UUID = Field(primary_key=True)
    draft_id: UUID
    preparation_id: UUID


class DraftAccount(SQLModel, table=True):
    __tablename__ = "draft_account"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "connection_id"],
            [
                "bc_account_access.tenant_id",
                "bc_account_access.bc_id",
                "bc_account_access.advertiser_id",
                "bc_account_access.connection_id",
            ],
        ),
        CheckConstraint("first_line > 0", name="ck_draft_account_line"),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    bc_id: str = Field(max_length=128)
    connection_id: UUID
    currency: str = Field(max_length=8)
    timezone: str = Field(max_length=128)
    first_line: int


class DraftDrama(SQLModel, table=True):
    __tablename__ = "draft_drama"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "drama_id"], ["provider_drama.tenant_id", "provider_drama.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "link_id"], ["promotion_link.tenant_id", "promotion_link.id"]
        ),
        CheckConstraint(
            "first_line > 0 AND matched_count >= 0", name="ck_draft_drama_progress"
        ),
        CheckConstraint(
            "material_state IN ('pending','matching','ready','manual')",
            name="ck_draft_drama_material_state",
        ),
        UniqueConstraint(
            "tenant_id", "draft_id", "bc_id", "drama_id", name="uq_draft_drama_bc"
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    drama_id: UUID = Field(primary_key=True)
    bc_id: str = Field(max_length=128)
    link_id: UUID
    title: str = Field(max_length=1000)
    first_line: int
    material_state: str = "pending"
    material_cursor: str | None = None
    matched_count: int = 0


class DraftGroupMaterial(SQLModel, table=True):
    """退役草稿表，仅为历史迁移/schema 审计保留；当前读写使用 DraftCreativeUnit。"""

    __tablename__ = "draft_group_material"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "drama_id"],
            [
                "draft_drama.tenant_id",
                "draft_drama.draft_id",
                "draft_drama.bc_id",
                "draft_drama.drama_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "drama_id",
            "material_id",
            name="uq_draft_drama_material",
        ),
        CheckConstraint(
            "group_no > 0 AND position > 0", name="ck_draft_group_position"
        ),
        Index("ix_draft_shared_material", "tenant_id", "draft_id", "material_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    drama_id: UUID = Field(primary_key=True)
    group_no: int = Field(primary_key=True)
    position: int = Field(primary_key=True)
    bc_id: str = Field(max_length=128)
    material_id: UUID
