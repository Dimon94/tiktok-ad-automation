# backend/app/modules/reporting/store_models.py
"""报表事实按“租户 + 广告账户”存放，不挂在 BC 下；BC 可见性由当前授权决定。

一行一个对象一个时间桶，指标各占一列；平台 '-' 存 NULL，金额保留平台精度。
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Numeric,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime

# 所有日/小时事实共享的指标列；顺序即 API 与导出列序。
METRIC_COLUMNS = (
    "spend",
    "impressions",
    "clicks",
    "ad_impression_events",
    "revenue_d0",
    "revenue_d3",
    "revenue_d7",
    "revenue_d14",
    "revenue_d30",
    "revenue_total",
)


def _num() -> Any:
    return Field(default=None, sa_type=Numeric, nullable=True)


class _Metrics(SQLModel):
    spend: Decimal | None = _num()
    impressions: Decimal | None = _num()
    clicks: Decimal | None = _num()
    ad_impression_events: Decimal | None = _num()
    revenue_d0: Decimal | None = _num()
    revenue_d3: Decimal | None = _num()
    revenue_d7: Decimal | None = _num()
    revenue_d14: Decimal | None = _num()
    revenue_d30: Decimal | None = _num()
    revenue_total: Decimal | None = _num()


class _Fact(_Metrics):
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    currency: str = Field(max_length=16)
    timezone: str = Field(max_length=64)
    fetched_at: datetime = Field(sa_type=AwareDateTime)
    sync_job_id: UUID


class AdReportDay(_Fact, table=True):
    """唯一明细来源：一条广告（ad_id_v2）一天；按月分区（迁移中声明）。"""

    __tablename__ = "ad_report_day"
    __table_args__ = (
        Index("ix_ad_report_day_period", "tenant_id", "stat_date", "advertiser_id"),
        Index(
            "ix_ad_report_day_spending",
            "tenant_id",
            "stat_date",
            "advertiser_id",
            postgresql_include=["timezone", "ad_id", "adgroup_id", "campaign_id"],
            postgresql_where=text("spend > 0"),
        ),
        Index("ix_ad_report_day_campaign", "tenant_id", "campaign_id", "stat_date"),
    )
    stat_date: date = Field(primary_key=True)
    ad_id: str = Field(primary_key=True, max_length=64)
    campaign_id: str = Field(max_length=64)
    adgroup_id: str = Field(max_length=64)
    campaign_automation_type: str | None = Field(default=None, max_length=64)
    ad_name: str | None = None
    campaign_name: str | None = None
    adgroup_name: str | None = None


class CampaignReportDay(_Fact, table=True):
    """由 ad_report_day 在同一事务汇总写入的查询加速表，不独立拉取。"""

    __tablename__ = "campaign_report_day"
    __table_args__ = (
        Index(
            "ix_campaign_report_day_period", "tenant_id", "stat_date", "advertiser_id"
        ),
    )
    stat_date: date = Field(primary_key=True)
    campaign_id: str = Field(primary_key=True, max_length=64)
    campaign_name: str | None = None


class CampaignReportHour(_Fact, table=True):
    """平台系列小时原始事实；stat_hour 为账户本地整点对应的 UTC 时刻，不自动删金额。"""

    __tablename__ = "campaign_report_hour"
    stat_hour: datetime = Field(primary_key=True, sa_type=AwareDateTime)
    campaign_id: str = Field(primary_key=True, max_length=64)


class MaterialReportDay(_Fact, table=True):
    """Smart+ 素材分时报表：主素材一天；按月分区。素材无 D3、D30（恒为 NULL）。"""

    __tablename__ = "material_report_day"
    __table_args__ = (
        Index(
            "ix_material_report_day_spending",
            "tenant_id",
            "stat_date",
            "advertiser_id",
            postgresql_include=["timezone", "main_material_id"],
            postgresql_where=text("spend > 0"),
        ),
        Index(
            "ix_material_report_day_period", "tenant_id", "stat_date", "advertiser_id"
        ),
        Index(
            "ix_material_report_day_material",
            "tenant_id",
            "main_material_id",
            "stat_date",
        ),
    )
    stat_date: date = Field(primary_key=True)
    main_material_id: str = Field(primary_key=True, max_length=64)
    main_material_type: str = Field(max_length=64)
    main_material_name: str | None = None


class MaterialPostLink(SQLModel, table=True):
    """素材日报主素材 → 所属广告的对照。

    Spark 广告的主素材 ID 是 TikTok 为每条广告发布的帖子 ID，素材日报本身不带广告；
    创意概览报表按“广告 × 主素材”返回同一 ID 及广告内素材 ID（ad_material_id），
    据此可精确对上广告目录里的素材使用位置，进而得到素材库视频。
    帖子发布后不会换广告，只增不改；非 Spark 主素材可能对应多条广告。
    """

    __tablename__ = "material_post_link"
    __table_args__ = (
        Index("ix_material_post_link_ad", "tenant_id", "advertiser_id", "ad_remote_id"),
        Index(
            "ix_material_post_link_video_page",
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
            "main_material_id",
            "main_material_type",
            postgresql_where=text("platform_material_id IS NOT NULL"),
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    main_material_id: str = Field(primary_key=True, max_length=64)
    ad_remote_id: str = Field(primary_key=True, max_length=128)
    main_material_type: str = Field(max_length=64)
    ad_material_id: str | None = Field(default=None, max_length=128)
    material_name: str | None = None
    # 建立对照时解析的素材库视频；目录之后变化也不改，已解析的金额归属保持稳定。
    platform_material_id: str | None = Field(default=None, max_length=128)
    observed_at: datetime = Field(sa_type=AwareDateTime)


class MaterialLinkScan(SQLModel, table=True):
    """每个账户最近一次对照扫描；身份证据变化或六小时复核时重扫。

    概览接口按帖子 ID 筛选不生效，每次只能拉全账户；已删除广告的帖子永远查不到，
    记下它们以免每次热刷新都重复整账户扫描。
    """

    __tablename__ = "material_link_scan"
    evidence_signature: str | None = Field(default=None, max_length=32)
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    query_scope: str = Field(default="unscoped", primary_key=True, max_length=32)
    scanned_at: datetime = Field(sa_type=AwareDateTime)
    unresolved_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )


class AccountReconciliationDay(SQLModel, table=True):
    """账户报表原值与广告汇总值的每日对账，只用于提示，不进入展示。"""

    __tablename__ = "account_reconciliation_day"
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    stat_date: date = Field(primary_key=True)
    account_spend: Decimal | None = _num()
    ad_sum_spend: Decimal | None = _num()
    account_revenue_total: Decimal | None = _num()
    ad_sum_revenue_total: Decimal | None = _num()
    checked_at: datetime = Field(sa_type=AwareDateTime)


REPORT_KINDS = (
    "ad_day",
    "campaign_hour",
    "material_day",
    "account_day",
)


class ReportDispatchCursor(SQLModel, table=True):
    """报表专属跨轮派发游标，不能与其他队列的 outbox 发布游标混用。"""

    __tablename__ = "report_dispatch_cursor"
    tenant_id: UUID = Field(
        primary_key=True, foreign_key="tenant.id", ondelete="CASCADE"
    )
    last_dispatched_at: datetime = Field(sa_type=AwareDateTime)
    fairness_state: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )


class DirectoryDispatchCursor(SQLModel, table=True):
    """定向目录窗口的持久租户游标，不与报表的轮转状态混用。"""

    __tablename__ = "directory_dispatch_cursor"
    tenant_id: UUID = Field(
        primary_key=True, foreign_key="tenant.id", ondelete="CASCADE"
    )
    last_dispatched_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    # 只记实际派发类别；跨轮交替缺口与普通刷新，单槽时也不饿死另一类。
    last_status_missing: bool | None = None


class ReportSyncState(SQLModel, table=True):
    """每个账户、每种报表、每一天的发布状态；页面据此显示更新时间与缺口。"""

    __tablename__ = "report_sync_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('COMPLETE','STALE','FAILED')",
            name="ck_report_sync_state_status",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    report_kind: str = Field(primary_key=True, max_length=32)
    stat_date: date = Field(primary_key=True)
    status: str = Field(max_length=16)
    row_count: int = 0
    last_success_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    # 完成时间只描述发布延迟；覆盖比较使用实际首页观测时点。
    observed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    publication_version: int = Field(default=0, sa_type=BigInteger)
    error_code: str | None = Field(default=None, max_length=128)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class ReportSyncJob(SQLModel, table=True):
    """一次报表请求（含全部分页）一行；领取有租约，重复投递按 job_key 幂等。"""

    __tablename__ = "report_sync_job"
    __table_args__ = (
        Index(
            "ix_report_summary_archive",
            "tenant_id",
            "finished_at",
            "id",
            postgresql_where=text("status IN ('COMPLETE','FAILED','CANCELLED')"),
        ),
        CheckConstraint(
            "execution_pool IS NULL OR execution_pool IN ('background','live-report')",
            name="ck_report_execution_pool",
        ),
        Index(
            "ix_report_execution_pool",
            "execution_pool",
            "claimed_until",
            postgresql_where=text("status='RUNNING'"),
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING','COMPLETE','FAILED','CANCELLED')",
            name="ck_report_sync_job_status",
        ),
        Index("ix_report_sync_job_due", "status", "priority", "due_at"),
        # 与已发布0064迁移保持一致，模型校验不能把收尾去重索引误判为多余。
        Index(
            "ix_report_closeout_scope",
            "tenant_id",
            "bc_id",
            "actor_id",
            "end_date",
            postgresql_where="priority = 0 AND start_date = end_date",
        ),
        Index(
            "ix_report_link_pending_scope",
            "tenant_id",
            "bc_id",
            "actor_id",
            "advertiser_ids",
            postgresql_where="kind='material_links' AND status IN ('QUEUED','WAITING')",
        ),
        Index(
            "uq_report_sync_job_active_key",
            "job_key",
            unique=True,
            postgresql_where="status IN ('QUEUED','RUNNING','WAITING')",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    kind: str = Field(max_length=32)
    priority: int
    status: str = Field(default="QUEUED", max_length=16)
    advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    campaign_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    start_date: date
    end_date: date
    job_key: str = Field(max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    actor_id: UUID
    due_at: datetime = Field(sa_type=AwareDateTime)
    claim_generation: int = Field(default=0, sa_type=BigInteger)
    # 派发时冻结真实消费池；不能把逻辑directory类的历史补链送入实时Worker。
    execution_pool: str | None = Field(default=None, max_length=32)
    owner_epoch: int = Field(default=1, sa_type=BigInteger)
    contract_version: int = 2
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(default=None, sa_type=AwareDateTime)
    attempts: int = 0
    error_code: str | None = Field(default=None, max_length=128)
    request_count: int = 0
    # 关联扫描保存概览游标；已写入的页不随下一次截止重新从第一页开始。
    progress: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    finished_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class ReportSyncPage(SQLModel, table=True):
    """报表完整发布前的私有分页证据；page为任务内槽号，基本报表另存查询页映射。"""

    __tablename__ = "report_sync_page"
    __table_args__ = (
        CheckConstraint(
            "page >= 1 AND total_page >= 0 AND total_number >= 0",
            name="ck_report_sync_page_numbers",
        ),
        Index("ix_report_sync_page_row_keys", "row_keys", postgresql_using="gin"),
    )
    job_id: UUID = Field(
        primary_key=True, foreign_key="report_sync_job.id", ondelete="CASCADE"
    )
    page: int = Field(primary_key=True)
    tenant_id: UUID
    scope_key: str = Field(max_length=64)
    rows: list[dict[str, Any]] = Field(sa_column=Column(JSONB, nullable=False))
    row_keys: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    total_page: int
    total_number: int
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class AdSyncBc(SQLModel, table=True):
    """每个已绑定 API 连接的 BC 一行：冻结路由、后台身份与各档位下次执行时间。"""

    __tablename__ = "ad_sync_bc"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    actor_id: UUID
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, max_length=128)
    # 仅在当天完整筛选成功时推进；部分页和缓存命中不能伪造新鲜度。
    active_screened_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    # None=未检查；False=授权用户无 BC 报表权限，退回账户级报表筛选。
    bc_report_supported: bool | None = None
    next_hot_at: datetime = Field(sa_type=AwareDateTime)
    next_material_at: datetime = Field(sa_type=AwareDateTime)
    next_directory_at: datetime = Field(sa_type=AwareDateTime)
    next_daily_at: datetime = Field(sa_type=AwareDateTime)
    # 账户级筛选逐请求保存游标，额度或期限耗尽后继续，不能从首批重扫。
    screening_progress: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    last_active_advertiser_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class AdSyncAccount(SQLModel, table=True):
    """账户同步归属：一个账户只归一个 BC 计划拉取，避免多 BC 重复调用。"""

    __tablename__ = "ad_sync_account"
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    owner_bc_id: str = Field(max_length=128)
    history_planned_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class ReportBalanceRun(SQLModel, table=True):
    """余额冻结范围和逐页进度只存数据库，队列载荷保持有界。"""

    __tablename__ = "report_balance_run"
    __table_args__ = (
        Index(
            "ix_balance_summary_archive",
            "tenant_id",
            "completed_at",
            "id",
            postgresql_where=text("status IN ('COMPLETE','FAILED','CANCELLED')"),
        ),
        Index("uq_report_balance_run_key", "tenant_id", "run_key", unique=True),
        CheckConstraint("provider_failures >= 0", name="ck_balance_provider_failures"),
        Index("ix_balance_active_lease", "status", "claimed_until"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    actor_id: UUID
    run_key: str = Field(max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    remaining_advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    next_page: int = 1
    claim_generation: int = Field(default=0, sa_type=BigInteger)
    owner_epoch: int = Field(default=1, sa_type=BigInteger)
    contract_version: int = 2
    # 换代重新派发仍保留平台失败次数，避免消息重投把重试预算重置。
    provider_failures: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(default=None, sa_type=AwareDateTime)
    error_code: str | None = Field(default=None, max_length=128)
    next_attempt_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    status: str = Field(default="ACTIVE", max_length=16)
    completed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class ReportSyncHealth(SQLModel, table=True):
    """BC 同步健康观测与恢复动作；页面和告警以真实数据年龄、覆盖为准。"""

    __tablename__ = "report_sync_health"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    status: str = Field(default="DEGRADED", max_length=16)
    metrics: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    issues: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    last_repair_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class ReportSyncHealthEvent(SQLModel, table=True):
    """健康状态切换的持久告警/恢复记录，不因进程重启丢失。"""

    __tablename__ = "report_sync_health_event"
    __table_args__ = (
        Index("ix_report_sync_health_event_scope", "tenant_id", "bc_id", "created_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    status: str = Field(max_length=16)
    issues: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
