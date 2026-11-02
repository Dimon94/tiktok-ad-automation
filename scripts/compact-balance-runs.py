#!/usr/bin/env python3
"""将旧余额重复执行交由已存在的新执行承接；默认回滚预演，显式 --apply 才提交。"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import engine
from app.modules.reporting.store_models import ReportBalanceRun
from app.modules.sync_control.balance_compaction import (
    supersede_balance_run,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=256)
    parser.add_argument("--budget-seconds", type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000 or not 0 < args.budget_seconds <= 600:
        parser.error("limit must be 1..1000 and budget 0..600 seconds")
    stop = monotonic() + args.budget_seconds
    with Session(engine) as session:
        ids = session.exec(
            select(col(ReportBalanceRun.id))
            .where(col(ReportBalanceRun.status) == "ACTIVE")
            .order_by(col(ReportBalanceRun.created_at), col(ReportBalanceRun.id))
            .limit(args.limit)
        ).all()
    counts = {
        "examined": 0,
        "superseded" if args.apply else "would_supersede": 0,
        "deferred": 0,
    }
    for identity in ids:
        if monotonic() >= stop:
            break
        try:
            with Session(engine) as session:
                session.execute(text("SET LOCAL statement_timeout='5s'"))
                session.execute(text("SET LOCAL lock_timeout='200ms'"))
                changed = supersede_balance_run(
                    session, run_id=identity, now=datetime.now(UTC)
                )
                session.commit() if args.apply else session.rollback()
                counts["examined"] += 1
                counts["superseded" if args.apply else "would_supersede"] += int(
                    changed
                )
        except DBAPIError:
            counts["deferred"] += 1
        sleep(0.1)  # 分批短事务，给正常worker留出scope/行锁机会。
    print(
        json.dumps(
            {
                "apply": args.apply,
                **counts,
                "remaining_candidates": len(ids)
                - counts["examined"]
                - counts["deferred"],
            }
        )
    )


if __name__ == "__main__":
    main()
