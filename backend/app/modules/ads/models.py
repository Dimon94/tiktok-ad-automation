"""远端投放目录：对象身份与采集通道解耦，缺失父目录不阻止落库。"""

from datetime import UTC, datetime
from typing import Any, cast
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

from app.integrations.tiktok.contracts.ads import EntityKind, EntityRef, MaterialUseRef


class AwareDateTime(DateTime):
    """可继承 SQLModel 字段使用类型而非共享 Column，且时间始终带时区。"""

    def __init__(self, timezone: bool = True) -> None:
        super().__init__(timezone=timezone)


def account_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "advertiser_id"],
        ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
    )


class AdObject(SQLModel, table=True):
    __tablename__ = "ad_object"
    __table_args__ = (
        account_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "source_channel"],
            [
                "tiktok_connection.tenant_id",
                "tiktok_connection.id",
                "tiktok_connection.kind",
            ],
        ),
        CheckConstraint(
            "kind IN ('campaign','adgroup','ad','creative')", name="ck_ad_object_kind"
        ),
        CheckConstraint(
            "(parent_kind IS NULL) = (parent_remote_id IS NULL) AND "
            "(parent_kind IS NULL OR parent_kind IN ('campaign','adgroup','ad','creative'))",
            name="ck_ad_object_parent",
        ),
        CheckConstraint(
            "(source_connection_id IS NULL) = (source_channel IS NULL)",
            name="ck_ad_object_source",
        ),
        CheckConstraint("published_version > 0", name="ck_ad_object_version"),
        # 类型存在性包括任意层级及已删除对象，三键匹配后首条即可结束。
        Index("ix_ad_object_type", "tenant_id", "advertiser_id", "ad_type"),
        Index(
            "ix_ad_object_parent",
            "tenant_id",
            "advertiser_id",
            "parent_kind",
            "parent_remote_id",
            "kind",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    kind: str = Field(primary_key=True, max_length=16)
    remote_id: str = Field(primary_key=True, max_length=128)
    # 父身份仅与本行共享租户/账户，不要求父对象已经同步。
    parent_kind: str | None = Field(default=None, max_length=16)
    parent_remote_id: str | None = Field(default=None, max_length=128)
    ad_type: str = Field(max_length=64)
    name: str = ""
    configuration: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    operation_status: str | None = None
    review_status: str | None = None
    delivery_status: str | None = None
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    published_version: int
    source_connection_id: UUID | None = None
    source_channel: str | None = Field(default=None, max_length=32)

    @property
    def ref(self) -> EntityRef:
        return EntityRef(
            self.tenant_id,
            self.advertiser_id,
            cast(EntityKind, self.kind),
            self.remote_id,
        )

    @property
    def parent_ref(self) -> EntityRef | None:
        if self.parent_kind is None or self.parent_remote_id is None:
            return None
        return EntityRef(
            self.tenant_id,
            self.advertiser_id,
            cast(EntityKind, self.parent_kind),
            self.parent_remote_id,
        )


class CampaignNameProjection(SQLModel, table=True):
    __tablename__ = "campaign_name_projection"
    __table_args__ = (
        account_reference(),
        CheckConstraint(
            "parser_revision > 0 AND name_revision > 0 AND grouping_revision > 0 AND grouping_revision <= name_revision",
            name="ck_campaign_name_revisions",
        ),
        CheckConstraint(
            "(status = 'VALID' AND provider_label IS NOT NULL AND drama_name IS NOT NULL) OR "
            "(status = 'INVALID' AND provider_label IS NULL AND drama_name IS NULL)",
            name="ck_campaign_name_status",
        ),
        Index(
            "ix_campaign_name_group",
            "tenant_id",
            "provider_label",
            "drama_name",
            "advertiser_id",
            "campaign_remote_id",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    campaign_remote_id: str = Field(primary_key=True, max_length=128)
    name_revision: int = Field(primary_key=True)
    raw_name: str
    provider_label: str | None = None
    drama_name: str | None = None
    status: str = Field(max_length=16)
    parser_revision: int
    grouping_revision: int
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    @property
    def campaign_ref(self) -> EntityRef:
        return EntityRef(
            self.tenant_id, self.advertiser_id, "campaign", self.campaign_remote_id
        )


class AdMaterialReference(SQLModel, table=True):
    __tablename__ = "ad_material_reference"
    __table_args__ = (
        account_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "local_material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        # NULLS NOT DISTINCT 使普通广告缺少广告内 ID 时仍是稳定使用身份。
        UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "ad_remote_id",
            "platform_material_id",
            "ad_material_id",
            "material_type",
            name="uq_ad_material_usage",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("published_version > 0", name="ck_ad_material_version"),
        CheckConstraint(
            "jsonb_typeof(creative_ids) = 'array'", name="ck_ad_material_creatives"
        ),
        Index("ix_ad_material_ad", "tenant_id", "advertiser_id", "ad_remote_id"),
        # 同时支持视频反查及完整使用身份。保留两列短索引时，估计每视频一行的
        # 缓存计划会选短索引，在热门视频上逐广告过滤4万引用；不能只再加长索引。
        Index(
            "ix_ad_material_video",
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
            "ad_remote_id",
            "material_type",
            postgresql_where=text("complete"),
        ),
        # 文件名无固定长度上限：哈希索引只用于缩小候选，查询仍以原文相等复核。
        Index(
            "ix_ad_material_complete_name",
            "tenant_id",
            "advertiser_id",
            text("md5(name)"),
            postgresql_where=text("complete"),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    ad_remote_id: str = Field(max_length=128)
    platform_material_id: str = Field(max_length=128)
    ad_material_id: str | None = Field(default=None, max_length=128)
    material_type: str = Field(max_length=64)
    # 外部素材可没有本地文件；目录保存素材使用身份和名称，报表主素材身份由帖子关联维护。
    name: str = ""
    creative_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    local_material_id: UUID | None = None
    operation_status: str | None = None
    complete: bool = False
    published_version: int

    @property
    def use_ref(self) -> MaterialUseRef:
        return MaterialUseRef(
            EntityRef(self.tenant_id, self.advertiser_id, "ad", self.ad_remote_id),
            self.platform_material_id,
            self.ad_material_id,
            self.material_type,
        )
