"""今日、昨日收尾与历史补数分槽；旧窗口不能长期占用实时容量。"""

from datetime import datetime

from sqlalchemy import text

# 收尾独立实时档，不与今日热报争名额；数值只标识档位，派发先服务今日 lane。
CLOSEOUT_PRIORITY = 0


def _day_predicate(alias: str, *, days_ago: int) -> str:
    # 别名仅由本模块的调用方提供，不接受任何请求输入；日期按各账户本地日判断。
    if alias not in {"j", "report_sync_job"}:
        raise ValueError("invalid report alias")
    return f"""({alias}.start_date={alias}.end_date AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements_text({alias}.advertiser_ids) ids(advertiser_id)
          LEFT JOIN advertiser_account a ON a.tenant_id={alias}.tenant_id
            AND a.advertiser_id=ids.advertiser_id
          WHERE NULLIF(a.timezone, '') IS NULL OR {alias}.end_date <>
            (CAST(:lane_now AS timestamptz) AT TIME ZONE NULLIF(a.timezone, ''))::date - {days_ago}))"""


def current_day_predicate(alias: str = "report_sync_job") -> str:
    return _day_predicate(alias, days_ago=0)


def closeout_predicate(alias: str = "report_sync_job") -> str:
    yesterday = _day_predicate(alias, days_ago=1)
    return f"({alias}.priority={CLOSEOUT_PRIORITY} AND {alias}.kind IN ('ad_day','creative_day','campaign_day') AND {yesterday})"


def live_predicate(alias: str = "report_sync_job") -> str:
    day = current_day_predicate(alias)
    return f"({alias}.priority BETWEEN 1 AND 2 AND {alias}.kind IN ('ad_day','creative_day','campaign_day','campaign_hour') AND {day})"


def lane_predicate(*, lane: str, alias: str = "report_sync_job") -> str:
    predicate = live_predicate(alias)
    if lane == "live":
        return predicate
    if lane == "closeout":
        return closeout_predicate(alias)
    if lane == "backfill":
        return f"NOT ({predicate} OR {closeout_predicate(alias)})"
    raise ValueError("invalid report lane")


def lane_condition(*, lane: str, now: datetime):
    condition = text(lane_predicate(lane=lane))
    return condition.bindparams(lane_now=now)


def report_workload(session, job, *, now):
    """实际执行时按账户当地日分类；午夜后旧热任务不会永久占用实时配额。"""
    if job.priority > 2:
        return "background"
    current = (
        session.connection()
        .execute(
            text(
                f"SELECT ({current_day_predicate()} OR {closeout_predicate()}) FROM report_sync_job WHERE id=:job"
            ),
            {"job": job.id, "lane_now": now},
        )
        .scalar_one()
    )
    return "live" if current else "background"


def has_background_facts(session, now: datetime) -> bool:
    """共享后台池有可运行事实时，目录只占一个候选槽。

    只探测是否存在，不统计积压、不加载 progress；未来退避中的事实不占保底。
    RUNNING 包含尚未到期的在途工作和待恢复的过期租约。
    """
    return bool(
        session.connection()
        .execute(
            text(f"""SELECT EXISTS (
            SELECT 1 FROM report_sync_job j
            WHERE j.status IN ('QUEUED','WAITING','RUNNING')
              AND (j.status='RUNNING' OR (j.due_at<=:lane_now
                AND (j.claimed_until IS NULL OR j.claimed_until<=:lane_now)))
              AND (j.priority>2 OR {lane_predicate(lane="backfill", alias="j")})
        )"""),
            {"lane_now": now},
        )
        .scalar_one()
    )
