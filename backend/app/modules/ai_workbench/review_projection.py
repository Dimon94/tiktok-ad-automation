"""AI 交付投影：完整来源留在不可变归档，交付只去除深层来源展开。"""


def metric_buckets_for_reading(buckets: list) -> list:
    """复盘读取指标值、可用性和原因；Native 深层来源完整保存在冻结归档。"""
    return [
        {
            **{key: value for key, value in bucket.items() if key != "provenance"},
            "provenance_details_archived": bool(bucket.get("provenance")),
        }
        for bucket in buckets
    ]


def report_row_for_reading(row: dict) -> dict:
    result = {
        k: v
        for k, v in row.items()
        if k not in {"source_evidence", "_source_storage_digest"}
    }
    if "metric_buckets" in row:
        result["metric_buckets"] = metric_buckets_for_reading(row["metric_buckets"])
    for key in ("material_instances", "parent_controls", "reconciliation_rows"):
        if key in row:
            result[key] = [report_row_for_reading(child) for child in row[key]]
    if "observations" in row:
        result.pop("observations")
        result["observations_archived"] = bool(row["observations"]) or any(
            (v.get("evidence_ref") or {}).get("record_count", 0)
            for v in row.get("metric_buckets", [])
        )
    if "platform_roas_observations" in row.get("display", {}):
        result["display"] = {
            key: value
            for key, value in row["display"].items()
            if key != "platform_roas_observations"
        }
        result["provenance_details_archived"] = True
    return result


def report_for_reading(report: dict) -> dict:
    result = dict(report)
    if "summary" in report:
        result["summary"] = {
            **report["summary"],
            "buckets": metric_buckets_for_reading(report["summary"].get("buckets", [])),
        }
    if "rows" in report:
        result["rows"] = [report_row_for_reading(row) for row in report["rows"]]
    return result
