"""只清理派生旧版本和到期导出；不删除平台事实、商业配置或审计。"""

from datetime import datetime, timedelta
from typing import Any, cast
from typing import cast as typing_cast

from sqlalchemy import delete, exists, func, or_, text
from sqlalchemy import select as sa_select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import QueryableAttribute, load_only
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.modules.business.exports import _path, _storage_configured
from app.modules.business.models import (
    BusinessExport,
    BusinessExportRow,
    BusinessPublication,
    BusinessRun,
    BusinessScope,
)
from app.modules.materials.storage import make_s3


def cleanup_business(session: Session, *, now: datetime, batch: int = 5000) -> int:
    expired = session.exec(
        select(BusinessExport)
        .where(
            col(BusinessExport.expires_at) <= now,
            or_(
                col(BusinessExport.status) != "EXPIRED",
                exists(
                    sa_select(col(BusinessExportRow.export_id)).where(
                        col(BusinessExportRow.export_id) == col(BusinessExport.id)
                    )
                ),
            ),
        )
        .order_by(col(BusinessExport.expires_at))
        .limit(5)
        .with_for_update(skip_locked=True)
    ).all()
    for export in expired:
        key = (
            export.object_key or f"business-exports/{export.tenant_id}/{export.id}.csv"
        )
        if _storage_configured():
            make_s3().delete_object(Bucket=settings.S3_BUCKET, Key=key)
        else:
            _path(key).unlink(missing_ok=True)
        _path(f"business-exports/{export.tenant_id}/{export.id}.partial").unlink(
            missing_ok=True
        )
        typing_cast(SQLAlchemySession, session).execute(
            text("""DELETE FROM business_export_row WHERE ctid IN (
            SELECT ctid FROM business_export_row WHERE export_id=:export LIMIT :batch
        )"""),
            {"export": export.id, "batch": batch},
        )
        export.status, export.object_key = "EXPIRED", None
        session.add(export)
    candidates = session.exec(
        select(BusinessRun)
        # 清理只需身份、范围与状态；不解码已冻结的大inputs/coverage快照。
        .options(
            load_only(
                *(
                    cast(QueryableAttribute[Any], attribute)
                    for attribute in (
                        col(BusinessRun.id),
                        col(BusinessRun.tenant_id),
                        col(BusinessRun.bc_id),
                        col(BusinessRun.quarter),
                        col(BusinessRun.status),
                    )
                )
            )
        )
        .where(
            col(BusinessRun.status).in_(["COMPLETE", "SUPERSEDED", "FAILED"]),
            func.coalesce(col(BusinessRun.finished_at), col(BusinessRun.created_at))
            < now - timedelta(days=2),
            col(BusinessRun.id).not_in(select(col(BusinessPublication.run_id))),
        )
        .order_by(col(BusinessRun.created_at))
        .limit(10)
        .with_for_update(skip_locked=True)
    ).all()
    for run in candidates:
        protected = session.exec(
            select(col(BusinessExport.id))
            .where(
                col(BusinessExport.expires_at) > now,
                col(BusinessExport.run_ids).contains([str(run.id)]),
            )
            .limit(1)
        ).first()
        if protected:
            continue
        active_manifest = session.exec(
            select(col(BusinessScope.tenant_id))
            .where(
                col(BusinessScope.tenant_id) == run.tenant_id,
                col(BusinessScope.bc_id) == run.bc_id,
                col(BusinessScope.published_runs).contains(
                    {run.quarter.isoformat(): str(run.id)}
                ),
            )
            .limit(1)
        ).first()
        if active_manifest:
            continue
        # 先退役再分批删除；旧快照请求得到明确过期错误，不能读取半删结果。
        run.status = "SUPERSEDED"
        session.add(run)
        session.flush()
        # 当前发布指针永远保留；每轮有界删除，避免大季度一次长事务。
        removed = 0
        for table in ("business_summary_day", "business_day"):
            result = typing_cast(SQLAlchemySession, session).execute(
                text(f"""
                WITH selected AS (
                    SELECT tableoid, ctid FROM {table} WHERE run_id=:run LIMIT :batch
                ) DELETE FROM {table} AS target USING selected
                  WHERE target.tableoid=selected.tableoid AND target.ctid=selected.ctid
            """),
                {"run": run.id, "batch": batch},
            )
            removed += cast(CursorResult[Any], result).rowcount
        remains = (
            typing_cast(SQLAlchemySession, session)
            .execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM business_day WHERE run_id=:run) OR EXISTS(SELECT 1 FROM business_summary_day WHERE run_id=:run)"
                ),
                {"run": run.id},
            )
            .scalar()
        )
        if not remains:
            typing_cast(SQLAlchemySession, session).execute(
                delete(BusinessRun).where(col(BusinessRun.id) == run.id)
            )
        return removed + len(expired)
    return len(expired)
