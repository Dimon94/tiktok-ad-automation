"""链接导入独立于临时原件；每个账户请求有持久发送围栏。"""

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

from .models import access_reference, tenant_material_reference
from .routes import route_constraint


def now() -> datetime:
    return datetime.now(UTC)


class MaterialLinkBatch(SQLModel, table=True):
    __tablename__ = "material_link_batch"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_material_link_request"),
        UniqueConstraint(
            "tenant_id", "bc_id", "id", name="uq_material_link_batch_scope"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        Index(
            "ix_material_link_batch_created", "tenant_id", "bc_id", "created_at", "id"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    request_id: UUID
    request_digest: str = Field(max_length=64)
    naming_date: str = Field(max_length=8)
    input_count: int
    unique_count: int
    account_count: int
    created_at: datetime = Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MaterialLinkItem(SQLModel, table=True):
    __tablename__ = "material_link_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "batch_id"],
            [
                "material_link_batch.tenant_id",
                "material_link_batch.bc_id",
                "material_link_batch.id",
            ],
        ),
        UniqueConstraint(
            "batch_id", "line_no", "advertiser_id", name="uq_material_link_item_target"
        ),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "id",
            name="uq_material_link_item_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "duplicate_of"],
            [
                "material_link_item.tenant_id",
                "material_link_item.bc_id",
                "material_link_item.advertiser_id",
                "material_link_item.id",
            ],
        ),
        access_reference(),
        tenant_material_reference(),
        route_constraint("material_link_item", "frozen_route", connection=True),
        CheckConstraint(
            "status IN ('pending','sending','submitted','verifying','unknown','needs_review','success','failed','blocked')",
            name="ck_material_link_item_status",
        ),
        CheckConstraint(
            "line_no > 0 AND revision >= 0 AND poll_attempts >= 0 AND search_page >= 1",
            name="ck_material_link_item_numbers",
        ),
        Index(
            "ix_material_link_item_due",
            "next_run_at",
            "id",
            postgresql_where=text(
                "status IN ('pending','sending','submitted','verifying','unknown')"
            ),
        ),
        Index("ix_material_link_item_batch", "tenant_id", "batch_id", "line_no", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    batch_id: UUID
    connection_id: UUID
    advertiser_id: str = Field(max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    line_no: int
    source_video_id: str = Field(max_length=128)
    source_url: str = Field(max_length=512)
    requested_file_name: str = Field(max_length=100)
    actual_file_name: str | None = Field(default=None, max_length=1000)
    status: str = Field(default="pending", max_length=32)
    # sent_at 在外部写入前落盘；中断恢复只读，不因空搜索重传。
    sent_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    video_id: str | None = Field(default=None, max_length=128)
    mid: str | None = Field(default=None, max_length=128)
    material_id: UUID | None = None
    duplicate_of: UUID | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
    request_id: str | None = Field(default=None, max_length=128)
    poll_attempts: int = 0
    # 分页期间只保存候选身份，完整查完后才判定唯一。
    search_candidate: dict[str, str | None] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    search_page: int = 1
    revision: int = 0
    claim_id: UUID | None = None
    lease_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    next_run_at: datetime = Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
