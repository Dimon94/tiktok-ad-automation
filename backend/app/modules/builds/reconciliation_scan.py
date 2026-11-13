"""普通与新授权核查共用的有界跨页判定；只消费实际类型证据。"""

import re
from collections.abc import Callable
from typing import Any, Literal

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import (
    AdGroupCreate,
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    BuildRecord,
    NativeAdCreate,
    NativeAdGroupCreate,
)
from app.modules.builds.readback_compare import (
    _equal,
    compare_record,
    safe_remote_id,
    with_adgroup_status,
)

ScanState = Literal["MORE", "UNKNOWN", "COMPLETE"]


def reconciliation_decision(
    *, page: BuildPage, matches: tuple[BuildRecord, ...]
) -> Literal["CONFIRMED", "UNKNOWN"]:
    # 仅供已完成跨页聚合的调用者判定；空页从来不能授权再次创建。
    if not page.complete or len(matches) != 1:
        return "UNKNOWN"
    item = matches[0]
    return (
        "CONFIRMED"
        if (item.intent is not None or item.observed_native is not None)
        and not item.missing_fields
        else "UNKNOWN"
    )


def _invalid() -> DomainError:
    return DomainError("readback_response_unknown", "原对象的字段或分页范围尚未核实")


def _core_matches(query: BuildReadQuery, record: BuildRecord) -> bool:
    return (
        isinstance(query.intent, AdGroupCreate)
        and record.observed_adgroup is not None
        and _equal(
            query.intent.model_dump(exclude={"operation_status"}),
            record.observed_adgroup.model_dump(),
        )
    )


def _candidate(query: BuildReadQuery, record: BuildRecord) -> bool:
    if query.remote_id is not None and (
        query.native_series
        or isinstance(query.intent, (NativeAdGroupCreate, NativeAdCreate))
    ):
        if record.remote_id != query.remote_id:
            raise _invalid()
        # 精确 ID 的缺字段/父级差异仍保存观察；比较器决定未决或不一致。
        return True
    if query.native_series or isinstance(
        query.intent, (NativeAdGroupCreate, NativeAdCreate)
    ):
        # 名称只作请求定位：新冻结请求含草稿 UUID；最终仍须全字段 MATCH。
        # M2 名称另含冻结 plan_key；完整名称比较仍禁止改挂兄弟计划。
        # 历史名称不具唯一请求身份，不能自动认领。
        if not re.search(
            r"-R[0-9a-f]{32}(?::[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?"
            r"(?:-G1|-A[0-9]+-C[0-9]+)?$",
            query.intent.name,
        ):
            return False
        actual = record.observed_native or record.intent
        if actual is None:
            return True
        if actual.kind != query.intent.kind:
            raise _invalid()
        if actual.advertiser_id != query.intent.advertiser_id:
            raise _invalid()
        if actual.name != query.intent.name:
            return False
        # 同名错父、缺字段或差异也计入候选，避免忽略冲突后认领另一条。
        return True
    actual = record.observed_native or record.intent or record.observed_adgroup
    if (
        actual is None
        or actual.kind != query.intent.kind
        or actual.advertiser_id != query.intent.advertiser_id
    ):
        raise _invalid()
    for field in {"ADGROUP": ("campaign_id",), "AD": ("adgroup_id",)}.get(
        actual.kind, ()
    ):
        if getattr(actual, field) != getattr(query.intent, field):
            raise _invalid()
    if query.remote_id is not None:
        if record.remote_id != query.remote_id:
            raise _invalid()
        return True
    if actual.kind == "CTA":
        raise _invalid()
    if query.intent.kind == "CTA":
        raise _invalid()
    return actual.name == query.intent.name


def conclusion(query: BuildReadQuery, record: BuildRecord) -> ScanState:
    comparison = compare_record(query=query, record=record)
    if (
        record.intent is not None or record.observed_native is not None
    ) and not record.missing_fields:
        if comparison == "MATCH" or query.remote_id is not None:
            return "COMPLETE"
    # CreateIntent只能表示ENABLE。真实停用状态与完整观察到的相同核心字段另记差异。
    if (
        query.remote_id == record.remote_id
        and record.operation_status in {"DISABLE", "FROZEN"}
        and _core_matches(query, record)
        and not any(field != "operation_status" for field in record.missing_fields)
    ):
        return "COMPLETE"
    return "UNKNOWN"


def candidate_summary(query: BuildReadQuery, record: BuildRecord) -> dict[str, Any]:
    from app.modules.builds.readback_compare import record_differences

    return {
        "differences": record_differences(query, record),
        "remote_id": record.remote_id,
        "comparison": compare_record(query=query, record=record),
        "operation_status": record.operation_status,
        "review_status": getattr(record, "review_status", None),
        "record": record.model_dump(mode="json"),
    }


def advance(
    *,
    query: BuildReadQuery,
    progress: dict[str, Any],
    page: BuildPage | AdGroupStatus,
    seen_before: Callable[[tuple[str, ...]], bool],
) -> tuple[dict[str, Any], ScanState]:
    next_progress = dict(progress)
    if progress.get("stage") == "STATUS":
        if not isinstance(page, AdGroupStatus):
            raise _invalid()
        candidate = progress.get("candidate")
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("record"), dict
        ):
            raise _invalid()
        original = BuildRecord.model_validate(candidate["record"])
        record = with_adgroup_status(record=original, status=page)
        if not _candidate(query, record):
            raise _invalid()
        next_progress.update(candidate=candidate_summary(query, record), done=True)
        return next_progress, conclusion(query, record)
    if (
        not isinstance(page, BuildPage)
        or not page.complete
        or page.page != progress["page"]
        or not 0 <= page.total_number <= 100000
        or not 0 <= page.total_pages <= 1000
        or page.total_pages
        not in {
            max(1, (page.total_number + 99) // 100),
            (page.total_number + 99) // 100,
        }
        or len(page.rows) != min(100, max(0, page.total_number - (page.page - 1) * 100))
    ):
        raise _invalid()
    ids = tuple(record.remote_id for record in page.rows)
    if len(set(ids)) != len(ids) or any(
        safe_remote_id(identity) is None for identity in ids
    ):
        raise _invalid()
    if page.page > 1 and (
        seen_before(ids)
        or progress.get("total") != page.total_number
        or progress.get("pages") != page.total_pages
    ):
        raise _invalid()
    next_progress.update(
        total=page.total_number,
        pages=page.total_pages,
        seen=progress["seen"] + len(ids),
    )
    for record in page.rows:
        if _candidate(query, record):
            next_progress["matches"] = min(2, next_progress["matches"] + 1)
            if next_progress["matches"] == 1:
                next_progress["candidate"] = candidate_summary(query, record)
    if page.page < page.total_pages:
        next_progress["page"] = page.page + 1
        return next_progress, "MORE"
    next_progress["done"] = True
    if next_progress["seen"] != page.total_number or next_progress["matches"] != 1:
        return next_progress, "UNKNOWN"
    record = BuildRecord.model_validate(next_progress["candidate"]["record"])
    if (
        record.operation_status is None
        and record.observed_adgroup is not None
        and (query.remote_id is not None or _core_matches(query, record))
        and not any(field != "operation_status" for field in record.missing_fields)
    ):
        next_progress.update(stage="STATUS", page=1, done=False)
        return next_progress, "MORE"
    if (
        record.observed_adgroup is None
        and reconciliation_decision(page=page, matches=(record,)) != "CONFIRMED"
    ):
        return next_progress, "UNKNOWN"
    return next_progress, conclusion(query, record)
