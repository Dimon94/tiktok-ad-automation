"""业务证据断言通过真实冻结读取入口解析；不从当前事实重建旧观察。"""

from types import SimpleNamespace
from uuid import UUID

from app.modules.reporting.frozen_evidence import read_snapshot_evidence
from app.modules.reporting.schemas import BucketStamp, MetricVector


def comparable_snapshot_evidence(session, seed, snapshot_id, value):
    """先验证各自快照并解析全部原件，只归一跨快照必然不同的定位 ID。"""
    resolved = []

    def compare(item):
        if isinstance(item, list):
            return [compare(child) for child in item]
        if not isinstance(item, dict):
            return item
        if ref := item.get("evidence_ref"):
            assert ref["owner"] == "QUERY_SNAPSHOT_ROWS"
            assert ref["owner_id"] == str(snapshot_id)
            resolved.append(records(session, seed, MetricVector.model_validate(item)))
            item = item | {"evidence_ref": ref | {"owner_id": "OWN_SNAPSHOT"}}
        return {key: compare(child) for key, child in item.items()}

    comparable = compare(value)
    assert any(resolved)
    return comparable, resolved


def records(session, seed, vector, source_row=None):
    ref = vector.evidence_ref
    if ref is None:
        return [
            r
            for r in source_row.source_evidence
            if (r["level"], r["source_kind"], r["contract_revision"], r["currency"])
            == (
                vector.level,
                vector.source_kind,
                vector.contract_revision,
                vector.currency,
            )
        ]
    result, cursor = [], None
    while True:
        page = read_snapshot_evidence(
            session,
            context=seed.context,
            bc_id=getattr(seed, "bc_id", "bc-a"),
            snapshot_id=UUID(ref.owner_id),
            row_key=ref.row_key,
            bucket=ref.bucket.model_dump(),
            component=ref.component,
            cursor=cursor,
            limit=100,
        ).evidence_page
        result.extend(page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(result) == ref.record_count
    return result


def provenance(session, seed, vector, metric, source_row=None):
    evidence = records(session, seed, vector, source_row)
    value = vector.provenance[metric].model_dump()
    value.update(
        observations=evidence,
        local_ranges=sorted(
            {f"{r['advertiser_id']} {r['local_day']} {r['timezone']}" for r in evidence}
        ),
        buckets=[
            BucketStamp(
                advertiser_id=r["advertiser_id"],
                level=r["level"],
                stat_date=r["local_day"],
                contract_revision=r["contract_revision"],
                publication_version=r["publication"].get("publication_version", 0),
                observed_at=r["publication"].get("observed_at"),
                published_at=r["publication"].get("last_success_at"),
                last_attempt_status=r["publication"].get("status", "PENDING"),
            )
            for r in evidence
        ],
    )
    return SimpleNamespace(**value)


def archived_records(report, vector):
    ref = vector["evidence_ref"]
    assert ref["owner"] == "FROZEN_REPORT_ROWS" and ref["owner_id"] == report["id"]
    return [
        record
        for row in report["rows"]
        if ref["row_key"] is None or row["row_key"] == ref["row_key"]
        for record in row["source_evidence"]
        if all(record.get(k) == v for k, v in ref["bucket"].items())
        and (
            record.get("component") == ref["component"]
            if ref.get("component")
            else record["role"] != "PARENT_CONTROL"
        )
        and (not ref["summary"] or record["role"] in {"SELECTED", "INSTANCE"})
    ]


def assert_csv_evidence(csv_row, vector, expected_records):
    import json
    from hashlib import sha256

    raw = vector.model_dump(mode="json") if hasattr(vector, "model_dump") else vector
    exported = json.loads(csv_row["metric_vector"])
    assert {k: v for k, v in exported.items() if k != "evidence_ref"} == {
        k: v for k, v in raw.items() if k != "evidence_ref"
    }
    evidence = json.loads(csv_row["source_evidence"])
    assert evidence == expected_records
    ref = exported["evidence_ref"]
    assert ref["owner"] == "ROW_EVIDENCE"
    assert ref["record_count"] == len(evidence)
    assert (
        ref["evidence_digest"]
        == sha256(
            json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )
