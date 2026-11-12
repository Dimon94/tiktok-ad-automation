"""Shared account/application scene jobs and bounded draft dependencies."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from .image_composition_schemas import MediaKind
from .scene_schemas import CREATIVE_COMPILER_REVISION

# 历史不可变路由缺少 binding_revision 时固定为代数 0；新路由显式记录绑定代数。
_ROUTE_VALID = """
jsonb_typeof(frozen_route) = 'object'
AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']
AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision','binding_revision'] = '{}'::jsonb
AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$'))
AND jsonb_typeof(frozen_route->'tenant_id') = 'string'
AND jsonb_typeof(frozen_route->'bc_id') = 'string'
AND jsonb_typeof(frozen_route->'connection_id') = 'string'
AND jsonb_typeof(frozen_route->'channel') = 'string'
AND jsonb_typeof(frozen_route->'authorization_revision') = 'number'
AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string'
AND frozen_route->>'tenant_id' = tenant_id::text
AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
AND length(btrim(frozen_route->>'bc_id')) > 0
AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP')
AND frozen_route->>'authorization_revision' ~ '^[0-9]+$'
AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


class SceneJob(SQLModel, table=True):
    __tablename__ = "build_scene_job"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_build_scene_job_tenant"),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "id",
            name="uq_build_scene_job_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "capability_job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
        CheckConstraint(
            "media_kind IN ('VIDEO','CAROUSEL')", name="ck_scene_media_kind"
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED','STALE','FAILED')",
            name="ck_build_scene_job_status",
        ),
        CheckConstraint(
            "resource IN ('capabilities','native_series','native_tt_identity','native_bc_identity','identity','minis','cta','vbo','regions','budget','done')",
            name="ck_build_scene_job_resource",
        ),
        CheckConstraint(
            "revision >= 0 AND next_page > 0 AND failure_count >= 0 AND credential_revision >= 0",
            name="ck_build_scene_job_counters",
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_build_scene_job_claim",
        ),
        CheckConstraint(
            "(first_observed_at IS NULL) = (expires_at IS NULL)",
            name="ck_build_scene_job_observation",
        ),
        CheckConstraint(
            "jsonb_typeof(facts) = 'object'", name="ck_build_scene_job_facts"
        ),
        CheckConstraint(
            "frozen_route IS NULL OR COALESCE(("
            + _ROUTE_VALID
            + " AND frozen_route->>'bc_id' = bc_id"
            + " AND frozen_route->>'connection_id' = connection_id::text), false)",
            name="ck_build_scene_job_frozen_route",
        ),
        Index(
            "uq_build_scene_job_active_basis",
            "tenant_id",
            "scope_basis",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_build_scene_job_latest", "tenant_id", "scope_basis", "created_at", "id"
        ),
        Index("ix_build_scene_job_repair", "status", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    actor_id: UUID = Field(foreign_key="user.id")
    bc_id: str = Field(max_length=128)
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    credential_revision: int
    # 旧任务缺少授权语义证据，空路由只允许保留历史，不能再次发送或发布。
    frozen_route: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    # 仅保留历史场景的来源记录；新场景按账户与 Mini 复用。
    provider_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, max_length=255)
    native_selection: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    minis_id: str | None = Field(default=None, max_length=255)
    media_kind: MediaKind = Field(
        default="VIDEO", sa_column=Column(String(16), nullable=False)
    )
    compiler_contract_revision: str = Field(
        default=CREATIVE_COMPILER_REVISION, max_length=64
    )
    scope_basis: str = Field(max_length=64)
    capability_job_id: UUID | None = None
    status: str = Field(default="PENDING", max_length=16)
    resource: str = Field(default="capabilities", max_length=32)
    next_page: int = 1
    facts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    revision: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    due_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    first_observed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=64)
    failure_count: int = 0


class SceneJobPage(SQLModel, table=True):
    __tablename__ = "build_scene_job_page"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["build_scene_job.tenant_id", "build_scene_job.id"]
        ),
        UniqueConstraint("job_id", "resource", "page", name="uq_build_scene_job_page"),
        CheckConstraint("page > 0", name="ck_build_scene_job_page_number"),
        CheckConstraint(
            "resource IN ('native_series','native_tt_identity','native_bc_identity','identity','minis','cta','vbo','regions','budget')",
            name="ck_build_scene_job_page_resource",
        ),
        CheckConstraint(
            "jsonb_typeof(facts) = 'object'", name="ck_build_scene_job_page_facts"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    job_id: UUID
    resource: str = Field(max_length=32)
    page: int
    endpoint: str = Field(max_length=255)
    request_id: str | None = Field(default=None, max_length=128)
    mcp_request_id: str | None = Field(default=None, max_length=128)
    remote_task_id: str | None = Field(default=None, max_length=128)
    source_revision: str = Field(max_length=64)
    scope_basis: str = Field(max_length=64)
    facts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class DraftScenePreparation(SQLModel, table=True):
    __tablename__ = "draft_scene_preparation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_preparation.tenant_id",
                "draft_preparation.draft_id",
                "draft_preparation.id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "preparation_id",
            name="uq_draft_scene_preparation_scope",
        ),
        CheckConstraint(
            "frozen_route IS NULL OR COALESCE((" + _ROUTE_VALID + "), false)",
            name="ck_draft_scene_preparation_frozen_route",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preparation_id: UUID = Field(primary_key=True)
    draft_id: UUID
    frozen_route: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    connection_after: UUID | None = None
    capabilities_queued: bool = False
    capabilities_complete: bool = False
    scene_after: str | None = Field(default=None, max_length=128)
    scenes_queued: bool = False


class DraftCapabilityDependency(SQLModel, table=True):
    __tablename__ = "draft_capability_dependency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_scene_preparation.tenant_id",
                "draft_scene_preparation.draft_id",
                "draft_scene_preparation.preparation_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED')",
            name="ck_draft_capability_dependency_status",
        ),
        Index(
            "ix_draft_capability_dependency_pending",
            "tenant_id",
            "preparation_id",
            "status",
            "connection_id",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preparation_id: UUID = Field(primary_key=True)
    connection_id: UUID = Field(primary_key=True)
    draft_id: UUID
    bc_id: str = Field(max_length=128)
    job_id: UUID
    status: str = Field(default="PENDING", max_length=16)
    error_code: str | None = Field(default=None, max_length=64)


class DraftSceneDependency(SQLModel, table=True):
    __tablename__ = "draft_scene_dependency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_scene_preparation.tenant_id",
                "draft_scene_preparation.draft_id",
                "draft_scene_preparation.preparation_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "advertiser_id"],
            [
                "draft_account.tenant_id",
                "draft_account.draft_id",
                "draft_account.advertiser_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "job_id"],
            [
                "build_scene_job.tenant_id",
                "build_scene_job.bc_id",
                "build_scene_job.advertiser_id",
                "build_scene_job.id",
            ],
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED')",
            name="ck_draft_scene_dependency_status",
        ),
        Index(
            "ix_draft_scene_dependency_pending",
            "tenant_id",
            "preparation_id",
            "status",
            "advertiser_id",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preparation_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    draft_id: UUID
    bc_id: str = Field(max_length=128)
    job_id: UUID | None = None
    status: str = Field(default="PENDING", max_length=16)
    error_code: str | None = Field(default=None, max_length=64)
