"""AI optimizer identity, immutable configuration versions and scoped bindings."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class AIOptimizer(SQLModel, table=True):
    __tablename__ = "ai_optimizer"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_optimizer_tenant_id"),
        Index("ix_ai_optimizer_tenant", "tenant_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=120)
    active: bool = True
    latest_version: int = Field(default=0, ge=0)
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )


class AIOptimizerStrategyBinding(SQLModel, table=True):
    """Current and historical strategy references for one optimizer.

    A partial unique index allows history to remain queryable while enforcing one
    active binding. The tenant columns in both foreign keys prevent cross-tenant
    IDs from being attached accidentally.
    """

    __tablename__ = "ai_optimizer_strategy_binding"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "optimizer_id", "id", name="uq_ai_binding_tenant_optimizer_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_binding_optimizer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
            name="fk_ai_binding_strategy_version",
        ),
        Index(
            "uq_ai_binding_one_active",
            "tenant_id",
            "optimizer_id",
            unique=True,
            postgresql_where="active IS TRUE",
        ),
        Index("ix_ai_binding_optimizer", "tenant_id", "optimizer_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    strategy_version_id: UUID
    active: bool = True
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )


class AIOptimizerVersion(SQLModel, table=True):
    """Immutable optimizer configuration snapshot used by downstream work."""

    __tablename__ = "ai_optimizer_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_optimizer_version_tenant_id"),
        UniqueConstraint(
            "tenant_id", "optimizer_id", "number", name="uq_ai_optimizer_version_number"
        ),
        UniqueConstraint(
            "tenant_id", "request_id", name="uq_ai_optimizer_version_request"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_version_optimizer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id", "strategy_binding_id"],
            [
                "ai_optimizer_strategy_binding.tenant_id",
                "ai_optimizer_strategy_binding.optimizer_id",
                "ai_optimizer_strategy_binding.id",
            ],
            name="fk_ai_version_binding",
        ),
        CheckConstraint(
            "number > 0 AND daily_spend > 0 AND (target_roas IS NULL OR target_roas > 0)",
            name="ck_ai_optimizer_version_values",
        ),
        CheckConstraint(
            "priority IN ('SPEND','ROAS','BALANCED')", name="ck_ai_optimizer_priority"
        ),
        CheckConstraint(
            "optimization_mode IN ('AUTO','MANUAL')", name="ck_ai_optimizer_mode"
        ),
        Index(
            "ix_ai_optimizer_version_optimizer", "tenant_id", "optimizer_id", "number"
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    number: int
    daily_spend: Decimal = Field(sa_column=Column(Numeric(38, 12), nullable=False))
    target_roas: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(38, 12), nullable=True)
    )
    priority: str = Field(default="BALANCED", max_length=16)
    bc_id: str = Field(max_length=128)
    advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    optimization_mode: str = Field(default="MANUAL", max_length=16)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    review_time: str = Field(default="13:00", max_length=5)
    material_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    authorization_limits: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    strategy_binding_id: UUID
    request_id: UUID
    request_digest: str = Field(max_length=64)
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )


class AIOptimizerRuleBinding(SQLModel, table=True):
    """Explicit optimizer-to-rule links; rules remain owned by automation_rules."""

    __tablename__ = "ai_optimizer_rule_binding"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "optimizer_id", "rule_id", name="uq_ai_optimizer_rule"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_rule_binding_optimizer",
        ),
        Index("ix_ai_rule_binding_optimizer", "tenant_id", "optimizer_id", "active"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    rule_id: UUID = Field(foreign_key="automation_rule.id")
    active: bool = True
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )


class BuildOccurrence(SQLModel, table=True):
    """Immutable handoff ledger for one scheduled daily-plan row.

    The row is the durable boundary between the AI workbench and the existing
    build pipeline.  Account and strategy values are copied at creation time;
    later optimizer edits therefore cannot retarget an already scheduled build.
    """

    __tablename__ = "ai_build_occurrence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_build_occurrence_tenant_id"),
        UniqueConstraint(
            "tenant_id", "request_id", name="uq_ai_build_occurrence_request"
        ),
        UniqueConstraint(
            "tenant_id",
            "optimizer_id",
            "plan_row_id",
            "strategy_version_id",
            "scheduled_at",
            name="uq_ai_build_occurrence_schedule",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_build_occurrence_optimizer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "plan_row_id"],
            ["ai_daily_plan_row.tenant_id", "ai_daily_plan_row.id"],
            name="fk_ai_build_occurrence_plan_row",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
            name="fk_ai_build_occurrence_strategy",
        ),
        CheckConstraint(
            "status IN ('PENDING','WAITING_MATERIAL','DISPATCHED','PARTIAL','UNKNOWN','COMPLETE','FAILED')",
            name="ck_ai_build_occurrence_status",
        ),
        Index("ix_ai_build_occurrence_due", "status", "scheduled_at", "id"),
        Index(
            "ix_ai_build_occurrence_optimizer",
            "tenant_id",
            "optimizer_id",
            "scheduled_at",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    plan_row_id: UUID
    request_id: UUID
    request_digest: str = Field(max_length=64)
    strategy_version_id: UUID
    # These are intentionally frozen snapshots, not mutable optimizer joins.
    bc_id: str = Field(max_length=128)
    advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    provider_connection_id: UUID
    application_id: str = Field(max_length=255)
    title: str = Field(max_length=1000)
    promotion_url: str | None = Field(default=None, max_length=8192)
    external_drama_id: str | None = Field(default=None, max_length=255)
    order_materials: bool = False
    scheduled_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    status: str = "PENDING"
    draft_id: UUID | None = None
    preview_id: UUID | None = None
    submission_id: UUID | None = None
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    error_code: str | None = None
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )
    updated_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )
