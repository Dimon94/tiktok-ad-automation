"""按账户本地日查询；SQL 严格汇总，BC 只决定当前账户的可见性。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import RowMapping, Text, any_, bindparam, func
from sqlalchemy import select as sql_select
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.sql import ColumnElement
from sqlmodel import Session, SQLModel, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.carousel import CarouselDirectory
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.directory import directory_carousels
from app.modules.ads.models import AdObject as CanonicalAdObject
from app.modules.reporting import query_diagnostics
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.metric_catalog import DERIVED_RATIOS
from app.modules.reporting.models import AccountBalanceObservation
from app.modules.reporting.read_models.models import AdEntityRead as AdObject
from app.modules.reporting.read_models.models import AdgroupReportDay
from app.modules.reporting.read_models.models import (
    CampaignNameCurrent as CampaignNameProjection,
)
from app.modules.reporting.row_set import SnapshotMember, entity_ref, ordered_members
from app.modules.reporting.schemas import (
    Availability,
    MetricVector,
    ReportingFilter,
    ReportRow,
)
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    AccountReconciliationDay,
    AdReportDay,
    CampaignReportDay,
    ReportSyncState,
)


def bucket_key(vector: MetricVector) -> tuple[str]:
    # 所选日期已按各账户本地日解释，同币种跨时区仍可合计，标记 MIXED。
    return (vector.currency,)


def metric_vector(
    values: Mapping[str, Decimal | None] | RowMapping,
    *,
    currency: str,
    timezone: str,
    unsupported: Collection[str] = (),
    partial: Collection[str] = frozenset(),
) -> MetricVector:
    resolved: dict[str, Decimal | None] = {
        name: None if name in unsupported else values.get(name)
        for name in METRIC_COLUMNS
    }
    states = {
        name: "UNSUPPORTED"
        if name in unsupported
        else "MISSING"
        if value is None
        else "PARTIAL"
        if name in partial
        else "AVAILABLE"
        for name, value in resolved.items()
    }
    for name, numerator, denominator, scale in DERIVED_RATIOS:
        a, b = resolved.get(numerator), resolved.get(denominator)
        resolved[name] = a / b * scale if a is not None and b else None
        # 派生比率由合计重算，状态跟随分子、分母中较差的一个。
        states[name] = (
            "UNSUPPORTED"
            if numerator in unsupported or denominator in unsupported
            else "MISSING"
            if resolved[name] is None
            else "PARTIAL"
            if numerator in partial or denominator in partial
            else "AVAILABLE"
        )
    return MetricVector(
        currency=currency,
        timezone=timezone,
        attribution="default",
        values=resolved,
        availability=cast(dict[str, Availability], states),
    )


# 覆盖完整的状态集合；快照覆盖、行合并与汇总共用，避免各处口径不一致。
COMPLETE_COVERAGE_STATUSES = frozenset({"COMPLETE", "COMPLETE_EMPTY"})


def aggregate_metrics(
    vectors: Sequence[MetricVector], *, incomplete: bool = False
) -> MetricVector:
    """合计同币种向量（部分合计规则，设计 7.3）。

    行内合并（同一对象多天、剧下多账户/多系列）与汇总卡片用同一规则：值为空的
    成员不参与求和、也不当成 0；只要有成员缺失或本身是部分合计，或调用方声明
    有贡献成员覆盖不完整（incomplete），合计就标 PARTIAL。全部成员缺失仍为空。
    只有全部成员都不支持某列时该列才是 UNSUPPORTED；部分不支持按缺失处理。
    """
    if not vectors or len({v.currency for v in vectors}) != 1:
        raise ValueError("incompatible currencies or empty metrics")
    values: dict[str, Decimal | None] = {}
    unsupported: set[str] = set()
    partial_names: set[str] = set()
    for name in METRIC_COLUMNS:
        states = [v.availability.get(name) for v in vectors]
        if all(s == "UNSUPPORTED" for s in states):
            unsupported.add(name)
            values[name] = None
            continue
        present = [
            cast(Decimal, v.values.get(name))
            for v in vectors
            if v.values.get(name) is not None
            and v.availability.get(name) in ("AVAILABLE", "PARTIAL")
        ]
        values[name] = sum(present, Decimal(0)) if present else None
        if present and (
            incomplete or len(present) < len(vectors) or "PARTIAL" in states
        ):
            partial_names.add(name)
    zones = {v.timezone for v in vectors}
    return metric_vector(
        values,
        currency=vectors[0].currency,
        timezone=next(iter(zones)) if len(zones) == 1 else "MIXED",
        unsupported=unsupported,
        partial=partial_names,
    )


def merge_metric_buckets(
    members: Iterable[tuple[Sequence[MetricVector], bool]],
) -> tuple[MetricVector, ...]:
    """按口径键合并多个成员的指标桶；成员为 (向量, 覆盖是否不完整)。

    某币种桶只要有一个覆盖不完整的成员参与，桶内已有值的列都标 PARTIAL：
    SQL 只合计了已同步的日期，数字本身看起来完整，但并不是全量。
    """
    buckets: dict[tuple[str], list[MetricVector]] = defaultdict(list)
    incomplete: dict[tuple[str], bool] = defaultdict(bool)
    for vectors, member_incomplete in members:
        for vector in vectors:
            key = bucket_key(vector)
            buckets[key].append(vector)
            incomplete[key] = incomplete[key] or member_incomplete
    return tuple(
        aggregate_metrics(values, incomplete=incomplete[key])
        for key, values in sorted(buckets.items())
    )


def scoped_rows[ModelT: SQLModel](
    session: Session,
    model: type[ModelT],
    *,
    tenant_id: UUID,
    advertisers: Collection[str],
) -> Sequence[ModelT]:
    tenant_column, advertiser_column = [
        col(getattr(model, name)) for name in ("tenant_id", "advertiser_id")
    ]
    return session.exec(
        select(model).where(
            tenant_column == tenant_id, advertiser_column.in_(advertisers)
        )
    ).all()


def _aggregate(
    session: Session,
    *,
    model: type[Any],
    group_cols: Sequence[str],
    tenant_id: UUID,
    advertisers: Collection[str],
    start: date,
    end: date,
    ids: Iterable[str] | None = None,
    material_types: Collection[str] | None = None,
) -> list[RowMapping]:
    # 同一对象跨日按部分合计规则（设计 7.3）：只合计有值的日期，另带
    # "<列>_has_null" 标记该列是否有日期为空，由 fact_vector 标成 PARTIAL。
    columns = [col(getattr(model, name)) for name in group_cols]
    metadata = [
        func.array_agg(
            aggregate_order_by(
                col(getattr(model, name)),
                col(model.stat_date).desc(),
                col(model.fetched_at).desc(),
            )
        )
        .filter(col(getattr(model, name)).is_not(None))[1]
        .label(name)
        for name in (
            "campaign_id",
            "adgroup_id",
            "campaign_name",
            "adgroup_name",
            "ad_name",
            "main_material_name",
            "main_material_type",
        )
        if hasattr(model, name)
        and name not in group_cols
        and not (
            model.__tablename__ == "material_entity_day"
            and name == "main_material_name"
        )
    ]
    stmt = (
        sql_select(
            *columns,
            col(model.currency),
            func.min(col(model.timezone)).label("zone_min"),
            func.max(col(model.timezone)).label("zone_max"),
            *metadata,
            *[
                func.sum(col(getattr(model, name))).label(name)
                for name in METRIC_COLUMNS
            ],
            *[
                (
                    func.bool_or(col(getattr(model, f"{name}_has_null")))
                    | (func.count(col(getattr(model, name))) < func.count())
                    if hasattr(model, f"{name}_has_null")
                    else (func.count(col(getattr(model, name))) < func.count())
                ).label(f"{name}_has_null")
                for name in METRIC_COLUMNS
            ],
        )
        .where(
            model.tenant_id == tenant_id,
            col(model.advertiser_id).in_(advertisers),
            model.stat_date >= start,
            model.stat_date <= end,
            # 只有明确的素材消费者传类型；广告/系列与一般事实聚合维持原口径。
            *(
                []
                if material_types is None
                else [col(model.main_material_type).in_(material_types)]
            ),
            # 分页补全只读页面对象的事实。
            *([] if ids is None else [any_of(col(getattr(model, group_cols[1])), ids)]),
        )
        .group_by(*columns, col(model.currency))
    )
    return list(session.connection().execute(stmt).mappings().all())


def _coverage(
    states: Sequence[ReportSyncState],
    *,
    advertisers: Collection[str],
    filters: ReportingFilter,
    kind: str = "ad_day",
) -> dict[str, Any]:
    relevant = {
        (s.advertiser_id, s.stat_date): s
        for s in states
        if s.report_kind == kind and s.advertiser_id in advertisers
    }
    dates = [
        filters.start_date + timedelta(days=i)
        for i in range((filters.end_date - filters.start_date).days + 1)
    ]
    selected = [relevant.get((adv, day)) for adv in advertisers for day in dates]
    stale = sorted(
        {
            day.isoformat()
            for adv in advertisers
            for day in dates
            if (s := relevant.get((adv, day))) is None or s.status != "COMPLETE"
        }
    )
    failed = sorted(
        {s.stat_date.isoformat() for s in selected if s and s.status == "FAILED"}
    )
    updated = [s.last_success_at for s in selected if s and s.last_success_at]
    # 剧维度一行跨多个账户，汇总需要知道具体是哪些账户不完整，而不只看首个成员。
    incomplete_advertisers = sorted(
        {
            adv
            for adv in advertisers
            for day in dates
            if (s := relevant.get((adv, day))) is None or s.status != "COMPLETE"
        }
    )
    return {
        "status": "MISSING"
        if not any(selected)
        else "INCOMPLETE"
        if stale
        else "COMPLETE",
        "stale_dates": stale,
        "failed_dates": failed,
        "incomplete_advertiser_ids": incomplete_advertisers,
        "updated_at": max(updated).isoformat() if updated else None,
    }


def empty_metric_values(
    states: Sequence[ReportSyncState],
    *,
    advertiser: str,
    filters: ReportingFilter,
    kind: str = "ad_day",
) -> dict[str, Decimal | None]:
    # 用户裁定：完整覆盖的无事实对象才为零；缺覆盖与平台显式 NULL 不能填零。
    complete = (
        _coverage(states, advertisers={advertiser}, filters=filters, kind=kind)[
            "status"
        ]
        == "COMPLETE"
    )
    return {name: Decimal(0) if complete else None for name in METRIC_COLUMNS}


def any_of(column: Any, values: Iterable[str]) -> ColumnElement[bool]:
    """以单个数组参数匹配对象身份列表（column = ANY(:ids)）。

    报表页的系列/广告/素材身份来自查询结果，大租户一页即可涉及数万个；
    IN 列表逐项绑定会超过 PostgreSQL 65535 个参数上限而使整页失败。
    """
    return cast(
        ColumnElement[bool],
        column == any_(bindparam(None, sorted(set(values)), type_=ARRAY(Text))),
    )


def latest_projections(
    rows: Sequence[CampaignNameProjection],
) -> dict[tuple[str, str], CampaignNameProjection]:
    result: dict[tuple[str, str], CampaignNameProjection] = {}
    for row in rows:
        key = (row.advertiser_id, row.campaign_remote_id)
        if key not in result or (
            row.grouping_revision,
            row.name_revision,
            row.observed_at,
        ) > (
            result[key].grouping_revision,
            result[key].name_revision,
            result[key].observed_at,
        ):
            result[key] = row
    return result


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except ValueError, InvalidOperation:
        return None


def _text_number(value: Any) -> str | None:
    number = _number(value)
    return format(number, "f") if number is not None else None


def _config_display(entity: AdObject | None) -> dict[str, str | None]:
    """把目录配置转成报表可读字段，避免把原始 JSON 直接暴露到表格。"""
    if entity is None:
        return {}
    return {
        "target_roas": _text_number(entity.target_roas),
        "budget": _text_number(entity.budget_value),
        "budget_mode": entity.budget_mode or None,
        "optimization_goal": entity.optimization_goal or None,
    }


def _entity_name(
    objects: dict[tuple[str, str, str], AdObject],
    advertiser_id: str,
    kind: str,
    remote_id: str | None,
) -> str | None:
    if not remote_id:
        return None
    entity = objects.get((advertiser_id, kind, remote_id))
    return entity.name or remote_id if entity else remote_id


def _hierarchy_display(
    *,
    accounts: dict[str, AdvertiserAccount],
    objects: dict[tuple[str, str, str], AdObject],
    advertiser_id: str,
    kind: str,
    remote_id: str,
    entity: AdObject | None,
) -> dict[str, str | None]:
    """按当前维度只返回需要的固定层级列，父级缺失时保留 ID 线索。"""
    account = accounts.get(advertiser_id)
    display: dict[str, str | None] = {
        "account_id": advertiser_id,
        "account_name": account.name or advertiser_id if account else advertiser_id,
    }
    if kind == "account":
        return display
    campaign_id: str | None = remote_id if kind == "campaign" else None
    adgroup_id: str | None = remote_id if kind == "adgroup" else None
    current = entity
    if current is not None:
        if current.kind == "adgroup":
            campaign_id = current.parent_remote_id
        elif current.kind == "ad":
            adgroup_id = current.parent_remote_id
            parent = objects.get((advertiser_id, "adgroup", adgroup_id or ""))
            campaign_id = parent.parent_remote_id if parent else None
    if kind == "campaign":
        display["campaign_id"] = remote_id
        display["campaign_name"] = entity.name or remote_id if entity else remote_id
    elif kind == "adgroup":
        display.update(
            {
                "campaign_id": campaign_id,
                "campaign_name": _entity_name(
                    objects, advertiser_id, "campaign", campaign_id
                ),
                "adgroup_id": remote_id,
                "adgroup_name": entity.name or remote_id if entity else remote_id,
            }
        )
    elif kind == "ad":
        display.update(
            {
                "campaign_id": campaign_id,
                "campaign_name": _entity_name(
                    objects, advertiser_id, "campaign", campaign_id
                ),
                "adgroup_id": adgroup_id,
                "adgroup_name": _entity_name(
                    objects, advertiser_id, "adgroup", adgroup_id
                ),
                "ad_id": remote_id,
                "ad_name": entity.name or remote_id if entity else remote_id,
            }
        )
    config = _config_display(entity)
    if config.get("target_roas"):
        display["target_roas"] = config["target_roas"]
    if config.get("optimization_goal"):
        display["optimization_goal"] = config["optimization_goal"]
    if kind == "campaign" and config.get("budget"):
        display["campaign_budget"] = config["budget"]
    if kind == "adgroup" and config.get("budget"):
        display["adgroup_budget"] = config["budget"]
    return display


def fact_vector(
    item: Mapping[str, Any] | RowMapping, *, unsupported: Collection[str] = ()
) -> MetricVector:
    """把 _aggregate 的一行跨日合计转成指标向量；有日期为空的列标 PARTIAL。"""
    return metric_vector(
        item,
        currency=item["currency"],
        timezone=item["zone_min"] if item["zone_min"] == item["zone_max"] else "MIXED",
        unsupported=unsupported,
        partial={name for name in METRIC_COLUMNS if item[f"{name}_has_null"]},
    )


def ref_digest(refs: Iterable[EntityRef]) -> str:
    return sha256(
        json.dumps(
            sorted(
                (str(ref.tenant_id), ref.advertiser_id, ref.kind, ref.remote_id)
                for ref in refs
            )
        ).encode()
    ).hexdigest()


def build_rows(
    session: Session, *, context: TenantContext, bc_id: str, filters: ReportingFilter
) -> tuple[ReportRow, ...]:
    """完整有序结果：数据库算出成员与顺序，再逐行补全展示与指标。"""
    compile_filter(filters)
    advertisers = [
        g.advertiser_id
        for g in authorized_grants(
            session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
        )
    ]
    members = ordered_members(
        session,
        tenant_id=context.tenant_id,
        advertisers=advertisers,
        bc_id=bc_id,
        filters=filters,
    )
    return materialize_rows(
        session,
        tenant_id=context.tenant_id,
        filters=filters,
        members=members,
        advertisers=advertisers,
    )


@query_diagnostics.timed("hydrate")
def materialize_rows(
    session: Session,
    *,
    tenant_id: UUID,
    filters: ReportingFilter,
    members: Sequence[SnapshotMember],
    advertisers: Sequence[str],
) -> tuple[ReportRow, ...]:
    """按快照冻结的成员与顺序补全展示字段、指标和覆盖；不再做筛选与排序。

    advertisers 为快照冻结的授权账户：素材行跨账户合计，只在这些账户内取事实。

    只读取这些行涉及的账户、对象和事实，翻页与分批导出的成本与页大小成正比。
    """
    if not members:
        return ()
    if filters.dimension == "material":
        from app.modules.reporting.material_query import materialize_material_rows

        return materialize_material_rows(
            session,
            tenant_id=tenant_id,
            filters=filters,
            members=members,
            advertisers=advertisers,
        )
    kind = "campaign" if filters.dimension == "drama" else filters.dimension
    row_refs = {
        member.row_key: tuple(entity_ref(ref) for ref in member.refs)
        for member in members
    }
    # 账户行的 refs 是冻结的系列集合，行身份是账户本身。
    identities = {
        member.row_key: (
            ((member.row_key.split(":account:", 1)[0],) * 2,)
            if kind == "account"
            else tuple(
                (ref.advertiser_id, ref.remote_id) for ref in row_refs[member.row_key]
            )
        )
        for member in members
    }
    advertisers = sorted({adv for ids in identities.values() for adv, _ in ids})
    member_ids = sorted({rid for ids in identities.values() for _, rid in ids})
    accounts = {
        a.advertiser_id: a
        for a in scoped_rows(
            session, AdvertiserAccount, tenant_id=tenant_id, advertisers=advertisers
        )
    }
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == tenant_id,
            col(ReportSyncState.advertiser_id).in_(advertisers),
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
        )
    ).all()
    model = (
        CampaignReportDay
        if kind in {"account", "campaign"}
        else AdgroupReportDay
        if kind == "adgroup"
        else AdReportDay
    )
    group_cols = ["advertiser_id"] + ([] if kind == "account" else [f"{kind}_id"])
    grouped_facts = _aggregate(
        session,
        model=model,
        group_cols=group_cols,
        tenant_id=tenant_id,
        advertisers=advertisers,
        start=filters.start_date,
        end=filters.end_date,
        ids=None if kind == "account" else member_ids,
    )
    facts = defaultdict(list)
    for fact in grouped_facts:
        adv = fact["advertiser_id"]
        facts[(adv, adv if kind == "account" else fact[f"{kind}_id"])].append(fact)
    # 目录缺失时父级身份来自报表元数据，名称仍优先取目录对象。
    parents = {
        parent: {
            item[f"{parent}_id"]
            for items in facts.values()
            for item in items
            if item.get(f"{parent}_id")
        }
        for parent in ("adgroup", "campaign")
    }
    objects = _page_objects(
        session,
        tenant_id=tenant_id,
        advertisers=advertisers,
        kind=kind,
        ids=member_ids,
        parents=parents,
    )
    # 轮播完整身份只在当前广告页补全；全候选筛选和排序始终只读窄投影。
    carousels = (
        {
            (entity.advertiser_id, entity.remote_id): directory_carousels(entity)
            for entity in session.exec(
                select(CanonicalAdObject).where(
                    CanonicalAdObject.tenant_id == tenant_id,
                    col(CanonicalAdObject.advertiser_id).in_(advertisers),
                    CanonicalAdObject.kind == "ad",
                    any_of(col(CanonicalAdObject.remote_id), member_ids),
                )
            ).all()
        }
        if kind == "ad"
        else {}
    )
    campaign_ids = (
        {o.remote_id for (_, k, _), o in objects.items() if k == "campaign"}
        | {
            o.parent_remote_id
            for (_, k, _), o in objects.items()
            if k == "adgroup" and o.parent_remote_id
        }
        | {
            item["campaign_id"]
            for items in facts.values()
            for item in items
            if item.get("campaign_id")
        }
    )
    projections = latest_projections(
        session.exec(
            select(CampaignNameProjection).where(
                CampaignNameProjection.tenant_id == tenant_id,
                col(CampaignNameProjection.advertiser_id).in_(advertisers),
                any_of(col(CampaignNameProjection.campaign_remote_id), campaign_ids),
            )
        ).all()
        if campaign_ids
        else []
    )
    reconciliations = (
        session.exec(
            select(AccountReconciliationDay).where(
                AccountReconciliationDay.tenant_id == tenant_id,
                col(AccountReconciliationDay.advertiser_id).in_(advertisers),
                AccountReconciliationDay.stat_date >= filters.start_date,
                AccountReconciliationDay.stat_date <= filters.end_date,
            )
        ).all()
        if kind == "account"
        else []
    )
    balances: dict[str, AccountBalanceObservation] = {}
    if kind == "account":
        for observation in session.exec(
            select(AccountBalanceObservation)
            .where(
                AccountBalanceObservation.tenant_id == tenant_id,
                col(AccountBalanceObservation.advertiser_id).in_(advertisers),
            )
            .distinct(col(AccountBalanceObservation.advertiser_id))
            .order_by(
                col(AccountBalanceObservation.advertiser_id),
                col(AccountBalanceObservation.observed_at).desc(),
                col(AccountBalanceObservation.id).desc(),
            )
        ):
            balances.setdefault(observation.advertiser_id, observation)
    # 覆盖状态只取决于账户集合与日期：按账户集合缓存，避免每行重复扫描同步状态。
    coverage_cache: dict[frozenset[str], dict[str, Any]] = {}

    def coverage_for(members: Collection[str]) -> dict[str, Any]:
        cache_key = frozenset(members)
        if cache_key not in coverage_cache:
            coverage_cache[cache_key] = _coverage(
                states, advertisers=cache_key, filters=filters
            )
        return dict(coverage_cache[cache_key])

    empty_cache: dict[str, dict[str, Decimal | None]] = {}

    def empty_values_for(adv: str) -> dict[str, Decimal | None]:
        if adv not in empty_cache:
            complete = coverage_for({adv})["status"] == "COMPLETE"
            # 用户裁定：完整覆盖的无事实对象才为零；缺覆盖不能填零。
            empty_cache[adv] = {
                name: Decimal(0) if complete else None for name in METRIC_COLUMNS
            }
        return empty_cache[adv]

    result = []
    for member in members:
        rows: list[ReportRow] = []
        statuses: list[str | None] = []
        providers: list[str] = []
        for adv, rid in identities[member.row_key]:
            entity = objects.get((adv, kind, rid))
            items = facts.get((adv, rid), [])
            metadata = items[0] if items else {}
            campaign_id = rid if kind == "campaign" else metadata.get("campaign_id")
            if entity and kind == "adgroup":
                campaign_id = entity.parent_remote_id or campaign_id
            if entity and kind == "ad":
                parent = objects.get((adv, "adgroup", entity.parent_remote_id or ""))
                campaign_id = parent.parent_remote_id if parent else campaign_id
            projection = (
                projections.get((adv, campaign_id))
                if isinstance(campaign_id, str)
                else None
            )
            rows.append(
                _member_row(
                    kind=kind,
                    filters=filters,
                    accounts=accounts,
                    objects=objects,
                    adv=adv,
                    rid=rid,
                    entity=entity,
                    items=items,
                    projection=projection,
                    balance=balances.get(adv),
                    reconciliations=[
                        r for r in reconciliations if r.advertiser_id == adv
                    ],
                    empty_values=empty_values_for(adv),
                    carousel=carousels.get((adv, rid)),
                )
            )
            statuses.append(entity.operation_status if entity else None)
            if projection and projection.provider_label:
                providers.append(projection.provider_label)
        refs = row_refs[member.row_key]
        coverage = coverage_for({adv for adv, _ in identities[member.row_key]})
        incomplete_ids = set(cast(list[str], coverage["incomplete_advertiser_ids"]))
        row = rows[0].model_copy(
            update={
                "row_key": member.row_key,
                "refs": refs,
                # 行内合并与汇总同一规则：覆盖不完整账户贡献的列标 PARTIAL。
                "metric_buckets": merge_metric_buckets(
                    (r.metric_buckets, r.display.get("advertiser_id") in incomplete_ids)
                    for r in rows
                ),
                "coverage": coverage,
                "membership_digest": ref_digest(refs),
                "directory_versions": {
                    k: v for r in rows for k, v in r.directory_versions.items()
                },
            }
        )
        if filters.dimension == "drama":
            row = row.model_copy(
                update={
                    "display": _drama_status_display(row.display, statuses, providers)
                }
            )
        result.append(row)
    return tuple(result)


def _page_objects(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    kind: str,
    ids: Sequence[str],
    parents: Mapping[str, set[str]],
) -> dict[tuple[str, str, str], AdObject]:
    """只读取页面对象及其上级（广告 -> 广告组 -> 系列），不加载整个目录。"""
    if kind == "account":
        return {}
    objects: dict[tuple[str, str, str], AdObject] = {}
    levels = ("ad", "adgroup", "campaign")
    above = levels[levels.index(kind) + 1 :]
    wanted = {kind: set(ids)} | {level: set(parents.get(level, ())) for level in above}
    for current, parent in (
        ("ad", "adgroup"),
        ("adgroup", "campaign"),
        ("campaign", None),
    ):
        remote_ids = wanted.get(current)
        if not remote_ids:
            continue
        for obj in session.exec(
            select(AdObject).where(
                AdObject.tenant_id == tenant_id,
                col(AdObject.advertiser_id).in_(advertisers),
                AdObject.kind == current,
                any_of(col(AdObject.remote_id), remote_ids),
            )
        ).all():
            objects[(obj.advertiser_id, obj.kind, obj.remote_id)] = obj
            if parent and obj.parent_remote_id:
                wanted.setdefault(parent, set()).add(obj.parent_remote_id)
    return objects


def _member_row(
    *,
    kind: str,
    filters: ReportingFilter,
    accounts: dict[str, AdvertiserAccount],
    objects: dict[tuple[str, str, str], AdObject],
    adv: str,
    rid: str,
    entity: AdObject | None,
    items: Sequence[Mapping[str, Any] | RowMapping],
    projection: CampaignNameProjection | None,
    balance: AccountBalanceObservation | None,
    reconciliations: Sequence[AccountReconciliationDay],
    empty_values: Mapping[str, Decimal | None],
    carousel: CarouselDirectory | None = None,
) -> ReportRow:
    metadata = items[0] if items else {}
    name = (
        accounts[adv].name or rid
        if kind == "account"
        else (
            entity.name
            if entity and entity.name
            else metadata.get(f"{kind}_name") or rid
        )
    )
    display = _hierarchy_display(
        accounts=accounts,
        objects=objects,
        advertiser_id=adv,
        kind=kind,
        remote_id=rid,
        entity=entity,
    )
    # 目录尚未同步时以报表中的父级身份与名称补齐，不能丢弃已删除广告的历史消耗。
    for parent_kind in ("campaign", "adgroup", "ad"):
        if parent_kind not in display and f"{parent_kind}_id" not in display:
            continue
        parent_id = display.get(f"{parent_kind}_id") or metadata.get(
            f"{parent_kind}_id"
        )
        if parent_id:
            obj = objects.get((adv, parent_kind, parent_id))
            display[f"{parent_kind}_id"] = parent_id
            display[f"{parent_kind}_name"] = (
                obj.name
                if obj and obj.name
                else metadata.get(f"{parent_kind}_name") or parent_id
            )
    if filters.dimension == "drama":
        name = (
            projection.drama_name
            if projection and projection.status == "VALID"
            else name
        )
        display.update(drama_name=name)
    display.update(
        name=name,
        remote_id=rid,
        advertiser_id=adv,
        provider=projection.provider_label if projection else None,
        naming_status=projection.status if projection else "INVALID",
        ad_type=entity.ad_type if entity else None,
        operation_status=entity.operation_status if entity else None,
        review_status=entity.review_status if entity else None,
        secondary_status=entity.delivery_status if entity else None,
    )
    if kind == "account":
        # 余额是独立时点观测；资金池余额必须标记共享，不纳入期间指标合计。
        display.update(
            account_status=accounts[adv].remote_status,
            balance=str(balance.amount)
            if balance
            and balance.availability == "AVAILABLE"
            and balance.amount is not None
            else None,
            balance_currency=balance.currency if balance else None,
            balance_scope=balance.balance_scope if balance else "UNKNOWN",
            balance_updated_at=balance.observed_at.isoformat() if balance else None,
        )
    if any(
        a is not None and b is not None and abs(a - b) > Decimal("0.01")
        for r in reconciliations
        for a, b in (
            (r.account_spend, r.ad_sum_spend),
            (r.account_revenue_total, r.ad_sum_revenue_total),
        )
    ):
        display["reconciliation"] = "DIFF"
        display["account_report_spend"] = (
            str(
                sum(
                    (cast(Decimal, r.account_spend) for r in reconciliations),
                    Decimal(0),
                )
            )
            if all(r.account_spend is not None for r in reconciliations)
            else None
        )
    if carousel and carousel.is_carousel:
        display.update(
            creative_format="CAROUSEL_ADS",
            metric_scope="AD",
            creative_linkage="COMPLETE" if carousel.complete else "INCOMPLETE",
        )
    vectors = tuple(fact_vector(item) for item in items) or (
        metric_vector(
            empty_values,
            currency=accounts[adv].currency,
            timezone=accounts[adv].timezone,
        ),
    )
    return ReportRow(
        row_key=f"{adv}:{kind}:{rid}",
        carousel_creatives=carousel.identities if carousel else (),
        display=display,
        metric_buckets=vectors,
        directory_versions={f"{adv}:{rid}": entity.published_version} if entity else {},
    )


def _drama_status_display(
    display: dict[str, Any], statuses: list[str | None], providers: list[str]
) -> dict[str, Any]:
    """剧行状态：任一成员系列开关开启即"已开启"，否则全部暂停为"已暂停"，全部删除为"已删除"。

    系列开关开启只代表 TikTok 侧开关打开，不代表正在花钱；开启数量一并给出，
    让投手看清是几个系列开着。审核与投放细分状态在系列维度查看。
    """
    enabled = sum(1 for status in statuses if status == "ENABLE")
    if enabled:
        operation = "ENABLE"
    elif any(status == "DISABLE" for status in statuses):
        operation = "DISABLE"
    elif statuses and all(status == "DELETE" for status in statuses):
        operation = "DELETE"
    else:
        operation = None
    return {
        **display,
        # 版权方直接显示系列名第一段；同一部剧各系列第一段不同时全部列出，按出现顺序去重。
        "provider": "、".join(dict.fromkeys(providers)) or None,
        "operation_status": operation,
        "secondary_status": None,
        "review_status": None,
        "campaign_count": str(len(statuses)),
        "enabled_campaign_count": str(enabled),
    }
