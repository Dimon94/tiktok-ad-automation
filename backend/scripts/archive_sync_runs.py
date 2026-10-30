# ruff: noqa: T201
"""按明确租户执行一批90天终态归档；默认预览，--apply才上传并验证后回收。"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session

from app.core.db import engine
from app.modules.sync_control.archive import (
    archive_run,
    resolve_archive,
    retention_candidates,
)
from app.modules.sync_control.archive_storage import S3RunArchive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument(
        "--kind", choices=("report", "directory", "balance"), required=True
    )
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--after-time", type=datetime.fromisoformat)
    parser.add_argument("--after-id", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--resolve-run-id", type=UUID, help="只读定位正式归档，忽略未提交候选blob"
    )
    parser.add_argument("--archive-cursor", help="归档marker分页游标")
    args = parser.parse_args()
    if bool(args.after_time) != bool(args.after_id):
        parser.error("--after-time and --after-id must be supplied together")
    if args.resolve_run_id:
        if args.apply:
            parser.error("--resolve-run-id cannot be combined with --apply")
        with Session(engine) as session:
            receipts, next_cursor = resolve_archive(
                session,
                S3RunArchive.configured(),
                tenant_id=args.tenant_id,
                kind=args.kind,
                run_id=args.resolve_run_id,
                cursor=args.archive_cursor,
                limit=args.limit,
            )
        print(
            json.dumps(
                {"archives": [asdict(r) for r in receipts], "next_cursor": next_cursor}
            )
        )
        return
    now = datetime.now(UTC)
    cursor = (args.after_time, args.after_id) if args.after_time else None
    with Session(engine) as session:
        candidates = retention_candidates(
            session,
            tenant_id=args.tenant_id,
            kind=args.kind,
            now=now,
            cursor=cursor,
            limit=args.limit,
        )
    storage = S3RunArchive.configured() if args.apply else None
    for _, run_id in candidates:
        if storage:
            result = archive_run(
                lambda: Session(engine),
                storage,
                tenant_id=args.tenant_id,
                kind=args.kind,
                run_id=run_id,
                now=now,
            )
            print(json.dumps({"run_id": str(run_id), **asdict(result)}))
        else:
            print(json.dumps({"run_id": str(run_id), "action": "preview"}))
    if candidates:
        timestamp, run_id = candidates[-1]
        print(
            json.dumps(
                {
                    "next_cursor": {
                        "after_time": timestamp.isoformat(),
                        "after_id": str(run_id),
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
