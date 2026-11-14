"""短期查询结果投影；原始事实只在构建事务内使用，不建立历史事实仓。"""

from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, col, select

from app.modules.reporting.metric_catalog import (
    METRIC_CATALOG,
    NATIVE_CONTRACT,
    semantics_for,
)
from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow
from app.modules.reporting.schemas import ReportingFilter, ReportRow
from app.modules.reporting.store_models import METRIC_COLUMNS

RESULT_CONTRACT = {
    "revision": "selected-report-result-v1",
    "native_revision": NATIVE_CONTRACT,
    "metrics": {
        m.key: semantics_for(m.key, NATIVE_CONTRACT).model_dump(mode="json")
        for m in METRIC_CATALOG
    },
}


def frozen_row(row: QuerySnapshotRow) -> ReportRow:
    if not row.frozen_result or not row.frozen_result.get("metric_buckets"):
        raise HTTPException(409, detail="query_snapshot_result_unavailable")
    try:
        return ReportRow.model_validate(row.frozen_result)
    except ValidationError as error:
        raise HTTPException(409, detail="query_snapshot_result_unavailable") from error


def freeze_results(
    session: Session, snapshot: QuerySnapshot, filters: ReportingFilter
) -> None:
    from app.modules.reporting.dimension_query import materialize_rows
    from app.modules.reporting.snapshot_members import snapshot_members

    after = -1
    while True:
        batch = session.exec(
            select(QuerySnapshotRow)
            .where(
                QuerySnapshotRow.snapshot_id == snapshot.id,
                QuerySnapshotRow.stable_sequence > after,
            )
            .order_by(col(QuerySnapshotRow.stable_sequence))
            .limit(100)
        ).all()
        if not batch:
            break
        members = snapshot_members(session, snapshot=snapshot, rows=batch)
        # SQL已选出的窄投影按100行组装展示；不重新读取金额事实、不做Python成员筛选。
        selected: list[dict[str, Any]] = []
        for row in batch:
            for bucket in row.metric_buckets:
                item = dict(bucket)
                item.update(
                    {
                        k: v
                        for k, v in (bucket.get("selected_fact") or {}).items()
                        if k.startswith("f_")
                    }
                )
                item.update(
                    {
                        m: Decimal(bucket[m]) if bucket.get(m) is not None else None
                        for m in METRIC_COLUMNS
                    }
                )
                item["rid"] = bucket.get("rid") or bucket["adv"]
                selected.append(item)
        items = materialize_rows(
            session,
            tenant_id=snapshot.tenant_id,
            filters=filters,
            members=[members[r.row_key] for r in batch],
            advertisers=snapshot.advertiser_ids,
            selected_facts=selected,
        )
        for row, report in zip(batch, items, strict=True):
            row.frozen_result = report.model_dump(mode="json")
            # 账户水位描述完整性；实际选择的行观测单独纳入同一个冻结发布图。
            for vector in report.metric_buckets:
                for provenance in vector.provenance.values():
                    for observation in provenance.observations:
                        publication = observation.get("publication") or {}
                        if not publication:
                            continue
                        coordinate = dict(publication) | {
                            "advertiser_id": observation["advertiser_id"],
                            "report_kind": f"{(publication.get('publication_coordinates') or {}).get('level', observation['level'])}_day",
                            "selected_level": observation["level"],
                            "stat_date": observation["local_day"],
                            "observation_source": "SELECTED_FACT",
                            "source_kind": observation["source_kind"],
                        }
                        if coordinate not in snapshot.publication_map:
                            snapshot.publication_map = [
                                *snapshot.publication_map,
                                coordinate,
                            ]
            # 中间聚合载荷包含计算分母；最终只保存展示向量/provenance和比率对照。
            row.metric_buckets = []
            session.add(row)
        session.flush()
        after = batch[-1].stable_sequence
