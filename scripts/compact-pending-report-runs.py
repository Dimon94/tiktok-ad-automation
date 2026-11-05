#!/usr/bin/env python3
"""取消由较新待执行补链完整承接的旧执行；保留新任务续步，默认预演。"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import settings
from app.core.db import engine
from app.modules.sync_control.pending_report_compaction import (
    discover_pending_report_pairs,
    supersede_pending_report,
)
from redis import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-schema", default="0099_transition_replan")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--pairs-json")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--budget-seconds", type=float, default=20)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.expected_revision != os.environ.get("TK_ADA_BUILD_REVISION"):
        parser.error("Running application revision differs from authorized revision")
    if args.discover == bool(args.pairs_json) or (args.discover and args.apply):
        parser.error("Choose read-only discovery or explicit candidate pairs")
    if not 0 < args.budget_seconds <= 30:
        parser.error("budget must be 0..30 seconds")
    with Session(engine) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL statement_timeout='3s'"))
        heads = (
            session.execute(text("SELECT version_num FROM alembic_version"))
            .scalars()
            .all()
        )
        if heads != [args.expected_schema]:
            raise RuntimeError("Schema differs from authorized schema")
        if args.discover:
            pairs, truncated = discover_pending_report_pairs(session, limit=args.limit)
            print(
                json.dumps(
                    {
                        "mode": "discover",
                        "revision": args.expected_revision,
                        "truncated": truncated,
                        "pairs": [
                            {"old_id": str(a), "replacement_id": str(b)}
                            for a, b in pairs
                        ],
                    }
                )
            )
            return
    values = json.loads(args.pairs_json)
    if not isinstance(values, list) or len(values) > 128:
        parser.error("Explicit batch must contain at most128 pairs")
    pairs = [(UUID(v["old_id"]), UUID(v["replacement_id"])) for v in values]
    result = {
        "mode": "apply" if args.apply else "preview",
        "examined": 0,
        "cancelled": 0,
        "eligible": [],
        "skipped": [],
        "deferred": [],
        "next_index": 0,
    }
    with Redis.from_url(
        settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1
    ) as redis:
        lock = redis.lock("reporting:cleanup:lock:v1", timeout=120, blocking=False)
        if not lock.acquire(blocking=False):
            print(json.dumps(result | {"maintenance_busy": True}))
            return
        try:
            deadline = monotonic() + args.budget_seconds
            for index, (old, newer) in enumerate(pairs):
                if monotonic() >= deadline:
                    break
                entry = {"old_id": str(old), "replacement_id": str(newer)}
                try:
                    with Session(engine) as session:
                        replacement = supersede_pending_report(
                            session,
                            job_id=old,
                            replacement_id=newer,
                            now=datetime.now(UTC),
                        )
                        if args.apply:
                            session.commit()
                        else:
                            session.rollback()
                    if replacement is None:
                        result["skipped"].append(entry)
                    else:
                        result["eligible"].append(entry)
                        result["cancelled"] += int(args.apply)
                    result["examined"] += 1
                except DBAPIError as error:
                    result["deferred"].append(
                        entry | {"sqlstate": getattr(error.orig, "sqlstate", None)}
                    )
                result["next_index"] = index + 1
        finally:
            lock.release()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
