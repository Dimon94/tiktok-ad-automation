"""昨日水位已经完成时不再读取全部收尾任务历史。"""

from datetime import timedelta

from sqlalchemy import event

from app.modules.reporting import planner
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.reporting.test_day_closeout import NOW, plan_for, seed_yesterday


def test_completed_yesterday_does_not_read_closeout_history(session, sync_seed):
    plan = plan_for(session, sync_seed)
    day, midnight = seed_yesterday(session, sync_seed)
    for kind in ("campaign_day", "adgroup_day", "ad_day", "creative_day"):
        row = session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", kind, day))
        if row is None:
            row = ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                report_kind=kind,
                stat_date=day,
                status="COMPLETE",
                row_count=0,
            )
            session.add(row)
        row.last_success_at = midnight + timedelta(seconds=1)
    session.flush()
    history = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if "FROM report_sync_job" in sql:
            history.append(sql)

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        assert planner.plan_day_closeout(session, plan, now=NOW) == 0
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    assert not history
