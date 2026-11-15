"""规则判断与健康巡检共用账户当地日的新鲜度，避免切日触发额外补拉。"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.modules.reporting.levels import DAY_KINDS
from app.modules.reporting.planner import HOT_INTERVAL

# 报表仍以一个热轮次加五分钟执行余量为 SLO，独立于目录的规划提前量。
FRESHNESS = {
    **{kind: HOT_INTERVAL + timedelta(minutes=5) for kind in DAY_KINDS},
    "creative_day": HOT_INTERVAL + timedelta(minutes=5),
}


def fresh_report_success(
    successes: Mapping[date, datetime | None],
    *,
    timezone: str,
    kind: str,
    now: datetime,
) -> datetime | None:
    """返回仍在 SLO 内的成功时间；当地零点后首个 SLO 可借用昨天事实。"""
    local_now = now.astimezone(ZoneInfo(timezone))
    days = [local_now.date()]
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if local_now - midnight < FRESHNESS[kind]:
        days.append(local_now.date() - timedelta(days=1))
    # 只相信已发布成功的时间戳；重试/切日不会把昨天的新鲜事实瞬间变旧。
    latest = max(
        (at for day in days if (at := successes.get(day)) is not None), default=None
    )
    return latest if latest is not None and latest >= now - FRESHNESS[kind] else None
