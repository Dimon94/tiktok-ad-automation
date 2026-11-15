"""素材订单的不可变请求与恢复状态，不在模型注册期间加载任务执行器。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MaterialOrder(SQLModel, table=True):
    __tablename__ = "ai_material_order"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_row_id", name="uq_ai_material_order_row"),
        ForeignKeyConstraint(
            ["tenant_id", "plan_row_id"],
            ["ai_daily_plan_row.tenant_id", "ai_daily_plan_row.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"], ["ai_optimizer.tenant_id", "ai_optimizer.id"]
        ),
        Index("ix_ai_material_order_due", "status", "next_attempt_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    plan_row_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    title: str = Field(max_length=1000)
    request_body: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    status: str = Field(default="PENDING", max_length=24)
    job_id: str | None = Field(default=None, max_length=255)
    evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    error_code: str | None = Field(default=None, max_length=128)
    attempts: int = 0
    dispatch_generation: int = 0
    next_attempt_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=lambda: datetime.now(UTC),
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=lambda: datetime.now(UTC),
        ),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=lambda: datetime.now(UTC),
        ),
    )
