#!/usr/bin/env python3
"""同步控制面离线迁移：默认只读盘点；应用需显式环境、停消费者声明及 --apply。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--consumers-stopped", action="store_true")
    parser.add_argument(
        "--kind", choices=("report", "directory", "balance", "all"), default="all"
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--after-id", type=UUID)
    parser.add_argument("--max-batches", type=int, default=100)
    parser.add_argument("--background-capacity", type=int)
    parser.add_argument("--background-prefetch-slots", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # 导入settings前检查，不允许意外加载工作目录中的真实.env。
    if not os.environ.get("APP_ENV_FILE"):
        parser.error("APP_ENV_FILE must explicitly select the intended environment")
    if args.apply and not args.consumers_stopped:
        parser.error("--apply requires --consumers-stopped after actual shutdown")
    configuring_pool = (
        args.background_capacity is not None
        or args.background_prefetch_slots is not None
    )
    if configuring_pool and (not args.apply or args.kind != "all"):
        parser.error(
            "pool configuration requires --apply --kind all after all consumers stop"
        )
    if (
        args.background_capacity is not None
        and not 1 <= args.background_capacity <= 1024
    ):
        parser.error("background capacity must be between 1 and 1024")
    if (
        args.background_prefetch_slots is not None
        and not 0 <= args.background_prefetch_slots <= 1024
    ):
        parser.error("background prefetch slots must be between 0 and 1024")
    if not 1 <= args.batch_size <= 1000 or not 1 <= args.max_batches <= 10000:
        parser.error("invalid bounded batch size/count")
    if args.after_id and args.kind == "all":
        parser.error("--after-id requires one --kind")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    import app.models  # noqa: F401
    from app.core.db import engine
    from app.modules.sync_control.migration import (
        adopt_legacy_batch,
        adoption_query,
        configure_background_pool,
        legacy_balance_dispatch_query,
        retire_legacy_balance_dispatches,
    )
    from app.modules.sync_control.models import SyncWorkerPool
    from sqlalchemy import func, text
    from sqlmodel import Session, select

    kinds = ("report", "directory", "balance") if args.kind == "all" else (args.kind,)
    report = {
        "mode": "apply" if args.apply else "inspect",
        "started_at": datetime.now(UTC).isoformat(),
        "kinds": {},
        "mapping_complete": False,
        "legacy_balance_dispatches_retired": 0,
        "legacy_balance_dispatches_remaining": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, default=str) + "\n")
        temporary.replace(args.output)

    try:
        for kind in kinds:
            entry = {
                "batches": [],
                "adopted": 0,
                "deferred": 0,
                "errors": [],
                "cursor": args.after_id,
            }
            report["kinds"][kind] = entry
            if args.apply:
                for _ in range(args.max_batches):
                    with Session(engine) as session, session.begin():
                        session.execute(text("SET LOCAL lock_timeout='200ms'"))
                        session.execute(text("SET LOCAL statement_timeout='10s'"))
                        result = adopt_legacy_batch(
                            session,
                            kind=kind,
                            consumers_stopped=True,
                            after_id=entry["cursor"],
                            limit=args.batch_size,
                        )
                    # 每批提交后才记录游标；中断后从头重跑也不会覆盖已映射任务。
                    entry["batches"].append(asdict(result))
                    entry["adopted"] += result.adopted
                    entry["deferred"] += result.deferred
                    entry["errors"].extend(result.errors)
                    if result.cursor:
                        entry["cursor"] = result.cursor
                    save()
                    if result.scanned < args.batch_size:
                        break
            with Session(engine) as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout='10s'"))
                _, query = adoption_query(kind)
                entry["unmapped_active"] = session.exec(
                    select(func.count()).select_from(query.subquery())
                ).one()
            save()
        if "balance" in kinds:
            if args.apply:
                for _ in range(args.max_batches):
                    with Session(engine) as session, session.begin():
                        session.execute(text("SET LOCAL lock_timeout='200ms'"))
                        session.execute(text("SET LOCAL statement_timeout='10s'"))
                        retired = retire_legacy_balance_dispatches(
                            session, consumers_stopped=True, limit=args.batch_size
                        )
                    report["legacy_balance_dispatches_retired"] += retired
                    save()
                    if retired < args.batch_size:
                        break
            with Session(engine) as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout='10s'"))
                report["legacy_balance_dispatches_remaining"] = session.exec(
                    select(func.count()).select_from(
                        legacy_balance_dispatch_query().subquery()
                    )
                ).one()
        report["mapping_complete"] = not report[
            "legacy_balance_dispatches_remaining"
        ] and all(
            entry["unmapped_active"] == 0 and not entry["errors"]
            for entry in report["kinds"].values()
        )
        if configuring_pool and report["mapping_complete"]:
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout='200ms'"))
                session.execute(text("SET LOCAL statement_timeout='10s'"))
                configure_background_pool(
                    session,
                    consumers_stopped=True,
                    capacity=args.background_capacity,
                    prefetch_slots=args.background_prefetch_slots,
                )
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout='10s'"))
            pool = session.get(SyncWorkerPool, "background")
            report["background_pool"] = {
                "capacity": pool.capacity if pool else 4,
                "prefetch_slots": pool.prefetch_slots if pool else 0,
                "source": "stored" if pool else "default",
                "configuration_applied": bool(
                    configuring_pool and report["mapping_complete"]
                ),
            }
        report["finished_at"] = datetime.now(UTC).isoformat()
        save()
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "mapping_complete": report["mapping_complete"],
                    "mode": report["mode"],
                }
            )
        )
        return 0 if report["mapping_complete"] else 2
    except Exception as exc:  # noqa: BLE001 - CLI只输出异常类型，避免泄露数据库配置
        report["error_code"] = type(exc).__name__
        save()
        print(
            f"cutover stopped: {type(exc).__name__}; inspect the selected database and output",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
