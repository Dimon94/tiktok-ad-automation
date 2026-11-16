"""短期查询结果投影；原始事实只在构建事务内使用，不建立历史事实仓。"""

import json
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, col, select

from app.modules.reporting.metric_catalog import (
    CREATIVE_OBSERVED_CONTRACT,
    METRIC_CATALOG,
    NATIVE_CONTRACT,
    semantics_for,
)
from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow
from app.modules.reporting.schemas import ReportingFilter, ReportRow
from app.modules.reporting.store_models import METRIC_COLUMNS

RESULT_CONTRACT = {
    "revision": "selected-report-result-v2",
    "native_revision": NATIVE_CONTRACT,
    "metrics": {
        m.key: semantics_for(m.key, NATIVE_CONTRACT).model_dump(mode="json")
        for m in METRIC_CATALOG
    },
}


MATERIAL_RESULT_CONTRACT = {
    "revision": "material-instance-result-v2",
    "material_query_contract": 4,
    "creative_semantics": "UNCONFIRMED",
    "creative_contract_revision": CREATIVE_OBSERVED_CONTRACT,
    "d0_click_hours": None,
    "traffic_scope": "UNCONFIRMED",
    "total_maturity": "UNKNOWN",
    "metrics": {
        m.key: semantics_for(
            m.key, CREATIVE_OBSERVED_CONTRACT, grain="creative"
        ).model_dump(mode="json")
        for m in METRIC_CATALOG
    },
}


def result_contract(dimension: str) -> dict:
    return MATERIAL_RESULT_CONTRACT if dimension == "material" else RESULT_CONTRACT


def freeze_material_results(session, snapshot, filters):
    """复用SQL成员计划，在同一RR内分批冻结展示载荷；后续读取不再解引用目录。"""
    from app.modules.reporting.frozen_evidence import EvidenceAccumulator, bind_row
    from app.modules.reporting.material_query import (
        freeze_instance_tracer,
        material_summary_contributions,
        materialize_material_rows,
    )
    from app.modules.reporting.snapshot_members import snapshot_members

    accumulator = EvidenceAccumulator(
        owner="QUERY_SNAPSHOT_ROWS", owner_id=str(snapshot.id), filters=snapshot.filters
    )
    ordinary = []
    after = -1
    while True:
        rows = session.exec(
            select(QuerySnapshotRow)
            .where(
                QuerySnapshotRow.snapshot_id == snapshot.id,
                QuerySnapshotRow.stable_sequence > after,
            )
            .order_by(col(QuerySnapshotRow.stable_sequence))
            .limit(100)
        ).all()
        if not rows:
            break
        members = snapshot_members(session, snapshot=snapshot, rows=rows)
        from app.modules.reporting.creative_query import read_facts

        facts = read_facts(
            session,
            tenant_id=snapshot.tenant_id,
            advertisers=snapshot.advertiser_ids,
            filters=filters,
            row_keys=[r.row_key for r in rows],
            observations=True,
        )
        reports = materialize_material_rows(
            session,
            tenant_id=snapshot.tenant_id,
            filters=filters,
            members=[members[r.row_key] for r in rows],
            advertisers=snapshot.advertiser_ids,
            selected_facts=facts,
        )
        reports = freeze_instance_tracer(
            session, snapshot=snapshot, filters=filters, reports=reports, facts=facts
        )
        for row, report in zip(rows, reports, strict=True):
            ordinary.extend(material_summary_contributions(report))
            row.frozen_result = bind_row(
                report.model_dump(mode="json"),
                owner="QUERY_SNAPSHOT_ROWS",
                owner_id=str(snapshot.id),
                filters=snapshot.filters,
            )
            accumulator.add(row.frozen_result)
            session.add(row)
        session.flush()
        after = rows[-1].stable_sequence
    from app.modules.reporting.dimension_query import merge_metric_buckets

    snapshot.summary = snapshot.summary | {
        "scope": "SELECTED_MATERIALS",
        "buckets": [
            v.model_dump(mode="json")
            for v in merge_metric_buckets(ordinary, strict_denominators=True)
        ],
    }

    snapshot.summary = accumulator.finalize(snapshot.summary)


def frozen_row(row: QuerySnapshotRow, *, full_evidence: bool = False) -> ReportRow:
    if not row.frozen_result or not row.frozen_result.get("metric_buckets"):
        raise HTTPException(409, detail="query_snapshot_result_unavailable")
    try:
        from app.modules.reporting.frozen_evidence import public_row

        return ReportRow.model_validate(
            {
                k: v
                for k, v in row.frozen_result.items()
                if k != "_source_storage_digest"
            }
            if full_evidence
            else public_row(row.frozen_result)
        )
    except ValidationError as error:
        raise HTTPException(409, detail="query_snapshot_result_unavailable") from error


def freeze_results(
    session: Session, snapshot: QuerySnapshot, filters: ReportingFilter
) -> None:
    from app.modules.reporting.dimension_query import materialize_rows
    from app.modules.reporting.frozen_evidence import EvidenceAccumulator, bind_row
    from app.modules.reporting.snapshot_members import snapshot_members

    accumulator = EvidenceAccumulator(
        owner="QUERY_SNAPSHOT_ROWS", owner_id=str(snapshot.id), filters=snapshot.filters
    )
    publication_keys = {json.dumps(p, sort_keys=True) for p in snapshot.publication_map}
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
            row.frozen_result = bind_row(
                report.model_dump(mode="json"),
                owner="QUERY_SNAPSHOT_ROWS",
                owner_id=str(snapshot.id),
                filters=snapshot.filters,
            )
            accumulator.add(row.frozen_result)
            # 账户水位描述完整性；实际选择的行观测单独纳入同一个冻结发布图。
            for observation in report.source_evidence:
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
                key = json.dumps(coordinate, sort_keys=True)
                if key not in publication_keys:
                    snapshot.publication_map = [*snapshot.publication_map, coordinate]
                    publication_keys.add(key)
            # 中间聚合载荷包含计算分母；最终只保存展示向量/provenance和比率对照。
            row.metric_buckets = []
            session.add(row)
        session.flush()
        after = batch[-1].stable_sequence

    snapshot.summary = accumulator.finalize(snapshot.summary)
