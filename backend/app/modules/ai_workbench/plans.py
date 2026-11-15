"""Daily drama plans owned by one tenant and one AI optimizer.

Plans intentionally contain no material identifiers.  Material matching is an
execution concern, so saving a plan cannot accidentally freeze an upload.
"""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Time,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class AIDailyPlan(SQLModel, table=True):
    __tablename__ = "ai_daily_plan"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_daily_plan_tenant_id"),
        UniqueConstraint(
            "tenant_id", "optimizer_id", "business_date", name="uq_ai_daily_plan_day"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_daily_plan_optimizer",
        ),
        CheckConstraint("revision > 0", name="ck_ai_daily_plan_revision"),
        Index(
            "ix_ai_daily_plan_optimizer_day",
            "tenant_id",
            "optimizer_id",
            "business_date",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    business_date: date
    revision: int = 1
    created_by: UUID = Field(foreign_key="user.id")
    updated_by: UUID = Field(foreign_key="user.id")
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


class AIDailyPlanRow(SQLModel, table=True):
    __tablename__ = "ai_daily_plan_row"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_daily_plan_row_tenant_id"),
        UniqueConstraint(
            "tenant_id", "plan_id", "position", name="uq_ai_daily_plan_row_position"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "plan_id"],
            ["ai_daily_plan.tenant_id", "ai_daily_plan.id"],
            name="fk_ai_daily_plan_row_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_daily_plan_row_optimizer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
            name="fk_ai_daily_plan_row_provider_application",
        ),
        Index(
            "ix_ai_plan_retry_dispatch",
            "next_dispatch_at",
            "business_date",
            "scheduled_time",
            "id",
        ),
        Index(
            "ix_ai_daily_plan_row_day",
            "tenant_id",
            "optimizer_id",
            "business_date",
            "position",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    plan_id: UUID
    optimizer_id: UUID
    business_date: date
    scheduled_time: time = Field(sa_column=Column(Time(timezone=False), nullable=False))
    title: str = Field(max_length=1000)
    provider_connection_id: UUID
    application_id: str = Field(max_length=255)
    promotion_url: str | None = Field(default=None, max_length=8192)
    # Keep this as text: converting Excel values to integers would erase 0012.
    external_drama_id: str | None = Field(default=None, max_length=255)
    order_materials: bool = False
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    next_dispatch_at: datetime = Field(
        default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC),
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=lambda: datetime(1970, 1, 1, tzinfo=UTC),
        ),
    )
    last_dispatch_error: str | None = Field(default=None, max_length=128)
    position: int = Field(default=0, ge=0)
