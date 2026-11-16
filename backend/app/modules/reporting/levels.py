"""日层级合同唯一注册；父级与广告共用需求、断点与执行 owner。"""

from dataclasses import dataclass
from typing import Literal

from app.modules.reporting.metric_catalog import NATIVE_CONTRACT
from app.modules.reporting.metrics import AD_DAY_METRICS, CAMPAIGN_METRICS


@dataclass(frozen=True)
class ReportLevel:
    level: Literal["campaign", "adgroup", "ad"]
    dimension: str
    data_level: str
    metrics: tuple[str, ...]
    parent: bool
    endpoint: str = "reports.integrated"
    contract: str = NATIVE_CONTRACT
    completeness: str = "ACCOUNT_LOCAL_DAY"
    source: str = "PLATFORM_DIRECT"


LEVELS = {
    "campaign_day": ReportLevel(
        "campaign", "campaign_id", "AUCTION_CAMPAIGN", CAMPAIGN_METRICS, True
    ),
    "adgroup_day": ReportLevel(
        "adgroup",
        "adgroup_id",
        "AUCTION_ADGROUP",
        CAMPAIGN_METRICS[:-1] + ("adgroup_name", "campaign_id", "campaign_name"),
        True,
    ),
    "ad_day": ReportLevel("ad", "ad_id_v2", "AUCTION_AD", AD_DAY_METRICS, False),
}
PARENT_KINDS = tuple(k for k, v in LEVELS.items() if v.parent)
DAY_KINDS = tuple(LEVELS)
BASIC_KINDS = frozenset((*DAY_KINDS, "creative_day", "account_day", "campaign_hour"))


def state_sql(kind: str) -> str:
    if kind not in PARENT_KINDS:
        raise ValueError(kind)
    return f"""SELECT s.tenant_id,s.advertiser_id,'{kind}' AS report_kind,s.stat_date,
    s.status,s.row_count,s.last_success_at,s.observed_at,s.publication_version,s.active_contract,s.publication_digest,s.publication_coordinates,s.error_code,s.updated_at
    FROM report_sync_state s WHERE
    (s.report_kind='{kind}' AND s.last_success_at IS NOT NULL)
    OR (s.report_kind='ad_day' AND NOT EXISTS (
      SELECT 1 FROM report_sync_state direct WHERE direct.tenant_id=s.tenant_id
      AND direct.advertiser_id=s.advertiser_id AND direct.stat_date=s.stat_date
      AND direct.report_kind='{kind}' AND direct.last_success_at IS NOT NULL))"""


def query_kind(dimension: str) -> str:
    if dimension in {"account", "drama"}:
        return "campaign_day"
    return next((k for k, v in LEVELS.items() if v.level == dimension), "ad_day")


def selected_states(session, *, tenant_id, advertiser_id, dimension, start, end):
    """复盘与页面使用同一父级选择 SQL；返回独立值，避免覆盖 ORM 原始 ad_day 身份。"""
    from sqlalchemy import text

    from app.modules.reporting.store_models import ReportSyncState

    kind = query_kind(dimension)
    source = (
        state_sql(kind)
        if kind in PARENT_KINDS
        else "SELECT * FROM report_sync_state WHERE report_kind='ad_day'"
    )
    rows = session.execute(
        text(
            f"SELECT * FROM ({source}) selected WHERE tenant_id=:tenant AND advertiser_id=:adv AND stat_date BETWEEN :start AND :end ORDER BY stat_date"
        ),
        {"tenant": tenant_id, "adv": advertiser_id, "start": start, "end": end},
    ).mappings()
    return [ReportSyncState.model_validate(dict(row)) for row in rows]
