"""既有冻结行的证据编码和引用；不持有新事实，也不读取实时来源。"""

import json
from collections import Counter, defaultdict
from hashlib import sha256
from typing import Any, Literal, cast

from fastapi import HTTPException

from app.modules.reporting.metric_catalog import (
    DERIVED_RATIOS,
    NATIVE_CONTRACT,
    semantics_for,
)
from app.modules.reporting.schemas import (
    EvidenceBucket,
    EvidenceRef,
    MetricProvenance,
    MetricVector,
)

COORDINATES = ("level", "source_kind", "contract_revision", "currency")
BOUNDS = ("local_day_min", "local_day_max", "observed_at_min", "observed_at_max")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def digest(value: Any) -> str:
    return sha256(canonical(value)).hexdigest()


def bucket_of(value: dict) -> dict:
    return {key: value.get(key) for key in COORDINATES}


def record_matches(record: dict, ref: dict) -> bool:
    return (
        bucket_of(record) == ref["bucket"]
        and (
            record.get("component") == ref["component"]
            if ref.get("component")
            else record.get("role") != "PARENT_CONTROL"
        )
        and (not ref.get("summary") or record.get("role") in {"SELECTED", "INSTANCE"})
    )


def selected_records(row: dict, ref: dict) -> list[dict]:
    return [r for r in row["source_evidence"] if record_matches(r, ref)]


def _stats(records: list[dict], name: str) -> dict:
    ratio = next((r for r in DERIVED_RATIOS if r[0] == name), None)
    counts = Counter()
    for record in records:
        for field in ratio[1:3] if ratio else (name,):
            state = record["fields"].get(field, {}).get("status") or (
                "NO_RETURNED_ROW"
                if not record.get("returned", bool(record["fact"]))
                else "LEGACY_UNKNOWN"
                if record["contract_revision"] == "LEGACY_UNKNOWN"
                else "VALUE"
                if record["fact"].get(field) is not None
                else "ABSENT"
            )
            counts[state] += 1
    numerator = ratio[1] if ratio else name
    known = sum(r["fact"].get(numerator) is not None for r in records)
    days = [str(r["local_day"]) for r in records]
    times = [
        str(
            r["publication"].get("observed_at")
            or r["publication"].get("last_success_at")
        )
        for r in records
        if r["publication"].get("observed_at")
        or r["publication"].get("last_success_at")
    ]
    return {
        "field_state_counts": dict(counts),
        "known_count": known,
        "missing_count": len(records) - known,
        "denominator_known_count": sum(
            r["fact"].get(ratio[2]) is not None for r in records
        )
        if ratio
        else None,
        "denominator_missing_count": sum(
            r["fact"].get(ratio[2]) is None for r in records
        )
        if ratio
        else None,
        "local_day_min": min(days, default=None),
        "local_day_max": max(days, default=None),
        "observed_at_min": min(times, default=None),
        "observed_at_max": max(times, default=None),
    }


def _provenance(vector: MetricVector, stats: dict, scope: str) -> dict:
    result = {}
    for name in vector.values:
        ratio = next((r for r in DERIVED_RATIOS if r[0] == name), None)
        data = stats[name]
        reason = vector.reasons.get(name) or (
            "PARTIAL_FIELDS"
            if vector.availability[name] == "PARTIAL"
            else "NO_RETURNED_ROW"
            if set(data["field_state_counts"]) <= {"NO_RETURNED_ROW"}
            else "MISSING_FIELD"
            if vector.values[name] is None
            else None
        )
        result[name] = MetricProvenance(
            level=cast(Any, vector.level),
            source_kind=cast(Any, vector.source_kind),
            contract_revision=cast(str, vector.contract_revision),
            scope_digest=scope,
            reason=reason,
            formula=(
                f"SUM({ratio[1]}) / COMPLETE_SUM({ratio[2]})"
                + (f" * {ratio[3]}" if ratio[3] != 1 else "")
            )
            if ratio
            else None,
            metric_contract=semantics_for(name, NATIVE_CONTRACT)
            if vector.contract_revision == NATIVE_CONTRACT
            else None,
            denominator_coverage_incomplete=bool(
                ratio
                and vector.reasons.get(name) == "MISSING_DENOMINATOR"
                and not data["denominator_missing_count"]
            ),
            **data,
        )
    return result


def provenance_for(
    vector: MetricVector, records: list[dict], scope: str
) -> MetricVector:
    return vector.model_copy(
        update={
            "provenance": _provenance(
                vector, {name: _stats(records, name) for name in vector.values}, scope
            )
        }
    )


def scope_filters(filters: dict) -> dict:
    # 搜索拼写和排序决定选择过程；冻结后的经济范围由实际行/成员及日期口径定义。
    return {
        k: filters.get(k)
        for k in ("dimension", "start_date", "end_date", "contribution")
    }


def row_scope(row: dict, filters: dict) -> str:
    # 包含选中的缺失成员及其覆盖；不以原始观察条数冒充请求范围。
    return digest(
        {
            "row_key": row["row_key"],
            "membership_digest": row.get("membership_digest"),
            "refs": row.get("refs", []),
            "material_uses": row.get("material_uses", []),
            "coverage": row.get("coverage", {}),
            "filters": scope_filters(filters),
        }
    )


def bind_vector(
    vector: dict,
    row: dict,
    *,
    owner: Literal["QUERY_SNAPSHOT_ROWS", "FROZEN_REPORT_ROWS", "ROW_EVIDENCE"],
    owner_id: str,
    scope: str,
    component: str | None = None,
) -> dict:
    selection = {"bucket": bucket_of(vector), "component": component, "summary": False}
    records = selected_records(row, selection)
    ref = EvidenceRef(
        owner=owner,
        owner_id=owner_id,
        row_key=row["row_key"],
        bucket=EvidenceBucket(**selection["bucket"]),
        component=component,
        scope_digest=scope,
        record_count=len(records),
        evidence_digest=digest([scope, selection, records]),
    )
    value = provenance_for(MetricVector.model_validate(vector), records, scope)
    return value.model_copy(update={"evidence_ref": ref}).model_dump(mode="json")


def component_key(child: dict) -> str:
    # 组件定位依赖原始经济身份，不能随浏览器/AI各自的覆盖说明而变化。
    return digest(
        {
            k: child.get(k)
            for k in ("key", "creative_ids", "fact_keys", "row_kind", "scope")
        }
    )


def bind_row(
    row: dict,
    *,
    owner: Literal["QUERY_SNAPSHOT_ROWS", "FROZEN_REPORT_ROWS", "ROW_EVIDENCE"],
    owner_id: str,
    filters: dict,
) -> dict:
    """完整来源只放一次；实例/父控制的指标用同一行内的 component 引用。"""
    row = dict(row)
    records = [
        dict(r, role="SELECTED", selected_row_key=row["row_key"])
        for r in row.get("source_evidence", [])
    ]
    for child in row.get("material_instances", []):
        component = component_key(child)
        for raw in child["observations"]:
            currency = raw["currency"]
            timezone = raw["timezone"]
            records.append(
                dict(raw)
                | {
                    "selected_row_key": row["row_key"],
                    "component": component,
                    "role": child["row_kind"],
                    "remote_id": raw["creative_id"],
                    "level": "creative",
                    "source_kind": "PLATFORM_DIRECT",
                    "contract_revision": child["metric_buckets"][0][
                        "contract_revision"
                    ],
                    "currency": currency,
                    "timezone": timezone,
                    "fact": raw["values"],
                    "fields": {
                        key: {"status": state}
                        for key, state in raw["field_states"].items()
                    },
                    "publication": raw.get("publisher") or {},
                    "returned": True,
                }
            )
    for child in row.get("parent_controls", []):
        records.extend(
            dict(
                r,
                selected_row_key=row["row_key"],
                component=component_key(child),
                role="PARENT_CONTROL",
            )
            for r in child["source_evidence"]
        )
    row["source_evidence"] = sorted(records, key=canonical)
    scope = row_scope(row, filters)
    row["metric_buckets"] = [
        bind_vector(v, row, owner=owner, owner_id=owner_id, scope=scope)
        for v in row["metric_buckets"]
    ]
    for key in ("material_instances", "parent_controls"):
        row[key] = [
            dict(child)
            | {
                "metric_buckets": [
                    bind_vector(
                        v,
                        row,
                        owner=owner,
                        owner_id=owner_id,
                        scope=scope,
                        component=component_key(child),
                    )
                    for v in child["metric_buckets"]
                ],
                **(
                    {"observations": []}
                    if key == "material_instances"
                    else {"source_evidence": []}
                ),
            }
            for child in row.get(key, [])
        ]
    return row


class EvidenceAccumulator:
    """只保留统计及每行固定长摘要，绝不收集整个结果的原始记录。"""

    def __init__(
        self,
        *,
        owner: Literal["QUERY_SNAPSHOT_ROWS", "FROZEN_REPORT_ROWS", "ROW_EVIDENCE"],
        owner_id: str,
        filters: dict,
    ):
        self.owner, self.owner_id, self.filters = owner, owner_id, filters
        self.rows: dict[str, list] = defaultdict(list)
        self.scopes: dict[str, str] = {}
        self.stats: dict[str, dict] = {}

    def add(self, row: dict) -> None:
        # 特殊素材行不属于普通素材合计；零实例普通行仍定义所选范围。
        if (
            self.filters["dimension"] == "material"
            and json.loads(row["row_key"])[0] != "material"
        ):
            return
        scope = row_scope(row, self.filters)
        self.scopes[row["row_key"]] = scope
        for vector in row["metric_buckets"]:
            bucket = bucket_of(vector)
            key = canonical(bucket).decode()
            records = selected_records(row, {"bucket": bucket, "summary": True})
            self.rows[key].append(
                [row["row_key"], scope, len(records), digest(records)]
            )
            stats = self.stats.setdefault(key, {})
            for name in vector["values"]:
                values = _stats(records, name)
                if name not in stats:
                    stats[name] = values
                    continue
                item = stats[name]
                item["field_state_counts"] = dict(
                    Counter(item["field_state_counts"])
                    + Counter(values["field_state_counts"])
                )
                for count in (
                    "known_count",
                    "missing_count",
                    "denominator_known_count",
                    "denominator_missing_count",
                ):
                    if values[count] is not None:
                        item[count] += values[count]
                for bound in BOUNDS:
                    candidates = [
                        v for v in (item[bound], values[bound]) if v is not None
                    ]
                    item[bound] = (
                        (min(candidates) if bound.endswith("min") else max(candidates))
                        if candidates
                        else None
                    )

    def finalize(self, summary: dict) -> dict:
        buckets = []
        for raw in summary.get("buckets", []):
            vector = MetricVector.model_validate(raw)
            bucket = bucket_of(raw)
            key = canonical(bucket).decode()
            parts = {r[0]: r for r in self.rows.get(key, [])}
            # 每个桶都绑定同一个实际所选全集；无该桶事实的成员以零原件摘要保留。
            rows = [
                parts.get(row_key, [row_key, scope, 0, digest([])])
                for row_key, scope in sorted(self.scopes.items())
            ]
            scope = digest([scope_filters(self.filters), [[r[0], r[1]] for r in rows]])
            ref = EvidenceRef(
                owner=self.owner,
                owner_id=self.owner_id,
                row_key=None,
                bucket=EvidenceBucket(**bucket),
                summary=True,
                scope_digest=scope,
                record_count=sum(r[2] for r in rows),
                evidence_digest=digest([scope, bucket, rows]),
            )
            stats = self.stats.get(key) or {
                name: _stats([], name) for name in vector.values
            }
            buckets.append(
                vector.model_copy(
                    update={
                        "provenance": _provenance(vector, stats, scope),
                        "evidence_ref": ref,
                    }
                ).model_dump(mode="json")
            )
        return summary | {"buckets": buckets}


def public_row(row: dict) -> dict:
    # 公共指标页不携带原件；完整来源仍在同一 frozen_result 中，通过已有路由分页。
    return {k: v for k, v in row.items() if k != "_source_storage_digest"} | {
        "source_evidence": []
    }


def rebind_report(report: dict) -> dict:
    """复制到归档后所有定位均报告内相对；不改变来源事实或经济证据摘要。"""

    def rebind(value):
        if isinstance(value, list):
            return [rebind(v) for v in value]
        if not isinstance(value, dict):
            return value
        if "evidence_ref" in value and value["evidence_ref"]:
            value = value | {
                "evidence_ref": value["evidence_ref"]
                | {"owner": "FROZEN_REPORT_ROWS", "owner_id": report["id"]}
            }
        return {k: rebind(v) for k, v in value.items()}

    return rebind(report) | {"projection_contract": "financial-review-v2"}


def archive_reports(payload: dict, category: str) -> list[dict]:
    reports = (
        [payload]
        if category == "reporting"
        else [
            r for mapping in payload.get("rows", []) for r in mapping.get("reports", [])
        ]
        if category == "attribution"
        else []
    )
    if any(
        r.get("status") != "UNAVAILABLE"
        and r.get("projection_contract") != "financial-review-v2"
        for r in reports
    ):
        raise HTTPException(409, "report_evidence_contract_changed")
    return reports


def seal_snapshot_rows(session, snapshot_id) -> None:
    """数据库按自身JSONB编码封存原件完整性；切片读取无需把大行搬回Python校验。"""
    from sqlalchemy import text

    session.flush()
    session.execute(
        text("""UPDATE query_snapshot_row SET frozen_result = frozen_result ||
        jsonb_build_object('_source_storage_digest', encode(sha256(convert_to((frozen_result->'source_evidence')::text,'UTF8')),'hex'))
        WHERE snapshot_id=:snapshot"""),
        {"snapshot": snapshot_id},
    )


def read_snapshot_evidence(
    session,
    *,
    context,
    bc_id,
    snapshot_id,
    row_key=None,
    bucket=None,
    component=None,
    cursor=None,
    limit=50,
):
    from sqlalchemy import text

    from app.modules.reporting.queries import _cursor, _decode_cursor, _snapshot_public
    from app.modules.reporting.query_models import read_snapshot
    from app.modules.reporting.schemas import AdsQueryPage, EvidencePage

    if snapshot_id is None or not 1 <= limit <= 100:
        raise HTTPException(422, "invalid_evidence_page")
    snapshot = read_snapshot(
        session, context=context, bc_id=bc_id, snapshot_id=snapshot_id
    )
    if row_key is None:
        vectors = snapshot.summary.get("buckets", [])
    else:
        payload = session.execute(
            text("""SELECT frozen_result - 'source_evidence' - '_source_storage_digest'
            FROM query_snapshot_row WHERE snapshot_id=:snapshot AND row_key=:row"""),
            {"snapshot": snapshot_id, "row": row_key},
        ).scalar_one_or_none()
        if payload is None:
            raise HTTPException(404, "query_evidence_not_found")
        vectors = (
            payload["metric_buckets"]
            if component is None
            else [
                v
                for child in payload.get("material_instances", [])
                + payload.get("parent_controls", [])
                for v in child["metric_buckets"]
                if v["evidence_ref"]["component"] == component
            ]
        )
    refs = [
        v["evidence_ref"] for v in vectors if bucket is None or bucket_of(v) == bucket
    ]
    if len(refs) != 1:
        raise HTTPException(422, "query_evidence_bucket_required")
    ref = refs[0]
    identity = {
        "snapshot_id": str(snapshot_id),
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "bc_id": bc_id,
        "view": "evidence",
        "ref": ref,
    }
    after, inner, delivered = "", 0, 0
    if cursor:
        decoded = _decode_cursor(cursor)
        if any(decoded.get(k) != v for k, v in identity.items()):
            raise HTTPException(404, "query_cursor_not_found")
        after, inner, delivered = (
            decoded.get("row"),
            decoded.get("inner"),
            decoded.get("delivered"),
        )
        if (
            not isinstance(after, str)
            or type(inner) is not int
            or inner < 0
            or type(delivered) is not int
            or not 0 <= delivered <= ref["record_count"]
        ):
            raise HTTPException(404, "query_cursor_not_found")
    conditions = " AND ".join(
        f"o.value->>'{key}' IS NOT DISTINCT FROM :{key}" for key in COORDINATES
    )
    predicate = f"""{conditions}
        AND (CASE WHEN CAST(:component AS text) IS NULL THEN o.value->>'role'<>'PARENT_CONTROL' ELSE o.value->>'component'=:component END)
        AND (NOT :summary OR o.value->>'role' IN ('SELECTED','INSTANCE'))
        AND (q.row_key COLLATE "C">:after COLLATE "C" OR (q.row_key=:after AND o.ordinality>:inner))"""
    # 先限制有匹配记录的行数，且每行只校验一次JSONB原件；大账户不会整行返回应用。
    records = session.execute(
        text(f"""WITH q AS MATERIALIZED (
        SELECT q.row_key,q.frozen_result->'source_evidence' AS records,
        q.frozen_result->>'_source_storage_digest'=encode(sha256(convert_to((q.frozen_result->'source_evidence')::text,'UTF8')),'hex') AS intact
        FROM query_snapshot_row q WHERE q.snapshot_id=:snapshot
        AND (CAST(:row AS text) IS NULL OR q.row_key=:row) AND q.row_key COLLATE "C">=:after COLLATE "C"
        AND EXISTS (SELECT 1 FROM jsonb_array_elements(q.frozen_result->'source_evidence') WITH ORDINALITY o(value,ordinality) WHERE {predicate})
        ORDER BY q.row_key COLLATE "C" LIMIT :limit)
        SELECT q.row_key,o.ordinality,o.value,q.intact
        FROM q CROSS JOIN LATERAL jsonb_array_elements(q.records) WITH ORDINALITY o(value,ordinality)
        WHERE {predicate} ORDER BY q.row_key COLLATE "C",o.ordinality LIMIT :limit"""),
        {
            "snapshot": snapshot_id,
            "row": row_key,
            "after": after,
            "inner": inner,
            "component": component,
            "summary": row_key is None,
            "limit": limit + 1,
            **ref["bucket"],
        },
    ).all()
    if any(not r.intact for r in records):
        raise HTTPException(409, "query_snapshot_result_unavailable")
    chosen = records[:limit]
    delivered += len(chosen)
    more = len(records) > limit
    if not more and delivered != ref["record_count"]:
        raise HTTPException(409, "query_snapshot_result_unavailable")
    continuation = (
        _cursor(
            identity
            | {
                "row": chosen[-1].row_key,
                "inner": chosen[-1].ordinality,
                "delivered": delivered,
            }
        )
        if more
        else None
    )
    return AdsQueryPage(
        snapshot=_snapshot_public(snapshot),
        total=snapshot.total,
        evidence_page=EvidencePage(
            records=tuple(r.value for r in chosen),
            next_cursor=continuation,
            total_records=ref["record_count"],
            scope_digest=ref["scope_digest"],
            evidence_digest=ref["evidence_digest"],
        ),
    )


def archive_evidence_page(
    payload, *, category, identity, report_id, row_key, bucket, component, cursor, limit
):
    from app.modules.reporting.queries import _cursor, _decode_cursor

    reports = [
        r
        for r in archive_reports(payload, category)
        if report_id is None or r["id"] == report_id
    ]
    if len(reports) != 1:
        raise HTTPException(422, "evidence_report_required")
    report = reports[0]
    rows = sorted(
        (r for r in report["rows"] if row_key is None or r["row_key"] == row_key),
        key=lambda r: r["row_key"],
    )
    if row_key is not None and len(rows) != 1:
        raise HTTPException(404, "evidence_row_not_found")
    vectors = (
        report["summary"]["buckets"]
        if row_key is None
        else rows[0]["metric_buckets"]
        if component is None
        else [
            v
            for child in rows[0].get("material_instances", [])
            + rows[0].get("parent_controls", [])
            for v in child["metric_buckets"]
            if v["evidence_ref"]["component"] == component
        ]
    )
    refs = [
        v["evidence_ref"] for v in vectors if bucket is None or bucket_of(v) == bucket
    ]
    if len(refs) != 1:
        raise HTTPException(422, "evidence_bucket_required")
    ref = refs[0]
    if ref["owner"] != "FROZEN_REPORT_ROWS" or ref["owner_id"] != report["id"]:
        raise HTTPException(409, "report_evidence_contract_changed")
    identity = identity | {"report_id": report["id"], "ref": ref}
    after, inner, delivered = "", 0, 0
    if cursor:
        token = _decode_cursor(cursor)
        if any(token.get(k) != v for k, v in identity.items()):
            raise HTTPException(404, "query_cursor_not_found")
        after, inner, delivered = (
            token.get("row"),
            token.get("inner"),
            token.get("delivered"),
        )
        if (
            not isinstance(after, str)
            or type(inner) is not int
            or inner < 0
            or type(delivered) is not int
            or not 0 <= delivered <= ref["record_count"]
        ):
            raise HTTPException(404, "query_cursor_not_found")
    selected = []
    for row in rows:
        if row["row_key"] < after:
            continue
        for index, record in enumerate(row["source_evidence"], 1):
            if row["row_key"] == after and index <= inner:
                continue
            if record_matches(record, ref):
                selected.append((row["row_key"], index, record))
                if len(selected) > limit:
                    break
        if len(selected) > limit:
            break
    more = len(selected) > limit
    selected = selected[:limit]
    delivered += len(selected)
    if not more and delivered != ref["record_count"]:
        raise HTTPException(409, "report_evidence_unavailable")
    continuation = (
        _cursor(
            identity
            | {"row": selected[-1][0], "inner": selected[-1][1], "delivered": delivered}
        )
        if more
        else None
    )
    return {
        "records": [r[2] for r in selected],
        "next_cursor": continuation,
        "total_records": ref["record_count"],
        "scope_digest": ref["scope_digest"],
        "evidence_digest": ref["evidence_digest"],
        "evidence_ref": ref,
    }
