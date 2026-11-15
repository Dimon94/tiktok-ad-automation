"""导出冻结计算版本，分批写文件；每次生成及下载重验当前权限。"""

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast as typing_cast
from uuid import UUID

from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from fastapi import HTTPException
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.business.configuration import scope_accounts
from app.modules.business.models import BusinessExport
from app.modules.business.queries import resolve_runs
from app.modules.business.schemas import DashboardFilter
from app.modules.materials.storage import make_s3

LOCAL_ROOT = Path(tempfile.gettempdir()) / "tk-ada-business-exports"


def _storage_configured() -> bool:
    return bool(
        settings.S3_BUCKET
        and settings.S3_ACCESS_KEY_ID
        and settings.S3_SECRET_ACCESS_KEY
    )


def _path(key: str) -> Path:
    path = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() not in path.parents:
        raise HTTPException(404, "导出不存在")
    return path


def check_export(
    session: Session, *, context: TenantContext, export_id: UUID
) -> BusinessExport:
    row = session.get(BusinessExport, export_id, populate_existing=True)
    if (
        row is None
        or row.tenant_id != context.tenant_id
        or row.actor_id != context.actor_id
    ):
        raise HTTPException(404, "导出不存在")
    allowed = scope_accounts(session, context, row.bc_id)
    if not set(row.advertiser_ids) <= set(allowed):
        raise HTTPException(403, "导出账户授权已变化")
    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(410, "导出已过期")
    return row


def create_export(
    session: Session, *, context: TenantContext, filters: DashboardFilter
) -> BusinessExport:
    if filters.run_ids is None:
        raise HTTPException(422, "请先读取看板并冻结计算版本")
    _, _, end, runs = resolve_runs(session, context, filters)
    if not runs:
        raise HTTPException(409, "暂无可导出的计算结果")
    filters = filters.model_copy(update={"end_date": end, "offset": 0, "limit": 200})
    # 除展示成员外也冻结补贴依赖成员，撤权后不能继续导出间接泄漏的汇总。
    dependency = sorted({a for run in runs for a in run.inputs["accounts"]})
    row = BusinessExport(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        bc_id=filters.bc_id,
        filters=filters.model_dump(mode="json"),
        run_ids=[str(r.id) for r in runs],
        advertiser_ids=dependency,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(row)
    session.flush()
    check_export(session, context=context, export_id=row.id)
    return row


def process_pending_export(session: Session, *, batch_size: int = 500) -> bool:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.business.export_snapshot import freeze_rows
    from app.modules.business.export_writer import write_batch

    row = session.exec(
        select(BusinessExport)
        .where(col(BusinessExport.status).in_(["QUEUED", "RUNNING"]))
        .order_by(col(BusinessExport.created_at))
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if row is None:
        return False
    context = TenantContext(
        tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
    )
    try:
        with session.begin_nested():
            check_export(session, context=context, export_id=row.id)
            if not row.frozen:
                prior = (
                    typing_cast(SQLAlchemySession, session)
                    .execute(text("SHOW statement_timeout"))
                    .scalar_one()
                )
                typing_cast(SQLAlchemySession, session).execute(
                    text("SELECT set_config('statement_timeout', '20s', true)")
                )
                freeze_rows(session, row, context)
                typing_cast(SQLAlchemySession, session).execute(
                    text("SELECT set_config('statement_timeout', :prior, true)"),
                    {"prior": prior},
                )
            write_batch(session, row, context, limit=batch_size)
    except HTTPException, DomainError, OSError, BotoCoreError, ClientError, DBAPIError:
        row.status, row.error = "FAILED", "导出未完成，请检查授权或缩小日期范围后重试"
    session.add(row)
    session.flush()
    return True


def export_chunks(
    session: Session, *, context: TenantContext, export_id: UUID
) -> Iterator[bytes]:
    row = check_export(session, context=context, export_id=export_id)
    if row.status != "COMPLETE" or not row.object_key:
        raise HTTPException(409, "导出尚未完成")
    if _storage_configured():
        response = make_s3().get_object(Bucket=settings.S3_BUCKET, Key=row.object_key)
        body = response["Body"]

        def chunks() -> Iterator[bytes]:
            try:
                yield from body.iter_chunks(chunk_size=65536)
            finally:
                body.close()

        return chunks()
    path = _path(row.object_key)
    if not path.is_file():
        raise HTTPException(410, "导出文件不可用，请重新生成")

    def local_chunks() -> Iterator[bytes]:
        with path.open("rb") as source:
            while chunk := source.read(65536):
                yield chunk

    return local_chunks()


def export_content(
    session: Session, *, context: TenantContext, export_id: UUID
) -> bytes:
    return b"".join(export_chunks(session, context=context, export_id=export_id))
