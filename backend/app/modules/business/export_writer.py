"""持久游标与文件偏移共同恢复CSV批次；不依赖进程内内存。"""

import csv
import io
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.modules.business.calculation import AMOUNT_KEYS, derive_metrics
from app.modules.business.export_snapshot import EXPORT_SNAPSHOT_CONTRACT
from app.modules.business.models import BusinessExport, BusinessExportRow
from app.modules.materials.storage import make_s3
from app.modules.reporting.exports import csv_safe_text

HEADERS = (
    "消耗",
    "平台收入",
    "版权方分成",
    "分成后收入",
    "其他返点",
    "经营收入",
    "预计赔付",
    "新开漫剧预计",
    "新开其他剧预计",
    "在投闭环预计",
)


def _csv_bytes(rows: list[list[Any]], *, header: bool = False) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(rows)
    return stream.getvalue().encode("utf-8-sig" if header else "utf-8")


def write_batch(
    session: Session, row: BusinessExport, context: TenantContext, *, limit: int = 500
) -> None:
    from app.modules.business.exports import _path, _storage_configured, check_export

    check_export(session, context=context, export_id=row.id)
    # 必须先拒绝旧快照，连创建文件头和截断旧文件也不可发生。
    if row.filters.get("_snapshot_contract") != EXPORT_SNAPSHOT_CONTRACT:
        raise HTTPException(409, "导出快照约定已变更，请重新创建导出")
    partial = _path(f"business-exports/{row.tenant_id}/{row.id}.partial")
    partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not partial.exists() or partial.stat().st_size < row.file_offset:
        row.cursor, row.file_offset = 0, 0
    if row.file_offset == 0:
        header = _csv_bytes(
            [
                [
                    "名称",
                    "版权方",
                    "账户ID",
                    "系列ID",
                    "币种",
                    *HEADERS,
                    "平台ROAS",
                    "经营ROAS",
                    "情景经营收入",
                    "情景经营ROAS",
                    "计算版本",
                    "数据截止日",
                    "数据说明",
                ]
            ],
            header=True,
        )
        partial.write_bytes(header)
        row.file_offset = len(header)
    batch = session.exec(
        select(BusinessExportRow)
        .where(
            col(BusinessExportRow.export_id) == row.id,
            col(BusinessExportRow.sequence) > row.cursor,
        )
        .order_by(col(BusinessExportRow.sequence))
        .limit(limit)
    ).all()
    output = []
    for item in batch:
        raw = item.payload
        values = {
            key: Decimal(raw[key]) if raw[key] is not None else None
            for key in AMOUNT_KEYS
        }
        ratios = derive_metrics(
            values, scenario=row.filters.get("scenario", "base"), issues=row.issues
        )
        amounts = []
        for key in AMOUNT_KEYS:
            value = values[key]
            if value is None:
                amounts.append("")
            else:
                rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                adjustment = item.adjustments.get(key)
                if adjustment is not None:
                    rounded += Decimal(adjustment)
                amounts.append(format(rounded, ".2f"))
        output.append(
            [
                csv_safe_text(raw["name"]),
                csv_safe_text(raw["provider_name"]),
                csv_safe_text(raw["advertiser_id"] or "")
                if row.filters["dimension"] == "campaign"
                else "",
                csv_safe_text(raw["campaign_id"] or "")
                if row.filters["dimension"] == "campaign"
                else "",
                row.filters.get("currency", "USD"),
                *amounts,
                *[
                    "" if ratios[k] is None else format(ratios[k], ".4f")
                    for k in (
                        "platform_roas",
                        "business_roas",
                        "scenario_revenue",
                        "scenario_roas",
                    )
                ],
                ";".join(row.run_ids),
                row.filters["end_date"],
                ";".join(row.issues),
            ]
        )
    # 文件可能领先于上次事务提交：先截到持久偏移，再追加，防止崩溃重放重复行。
    with partial.open("r+b") as target:
        target.truncate(row.file_offset)
        target.seek(row.file_offset)
        target.write(_csv_bytes(output))
        target.flush()
        os.fsync(target.fileno())
        row.file_offset = target.tell()
    if batch:
        row.cursor = batch[-1].sequence
    if row.cursor >= row.rows_count:
        check_export(session, context=context, export_id=row.id)
        key = f"business-exports/{row.tenant_id}/{row.id}.csv"
        if _storage_configured():
            with partial.open("rb") as source:
                make_s3().upload_fileobj(source, settings.S3_BUCKET, key)
        else:
            partial.replace(_path(key))
        row.status, row.object_key = "COMPLETE", key
    session.add(row)
