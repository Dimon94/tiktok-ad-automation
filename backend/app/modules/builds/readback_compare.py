"""只从实际响应生成回读事实；完整单页不代表找到了对象或全扫描完成。"""

import re
from typing import Literal, cast

from pydantic import ValidationError

from app.integrations.tiktok.contracts.builds import (
    CREATE_HIGHEST_VALUE_EVENT,
    SMART_PLUS_READBACK_EVENTS,
    STANDARD_READBACK_EVENTS,
    AdCreate,
    AdGroupCreate,
    AdGroupObservedFacts,
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    BuildRecord,
    NativeAdCreate,
    NativeAdGroupCreate,
    NativeAdGroupObservedFacts,
    NativeAdObservedFacts,
    VideoCreative,
)
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.modules.builds.request_compiler import decode_observed_intent, encode_intent

PAGE_SIZE = 100
ID_KEYS = {
    "CAMPAIGN": "campaign_id",
    "ADGROUP": "adgroup_id",
    "AD": "smart_plus_ad_id",
    "CTA": "creative_portfolio_id",
}


def readback_error(evidence: CallEvidence) -> RemoteCallError:
    return RemoteCallError(
        "build_readback_unverified", effect="UNKNOWN", evidence=evidence
    )


def page_rows(
    *, data: object, page: int, evidence: CallEvidence
) -> tuple[list[dict[str, object]], int, int]:
    if not isinstance(data, dict):
        raise readback_error(evidence)
    rows, info = data.get("list"), data.get("page_info")
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
        or not isinstance(info, dict)
    ):
        raise readback_error(evidence)
    numbers = [
        info.get(key) for key in ("page", "page_size", "total_page", "total_number")
    ]
    if any(type(value) is not int for value in numbers):
        raise readback_error(evidence)
    actual, size, pages, total = cast(list[int], numbers)
    if (
        actual != page
        or size != PAGE_SIZE
        or not 0 <= pages <= 1000
        or total < 0
        or (
            pages != (total + PAGE_SIZE - 1) // PAGE_SIZE
            and not (total == 0 and pages == 1)
        )
        or not 1 <= page <= max(1, pages)
        or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
    ):
        raise readback_error(evidence)
    return rows, pages, total


def _project(shape: object, actual: object, path: str, missing: list[str]) -> object:
    # shape 只提供必须观察的字段路径；所有返回值均来自 actual，绝不复制请求值。
    if isinstance(shape, dict):
        if not isinstance(actual, dict):
            missing.append(path)
            return None
        result = {}
        for key, child in shape.items():
            child_path = f"{path}.{key}" if path else key
            if key not in actual or actual[key] is None:
                missing.append(child_path)
            else:
                result[key] = _project(child, actual[key], child_path, missing)
        # 格式互斥字段必须保留到严格 DTO 验证，不能投影掉后误判 MATCH。
        if "ad_format" in shape:
            for key in ("video_info", "music_info"):
                if key in actual and key not in result and actual[key] is not None:
                    result[key] = actual[key]
        return result
    if isinstance(shape, list):
        if not isinstance(actual, list):
            missing.append(path)
            return None
        if path == "creative_list" and shape:
            return [
                _project_creative(shape[0], item, f"{path}.{index}", missing)
                for index, item in enumerate(actual)
            ]
        return (
            [
                _project(shape[0], item, f"{path}.{index}", missing)
                for index, item in enumerate(actual)
            ]
            if shape
            else actual
        )

    return actual


def _project_creative(shape, actual, path, missing):
    # Creative assets are unordered: optional VIDEO fields come from this actual
    # creative, never from the expected asset occupying the same list position.
    info = actual.get("creative_info") if isinstance(actual, dict) else None
    if not isinstance(info, dict):
        missing.append(f"{path}.creative_info")
        return None
    common = {
        key: value
        for key, value in shape["creative_info"].items()
        if key not in {"video_info", "image_info", "music_info"}
    }
    if info.get("ad_format") == "SINGLE_VIDEO":
        common["video_info"] = {"video_id": None}
        if (
            isinstance(info.get("video_info"), dict)
            and info["video_info"].get("file_name") is not None
        ):
            common["video_info"]["file_name"] = None
        if info.get("image_info") is not None:
            common["image_info"] = [{"web_uri": None}]
    elif info.get("ad_format") == "CAROUSEL_ADS":
        common.update(image_info=[{"web_uri": None}], music_info={"music_id": None})
    if info.get("identity_authorized_bc_id") is not None:
        common["identity_authorized_bc_id"] = None
    return _project({"creative_info": common}, actual, path, missing)


def _video_omissions(expected, observed, missing):
    if (
        not isinstance(expected, AdCreate)
        or not isinstance(observed, AdCreate)
        or not all(
            isinstance(asset, VideoCreative)
            for asset in (*expected.assets, *observed.assets)
        )
    ):
        return observed
    if len(expected.assets) != len(observed.assets):
        return observed
    # Maximum bipartite matching preserves multiplicity even when duplicate video
    # IDs have different requested covers, and another occurrence omits a cover.
    wanted_assets = cast(tuple[VideoCreative, ...], expected.assets)
    actual_assets = cast(tuple[VideoCreative, ...], observed.assets)
    optional_fields = {
        "cover_image_id": "image_info",
        "file_name": "video_info.file_name",
    }

    def matching(mode):
        pairs = {}

        def assign(index, seen):
            wanted = wanted_assets[index]
            for actual_index, actual in enumerate(actual_assets):
                if actual_index in seen or wanted.video_id != actual.video_id:
                    continue
                if mode != "identity" and any(
                    getattr(wanted, field) is not None
                    and (
                        getattr(actual, field) is None
                        or (
                            mode == "exact"
                            and getattr(wanted, field) != getattr(actual, field)
                        )
                    )
                    for field in optional_fields
                ):
                    continue
                seen.add(actual_index)
                if actual_index not in pairs or assign(pairs[actual_index], seen):
                    pairs[actual_index] = index
                    return True
            return False

        return (
            pairs
            if all(assign(index, set()) for index in range(len(wanted_assets)))
            else None
        )

    pairs = matching("exact")
    if pairs is None:
        # Explicitly different values are complete mismatch evidence; absent
        # requested facts are not. Presence matching also respects duplicates,
        # so an omitted request cannot consume another occurrence's only cover.
        if matching("present") is None:
            identities = matching("identity")
            if identities is not None:
                for actual_index, wanted_index in identities.items():
                    for field, wire_path in optional_fields.items():
                        if (
                            getattr(wanted_assets[wanted_index], field) is not None
                            and getattr(actual_assets[actual_index], field) is None
                        ):
                            missing.append(
                                f"creative_list.{actual_index}.creative_info.{wire_path}"
                            )
        return observed
    return observed.model_copy(
        update={
            "assets": tuple(
                actual.model_copy(
                    update={
                        field: None
                        for field in ("cover_image_id", "file_name")
                        if getattr(wanted_assets[pairs[index]], field) is None
                    }
                )
                for index, actual in enumerate(actual_assets)
            )
        }
    )


def _safe_status(value: object) -> str | None:
    return (
        value
        if type(value) is str
        and 0 < len(value) <= 128
        and all(char.isalnum() or char == "_" for char in value)
        else None
    )


def safe_remote_id(value: object) -> str | None:
    # 与创建回执使用同一远端对象 ID 语法；名称/文案的共用 Id 类型不收紧。
    return (
        value
        if type(value) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value)
        else None
    )


def parse_page(*, query: BuildReadQuery, response: McpBusinessResponse) -> BuildPage:
    kind = query.intent.kind
    if kind == "CTA":
        if (
            query.remote_id is None
            or query.page != 1
            or not isinstance(response.data, dict)
        ):
            raise readback_error(response.evidence)
        # portfolio/get 的账户归属由已授权请求参数限定，响应只返回
        # portfolio ID/type/content；它不回显 advertiser_id。只补查询
        # 范围，绝不补创意内容；若平台实际返回账户则保留并核对它。
        rows, pages, total = (
            [{"advertiser_id": query.intent.advertiser_id, **response.data}],
            1,
            1,
        )
    else:
        rows, pages, total = page_rows(
            data=response.data, page=query.page, evidence=response.evidence
        )
    records = []
    seen = set()
    shape = encode_intent(query.intent)
    for row in rows:
        remote_id = safe_remote_id(row.get(ID_KEYS[kind]))
        if remote_id is None or remote_id in seen:
            raise readback_error(response.evidence)
        seen.add(remote_id)
        status = row.get("operation_status")
        if type(status) is not str or not status.strip():
            status = None
        missing: list[str] = []
        if (
            "secondary_status" in row
            and row["secondary_status"] is not None
            and _safe_status(row["secondary_status"]) is None
        ):
            missing.append("secondary_status")
        projected = _project(shape, row, "", missing)
        native = isinstance(query.intent, (NativeAdGroupCreate, NativeAdCreate))
        # 原生 CBO/直接目的地的互斥字段也必须保留，不能投影掉违规事实。
        if native and isinstance(projected, dict):
            for key in (
                "budget",
                "budget_mode",
                "minis_id",
                "landing_page_url_list",
                "distribution_link",
                "identity_authorized_bc_id",
                "schedule_end_time",
                "dayparting",
            ):
                if key in row and row[key] is not None:
                    projected[key] = row[key]
            if (
                kind == "ADGROUP"
                and isinstance(row.get("targeting_spec"), dict)
                and isinstance(projected.get("targeting_spec"), dict)
            ):
                from .request_compiler import TARGETING_FIELDS

                for key in TARGETING_FIELDS:
                    if key in row["targeting_spec"]:
                        projected["targeting_spec"][key] = row["targeting_spec"][key]
        direct_fields = {}
        if native:
            # 保留实际直接模式禁用字段的路径；明确不一致优先于缺少事实。
            forbidden = (
                "landing_page_url_list",
                "distribution_link",
                "minis_id",
                "call_to_action_list",
                "interactive_add_on_list",
            )
            for key in forbidden:
                if row.get(key):
                    direct_fields[key] = row[key]
            selling = row.get("drama_selling_setting")
            if isinstance(selling, dict) and selling.get("distribution_link"):
                direct_fields["drama_selling_setting.distribution_link"] = selling[
                    "distribution_link"
                ]
        observed_native = None
        observed_adgroup = None
        # Smart+组接口缺status时仅保留其余实际观察到的完整typed字段，不补请求默认值。
        if (
            kind == "ADGROUP"
            and isinstance(projected, dict)
            and not any(field != "operation_status" for field in missing)
        ):
            core = dict(projected)
            core.pop("operation_status", None)
            targeting = core.pop("targeting_spec", None)
            if isinstance(targeting, dict) and "location_ids" in targeting:
                core.update(targeting)
                core["name"] = core.pop("adgroup_name", None)
                try:
                    observed_adgroup = AdGroupObservedFacts.model_validate(core)
                except ValidationError:
                    pass
        intent = None
        if not missing and isinstance(projected, dict):
            try:
                intent = _video_omissions(
                    query.intent,
                    decode_observed_intent(kind, projected, native_observation=native),
                    missing,
                )
            except ValidationError as error:
                missing.extend(
                    ".".join(str(part) for part in item["loc"]) or "intent"
                    for item in error.errors()
                )
        if native and intent is not None:
            observed_native = (
                intent
                if isinstance(intent, NativeAdGroupObservedFacts)
                else NativeAdObservedFacts.model_validate(intent.model_dump())
            )
            intent = None
        records.append(
            BuildRecord(
                remote_id=remote_id,
                intent=intent,
                operation_status=status,
                missing_fields=tuple(sorted(set(missing))),
                observed_direct_fields=direct_fields,
                observed_adgroup=observed_adgroup,
                observed_native=observed_native,
                review_status=_safe_status(row.get("secondary_status")),
            )
        )
    return BuildPage(
        rows=tuple(records),
        page=query.page,
        total_pages=pages,
        total_number=total,
        complete=True,
        evidence=response.evidence,
    )


def _equal(left: object, right: object, path: str = "") -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equal(value, right[key], f"{path}.{key}") for key, value in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        # 每个不可拆轮播内部图片必须按顺序；外层创意保留原有多重集合语义。
        if path.endswith(".image_ids"):
            return len(left) == len(right) and all(
                _equal(a, b, path) for a, b in zip(left, right, strict=True)
            )
        remaining = list(right)
        for item in left:
            matched = next(
                (index for index, value in enumerate(remaining) if _equal(item, value)),
                None,
            )
            if matched is None:
                return False
            remaining.pop(matched)
        return not remaining
    return type(left) is type(right) and left == right


def compare_record(
    *, query: BuildReadQuery, record: BuildRecord
) -> Literal["MATCH", "MISMATCH", "INCOMPLETE"]:
    if query.remote_id is not None and record.remote_id != query.remote_id:
        return "MISMATCH"
    if record.review_status and any(
        token in record.review_status for token in ("REJECT", "DISAPPROV", "DENY")
    ):
        return "MISMATCH"
    # 明确停用优先于缺字段：不能把已知状态不一致隐藏成仅证据不足。
    if (
        query.intent.kind != "CTA"
        and record.operation_status is not None
        and record.operation_status != "ENABLE"
    ):
        return "MISMATCH"
    if record.observed_direct_fields:
        return "MISMATCH"
    if record.observed_native is not None:
        if record.missing_fields:
            return "INCOMPLETE"
        expected = query.intent.model_dump()
        actual = record.observed_native.model_dump()
        if isinstance(query.intent, NativeAdGroupCreate) and (
            expected["optimization_event"] == "AD_REVENUE_VALUE"
            and actual["optimization_event"] == "IMPRESSION_LEVEL_AD_REVENUE"
        ):
            expected["optimization_event"] = actual["optimization_event"]
        return "MATCH" if _equal(expected, actual) else "MISMATCH"
    if record.intent is None or record.missing_fields:
        return "INCOMPLETE"
    expected = query.intent.model_dump()
    actual = record.intent.model_dump()
    if query.intent.kind == "AD" and (
        expected.get("text_variants") or actual.get("text_variants")
    ):
        # text只是用于页面摘要的首条文案；多文案实际内容按完整多重集合核验。
        expected.pop("text", None)
        actual.pop("text", None)
    if query.intent.kind == "ADGROUP":
        # 创建 wire、标准 adgroup 回读和 Smart+ 回读的事件枚举不同；
        # deep_bid_type/roas_bid 已把它们映射到统一策略，事件本身仍保留为证据。
        event_values = {
            CREATE_HIGHEST_VALUE_EVENT,
            *STANDARD_READBACK_EVENTS.values(),
            *SMART_PLUS_READBACK_EVENTS.values(),
        }
        if (
            expected.get("bid_strategy") == actual.get("bid_strategy")
            and expected.get("optimization_event") in event_values
            and actual.get("optimization_event") in event_values
        ):
            expected["optimization_event"] = actual["optimization_event"]
    return "MATCH" if _equal(expected, actual) else "MISMATCH"


def parse_status(
    *, advertiser_id: str, adgroup_id: str, response: McpBusinessResponse
) -> AdGroupStatus:
    rows, _, total = page_rows(data=response.data, page=1, evidence=response.evidence)
    if total != 1 or len(rows) != 1:
        raise readback_error(response.evidence)
    row = rows[0]
    if (
        safe_remote_id(row.get("advertiser_id")) is None
        or safe_remote_id(row.get("adgroup_id")) is None
        or row.get("advertiser_id") != advertiser_id
        or row.get("adgroup_id") != adgroup_id
    ):
        raise readback_error(response.evidence)
    try:
        return AdGroupStatus.model_validate(
            {
                "advertiser_id": row["advertiser_id"],
                "adgroup_id": row["adgroup_id"],
                "operation_status": row.get("operation_status"),
                "evidence": response.evidence,
                "review_status": _safe_status(row.get("secondary_status")),
            }
        )
    except ValidationError:
        raise readback_error(response.evidence) from None


def with_adgroup_status(*, record: BuildRecord, status: AdGroupStatus) -> BuildRecord:
    """只将同账户/同对象的实际状态与既有强类型观察合并；父级字段从未重填。"""
    observed = record.observed_adgroup
    if (
        observed is None
        or status.advertiser_id != observed.advertiser_id
        or status.adgroup_id != record.remote_id
        or any(field != "operation_status" for field in record.missing_fields)
    ):
        raise readback_error(status.evidence)
    intent = None
    missing: tuple[str, ...] = ("operation_status",)
    if status.operation_status == "ENABLE":
        intent = AdGroupCreate(**observed.model_dump(), operation_status="ENABLE")
        missing = ()
    return record.model_copy(
        update={
            "intent": intent,
            "operation_status": status.operation_status,
            "missing_fields": missing,
            "review_status": status.review_status
            if status.review_status is not None
            else record.review_status,
        }
    )


def record_differences(query: BuildReadQuery, record: BuildRecord) -> list[str]:
    """只返回差异路径；原始观察保存在同一 candidate 的强类型 record 内。"""
    paths = list(record.missing_fields) + list(record.observed_direct_fields)
    observed = record.observed_native or record.intent
    if observed is not None:
        left, right = query.intent.model_dump(), observed.model_dump()
        for key in left.keys() | right.keys():
            if not _equal(left.get(key), right.get(key)):
                if (
                    key == "optimization_event"
                    and isinstance(query.intent, NativeAdGroupCreate)
                    and (left.get(key), right.get(key))
                    == ("AD_REVENUE_VALUE", "IMPRESSION_LEVEL_AD_REVENUE")
                ):
                    continue
                paths.append(key)
    if query.remote_id != record.remote_id:
        paths.append("remote_id")
    if record.review_status and any(
        t in record.review_status for t in ("REJECT", "DISAPPROV", "DENY")
    ):
        paths.append("review_status")
    return sorted(set(paths))
