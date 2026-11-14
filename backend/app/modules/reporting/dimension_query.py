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

from sqlalchemy import RowMapping, Text, any_, bindparam, func, text, true
from sqlalchemy import select as sql_select
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import ColumnElement
from sqlmodel import Session, SQLModel, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.carousel import CarouselDirectory
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.directory import directory_carousels
from app.modules.ads.models import AdObject as CanonicalAdObject
from app.modules.reporting import query_diagnostics
from app.modules.reporting.balance_queries import latest_balance_observations
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.levels import PARENT_KINDS, query_kind, state_sql
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, NATIVE_CONTRACT
from app.modules.reporting.models import AccountBalanceObservation
from app.modules.reporting.read_models.models import AdEntityRead as AdObject
from app.modules.reporting.read_models.models import AdgroupReportDay
from app.modules.reporting.read_models.models import (
    CampaignNameCurrent as CampaignNameProjection,
)
from app.modules.reporting.row_set import SnapshotMember, entity_ref, ordered_members
from app.modules.reporting.schemas import (
    Availability,
    BucketStamp,
    MetricProvenance,
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
    identities: Collection[tuple[str, str]] | None = None,
) -> list[RowMapping]:
    source = None
    wanted = None
    if identities is not None:
        scope = set(advertisers)
        pairs = sorted({pair for pair in identities if pair[0] in scope})
        if not pairs:
            return []
        wanted = (
            func.unnest(
                bindparam(None, [adv for adv, _ in pairs], type_=ARRAY(Text)),
                bindparam(None, [remote for _, remote in pairs], type_=ARRAY(Text)),
            )
            .table_valued("adv", "rid")
            .render_derived()
        )
        source = (
            sql_select(model)
            .where(
                model.tenant_id == tenant_id,
                model.advertiser_id == wanted.c.adv,
                getattr(model, group_cols[1]) == wanted.c.rid,
                model.stat_date >= start,
                model.stat_date <= end,
            )
            .offset(0)
            .correlate(wanted)
            .lateral()
        )
        model = aliased(model, source)
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
        )
        if hasattr(model, name) and name not in group_cols
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
            # 分页补全只读页面对象的事实。
            *([] if ids is None else [any_of(col(getattr(model, group_cols[1])), ids)]),
        )
        .group_by(*columns, col(model.currency))
    )
    if source is not None and wanted is not None:
        stmt = stmt.select_from(wanted).join(source, true())
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
        **getattr(entity, "_report_config", {}),
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
    for key in ("optimization_event", "deep_bid_type"):
        if config.get(key):
            display[key] = config[key]
    if kind == "ad" and adgroup_id:
        parent_config = _config_display(
            objects.get((advertiser_id, "adgroup", adgroup_id))
        )
        for key in (
            "target_roas",
            "optimization_goal",
            "optimization_event",
            "deep_bid_type",
        ):
            if parent_config.get(key):
                display[key] = parent_config[key]
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
            (
                (
                    member.row_key.split(":account:", 1)[0],
                    member.row_key.split(":account:", 1)[0],
                ),
            )
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
    if query_kind(filters.dimension) in PARENT_KINDS:
        states = [
            ReportSyncState.model_validate(r)
            for r in session.connection()
            .execute(
                text(
                    f"SELECT * FROM ({state_sql(query_kind(filters.dimension))}) selected WHERE tenant_id=:tenant AND advertiser_id=ANY(:advs) AND stat_date BETWEEN :start AND :end"
                ),
                {
                    "tenant": tenant_id,
                    "advs": advertisers,
                    "start": filters.start_date,
                    "end": filters.end_date,
                },
            )
            .mappings()
        ]
    parent_model = CampaignReportDay if kind == "campaign" else AdgroupReportDay
    model = (
        aliased(
            parent_model,
            text(f"SELECT * FROM {kind}_selected_day")
            .columns(*cast(Any, parent_model).__table__.columns)
            .subquery(),
            adapt_on_names=True,
        )
        if query_kind(filters.dimension) in PARENT_KINDS
        else CampaignReportDay
        if kind == "account"
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
        identities=None
        if kind == "account"
        else {identity for pairs in identities.values() for identity in pairs},
    )
    facts = defaultdict(list)
    for fact in grouped_facts:
        adv = fact["advertiser_id"]
        facts[(adv, adv if kind == "account" else fact[f"{kind}_id"])].append(fact)
    # 目录缺失时父级身份来自报表元数据，名称仍优先取目录对象。
    parents = {
        parent: {
            (item["advertiser_id"], item[f"{parent}_id"])
            for items in facts.values()
            for item in items
            if item.get(f"{parent}_id")
        }
        for parent in ("adgroup", "campaign")
    }
    objects = _page_objects(
        session,
        tenant_id=tenant_id,
        kind=kind,
        parents=parents,
        identities=tuple(
            {identity for pairs in identities.values() for identity in pairs}
        ),
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
        for observation in latest_balance_observations(
            session, tenant_id=tenant_id, advertisers=advertisers
        ):
            balances.setdefault(observation.advertiser_id, observation)
    # 当前页面所有非素材维度共用ad_day覆盖；预索引一次，后续单账户或剧行
    # 只遍历自身日期，避免N个账户各重扫整个页面的N×日期×报表种类状态。
    states_by_adv: dict[str, list[ReportSyncState]] = defaultdict(list)
    for state in states:
        if state.report_kind == (query_kind(filters.dimension)):
            states_by_adv[state.advertiser_id].append(state)
    # 覆盖状态只取决于账户集合与日期，同一账户集合复用计算结果。
    coverage_cache: dict[frozenset[str], dict[str, Any]] = {}

    def coverage_for(members: Collection[str]) -> dict[str, Any]:
        cache_key = frozenset(members)
        if cache_key not in coverage_cache:
            coverage_cache[cache_key] = _coverage(
                [state for adv in cache_key for state in states_by_adv[adv]],
                advertisers=cache_key,
                filters=filters,
                kind=query_kind(filters.dimension),
            )
        return dict(coverage_cache[cache_key])

    empty_cache: dict[str, dict[str, Decimal | None]] = {}

    def empty_values_for(adv: str) -> dict[str, Decimal | None]:
        if adv not in empty_cache:
            complete = coverage_for({adv})["status"] == "COMPLETE"
            # 用户裁定：完整覆盖的无事实对象才为零；缺覆盖不能填零。
            empty_cache[adv] = {
                name: Decimal(0)
                if complete
                and not (
                    query_kind(filters.dimension) in PARENT_KINDS
                    and name.startswith("revenue")
                )
                else None
                for name in METRIC_COLUMNS
            }
        return empty_cache[adv]

    result = []
    for member in members:
        rows: list[ReportRow] = []
        drama_vectors: list[tuple[str, tuple[MetricVector, ...]]] = []
        drama_versions: dict[str, int] = {}
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
            if filters.dimension == "drama" and rows:
                # 剧行只展示首成员信息；后续成员无需构建层级display与完整ReportRow。
                # 指标仍走相同fact_vector/空值规则，避免节省对象时改变部分合计口径。
                drama_vectors.append(
                    (adv, _member_vectors(items, empty_values_for(adv), accounts, adv))
                )
                if entity:
                    # 原_member_row只记录成员自身目录版本，不增加其父级版本。
                    drama_versions[f"{adv}:{rid}"] = entity.published_version
            else:
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
                if filters.dimension == "drama":
                    drama_vectors.append((adv, rows[-1].metric_buckets))
                    drama_versions.update(rows[-1].directory_versions)
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
                    ((vectors, adv in incomplete_ids) for adv, vectors in drama_vectors)
                    if filters.dimension == "drama"
                    else (
                        (
                            r.metric_buckets,
                            r.display.get("advertiser_id") in incomplete_ids,
                        )
                        for r in rows
                    )
                ),
                "coverage": coverage,
                "membership_digest": ref_digest(refs),
                "directory_versions": drama_versions
                if filters.dimension == "drama"
                else {k: v for r in rows for k, v in r.directory_versions.items()},
            }
        )
        if filters.dimension == "drama":
            row = row.model_copy(
                update={
                    "display": _drama_status_display(row.display, statuses, providers)
                }
            )
        if filters.dimension in {"campaign", "adgroup", "ad"}:
            row = report_provenance(session, row, tenant_id=tenant_id, filters=filters)
        result.append(row)
    return tuple(result)


def _page_objects(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    parents: Mapping[str, set[tuple[str, str]]],
    identities: Sequence[tuple[str, str]],
) -> dict[tuple[str, str, str], AdObject]:
    """只读取页面对象及其上级（广告 -> 广告组 -> 系列），不加载整个目录。"""
    if kind == "account":
        return {}
    objects: dict[tuple[str, str, str], AdObject] = {}
    levels = ("ad", "adgroup", "campaign")
    above = levels[levels.index(kind) + 1 :]
    wanted = {kind: set(identities)} | {
        level: set(parents.get(level, ())) for level in above
    }
    for current, parent in (
        ("ad", "adgroup"),
        ("adgroup", "campaign"),
        ("campaign", None),
    ):
        pairs = sorted(wanted.get(current, ()))
        if not pairs:
            continue
        statement = sql_select(AdObject).from_statement(
            text("""
            SELECT entity.*
            FROM unnest(CAST(:advertisers AS text[]), CAST(:ids AS text[])) wanted(adv, rid)
            CROSS JOIN LATERAL (
                SELECT obj.* FROM ad_entity_read obj
                WHERE obj.tenant_id=:tenant AND obj.advertiser_id=wanted.adv
                    AND obj.kind=:kind AND obj.remote_id=wanted.rid OFFSET 0
            ) entity
            """)
        )
        for obj in session.scalars(
            statement,
            {
                "tenant": tenant_id,
                "kind": current,
                "advertisers": [advertiser for advertiser, _ in pairs],
                "ids": [remote_id for _, remote_id in pairs],
            },
        ).all():
            objects[(obj.advertiser_id, obj.kind, obj.remote_id)] = obj
            if parent and obj.parent_remote_id:
                wanted.setdefault(parent, set()).add(
                    (obj.advertiser_id, obj.parent_remote_id)
                )
    # 新增展示项只在当前页广告组按完整主键补读，避免为全候选扩宽报表投影。
    group_pairs = sorted((adv, rid) for adv, k, rid in objects if k == "adgroup")
    if group_pairs:
        metadata = (
            cast(SASession, session)
            .execute(
                text("""
            SELECT obj.advertiser_id,obj.remote_id,obj.published_version,
                obj.configuration->>'optimization_event' AS optimization_event,
                obj.configuration->>'deep_bid_type' AS deep_bid_type
            FROM unnest(CAST(:advertisers AS text[]),CAST(:ids AS text[])) wanted(adv,rid)
            CROSS JOIN LATERAL (SELECT obj.* FROM ad_object obj
                WHERE obj.tenant_id=:tenant AND obj.advertiser_id=wanted.adv
                AND obj.kind='adgroup' AND obj.remote_id=wanted.rid OFFSET 0) obj
        """),
                {
                    "tenant": tenant_id,
                    "advertisers": [a for a, _ in group_pairs],
                    "ids": [r for _, r in group_pairs],
                },
            )
            .mappings()
            .all()
        )
        for item in metadata:
            obj = objects[(item["advertiser_id"], "adgroup", item["remote_id"])]
            if obj.published_version != item["published_version"]:
                continue
            object.__setattr__(
                obj,
                "_report_config",
                {key: item[key] for key in ("optimization_event", "deep_bid_type")},
            )
    return objects


def _member_vectors(
    items: Sequence[Mapping[str, Any] | RowMapping],
    empty_values: Mapping[str, Decimal | None],
    accounts: Mapping[str, AdvertiserAccount],
    advertiser_id: str,
) -> tuple[MetricVector, ...]:
    """完整成员展示与剧行轻量成员共用同一指标构造口径。"""
    return tuple(fact_vector(item) for item in items) or (
        metric_vector(
            empty_values,
            currency=accounts[advertiser_id].currency,
            timezone=accounts[advertiser_id].timezone,
        ),
    )


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
    vectors = _member_vectors(items, empty_values, accounts, adv)
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


def report_provenance(
    session: Session, row: ReportRow, *, tenant_id: UUID, filters: ReportingFilter
) -> ReportRow:
    """只读取当前页对象的证据；数值/筛选由共同 SQL selected view 决定。"""
    ref = row.refs[0]
    level = ref.kind
    # 两个固定已知表，不接受用户提供 SQL 标识符。
    source_table = (
        "parent_report_day" if f"{level}_day" in PARENT_KINDS else "ad_report_day"
    )
    identity = (
        f"p.level='{level}' AND p.remote_id=:rid"
        if f"{level}_day" in PARENT_KINDS
        else "p.ad_id=:rid"
    )
    records = (
        session.connection()
        .execute(
            text(f"""
        SELECT s.stat_date,s.observed_at,s.publication_version,s.last_success_at,s.status,s.active_contract,p.evidence
        FROM report_sync_state s LEFT JOIN {source_table} p
          ON p.tenant_id=s.tenant_id AND p.advertiser_id=s.advertiser_id
          AND p.stat_date=s.stat_date AND {identity}
        WHERE s.tenant_id=:tenant AND s.advertiser_id=:adv
          AND s.report_kind=:kind AND s.last_success_at IS NOT NULL
          AND s.stat_date BETWEEN :start AND :end ORDER BY s.stat_date
    """),
            {
                "tenant": tenant_id,
                "adv": ref.advertiser_id,
                "rid": ref.remote_id,
                "kind": f"{level}_day",
                "start": filters.start_date,
                "end": filters.end_date,
            },
        )
        .mappings()
        .all()
    )
    daily: list[dict[str, Any]] = [
        {
            "local_day": str(r["stat_date"]),
            "observed_at": r["observed_at"].isoformat() if r["observed_at"] else None,
            "publication_version": r["publication_version"],
            "fields": r["evidence"]["fields"] if r["evidence"] else {},
            "reason": None
            if r["evidence"]
            else "LEGACY_UNKNOWN"
            if level == "ad"
            else "NO_RETURNED_ROW",
        }
        for r in records
    ]
    source = "PLATFORM_DIRECT" if records or level == "ad" else "CHILD_DERIVED"
    revision = (
        records[0]["active_contract"]
        if records and records[0]["active_contract"]
        else NATIVE_CONTRACT
        if any(r["evidence"] for r in records)
        else "LEGACY_UNKNOWN"
    )
    if level == "ad" and not any(r["evidence"] for r in records):
        revision = "LEGACY_UNKNOWN"
    scope = sha256(
        json.dumps(
            [
                str(tenant_id),
                ref.advertiser_id,
                level,
                ref.remote_id,
                str(filters.start_date),
                str(filters.end_date),
            ]
        ).encode()
    ).hexdigest()
    stamps = tuple(
        BucketStamp(
            advertiser_id=ref.advertiser_id,
            level=cast(Any, level),
            stat_date=r["stat_date"],
            contract_revision=revision,
            publication_version=r["publication_version"],
            observed_at=r["observed_at"],
            published_at=r["last_success_at"],
            last_attempt_status=r["status"],
        )
        for r in records
    )
    provenance = {}
    for name in row.metric_buckets[0].values if row.metric_buckets else ():
        counts: dict[str, int] = {}
        ratio = next((r for r in DERIVED_RATIOS if r[0] == name), None)
        source_fields = (ratio[1], ratio[2]) if ratio else (name,)
        for observation in daily:
            for source_field in source_fields:
                field_state = (
                    observation["fields"]
                    .get(source_field, {})
                    .get("status", "LEGACY_UNKNOWN")
                )
                counts[field_state] = counts.get(field_state, 0) + 1
        reason = (
            "NO_RETURNED_ROW"
            if records
            and not any(r["evidence"] for r in records)
            and f"{level}_day" in PARENT_KINDS
            else "LEGACY_UNKNOWN"
            if revision == "LEGACY_UNKNOWN"
            else None
        )
        provenance[name] = MetricProvenance(
            level=cast(Any, level),
            source_kind=cast(Any, source),
            contract_revision=revision,
            scope_digest=scope,
            buckets=stamps,
            field_state_counts=counts,
            reason=reason,
            formula="SAME_SCOPE_REVENUE / SAME_SCOPE_SPEND"
            if name.startswith("roas_")
            else None,
        )
    display = dict(row.display)
    from app.modules.reporting.levels import DAY_KINDS, LEVELS
    from app.modules.reporting.metrics import PLATFORM_RATIOS

    layer_states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == tenant_id,
            ReportSyncState.advertiser_id == ref.advertiser_id,
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
            col(ReportSyncState.report_kind).in_(DAY_KINDS),
        )
    ).all()
    indexed = {(r.report_kind, r.stat_date): r for r in layer_states}
    layer_lines = []
    for kind in DAY_KINDS:
        day = filters.start_date
        while day <= filters.end_date:
            state = indexed.get((kind, day))
            layer_lines.append(
                f"{kind} {day}: {state.status if state else 'MISSING'}; "
                f"成功 {state.last_success_at if state else None}; "
                f"尝试 {state.updated_at if state else None}; "
                f"观测 {state.observed_at if state else None}; "
                f"版本 {state.publication_version if state else 0}; "
                f"合同 {state.active_contract if state else None}; "
                f"原因 {state.error_code if state else None}"
            )
            day += timedelta(days=1)
    expected_buckets = len(DAY_KINDS) * (
        (filters.end_date - filters.start_date).days + 1
    )
    refresh_status = (
        "COMPLETE"
        if len(indexed) == expected_buckets
        and all(r.status == "COMPLETE" for r in layer_states)
        else "PARTIAL"
        if any(r.last_success_at is not None for r in layer_states)
        else "MISSING"
    )
    display["report_refresh_status"] = refresh_status
    display["report_layer_status"] = "\n".join(
        [f"刷新概览 {refresh_status}", *layer_lines]
    )
    display.update(
        report_source=source,
        report_local_days=f"{filters.start_date} — {filters.end_date}",
        report_window="D0 点击后24h；total成熟截止未知",
        report_semantics="UNCONFIRMED · 自然＋付费口径待确认 · 非结算",
        report_observed_at=" / ".join(str(r["observed_at"]) for r in records)
        or "LEGACY_UNKNOWN",
    )
    for key in ("platform_roas_total", "platform_roas_d0"):
        field = (
            daily[0]["fields"].get(key, {})
            if len(daily) == 1 and filters.start_date == filters.end_date
            else {}
        )
        display[key] = field.get("value")
        display[key + "_reason"] = (
            "NOT_REQUESTED"
            if PLATFORM_RATIOS[key] not in LEVELS[f"{level}_day"].metrics
            else field.get("status", "NON_ADDITIVE" if daily else "LEGACY_UNKNOWN")
        )
    return row.model_copy(
        update={
            "display": display,
            "metric_buckets": tuple(
                v.model_copy(update={"provenance": provenance})
                for v in row.metric_buckets
            ),
        }
    )
