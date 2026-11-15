"""Execution facts reference frozen intent; remote evidence is append-only."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.materials import cover_models  # noqa: F401 - register scoped FK target


def utcnow() -> datetime:
    return datetime.now(UTC)


def submission_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "submission_id", "preview_id", "bc_id"],
        [
            "build_submission.tenant_id",
            "build_submission.id",
            "build_submission.preview_id",
            "build_submission.bc_id",
        ],
    )


class Submission(SQLModel, table=True):
    __tablename__ = "build_submission"
    __table_args__ = (
        UniqueConstraint("tenant_id", "preview_id", name="uq_submission_preview"),
        UniqueConstraint(
            "tenant_id", "draft_id", "ordinal", name="uq_submission_ordinal"
        ),
        UniqueConstraint(
            "tenant_id", "id", "preview_id", "bc_id", name="uq_submission_scope"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_submission_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','NEEDS_REVIEW')",
            name="ck_submission_status",
        ),
        CheckConstraint(
            "dispatch_revision >= 0 AND ordinal > 0", name="ck_submission_revision"
        ),
        Index("ix_submission_page", "tenant_id", "bc_id", "created_at", "id"),
        Index("ix_submission_repair", "expanded", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    preview_id: UUID
    draft_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    authorization_stamp: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    ordinal: int
    status: str = "QUEUED"
    expanded: bool = False
    progress: dict[str, Any] = Field(
        default_factory=lambda: {"phase": "units"},
        sa_column=Column(JSONB, nullable=False),
    )
    dispatch_revision: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    error_code: str | None = None
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SubmissionRequest(SQLModel, table=True):
    __tablename__ = "submission_request"
    __table_args__ = (submission_fk(),)
    tenant_id: UUID = Field(primary_key=True)
    request_id: UUID = Field(primary_key=True)
    submission_id: UUID
    preview_id: UUID
    bc_id: str = Field(max_length=128)


class DraftUnitReservation(SQLModel, table=True):
    __tablename__ = "draft_unit_reservation"
    __table_args__ = (
        submission_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "unit_id", "preview_id", "bc_id"],
            [
                "build_unit.tenant_id",
                "build_unit.id",
                "build_unit.preview_id",
                "build_unit.bc_id",
            ],
        ),
        Index("ix_reservation_submission", "tenant_id", "submission_id", "unit_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    drama_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    bc_id: str = Field(max_length=128)
    submission_id: UUID
    preview_id: UUID
    unit_id: UUID


class SubmissionUnit(SQLModel, table=True):
    __tablename__ = "submission_unit"
    __table_args__ = (
        Index(
            "ix_submission_unit_dispatch",
            "tenant_id",
            "submission_id",
            "unit_id",
            postgresql_where=text(
                "disposition = 'INCLUDED' AND (expanded = false OR dispatch_id IS NOT NULL)"
            ),
        ),
        submission_fk(),
        UniqueConstraint("tenant_id", "unit_id", name="uq_submission_frozen_unit"),
        CheckConstraint("dispatch_revision >= 0", name="ck_unit_dispatch_revision"),
        Index("ix_unit_dispatch_repair", "expanded", "repair_after", "unit_id"),
        ForeignKeyConstraint(
            ["tenant_id", "unit_id", "preview_id", "bc_id"],
            [
                "build_unit.tenant_id",
                "build_unit.id",
                "build_unit.preview_id",
                "build_unit.bc_id",
            ],
        ),
        UniqueConstraint(
            "tenant_id",
            "submission_id",
            "preview_id",
            "bc_id",
            "unit_id",
            name="uq_submission_unit_scope",
        ),
        CheckConstraint(
            "disposition IN ('INCLUDED','EXCLUDED')",
            name="ck_submission_unit_disposition",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    submission_id: UUID = Field(primary_key=True)
    unit_id: UUID = Field(primary_key=True)
    preview_id: UUID
    bc_id: str = Field(max_length=128)
    disposition: str
    reason_code: str | None = None
    expanded: bool = False

    dispatch_revision: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    due_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ExecutionStep(SQLModel, table=True):
    __tablename__ = "execution_step"
    __table_args__ = (
        Index(
            "ix_recovery_candidates",
            "tenant_id",
            "submission_id",
            "id",
            postgresql_where=text(
                "dispatch_id IS NULL AND (status='UNKNOWN' OR mismatch "
                "OR (kind='MATERIAL' AND status='FAILED'))"
            ),
        ),
        Index(
            "ix_recovery_known_parent",
            "tenant_id",
            "submission_id",
            "id",
            postgresql_where=text("remote_id IS NOT NULL"),
        ),
        Index(
            "ix_recovery_readback_parent",
            "tenant_id",
            "submission_id",
            "parent_step_id",
            "id",
            postgresql_where=text(
                "dispatch_id IS NULL AND kind='READBACK' AND status<>'SUCCEEDED'"
            ),
        ),
        Index("ix_execution_scope_status", "tenant_id", "submission_id", "status"),
        Index(
            "ix_execution_unit_due",
            "tenant_id",
            "submission_id",
            "unit_id",
            "due_at",
            "id",
            postgresql_where=text(
                "status IN ('PENDING','RETRYABLE','UNKNOWN','RUNNING')"
            ),
        ),
        submission_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "preview_id", "bc_id", "unit_id"],
            [
                "submission_unit.tenant_id",
                "submission_unit.submission_id",
                "submission_unit.preview_id",
                "submission_unit.bc_id",
                "submission_unit.unit_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "group_id"],
            ["planned_group.tenant_id", "planned_group.preview_id", "planned_group.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "planned_ad_id"],
            ["planned_ad.tenant_id", "planned_ad.preview_id", "planned_ad.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "parent_step_id"],
            [
                "execution_step.tenant_id",
                "execution_step.submission_id",
                "execution_step.id",
            ],
        ),
        UniqueConstraint("tenant_id", "submission_id", "id", name="uq_step_scope"),
        UniqueConstraint(
            "tenant_id", "submission_id", "step_key", name="uq_execution_step_key"
        ),
        CheckConstraint(
            "kind IN ('MATERIAL','CTA','CAMPAIGN','ADGROUP','AD','READBACK')",
            name="ck_step_kind",
        ),
        CheckConstraint(
            "status IN ('QUEUED','PENDING','RUNNING','SUCCEEDED','FAILED','RETRYABLE','UNKNOWN')",
            name="ck_step_status",
        ),
        CheckConstraint(
            "phase IN ('IDLE','CLAIMED','REQUEST_ARMED','DONE')", name="ck_step_phase"
        ),
        CheckConstraint(
            "attempt >= 0 AND dispatch_revision >= 0", name="ck_step_counters"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "id", "attempt", "attempt_id"],
            [
                "build_attempt_context.tenant_id",
                "build_attempt_context.step_id",
                "build_attempt_context.attempt",
                "build_attempt_context.attempt_id",
            ],
            name="fk_step_current_attempt",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "attempt = 0 OR attempt_id IS NOT NULL", name="ck_step_attempt_identity"
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)", name="ck_step_lease"
        ),
        CheckConstraint(
            "phase != 'REQUEST_ARMED' OR (request_body IS NOT NULL AND request_body_digest IS NOT NULL)",
            name="ck_step_request_intent",
        ),
        CheckConstraint(
            "request_body IS NULL OR jsonb_typeof(request_body) = 'object'",
            name="ck_step_body_object",
        ),
        CheckConstraint(
            "remote_id IS NULL OR length(trim(remote_id)) > 0", name="ck_step_remote_id"
        ),
        Index("ix_execution_step_due", "status", "due_at", "id"),
        Index(
            "ix_execution_step_unit",
            "tenant_id",
            "submission_id",
            "unit_id",
            "kind",
            "id",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    submission_id: UUID
    preview_id: UUID
    bc_id: str = Field(max_length=128)
    unit_id: UUID
    kind: str
    step_key: str = Field(max_length=255)
    group_id: UUID | None = None
    planned_ad_id: UUID | None = None
    material_id: UUID | None = None
    parent_step_id: UUID | None = None
    distribution_id: UUID | None = Field(
        default=None, foreign_key="material_distribution.id"
    )
    cover_job_id: UUID | None = Field(default=None, foreign_key="material_cover_job.id")
    status: str = "PENDING"
    phase: str = "IDLE"
    remote_id: str | None = None
    attempt: int = 0
    attempt_id: UUID | None = None
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    request_body: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    request_body_digest: str | None = Field(default=None, max_length=64)
    resolved: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    error_code: str | None = None
    operation_status: str | None = None
    review_status: str | None = None
    mismatch: bool = False
    checked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    due_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    dispatch_revision: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class StepEvidence(SQLModel, table=True):
    __tablename__ = "step_evidence"
    __table_args__ = (
        Index("ix_step_evidence_identity", "tenant_id", "submission_id", "step_id"),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "step_id"],
            [
                "execution_step.tenant_id",
                "execution_step.submission_id",
                "execution_step.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "step_id", "attempt"],
            [
                "build_attempt_context.tenant_id",
                "build_attempt_context.submission_id",
                "build_attempt_context.step_id",
                "build_attempt_context.attempt",
            ],
            name="fk_evidence_attempt_context",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("attempt >= 0", name="ck_evidence_attempt"),
        Index(
            "ix_step_evidence_page", "tenant_id", "submission_id", "observed_at", "id"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    submission_id: UUID
    step_id: UUID
    attempt: int
    lease_token: UUID | None = None
    request_id: str | None = None
    conclusion: str = Field(max_length=64)
    summary: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
