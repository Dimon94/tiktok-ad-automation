"""经营冻结日志/锚点版本，逐日核算只读该版本，避免分页混用。"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.modules.accounts.models import AdvertiserAccount
from app.modules.operation_logs.models import (
    OperationCoverage,
    OperationDayEvidence,
    OperationExport,
    OperationLogEvent,
    OperationObservation,
)
from app.modules.operation_logs.parser import utc_window
from app.modules.operation_logs.replay import (
    BID_FIELDS,
    BidChange,
    evaluate_day,
    number,
    restore_bid,
)
from app.modules.operation_logs.store import digest


def frozen_sources(
    session: Session, tenant_id: UUID, accounts: list[str], start: date, end: date
) -> dict[str, Any]:
    if end < start:
        raise ValueError("invalid evidence window")
    if not accounts:
        return {
            "days": {},
            "observations": [],
            "exports": [],
            "account_signatures": {},
            "start": str(start),
            "end": str(end),
            "signature": digest([]),
        }
    # 优先选择区间结束后的首个锚点；新目录观察不应反复改写旧区间的依据。
    boundary = datetime.combine(end + timedelta(days=1), time.min, UTC)
    # 冻结只消费身份/账户/时间；百万目录锚点不能展开完整配置JSON和ORM。
    # DISTINCT ON与原排序不变，实际逐系列核算仍按冻结ID读取完整配置。
    observations = session.exec(
        select(
            col(OperationObservation.id),
            col(OperationObservation.advertiser_id),
            col(OperationObservation.observed_at),
        )
        .where(
            col(OperationObservation.tenant_id) == tenant_id,
            col(OperationObservation.advertiser_id).in_(accounts),
        )
        .distinct(
            col(OperationObservation.advertiser_id),
            col(OperationObservation.kind),
            col(OperationObservation.remote_id),
        )
        .order_by(
            col(OperationObservation.advertiser_id),
            col(OperationObservation.kind),
            col(OperationObservation.remote_id),
            case((col(OperationObservation.observed_at) >= boundary, 0), else_=1),
            case(
                (
                    col(OperationObservation.observed_at) >= boundary,
                    col(OperationObservation.observed_at),
                )
            ).asc(),
            col(OperationObservation.observed_at).desc(),
        )
    ).all()
    days = session.exec(
        select(OperationCoverage)
        .where(
            col(OperationCoverage.tenant_id) == tenant_id,
            col(OperationCoverage.advertiser_id).in_(accounts),
            col(OperationCoverage.stat_date) >= start,
            col(OperationCoverage.stat_date)
            <= max([end, *(observed_at.date() for _, _, observed_at in observations)]),
        )
        .order_by(
            col(OperationCoverage.advertiser_id), col(OperationCoverage.stat_date)
        )
    ).all()
    export_ids = sorted({str(d.export_id) for d in days})
    export_digests = (
        session.exec(
            select(OperationExport).where(
                col(OperationExport.tenant_id) == tenant_id,
                col(OperationExport.id).in_([UUID(i) for i in export_ids]),
            )
        ).all()
        if export_ids
        else []
    )
    timezone_map = dict(
        session.exec(
            select(
                col(AdvertiserAccount.advertiser_id), col(AdvertiserAccount.timezone)
            ).where(
                col(AdvertiserAccount.tenant_id) == tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(accounts),
            )
        ).all()
    )
    footprints = {}
    for advertiser in {e.advertiser_id for e in export_digests}:
        zone = timezone_map.get(advertiser)
        if not zone:
            continue
        first = utc_window(start, start, zone)[0]
        last = utc_window(end, end, zone)[1]
        anchors = [
            observed_at
            for _, account, observed_at in observations
            if account == advertiser
        ]
        required_start, required_end = min([first, *anchors]), max([last, *anchors])
        segments: list[list[datetime]] = []
        for exported in sorted(
            (e for e in export_digests if e.advertiser_id == advertiser),
            key=lambda e: e.start_at,
        ):
            left, right = (
                max(required_start, exported.start_at),
                min(required_end, exported.end_at),
            )
            if left >= right:
                continue
            if segments and left <= segments[-1][1]:
                segments[-1][1] = max(segments[-1][1], right)
            else:
                segments.append([left, right])
        # 尾段延长到锚点是新的有效依据；超过所需区间的空重放不推进修订。
        footprints[advertiser] = segments
    account_signatures = {
        a: digest(
            [[str(d.stat_date), d.digest] for d in days if d.advertiser_id == a]
            + [str(identity) for identity, account, _ in observations if account == a]
            + sorted({e.digest for e in export_digests if e.advertiser_id == a})
            + [footprints.get(a, [])]
        )
        for a in sorted(
            {d.advertiser_id for d in days}
            | {account for _, account, _ in observations}
        )
    }
    return {
        "account_signatures": account_signatures,
        "start": str(start),
        "end": str(end),
        "days": {
            f"{d.advertiser_id}/{d.stat_date}": {
                "export_id": str(d.export_id),
                "digest": d.digest,
                "revision": d.revision,
            }
            for d in days
        },
        "observations": [str(identity) for identity, _, _ in observations],
        "exports": export_ids,
        "signature": digest(account_signatures),
    }


def _continuous(exports: list[OperationExport], start: datetime, end: datetime) -> bool:
    covered = start
    for e in sorted(exports, key=lambda e: e.start_at):
        if e.start_at <= covered:
            covered = max(covered, e.end_at)
    return covered >= end


def _pairs(detail: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = detail.get("before_after", [])
    return (
        pairs
        if isinstance(pairs, list)
        and all(isinstance(p, dict) and "before" in p and "after" in p for p in pairs)
        else []
    )


def _money(value: Any) -> tuple[Decimal, str] | None:
    try:
        number_text, currency = str(value).rsplit(" ", 1)
        numeric = Decimal(number_text.replace(",", ""))
        return (numeric, currency) if numeric.is_finite() else None
    except ValueError, InvalidOperation:
        return None


def build_evidence(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    campaign_id: str,
    day: date,
    timezone: str,
    frozen: dict[str, Any],
    spend: Decimal | None,
    events: Decimal | None,
    revenue: Decimal | None,
    cache: dict[Any, Any] | None = None,
    report_revision: int | None = None,
) -> OperationDayEvidence:
    cache = {} if cache is None else cache
    cache_key = (str(tenant_id), advertiser_id, frozen.get("signature"))
    if cache_key not in cache:
        exports = session.exec(
            select(OperationExport).where(
                col(OperationExport.tenant_id) == tenant_id,
                col(OperationExport.advertiser_id) == advertiser_id,
                col(OperationExport.id).in_(
                    [UUID(i) for i in frozen.get("exports", [])]
                ),
            )
        ).all()
        observations = session.exec(
            select(OperationObservation).where(
                col(OperationObservation.tenant_id) == tenant_id,
                col(OperationObservation.advertiser_id) == advertiser_id,
                col(OperationObservation.id).in_(
                    [UUID(i) for i in frozen.get("observations", [])]
                ),
            )
        ).all()
        log_rows = (
            session.exec(
                select(OperationLogEvent).where(
                    col(OperationLogEvent.export_id).in_([e.id for e in exports])
                )
            ).all()
            if exports
            else []
        )
        cache[cache_key] = exports, observations, log_rows
    exports, observations, unique = cache[cache_key]
    authoritative = {}
    for event in unique:
        identity = event.identity
        local_day = event.occurred_at.astimezone(ZoneInfo(timezone)).date()
        selected = frozen.get("days", {}).get(f"{advertiser_id}/{local_day}")
        if selected:
            accepted = str(event.export_id) == selected["export_id"]
        else:
            candidates = [
                e for e in exports if e.start_at <= event.occurred_at < e.end_at
            ]
            winner = (
                max(candidates, key=lambda e: e.observed_at) if candidates else None
            )
            accepted = winner is not None and event.export_id == winner.id
        if accepted:
            authoritative[identity] = event
    unique = authoritative
    campaign = next(
        (
            o
            for o in observations
            if o.kind == "campaign" and o.remote_id == campaign_id
        ),
        None,
    )
    groups = [
        o for o in observations if o.kind == "adgroup" and o.parent_id == campaign_id
    ]
    lower, upper = utc_window(day, day, timezone)
    # 组的存在区间也是历史事实，后来创建的低出价不能进入旧日最低值。
    lifetimes: dict[str, tuple[datetime | None, datetime | None]] = {}
    existing_groups = []
    for group in groups:
        creates, deletes = [], []
        for event in unique.values():
            if event.object_id != group.remote_id:
                continue
            if any(d["action"].strip().lower() == "create" for d in event.details):
                creates.append(event.occurred_at)
            if any(d["action"].strip().lower() == "delete" for d in event.details):
                deletes.append(event.occurred_at)
        created, deleted = min(creates, default=None), min(deletes, default=None)
        if (
            created is not None
            and created >= upper
            or deleted is not None
            and deleted <= lower
        ):
            continue
        lifetimes[group.remote_id] = created, deleted
        existing_groups.append(group)
    groups = existing_groups
    observations_for_chain = [*groups, *([campaign] if campaign else [])]
    chain_lower = min([lower, *(o.observed_at for o in observations_for_chain)])
    chain_upper = max([upper, *(o.observed_at for o in observations_for_chain)])
    reasons = []
    if f"{advertiser_id}/{day}" not in frozen.get("days", {}):
        reasons.append("LOG_COVERAGE_MISSING")
    if not campaign or not groups:
        reasons.append("CONFIGURATION_ANCHOR_MISSING")
    if campaign and (
        campaign.configuration.get("ad_type") != "SMART_PLUS"
        or campaign.configuration.get("budget_optimize_on") is not True
        or campaign.configuration.get("app_promotion_type") != "MINIS"
    ):
        reasons.append("STRUCTURE_UNSUPPORTED")
    object_ids = {campaign_id, *(o.remote_id for o in groups)}
    # 不同窗口的同内容事件按稳定身份取一次；一份导出内的多重序号仍保留。
    changes: dict[str, list[BidChange]] = {o.remote_id: [] for o in groups}
    manual_count = 0
    unknown_bid = False
    ignored = {
        "name",
        "ad name",
        "languages",
        "review status",
        "targeting",
        "schedule",
        "placements",
        "location",
        "age",
        "gender",
    }
    for e in unique.values():
        if e.object_id not in object_ids:
            # 缺少父对象的组日志不能假定与本系列无关，关联缺口需明确保留。
            if (
                e.object_type == "ADGROUP"
                and not any(
                    o.kind == "adgroup" and o.remote_id == e.object_id
                    for o in observations
                )
                and lower <= e.occurred_at < upper
            ):
                reasons.append("OBJECT_RELATION_UNKNOWN")
            continue
        if not chain_lower <= e.occurred_at < chain_upper:
            continue
        for detail in e.details:
            action, name = (
                detail["action"].strip().lower(),
                detail["name"].strip().lower(),
            )
            pairs = _pairs(detail)
            in_day = lower <= e.occurred_at < upper
            if action == "create":
                continue  # 初始暂停创建不是投放后的暂停
            if in_day and action == "delete" and e.object_id == campaign_id:
                reasons.append("CAMPAIGN_DELETED")
            if name in BID_FIELDS:
                if e.object_id not in changes or len(pairs) != 1:
                    reasons.append("BID_LOG_UNSUPPORTED")
                    continue
                before, after = number(pairs[0]["before"]), number(pairs[0]["after"])
                if before is None or after is None:
                    reasons.append("BID_VALUE_UNKNOWN")
                    continue
                changes[e.object_id].append(
                    {"at": e.occurred_at, "before": before, "after": after}
                )
                if in_day:
                    if e.attribution == "MANUAL" or e.source.strip().lower() in {
                        "ads manager",
                        "tiktok ads manager",
                    }:
                        manual_count += 1
                    elif e.attribution != "AUTOMATED":
                        unknown_bid = True
            elif (
                name in {"campaign", "status", "operation status"}
                and e.object_id == campaign_id
            ):
                if not in_day:
                    continue
                known_status = {
                    "enabled",
                    "enable",
                    "paused",
                    "deleted",
                    "disable",
                    "disabled",
                }
                if not pairs or any(
                    str(p["before"]).strip().lower() not in known_status
                    or str(p["after"]).strip().lower() not in known_status
                    for p in pairs
                ):
                    reasons.append("STATUS_VALUE_UNKNOWN")
                if any(
                    str(p["after"]).strip().lower()
                    in {"paused", "deleted", "disable", "disabled"}
                    for p in pairs
                ):
                    reasons.append("CAMPAIGN_PAUSED")
            elif "budget" in name:
                if not in_day:
                    continue  # 其它日期的预算操作不改变本日ROAS或资格。
                if not pairs:
                    reasons.append("BUDGET_VALUE_UNKNOWN")
                    continue
                if e.object_type == "ADGROUP" and ("minimum" in name or "min" in name):
                    if any(p["before"] != p["after"] for p in pairs):
                        reasons.append("ADGROUP_MIN_BUDGET_CHANGED")
                elif e.object_type == "ADGROUP":
                    reasons.append("BUDGET_VALUE_UNKNOWN")
                elif e.object_id == campaign_id:
                    for p in pairs:
                        budget_before, budget_after = (
                            _money(p["before"]),
                            _money(p["after"]),
                        )
                        if (
                            budget_before is None
                            or budget_after is None
                            or budget_before[1] != budget_after[1]
                        ):
                            reasons.append("BUDGET_VALUE_UNKNOWN")
                        elif budget_after[0] < budget_before[0]:
                            reasons.append("BUDGET_DECREASED")
            elif (
                action == "change"
                and name not in ignored
                and name not in {"campaign", "ad group"}
            ):
                reasons.append("OPERATION_FIELD_UNKNOWN")
    if manual_count > 2:
        reasons.append("MANUAL_BID_LIMIT")
    bids = []
    for group in groups:
        config = group.configuration
        # D0 IAA的明确结构才使用300事件模型；IAP及其它目标不套同一公式。
        if (
            config.get("promotion_type") != "MINI_APP"
            or config.get("optimization_goal") != "VALUE"
            or config.get("deep_bid_type") != "VO_MIN_ROAS"
            or config.get("optimization_event")
            not in {"IMPRESSION_LEVEL_AD_REVENUE", "AD_REVENUE_VALUE"}
            or config.get("vbo_window") != "ZERO_DAY"
        ):
            reasons.append("STRUCTURE_UNSUPPORTED")
        created, deleted = lifetimes[group.remote_id]
        active_start = max(lower, created) if created else lower
        active_end = min(upper, deleted) if deleted else upper
        chain_start, chain_end = (
            min(active_start, group.observed_at),
            max(active_end, group.observed_at),
        )
        if not _continuous(exports, chain_start, chain_end):
            reasons.append("LOG_CHAIN_INCOMPLETE")
            continue
        bid = restore_bid(
            number(config.get("roas_bid")),
            group.observed_at,
            active_start,
            active_end,
            changes[group.remote_id],
        )
        if bid is None:
            reasons.append("BID_CHAIN_AMBIGUOUS")
        else:
            bids.append(bid)
    bid = min(bids) if bids and len(bids) == len(groups) else None
    result = evaluate_day(
        spend=spend, events=events, revenue=revenue, bid=bid, reasons=reasons
    )
    result.update(
        bid=str(bid) if bid is not None else None,
        manual_bid_changes=manual_count,
        unknown_bid_source=unknown_bid,
        policy="minis-iaa-cbo-d0-2026-07-v1",
        sources=frozen.get("account_signatures", {}).get(advertiser_id),
        source_start=frozen.get("start", str(day)),
        source_end=frozen.get("end", str(day)),
        report_revision=report_revision,
        timezone=timezone,
        inputs={
            "spend": str(spend) if spend is not None else None,
            "revenue_d0": str(revenue) if revenue is not None else None,
            "iaa_events": str(events) if events is not None else None,
        },
    )
    if unknown_bid and result["status"] == "ESTIMATED_PENDING":
        result["reasons"].append("BID_SOURCE_UNKNOWN")
    fingerprint = digest([advertiser_id, campaign_id, day, result])
    row = OperationDayEvidence(
        id=uuid5(tenant_id, fingerprint),
        tenant_id=tenant_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        stat_date=day,
        digest=fingerprint,
        data=result,
    )
    typing_cast(SQLAlchemySession, session).execute(
        insert(OperationDayEvidence)
        .values(**row.model_dump())
        .on_conflict_do_nothing(constraint="uq_operation_day_evidence")
    )
    return row
