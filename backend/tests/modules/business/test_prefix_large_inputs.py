"""用真实账户日和约33MB冻结JSON验证预算与内存，不访问外部平台。"""

import os
import resource
import subprocess
import sys
from datetime import UTC, date, datetime
from time import monotonic

from sqlalchemy import inspect, text

from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.business.models import BusinessRun
from app.modules.business.prefix import _dirty_start
from app.modules.business.projection import advance_run, request_rebuild
from app.modules.reporting.query_budget import query_budget


def test_large_snapshot_and_database_diff_remain_bounded(request):
    # ru_maxrss 是整个进程的历史峰值；独立 pytest 子进程保留绝对内存上限，
    # 防止之前的万组合容量用例污染本次测量。父进程不领取数据库事务夹具。
    marker = "TKADA_PREFIX_PROFILE_CHILD"
    if os.environ.get(marker) != request.node.nodeid:
        env: dict[str, str] = {**os.environ, marker: request.node.nodeid}
        env.pop("CI_RESULTS_FILE", None)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-s", request.node.nodeid],
            cwd=request.config.rootpath,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
        for line in completed.stdout.splitlines():
            if line.startswith(
                ("SNAPSHOT_BENCH ", "PREFIX_CHECK_BENCH ", "REPAIR_WINDOW_BENCH ")
            ):
                print(line)  # noqa: T201 - 只转发数字性能测量
        assert completed.returncode == 0, completed.stdout
        return
    session = request.getfixturevalue("session")
    c = request.getfixturevalue("business_case")
    params = {
        "tenant": c.context.tenant_id,
        "original": c.advertiser_id,
        "bc": c.bc_id,
        "actor": c.context.actor_id,
    }
    # SQL复制本地fixture授权，避免构造近20万ORM对象污染被测RSS。
    for model in (AdvertiserAccount, BCAccountAccess):
        table = inspect(model).local_table
        columns = [column.name for column in table.columns]
        values = [
            "'10000000000000'||lpad(g::text,5,'0')"
            if col == "advertiser_id"
            else "a." + col
            for col in columns
        ]
        session.execute(
            text(f"""INSERT INTO {table.name} ({",".join(columns)})
            SELECT {",".join(values)} FROM {table.name} a CROSS JOIN generate_series(0,4858) g
            WHERE a.tenant_id=:tenant AND a.advertiser_id=:original"""),
            params,
        )
    session.execute(
        text("""INSERT INTO business_member
        (tenant_id,bc_id,advertiser_id,start_date,end_date)
        SELECT :tenant,:bc,'10000000000000'||lpad(g::text,5,'0'),'2026-10-01',NULL
        FROM generate_series(0,4858) g"""),
        params,
    )
    session.execute(
        text("""INSERT INTO report_sync_state
        (tenant_id,advertiser_id,report_kind,stat_date,status,row_count,
         last_success_at,observed_at,publication_version,updated_at)
        SELECT :tenant,'10000000000000'||lpad(g::text,5,'0'),'ad_day',d::date,
               'COMPLETE',g%999,'2026-11-08 12:00:00.123456+00','2026-11-08 12:00:00.123456+00',
               g+1,'2026-11-08 12:00:00.123456+00'
        FROM generate_series(0,4858) g CROSS JOIN
             generate_series('2026-10-01'::date,'2026-11-07'::date,'1 day') d"""),
        params,
    )
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 7),
        now=now,
        defer_snapshot=True,
        coalesce_pending=True,
    )[0]
    started = monotonic()
    with query_budget(session, seconds=18):
        assert not advance_run(session, run.id, now=now)
    elapsed = monotonic() - started
    size = session.execute(
        text("SELECT octet_length(inputs::text) FROM business_run WHERE id=:id"),
        {"id": run.id},
    ).scalar_one()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    print(  # noqa: T201 - 本地性能回归仅输出大小/耗时，不含业务标识
        f"SNAPSHOT_BENCH seconds={elapsed:.3f} json_bytes={size} peak_rss_bytes={peak}"
    )
    assert size >= 33_000_000
    assert elapsed < 18
    assert peak < 800 * 1024 * 1024
    clone = BusinessRun(
        tenant_id=run.tenant_id,
        bc_id=run.bc_id,
        actor_id=run.actor_id,
        quarter=run.quarter,
        end_date=run.end_date,
        scope_revision=run.scope_revision,
        cursor=run.quarter,
    )
    session.add(clone)
    session.flush()
    session.execute(
        text(
            "UPDATE business_run SET inputs=(SELECT inputs FROM business_run WHERE id=:old) WHERE id=:new"
        ),
        {"old": run.id, "new": clone.id},
    )
    started = monotonic()
    with query_budget(session, seconds=3):
        assert _dirty_start(session, clone, run) == date(2026, 11, 8)
    print(f"PREFIX_CHECK_BENCH seconds={monotonic() - started:.3f}")  # noqa: T201
    from app.modules.business.prefix import frozen_accounts, repair_windows

    started = monotonic()
    with query_budget(session, seconds=2):
        owned = frozen_accounts(session, run.id)
        windows, after = repair_windows(session, run, owned, None)
    assert windows == [] and after == sorted(owned)[127]
    print(f"REPAIR_WINDOW_BENCH seconds={monotonic() - started:.3f}")  # noqa: T201
