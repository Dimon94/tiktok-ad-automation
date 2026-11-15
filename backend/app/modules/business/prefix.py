"""经营准备阶段的小元数据与原子分页；大冻结JSON只在数据库内比较。"""

from datetime import date, datetime, time, timedelta
from typing import Any, cast
from typing import cast as typing_cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Table, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import QueryableAttribute, load_only
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.modules.business.models import (
    BusinessDay,
    BusinessPublication,
    BusinessRun,
    BusinessSummaryDay,
)
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget

HEAVY_PHASES = frozenset({"SNAPSHOT", "VERIFY"})
PREPARATION_PHASES = HEAVY_PHASES | {
    "PREFIX_CHECK",
    "COPY_DAY",
    "COPY_SUMMARY",
    "REBASE",
}
KNOWN_PHASES = PREPARATION_PHASES | {"BASE", "SUBSIDY"}
SNAPSHOT_LOCK = 7348061992109


def rebase_dirty_start(run: BusinessRun, snapshot: dict[str, Any]) -> date | None:
    """只复用逐日冻结状态完全相同的前缀；None表示结构/旧证据不可复用。"""
    old = run.inputs
    ignored = {"states", "accounts", "dirty_start", "after"}
    if (
        not isinstance(old, dict)
        or {k: v for k, v in old.items() if k not in ignored}
        != {k: v for k, v in snapshot.items() if k not in ignored}
        or not all(
            isinstance(s.get(k), dict)
            for s in (old, snapshot)
            for k in ("accounts", "states")
        )
        or old["accounts"].keys() != snapshot["accounts"].keys()
    ):
        return None
    dirty = run.end_date + timedelta(days=1)
    try:
        for advertiser, meta in old["accounts"].items():
            current = snapshot["accounts"][advertiser]
            if not isinstance(meta, dict) or not isinstance(current, dict):
                return None
            if {k: v for k, v in meta.items() if k != "complete_through"} != {
                k: v for k, v in current.items() if k != "complete_through"
            }:
                return None
            before = date.fromisoformat(meta["complete_through"])
            after = date.fromisoformat(current["complete_through"])
            if before != after:
                # cutoff变化只有在账户/完整观测资格成立时改变计算结果。
                day = max(
                    run.quarter,
                    min(before, after) + timedelta(days=1),
                    date.fromisoformat(meta["start"]),
                )
                stop = min(
                    run.end_date,
                    max(before, after),
                    date.fromisoformat(meta["end"]) if meta["end"] else run.end_date,
                )
                while day <= stop:
                    key = f"{advertiser}/{day}"
                    state = snapshot["states"].get(key)
                    if (
                        state
                        and state["status"] == "COMPLETE"
                        and datetime.fromisoformat(state["observed"])
                        >= datetime.combine(
                            day + timedelta(days=1),
                            time.min,
                            ZoneInfo(meta["timezone"]),
                        )
                    ):
                        dirty = min(dirty, day)
                    day += timedelta(days=1)
        # 不把185k状态额外复制成集合/JSON；旧、新各遍历一次，任何版本字段都比较。
        for states, other in (
            (old["states"], snapshot["states"]),
            (snapshot["states"], old["states"]),
        ):
            for key, state in states.items():
                advertiser, _, day_text = key.rpartition("/")
                day = date.fromisoformat(day_text)
                if (
                    advertiser not in snapshot["accounts"]
                    or not run.quarter <= day <= run.end_date
                    or not isinstance(state, dict)
                    or not {"status", "version", "count", "updated", "observed"}
                    <= state.keys()
                    or state["status"] not in {"COMPLETE", "STALE", "FAILED"}
                    or type(state["version"]) is not int
                    or type(state["count"]) is not int
                    or datetime.fromisoformat(state["updated"]).tzinfo is None
                    or datetime.fromisoformat(state["observed"]).tzinfo is None
                ):
                    return None
                if state != other.get(key):
                    dirty = min(dirty, day)
    except KeyError, TypeError, ValueError, AttributeError:
        return None
    return dirty


def begin_rebase(
    run: BusinessRun, snapshot: dict[str, Any], signature: str, dirty: date
) -> None:
    """新真实快照与待清理范围同事务落盘；正式版本仍指向旧完整结果。"""
    previous = run.coverage.get("_rebase", {})
    run.inputs = {**snapshot, "dirty_start": dirty.isoformat(), "after": None}
    run.source_signature = signature
    run.phase, run.base_after = "REBASE", None
    run.coverage = {
        "subsidy_unknown": {
            k: v
            for k, v in run.coverage.get("subsidy_unknown", {}).items()
            if date.fromisoformat(v["from"]) < dirty
        },
        "_rebase": {
            "dirty": dirty.isoformat(),
            "stage": "day",
            "rounds": int(previous.get("rounds", 0)) + 1,
            "day_deleted": int(previous.get("day_deleted", 0)),
            "summary_deleted": int(previous.get("summary_deleted", 0)),
        },
    }


def advance_rebase(session: Session, run: BusinessRun, *, batch: int = 500) -> None:
    progress = dict(run.coverage["_rebase"])
    model = BusinessDay if progress["stage"] == "day" else BusinessSummaryDay
    table = cast(Table, inspect(model).local_table)
    keys = [c.name for c in table.primary_key.columns if c.name != "run_id"]
    # 每次删除最前一页，已提交的删除本身就是keyset进度；重放不会跳过剩余行。
    deleted = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            text(f"""
        WITH page AS MATERIALIZED (
          SELECT {",".join(keys)} FROM {table.name}
          WHERE run_id=:run AND tenant_id=:tenant AND bc_id=:bc AND stat_date>=:dirty
          ORDER BY {",".join(keys)} LIMIT :batch
        ), removed AS (
          DELETE FROM {table.name} target USING page
          WHERE target.run_id=:run AND target.tenant_id=:tenant AND target.bc_id=:bc
            AND {" AND ".join("target." + k + "=page." + k for k in keys)} RETURNING 1
        ) SELECT count(*) FROM removed
        """),
            {
                "run": run.id,
                "tenant": run.tenant_id,
                "bc": run.bc_id,
                "dirty": date.fromisoformat(progress["dirty"]),
                "batch": min(batch, 500),
            },
        )
        .scalar_one()
    )
    counter = "day_deleted" if model is BusinessDay else "summary_deleted"
    progress[counter] += deleted
    if deleted < min(batch, 500):
        if model is BusinessDay:
            progress["stage"] = "summary"
        else:
            progress["stage"] = "recompute"
            run.phase, run.cursor = "BASE", date.fromisoformat(progress["dirty"])
    run.coverage = {**run.coverage, "_rebase": progress}


def narrow_runs() -> SelectOfScalar[BusinessRun]:
    """禁止准备阶段意外lazy-load整个季度inputs；coverage仅存小游标。"""
    attributes = (
        getattr(BusinessRun, column.name)
        for column in inspect(BusinessRun).local_table.columns
        if column.name != "inputs"
    )
    return select(BusinessRun).options(
        load_only(
            *(cast(QueryableAttribute[Any], attr) for attr in attributes),
            raiseload=True,
        )
    )


def frozen_metadata(session: Session, run_id: UUID) -> dict[str, Any]:
    row = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            text("""SELECT inputs->'calculation_revision' calculation_revision,
                       inputs->'identity_catalog' identity_catalog,
                       inputs->'rate_schedules' rate_schedules
                FROM business_run WHERE id=:run"""),
            {"run": run_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


def frozen_accounts(session: Session, run_id: UUID) -> set[str]:
    return set(
        typing_cast(SQLAlchemySession, session)
        .execute(
            text(
                "SELECT jsonb_object_keys(inputs->'accounts') FROM business_run WHERE id=:run"
            ),
            {"run": run_id},
        )
        .scalars()
    )


def begin_prefix(session: Session, run: BusinessRun) -> None:
    publication = session.get(
        BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)
    )
    if publication is None or publication.run_id == run.id:
        run.phase, run.cursor = "BASE", run.quarter
        run.coverage = {}
        return
    run.phase = "PREFIX_CHECK"
    run.coverage = {"_prefix": {"source_id": str(publication.run_id)}}


def _source(session: Session, run: BusinessRun) -> BusinessRun | None:
    progress = run.coverage["_prefix"]
    source = session.exec(
        narrow_runs().where(
            col(BusinessRun.id) == UUID(progress["source_id"]),
            col(BusinessRun.tenant_id) == run.tenant_id,
            col(BusinessRun.bc_id) == run.bc_id,
            col(BusinessRun.quarter) == run.quarter,
            col(BusinessRun.status) == "COMPLETE",
        )
    ).first()
    current = session.get(
        BusinessPublication,
        (run.tenant_id, run.bc_id, run.quarter),
        populate_existing=True,
    )
    # 当前正式来源由清理器保护；指针改变即停止，不能读取可能正被删除的旧版本。
    if source is None or current is None or current.run_id != source.id:
        run.status = "SUPERSEDED"
        return None
    return source


def _dirty_start(
    session: Session, run: BusinessRun, old: BusinessRun, *, day: date | None = None
) -> date:
    params: dict[str, Any] = {
        "old": old.id,
        "new": run.id,
        "start": day or run.quarter,
        "stop": day or min(old.end_date, run.end_date),
    }
    compatible = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            text("""
        WITH meta AS MATERIALIZED (
          SELECT id,inputs->'calculation_revision' calculation,
                 inputs->'identity_catalog' catalog, inputs->'rate_schedules' rates,
                 inputs->'operation_logs'->'signature' logs, inputs->'manual_evidence' manual,
                 (SELECT jsonb_object_agg(key,value-'complete_through')
                  FROM jsonb_each(inputs->'accounts')) accounts
          FROM business_run WHERE id IN (:old,:new)
        ) SELECT a.calculation=b.calculation AND a.catalog=b.catalog
                 AND a.accounts=b.accounts AND a.rates IS NOT DISTINCT FROM b.rates
                 AND a.logs IS NOT DISTINCT FROM b.logs AND a.manual IS NOT DISTINCT FROM b.manual
          FROM meta a,meta b WHERE a.id=:old AND b.id=:new
        """),
            params,
        )
        .scalar_one()
    )
    if old.scope_revision != run.scope_revision or not compatible:
        return run.quarter
    # 两份JSON各展开一次，返回一个日期；不把大inputs传到Python，也不按账号
    # 重复解压整TOAST。raw state相同但complete_through跨日仍须比较覆盖语义。
    dirty = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            text("""
        WITH frozen AS MATERIALIZED (
          SELECT id,inputs->'states' states,inputs->'accounts' accounts
          FROM business_run WHERE id IN (:old,:new)
        ), old_states AS MATERIALIZED (
          SELECT key,value,left(key,length(key)-11) advertiser,
                 right(key,10)::date AS stat_day
          FROM frozen,LATERAL jsonb_each(states)
          WHERE id=:old AND right(key,10)::date BETWEEN :start AND :stop
        ), new_states AS MATERIALIZED (
          SELECT key,value,right(key,10)::date AS stat_day
          FROM frozen,LATERAL jsonb_each(states)
          WHERE id=:new AND right(key,10)::date BETWEEN :start AND :stop
        ), old_accounts AS MATERIALIZED (
          SELECT key,value FROM frozen,LATERAL jsonb_each(accounts) WHERE id=:old
        ), new_accounts AS MATERIALIZED (
          SELECT key,value FROM frozen,LATERAL jsonb_each(accounts) WHERE id=:new
        ), differences AS (
          SELECT coalesce(a.stat_day,b.stat_day) AS stat_day FROM old_states a
          FULL JOIN new_states b USING(key) WHERE a.value IS DISTINCT FROM b.value
          UNION ALL
          SELECT s.stat_day FROM old_states s JOIN old_accounts a ON a.key=s.advertiser
          JOIN new_accounts b ON b.key=s.advertiser
          WHERE s.value->>'status'='COMPLETE'
            AND s.stat_day >= (a.value->>'start')::date
            AND (a.value->>'end' IS NULL OR s.stat_day <= (a.value->>'end')::date)
            AND (s.value->>'observed')::timestamptz >=
                ((s.stat_day+1)::timestamp AT TIME ZONE (a.value->>'timezone'))
            AND ((s.stat_day <= (a.value->>'complete_through')::date)
                 IS DISTINCT FROM (s.stat_day <= (b.value->>'complete_through')::date))
        ) SELECT min(stat_day) FROM differences
        """),
            params,
        )
        .scalar_one()
    )
    return (
        cast(date, dirty)
        if dirty is not None
        else (day or min(old.end_date, run.end_date)) + timedelta(days=1)
    )


def advance_prefix(session: Session, run: BusinessRun, *, batch: int = 500) -> None:
    old = _source(session, run)
    if old is None:
        return
    progress = dict(run.coverage["_prefix"])
    if run.phase == "PREFIX_CHECK":
        if progress.get("check_day"):
            day = date.fromisoformat(progress["check_day"])
            dirty = _dirty_start(session, run, old, day=day)
            if dirty > day and day < min(old.end_date, run.end_date):
                progress["check_day"] = (day + timedelta(days=1)).isoformat()
                run.coverage = {"_prefix": progress}
                return
        else:
            try:
                # 快速整段比较失败后保留日游标，不能在同一大聚合上永久重试。
                # SAVEPOINT回滚SQL取消，外层仍能在原3秒总预算内提交降粒度进度。
                with session.begin_nested(), query_budget(session, seconds=1.5):
                    dirty = _dirty_start(session, run, old)
            except (QueryBudgetExceeded, DBAPIError) as error:
                if (
                    isinstance(error, DBAPIError)
                    and getattr(error.orig, "sqlstate", None) != "57014"
                ):
                    raise
                progress["check_day"] = run.quarter.isoformat()
                run.coverage = {"_prefix": progress}
                return
        progress.update(dirty=dirty.isoformat(), after=None)
        run.coverage = {"_prefix": progress}
        run.phase = "COPY_DAY"
        return
    dirty = date.fromisoformat(progress["dirty"])
    model = BusinessDay if run.phase == "COPY_DAY" else BusinessSummaryDay
    table = cast(Table, inspect(model).local_table)
    keys = [
        column.name for column in table.primary_key.columns if column.name != "run_id"
    ]
    columns = [column.name for column in table.columns]
    order = ",".join(keys)
    params: dict[str, Any] = {
        "old": old.id,
        "new": run.id,
        "tenant": run.tenant_id,
        "bc": run.bc_id,
        "start": run.quarter,
        "dirty": dirty,
        "end": run.end_date,
        "batch": batch,
    }
    after = ""
    if progress.get("after"):
        values = progress["after"]
        params.update(
            {
                f"key{i}": date.fromisoformat(v) if i == 0 else v
                for i, v in enumerate(values)
            }
        )
        after = f"AND ({order}) > ({','.join(f':key{i}' for i in range(len(keys)))})"
    # 表/列仅来自固定模型；先选有界页，复制与PK游标在调用方同一事务提交。
    statement = f"""
      WITH page AS MATERIALIZED (
        SELECT {",".join(columns)} FROM {table.name}
        WHERE run_id=:old AND tenant_id=:tenant AND bc_id=:bc
          AND stat_date>=:start AND stat_date<:dirty AND stat_date<=:end {after}
        ORDER BY {order} LIMIT :batch
      ), copied AS (
        INSERT INTO {table.name} ({",".join(columns)})
        SELECT {",".join(":new" if c == "run_id" else c for c in columns)} FROM page
        RETURNING {order}
      ) SELECT {order} FROM copied ORDER BY {order}
    """
    rows = (
        typing_cast(SQLAlchemySession, session).execute(text(statement), params).all()
    )
    if rows:
        progress["after"] = [str(value) for value in rows[-1]]
        key = "day_rows" if model is BusinessDay else "summary_rows"
        progress[key] = int(progress.get(key, 0)) + len(rows)
    if len(rows) < batch:
        if run.phase == "COPY_DAY":
            run.phase = "COPY_SUMMARY"
            progress["after"] = None
        else:
            # 只在准备结束改一次大JSON；COPY每批仅写小coverage和游标。
            typing_cast(SQLAlchemySession, session).execute(
                text("""UPDATE business_run SET inputs=jsonb_set(inputs,'{dirty_start}',
                        to_jsonb(CAST(:dirty AS text))) WHERE id=:run"""),
                {"run": run.id, "dirty": dirty.isoformat()},
            )
            run.phase, run.cursor, run.base_after = "BASE", dirty, None
            run.coverage = {
                "subsidy_unknown": {
                    key: value
                    for key, value in old.coverage.get("subsidy_unknown", {}).items()
                    if date.fromisoformat(value["from"]) < dirty
                }
            }
            session.expire(run, ["inputs"])
            return
    run.coverage = {"_prefix": progress}


def repair_windows(
    session: Session, run: BusinessRun, owned: set[str], cursor: str | None
) -> tuple[list[tuple[str, date, date]], str | None]:
    """长候选仍按检查周期补源，最多128账号；大JSON只在PG内展开一次。"""
    if not owned:
        return [], None
    after = cursor.rsplit("/", 2)[0] if cursor else ""
    params = {
        "run": run.id,
        "tenant": run.tenant_id,
        "bc": run.bc_id,
        "owned": sorted(owned),
        "after": after,
        "start": run.quarter,
        "end": run.end_date,
    }
    statement = text("""
      WITH frozen AS MATERIALIZED (
        SELECT inputs->'accounts' accounts,inputs->'states' states
        FROM business_run WHERE id=:run AND tenant_id=:tenant AND bc_id=:bc
      ), accounts AS MATERIALIZED (
        SELECT key advertiser,value meta FROM frozen,LATERAL jsonb_each(accounts)
        WHERE key=ANY(CAST(:owned AS text[])) AND key>:after ORDER BY key LIMIT 128
      ), states AS MATERIALIZED (
        SELECT left(key,length(key)-11) advertiser,right(key,10)::date stat_day,value
        FROM frozen,LATERAL jsonb_each(states)
        WHERE left(key,length(key)-11) IN (SELECT advertiser FROM accounts)
      ), missing AS (
        SELECT a.advertiser,min(d::date) FILTER (WHERE
          (s.value->>'status'='COMPLETE' AND
           (s.value->>'observed')::timestamptz >=
             ((d::date+1)::timestamp AT TIME ZONE (a.meta->>'timezone'))) IS NOT TRUE
        ) first_day,
        min(least(:end,(a.meta->>'complete_through')::date,
                  coalesce((a.meta->>'end')::date,:end))) window_end
        FROM accounts a LEFT JOIN LATERAL generate_series(
          greatest(:start,(a.meta->>'start')::date),
          least(:end,(a.meta->>'complete_through')::date,
                coalesce((a.meta->>'end')::date,:end)),interval '1 day') d ON true
        LEFT JOIN states s ON s.advertiser=a.advertiser AND s.stat_day=d::date
        GROUP BY a.advertiser
      ) SELECT advertiser,first_day,least(first_day+29,window_end) last_day
        FROM missing ORDER BY advertiser
    """)
    rows = typing_cast(SQLAlchemySession, session).execute(statement, params).all()
    if not rows and after:
        params["after"] = ""
        rows = typing_cast(SQLAlchemySession, session).execute(statement, params).all()
    windows = [
        (advertiser, start, end) for advertiser, start, end in rows if start is not None
    ]
    return windows, rows[-1][0] if rows else None
