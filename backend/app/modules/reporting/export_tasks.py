"""本地 ads-reporting 队列中的导出 worker；不访问 TikTok 或 MCP。"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.modules.reporting.exports import (
    _delete_object,
    _expected_key,
    _scope_check,
    cleanup_expired_exports,
    generate_export,
)
from app.modules.reporting.query_models import ReportExport
from app.modules.tenants.permissions import require_tenant


def run_export(export_id: UUID) -> None:
    """生成冻结副本；失败时只保留 FAILED 状态且不暴露对象键。"""
    try:
        export_id = UUID(str(export_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("report_export_not_found", "导出不存在") from exc

    with Session(engine) as session:
        export = session.get(ReportExport, export_id, populate_existing=True)
        if export is None:
            raise DomainError("report_export_not_found", "导出不存在")
        try:
            context = require_tenant(
                session,
                actor_id=export.actor_id,
                tenant_id=export.tenant_id,
                action="read",
            )
            _scope_check(session, context=context, bc_id=export.bc_id, export=export)
        except (HTTPException, DomainError) as exc:
            # 撤权是确定性失败；过期保留 EXPIRED 语义，其他授权错误写入
            # FAILED，绝不留下可下载对象。
            detail = getattr(exc, "detail", getattr(exc, "code", ""))
            # A late/replayed task can discover a revoked COMPLETE export.  Only
            # delete a key derived from this exact export scope; malformed or
            # foreign keys remain untouched and are rejected by download.
            if export.object_key == _expected_key(export):
                _delete_object(export.object_key)
                export.object_key = None
            if detail == "report_export_expired":
                export.status = "EXPIRED"
            else:
                export.status = "FAILED"
            session.add(export)
            session.commit()
            return
        if export.status == "COMPLETE":
            return
        export.status = "RUNNING"
        session.add(export)
        session.commit()
        try:
            # 生成阶段再次重建上下文和授权，覆盖队列等待期间的撤权。
            with Session(engine) as verify_session:
                verify_export = verify_session.get(
                    ReportExport, export_id, populate_existing=True
                )
                if verify_export is None:
                    return
                verify_context = require_tenant(
                    verify_session,
                    actor_id=verify_export.actor_id,
                    tenant_id=verify_export.tenant_id,
                    action="read",
                )
                _scope_check(
                    verify_session,
                    context=verify_context,
                    bc_id=verify_export.bc_id,
                    export=verify_export,
                )
                generate_export(verify_session, verify_export)
                verify_session.commit()
        except Exception as exc:
            with Session(engine) as failed_session:
                failed = failed_session.get(
                    ReportExport, export_id, populate_existing=True
                )
                if failed is not None:
                    failed.status = "FAILED"
                    if isinstance(exc, HTTPException):
                        failed.coverage = dict(failed.coverage) | {
                            "export_error": str(exc.detail)
                        }
                    failed.object_key = None
                    failed_session.add(failed)
                    failed_session.commit()


@celery_app.task(name="reporting.export", queue="ads-reporting")
def run_export_task(*, tenant_id: str, actor_id: str, payload: dict) -> None:
    """Outbox contract supplies tenant/actor separately from the payload."""
    payload = validate_dispatch_payload(payload)
    if set(payload) != {"export_id"}:
        raise DomainError("dispatch_payload_invalid", "导出任务参数无效")
    try:
        tenant = UUID(tenant_id)
        actor = UUID(actor_id)
        export_id = UUID(str(payload["export_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "导出任务标识无效") from exc
    with Session(engine) as session:
        export = session.get(ReportExport, export_id, populate_existing=True)
        if export is None or export.tenant_id != tenant or export.actor_id != actor:
            raise DomainError("report_export_not_found", "导出不存在")
    run_export(export_id)


@celery_app.task(name="reporting.cleanup_exports", queue="ads-reporting")
def cleanup_exports_task() -> int:
    with Session(engine) as session:
        count = cleanup_expired_exports(session)
        session.commit()
        return count


__all__ = ["run_export", "run_export_task"]
