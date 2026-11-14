"""冻结报表导出。

导出创建时把快照成员与顺序复制到 ``ReportExport.frozen_rows``，后台生成阶段
直接读取冻结结果（素材仍沿原成员补全合同）。下载和生成都重新检查当前租户、BC 与账户授权；
撤权后即使队列消息迟到也不会写出或下载旧报表。
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.modules.accounts.models import TenantBC
from app.modules.materials.storage import make_s3
from app.modules.reporting.dimension_query import materialize_rows
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.frozen_result import RESULT_CONTRACT, frozen_row
from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotRow,
    ReportExport,
    read_snapshot,
)
from app.modules.reporting.row_set import SnapshotMember
from app.modules.reporting.schemas import ExportPublic, ReportingFilter
from app.modules.reporting.snapshot_members import snapshot_members, snapshot_names
from app.modules.tenants.permissions import require_tenant

_EXPORT_PREFIX = "report-exports"
EXPORT_BATCH = 500
_LOCAL_ROOT = Path(tempfile.gettempdir()) / "tk-ada-report-exports"
_MEMORY_OBJECTS: dict[str, bytes] = {}


def _enqueue_export(
    session: Session, *, context: TenantContext, export_id: UUID
) -> None:
    from app.jobs.outbox import enqueue_after_commit

    enqueue_after_commit(
        session,
        context=context,
        task_name="reporting.export",
        task_key=f"reporting.export:{export_id}",
        payload={"export_id": str(export_id)},
    )


def csv_safe_text(value: str) -> str:
    """保护可能被表格软件解释为公式的文本单元格。"""
    if not isinstance(value, str):
        raise TypeError("csv_safe_text expects text")
    stripped = value.lstrip(" \t\r\n")
    if stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _public(row: ReportExport) -> ExportPublic:
    return ExportPublic(
        id=row.id,
        status=cast(
            Literal["QUEUED", "RUNNING", "COMPLETE", "FAILED", "EXPIRED"], row.status
        ),
        coverage=dict(row.coverage),
        expires_at=row.expires_at,
    )


def _scope_check(
    session: Session, *, context: TenantContext, bc_id: str, export: ReportExport
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if (
        export.tenant_id != context.tenant_id
        or export.bc_id != bc_id
        or export.actor_id != context.actor_id
    ):
        raise HTTPException(404, detail="report_export_not_found")
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise HTTPException(403, detail="report_scope_forbidden")
    allowed = authorized_grants(session, context=context, bc_id=bc_id)
    allowed_ids = {grant.advertiser_id for grant in allowed}
    if not set(export.advertiser_ids) <= allowed_ids:
        raise HTTPException(403, detail="report_scope_forbidden")
    if export.expires_at <= datetime.now(UTC):
        if export.status != "EXPIRED":
            export.status = "EXPIRED"
            session.add(export)
            session.flush()
        raise HTTPException(409, detail="report_export_expired")


def _rows_for_snapshot(
    session: Session, snapshot: QuerySnapshot
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """冻结快照成员与顺序；行内容在后台生成时按批补全，避免大快照阻塞请求。"""
    rows = session.exec(
        select(QuerySnapshotRow)
        .where(QuerySnapshotRow.snapshot_id == snapshot.id)
        .order_by(col(QuerySnapshotRow.stable_sequence), col(QuerySnapshotRow.row_key))
    ).all()
    if len(rows) != snapshot.total:
        raise HTTPException(409, detail="query_snapshot_incomplete")
    if snapshot.filters.get("dimension") != "material":
        return [frozen_row(row).model_dump(mode="json") for row in rows], dict(
            snapshot.naming_versions
        )
    members = snapshot_members(session, snapshot=snapshot, rows=rows)
    frozen_rows = [
        {
            "row_key": row.row_key,
            "refs": list(members[row.row_key].refs),
            "material_uses": list(members[row.row_key].material_uses),
        }
        for row in rows
    ]
    return frozen_rows, snapshot_names(
        session, snapshot=snapshot, rows=rows, members=members
    )


def create_export(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    snapshot_id: UUID,
    idempotency_key: str,
) -> ExportPublic:
    if (
        type(idempotency_key) is not str
        or not idempotency_key.strip()
        or len(idempotency_key) > 128
    ):
        raise HTTPException(422, detail="invalid_export_idempotency_key")
    # Idempotency lookup itself is scoped by all three dimensions. A key from
    # another BC/user can never return an existing export.
    existing = session.exec(
        select(ReportExport).where(
            ReportExport.tenant_id == context.tenant_id,
            ReportExport.bc_id == bc_id,
            ReportExport.actor_id == context.actor_id,
            ReportExport.idempotency_key == idempotency_key.strip(),
        )
    ).first()
    if existing is not None:
        _scope_check(session, context=context, bc_id=bc_id, export=existing)
        if existing.snapshot_id != snapshot_id:
            raise HTTPException(409, detail="export_idempotency_conflict")
        _enqueue_export(session, context=context, export_id=existing.id)
        return _public(existing)

    snapshot = read_snapshot(
        session, context=context, bc_id=bc_id, snapshot_id=snapshot_id
    )
    if snapshot.actor_id != context.actor_id:
        raise HTTPException(404, detail="query_snapshot_not_found")
    # 导出复制完整成员和名称证据，后续生成不再依赖短期成员版本或快照存活。
    frozen_rows, naming_versions = _rows_for_snapshot(session, snapshot)
    now = datetime.now(UTC)
    row = ReportExport(
        id=uuid4(),
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        advertiser_ids=list(snapshot.advertiser_ids),
        filters=dict(snapshot.filters),
        filter_digest=snapshot.filter_digest,
        publication_versions=dict(snapshot.publication_versions),
        naming_versions=naming_versions,
        snapshot_id=snapshot.id,
        idempotency_key=idempotency_key.strip(),
        status="QUEUED",
        frozen_rows=frozen_rows,
        result_contract=dict(snapshot.result_contract),
        publication_map=list(snapshot.publication_map),
        coverage=dict(snapshot.coverage),
        expires_at=now.replace(microsecond=0) + timedelta(hours=24),
    )
    try:
        # Savepoint converts a concurrent unique-key loser into a re-read while
        # preserving the caller's transaction for the outbox enqueue.
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.exec(
            select(ReportExport).where(
                ReportExport.tenant_id == context.tenant_id,
                ReportExport.bc_id == bc_id,
                ReportExport.actor_id == context.actor_id,
                ReportExport.idempotency_key == idempotency_key.strip(),
            )
        ).one_or_none()
        if existing is None:
            raise
        _scope_check(session, context=context, bc_id=bc_id, export=existing)
        if existing.snapshot_id != snapshot_id:
            raise HTTPException(409, detail="export_idempotency_conflict")
        _enqueue_export(session, context=context, export_id=existing.id)
        return _public(existing)
    _enqueue_export(session, context=context, export_id=row.id)
    return _public(row)


def _expected_key(export: ReportExport) -> str:
    return f"{_EXPORT_PREFIX}/tenants/{export.tenant_id}/{export.bc_id}/{export.id}.csv"


def get_export(
    session: Session, *, context: TenantContext, bc_id: str, export_id: UUID
) -> ReportExport:
    row = session.get(ReportExport, export_id, populate_existing=True)
    if row is None:
        raise HTTPException(404, detail="report_export_not_found")
    _scope_check(session, context=context, bc_id=bc_id, export=row)
    return row


def list_exports(
    session: Session, *, context: TenantContext, bc_id: str
) -> tuple[ExportPublic, ...]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    # Scope must be checked even for an empty result, preventing a typoed BC from
    # being treated as a valid private namespace.
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise HTTPException(403, detail="report_scope_forbidden")
    authorized_grants(session, context=context, bc_id=bc_id)
    rows = session.exec(
        select(ReportExport)
        .where(
            ReportExport.tenant_id == context.tenant_id,
            ReportExport.bc_id == bc_id,
            ReportExport.actor_id == context.actor_id,
        )
        .order_by(col(ReportExport.created_at).desc(), col(ReportExport.id).desc())
    ).all()
    result: list[ExportPublic] = []
    for row in rows:
        if row.expires_at <= datetime.now(UTC):
            row.status = "EXPIRED"
        result.append(_public(row))
    session.flush()
    return tuple(result)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _csv_rows(export: ReportExport, rows: Iterable[dict[str, Any]]) -> bytes:
    from app.modules.reporting.metric_catalog import METRIC_CATALOG, metric_name_zh

    # 创意日报与广告日报使用相同收入窗口，包括 D3/D30。
    metric_fields = [item.key for item in METRIC_CATALOG]
    fields = [
        "row_key",
        "name",
        "advertiser_id",
        "currency",
        "timezone",
        "coverage",
        "fetched_at",
        *metric_fields,
    ]
    balance_fields = [
        "balance",
        "balance_currency",
        "balance_scope",
        "balance_updated_at",
    ]
    fields.extend(balance_fields)
    frozen = bool(export.result_contract)
    evidence_fields = [
        "snapshot_id",
        "dimension",
        "filter_digest",
        "filters",
        "result_contract",
        "publication_map",
        "metric_vector",
        "report_display",
    ]
    if frozen:
        fields.extend(evidence_fields)
    captured = export.created_at.astimezone(UTC).isoformat()
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    headers = {key: key for key in fields} | {
        key: metric_name_zh(key, "ad") for key in metric_fields
    }
    if frozen and export.filters.get("dimension") != "material":
        headers.update(
            revenue_total="Native Growth 总展示价值（平台原字段）",
            revenue_d0="Native Growth D0 收入（点击后24小时）",
            roas_total="收益/花费（本地计算）",
            roas_d0="D0收入/花费（本地计算）",
        )
    writer.writerow(headers)
    for raw in rows:
        display = dict(raw.get("display") or {})
        coverage = raw.get("coverage") or export.coverage
        row_coverage = coverage.get("status", "UNKNOWN")
        buckets = raw.get("metric_buckets") or [{}]
        for bucket in buckets:
            bucket = bucket or {}
            values = dict(bucket.get("values") or {})
            currency = bucket.get("currency")
            timezone = bucket.get("timezone")
            data: dict[str, Any] = {
                "row_key": csv_safe_text(str(raw.get("row_key", ""))),
                "name": csv_safe_text(
                    str(display.get("name") or display.get("campaign_name") or "")
                ),
                "advertiser_id": csv_safe_text(
                    str(display.get("advertiser_id") or display.get("remote_id") or "")
                ),
                "currency": csv_safe_text(
                    str(currency or display.get("currency") or "")
                ),
                "timezone": csv_safe_text(
                    str(timezone or display.get("timezone") or "")
                ),
                "coverage": csv_safe_text(str(row_coverage)),
                "fetched_at": coverage.get("updated_at") or captured,
            }
            for key in metric_fields:
                value = _decimal(values.get(key))
                data[key] = value if value is not None else ""
            # 余额是独立时点观测，不混入日期段消耗与回收币种。
            for key in balance_fields:
                value = display.get(key)
                data[key] = csv_safe_text(str(value)) if value is not None else ""
            if frozen:
                # 向量保留NULL/真零、availability、字段/公式、partial计数和逐日水位；不重算。
                metadata = {
                    "snapshot_id": str(export.snapshot_id),
                    "dimension": export.filters.get("dimension"),
                    "filter_digest": export.filter_digest,
                    "filters": export.filters,
                    "result_contract": export.result_contract,
                    "publication_map": export.publication_map,
                    "metric_vector": bucket,
                    "report_display": display,
                }
                for field, cell in metadata.items():
                    data[field] = csv_safe_text(
                        cell
                        if isinstance(cell, str)
                        else json.dumps(
                            cell, ensure_ascii=False, separators=(",", ":")
                        )
                    )
            writer.writerow(data)
    return out.getvalue().encode("utf-8")


def _object_path(key: str) -> Path:
    # Key is generated internally; resolve plus a strict prefix prevents a
    # malformed database value from escaping the local fallback namespace.
    path = (_LOCAL_ROOT / key).resolve()
    root = _LOCAL_ROOT.resolve()
    if root not in path.parents:
        raise ValueError("invalid report export key")
    return path


def _put_object(key: str, content: bytes) -> None:
    """Write atomically to configured object storage, with a test-local fallback."""
    if (
        settings.S3_BUCKET
        and settings.S3_ACCESS_KEY_ID
        and settings.S3_SECRET_ACCESS_KEY
    ):
        client = make_s3()
        client.put_object(
            Bucket=settings.S3_BUCKET,
            Key=key,
            Body=content,
            ContentType="text/csv; charset=utf-8",
        )
        return
    path = _object_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    _MEMORY_OBJECTS[key] = content


def _get_object(key: str) -> bytes:
    if (
        settings.S3_BUCKET
        and settings.S3_ACCESS_KEY_ID
        and settings.S3_SECRET_ACCESS_KEY
    ):
        response = make_s3().get_object(Bucket=settings.S3_BUCKET, Key=key)
        body = response["Body"]
        return body.read()  # type: ignore[no-any-return]
    if key in _MEMORY_OBJECTS:
        return _MEMORY_OBJECTS[key]
    path = _object_path(key)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="report_export_object_not_found") from exc


def _delete_object(key: str) -> None:
    if (
        settings.S3_BUCKET
        and settings.S3_ACCESS_KEY_ID
        and settings.S3_SECRET_ACCESS_KEY
    ):
        make_s3().delete_object(Bucket=settings.S3_BUCKET, Key=key)
        return
    _MEMORY_OBJECTS.pop(key, None)
    _object_path(key).unlink(missing_ok=True)


def _export_rows(session: Session, export: ReportExport) -> Iterator[dict[str, Any]]:
    """普通报表只消费任务自有冻结结果；素材保留原有读取路径。"""
    if export.filters.get("dimension") != "material":
        if export.result_contract != RESULT_CONTRACT:
            raise HTTPException(
                409, detail="report_export_contract_incompatible_reexport"
            )
        yield from export.frozen_rows
        return
    filters = ReportingFilter.model_validate(export.filters)
    members = [
        SnapshotMember(raw["row_key"], tuple(raw["refs"]), tuple(raw["material_uses"]))
        for raw in export.frozen_rows
    ]
    for start in range(0, len(members), EXPORT_BATCH):
        for row in materialize_rows(
            session,
            tenant_id=export.tenant_id,
            filters=filters,
            members=members[start : start + EXPORT_BATCH],
            advertisers=export.advertiser_ids,
        ):
            yield row.model_dump(mode="json")


def generate_export(session: Session, export: ReportExport) -> None:
    """Generate a complete object or leave no externally visible partial file."""
    key = _expected_key(export)
    content = _csv_rows(export, _export_rows(session, export))
    _put_object(key, content)
    export.object_key = key
    export.status = "COMPLETE"
    session.add(export)
    session.flush()


def download_export(
    session: Session, *, context: TenantContext, bc_id: str, export_id: UUID
) -> bytes:
    row = get_export(session, context=context, bc_id=bc_id, export_id=export_id)
    if row.status == "EXPIRED":
        raise HTTPException(409, detail="report_export_expired")
    if row.status != "COMPLETE" or not row.object_key:
        raise HTTPException(409, detail="report_export_not_ready")
    if row.object_key != _expected_key(row):
        raise HTTPException(409, detail="report_export_object_invalid")
    # Recheck scope immediately before object read: a revoke between DB fetch and
    # storage access must never leak a finished export.
    _scope_check(session, context=context, bc_id=bc_id, export=row)
    return _get_object(row.object_key)


def cleanup_expired_exports(
    session: Session, *, now: datetime | None = None, limit: int = 100
) -> int:
    """删除过期导出自己的对象，并清空其键；不会触碰其他前缀。"""
    now = now or datetime.now(UTC)
    rows = session.exec(
        select(ReportExport)
        .where(ReportExport.expires_at <= now)
        .order_by(col(ReportExport.expires_at))
        .limit(limit)
    ).all()
    for row in rows:
        if row.object_key and row.object_key == _expected_key(row):
            _delete_object(row.object_key)
        row.object_key = None
        row.frozen_rows = []
        row.result_contract = {}
        row.publication_map = []
        row.status = "EXPIRED"
        session.add(row)
    session.flush()
    return len(rows)


def run_export(export_id: UUID) -> None:
    """兼容业务合同的 worker 入口；实现位于 queue task 模块。"""
    from app.modules.reporting.export_tasks import run_export as _run_export

    _run_export(export_id)


__all__ = [
    "_csv_rows",
    "create_export",
    "cleanup_expired_exports",
    "csv_safe_text",
    "download_export",
    "generate_export",
    "get_export",
    "list_exports",
    "run_export",
]
