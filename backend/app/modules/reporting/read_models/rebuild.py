"""回填公开入口：有界游标、当前源重读和同事务输入围栏。"""

import json
from dataclasses import dataclass
from datetime import date
from typing import TypedDict
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting.read_models.ddl import (
    DIRECTORY_BUCKET,
    _bump,
    entity_refresh,
    material_refresh,
    naming_refresh,
)
from app.modules.reporting.read_models.dirty_buckets import ReadBucketKey, mark_dirty


class _Checkpoint(TypedDict):
    scope: dict[str, str]
    stage: int
    after: tuple[str, ...] | None


@dataclass(frozen=True)
class RebuildPage:
    processed: int
    after: tuple[str, ...] | None


def backfill_directory_page(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    kind: str,
    after: tuple[str, ...] | None = None,
    limit: int = 500,
) -> RebuildPage:
    if not 1 <= limit <= 2000:
        raise ValueError("invalid rebuild page size")
    source, fields, refresh = {
        "directory": ("ad_object", ("kind", "remote_id"), entity_refresh),
        "naming": ("campaign_name_projection", ("campaign_remote_id",), naming_refresh),
        "usage": (
            "ad_material_reference",
            ("ad_remote_id", "platform_material_id", "material_type"),
            material_refresh,
        ),
    }[kind]
    params = {"tenant": tenant_id, "advertiser": advertiser_id, "limit": limit}
    predicate = ""
    if after is not None:
        if len(after) != len(fields):
            raise ValueError("invalid rebuild cursor")
        params.update({f"key_{i}": value for i, value in enumerate(after)})
        predicate = f" AND ROW({','.join(fields)})>ROW({','.join(':key_' + str(i) for i in range(len(fields)))})"
    rows = (
        session.connection()
        .execute(
            text(f"""
        SELECT DISTINCT {",".join(fields)} FROM {source}
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser {predicate}
        ORDER BY {",".join(fields)} LIMIT :limit
    """),
            params,
        )
        .all()
    )
    if not rows:
        return RebuildPage(0, None)
    records = [dict(zip(fields, row, strict=True)) for row in rows]
    keys = f"SELECT CAST(:tenant AS uuid) AS tenant_id,CAST(:advertiser AS varchar) AS advertiser_id,k.* FROM jsonb_to_recordset(CAST(:keys AS jsonb)) AS k({','.join(f + ' text' for f in fields)})"
    params["keys"] = json.dumps(records)
    session.connection().execute(
        text(
            _bump(
                f"SELECT CAST(:tenant AS uuid) AS tenant_id,CAST(:advertiser AS varchar) AS advertiser_id,'{kind}'::varchar AS kind,{DIRECTORY_BUCKET} AS bucket",
                complete=True,
            )
        ),
        params,
    )
    # 固定模板的DELETE/INSERT分别绑定；psycopg禁止一次prepared执行多条命令。
    for statement in refresh(keys).split(";"):
        if statement.strip():
            session.connection().execute(text(statement), params)
    return RebuildPage(len(rows), tuple(rows[-1]) if len(rows) == limit else None)


def enqueue_fact_buckets(
    session: Session, *, tenant_id: UUID, advertiser_id: str, start: date, end: date
) -> int:
    if not 0 <= (end - start).days <= 366:
        raise ValueError("rebuild date window must be within 367 days")
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT 'ad_day' AS kind,stat_date FROM ad_report_day
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date BETWEEN :start AND :end
        UNION SELECT DISTINCT 'material_day',stat_date FROM material_report_day
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date BETWEEN :start AND :end
        UNION SELECT report_kind,stat_date FROM report_sync_state
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date BETWEEN :start AND :end
            AND report_kind IN ('ad_day','material_day')
    """),
            {
                "tenant": tenant_id,
                "advertiser": advertiser_id,
                "start": start,
                "end": end,
            },
        )
        .all()
    )
    mark_dirty(
        session,
        [ReadBucketKey(tenant_id, advertiser_id, r.kind, r.stat_date) for r in rows],
    )
    return len(rows)


def main() -> None:
    """显式账户/日期范围、每页提交后落检查点；中断重放只重建同一源数据。"""
    import argparse
    import os
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Rebuild one account's query projections with a resumable checkpoint"
    )
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--advertiser", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--page-size", type=int, default=500)
    args = parser.parse_args()
    # 先解析帮助及校验显式环境，禁止运维命令意外读取默认.env。
    if not os.environ.get("APP_ENV_FILE"):
        parser.error("APP_ENV_FILE must explicitly select the intended environment")

    from app.core.db import engine
    from app.modules.reporting.read_models.projector import project_bucket

    scope = {
        "tenant": str(args.tenant),
        "advertiser": args.advertiser,
        "start": str(args.start),
        "end": str(args.end),
    }
    progress: _Checkpoint = {"scope": scope, "stage": 0, "after": None}
    if args.checkpoint.exists():
        progress = json.loads(args.checkpoint.read_text())
        if progress.get("scope") != scope:
            parser.error("checkpoint scope mismatch")

    def save() -> None:
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.checkpoint.with_suffix(args.checkpoint.suffix + ".tmp")
        temporary.write_text(json.dumps(progress, sort_keys=True))
        temporary.replace(args.checkpoint)

    kinds = ("directory", "naming", "usage")
    while progress["stage"] < 3:
        with Session(engine) as session:
            page = backfill_directory_page(
                session,
                tenant_id=args.tenant,
                advertiser_id=args.advertiser,
                kind=kinds[progress["stage"]],
                after=tuple(progress["after"]) if progress["after"] else None,
                limit=args.page_size,
            )
            session.commit()
        progress["after"] = page.after
        if page.after is None:
            progress["stage"] += 1
        save()
    if progress["stage"] == 3:
        with Session(engine) as session:
            enqueue_fact_buckets(
                session,
                tenant_id=args.tenant,
                advertiser_id=args.advertiser,
                start=args.start,
                end=args.end,
            )
            session.commit()
        progress["stage"] = 4
        save()
    while progress["stage"] == 4:
        with Session(engine) as session:
            row = (
                session.connection()
                .execute(
                    text("""
                SELECT kind,bucket,source_revision FROM read_bucket_state
                WHERE tenant_id=:tenant AND advertiser_id=:advertiser
                    AND kind IN ('ad_day','material_day') AND bucket BETWEEN :start AND :end
                    AND source_revision>derived_revision ORDER BY kind,bucket LIMIT 1 FOR UPDATE
            """),
                    {
                        "tenant": args.tenant,
                        "advertiser": args.advertiser,
                        "start": args.start,
                        "end": args.end,
                    },
                )
                .first()
            )
            if row:
                project_bucket(
                    session,
                    ReadBucketKey(args.tenant, args.advertiser, row.kind, row.bucket),
                    expected_version=row.source_revision,
                )
            session.commit()
        if row is None:
            progress["stage"] = 5
        save()
    print("query projection rebuild complete")  # noqa: T201 - CLI completion receipt


if __name__ == "__main__":
    main()
