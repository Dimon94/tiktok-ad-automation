"""报表快照成员在数据库内一次算出：身份、筛选、排序、汇总都不经过 Python。

快照只冻结"有哪些行、按什么顺序、每行管理哪些对象"：单对象身份键、
剧成员二元组或不可变成员版本；展示字段与指标在翻页、导出时只对用到的行计算
（见 dimension_query.materialize_rows）。这样快照成本与总行数近似线性且只发生在
数据库内，几十万行也不需要把全部数据搬进应用进程。

口径与原 Python 实现一致：
- 身份 = 所选日期有事实的对象 ∪ 目录中该维度的全部对象（账户维度为全部授权账户）。
- 无事实对象：账户所选日期全部同步完成才填 0，否则为空（用户裁定，见
  dimension_query.empty_metric_values）。
- 同一行按币种分桶；金额类排序先比较币种，多币种行与无值行排在末尾，
  并列时按 row_key 升序，保证翻页稳定。
- 关键词搜索按页面约定匹配"名称、剧名、版权方或 ID"，多关键词 AND。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.reporting import query_diagnostics
from app.modules.reporting.filters import CONFIG_SORT_FIELDS, NAME_SORT_FIELDS
from app.modules.reporting.levels import PARENT_KINDS, query_kind, state_sql
from app.modules.reporting.metric_catalog import NATIVE_CONTRACT
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import METRIC_COLUMNS

# 派生比率：名称 -> (分子, 分母, 倍数)，与 dimension_query.metric_vector 一致。
DERIVED_METRICS: dict[str, tuple[str, str, int]] = {
    **{
        f"roas_{d}": (f"revenue_{d}", "spend", 1)
        for d in ("d0", "d3", "d7", "d14", "d30", "total")
    },
    "ctr": ("clicks", "impressions", 1),
    "cpm": ("spend", "impressions", 1000),
    "cpc": ("spend", "clicks", 1),
    "cost_per_ad_impression_event": ("spend", "ad_impression_events", 1),
}
# 金额类指标排序先按币种分组，避免不同币种金额直接比较。
MONEY_SORT_FIELDS = frozenset(
    {
        "spend",
        "revenue_d0",
        "revenue_d3",
        "revenue_d7",
        "revenue_d14",
        "revenue_d30",
        "revenue_total",
        "cost_per_ad_impression_event",
        "cpm",
        "cpc",
    }
)
NAME_FIELDS = (
    "name",
    "account_name",
    "campaign_name",
    "adgroup_name",
    "ad_name",
    "material_name",
    "drama_name",
)

_FACT_TABLE = {
    "account": ("campaign_selected_day", None),
    "campaign": ("campaign_selected_day", "campaign_id"),
    "drama": ("campaign_selected_day", "campaign_id"),
    "adgroup": ("adgroup_selected_day", "adgroup_id"),
    "ad": ("ad_report_day", "ad_id"),
}
_FACT_META = {
    "account_report_day": ("campaign_id", "campaign_name"),
    "campaign_report_day": ("campaign_id", "campaign_name"),
    "campaign_selected_day": ("campaign_id", "campaign_name"),
    "ad_report_day": (
        "campaign_id",
        "adgroup_id",
        "campaign_name",
        "adgroup_name",
        "ad_name",
    ),
    "adgroup_selected_day": ("campaign_id", "campaign_name", "adgroup_name"),
    "adgroup_report_day": ("campaign_id", "campaign_name", "adgroup_name"),
    # 纯指标路径仍保留后续CTE的列合同，但只生成NULL占位，不读历史名称。
    "campaign_metric_day": ("campaign_id", "campaign_name"),
    "adgroup_metric_day": ("campaign_id", "campaign_name", "adgroup_name"),
    "ad_metric_day": (
        "campaign_id",
        "adgroup_id",
        "campaign_name",
        "adgroup_name",
        "ad_name",
    ),
}


def entity_ref(data: dict[str, Any]) -> EntityRef:
    """快照 JSON 中的对象引用 -> EntityRef。"""
    return EntityRef(
        UUID(str(data["tenant_id"])),
        data["advertiser_id"],
        data["kind"],
        data["remote_id"],
    )


@dataclass(frozen=True)
class SnapshotMember:
    """快照冻结的一行：顺序、身份和可管理对象；展示与指标按需重算。"""

    row_key: str
    refs: tuple[dict[str, Any], ...]
    material_uses: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SnapshotTotals:
    total: int
    # 每个币种桶的 (值, 是否部分合计, 时区)；由调用方按 metric_vector 组装。
    buckets: tuple[dict[str, Any], ...]
    incomplete_advertisers: tuple[str, ...]
    incomplete_dates: tuple[str, ...]
    complete: bool
    account_versions: tuple[UUID, ...] = ()
    unallocated_group_count: int = 0


class _Params:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def add(self, name: str, value: Any) -> str:
        self.values[name] = value
        return f":{name}"


def _entity_conditions(
    filters: ReportingFilter, params: _Params, *, e: str, p: str
) -> list[str]:
    """目录状态与配置筛选；对象缺失时与 _entity_matches(None, ...) 相同：一律不匹配。"""
    conditions: list[str] = []
    for column, values, name in (
        ("ad_type", filters.ad_types, "ad_types"),
        ("operation_status", filters.operation_statuses, "operation_statuses"),
        ("review_status", filters.review_statuses, "review_statuses"),
    ):
        if values:
            conditions.append(f"{e}.{column} = ANY({params.add(name, list(values))})")
    if filters.naming_status:
        conditions.append(
            f"COALESCE({p}.status, 'INVALID') = {params.add('naming_status', filters.naming_status)}"
        )
    if filters.budget_modes:
        conditions.append(
            f"{e}.budget_mode = ANY({params.add('budget_modes', list(filters.budget_modes))})"
        )
    if filters.created_from or filters.created_to:
        created = f"{e}.created_local_date"
        if filters.created_from:
            conditions.append(
                f"{created} >= {params.add('created_from', filters.created_from)}"
            )
        if filters.created_to:
            conditions.append(
                f"{created} <= {params.add('created_to', filters.created_to)}"
            )
    if filters.min_target_roas is not None or filters.max_target_roas is not None:
        target = f"{e}.roas_bid"
        if filters.min_target_roas is not None:
            conditions.append(
                f"{target} >= {params.add('min_target_roas', filters.min_target_roas)}"
            )
        if filters.max_target_roas is not None:
            conditions.append(
                f"{target} <= {params.add('max_target_roas', filters.max_target_roas)}"
            )
    return conditions


def _null_entity_matches(filters: ReportingFilter) -> bool:
    """_entity_matches(None, None, ...)：只有未设置目录筛选（或只要求未规范命名）时成立。"""
    return not (
        filters.ad_types
        or filters.operation_statuses
        or filters.review_statuses
        or filters.budget_modes
        or filters.created_from
        or filters.created_to
        or filters.min_target_roas is not None
        or filters.max_target_roas is not None
        or (filters.naming_status not in (None, "INVALID"))
    )


def _metric_sums(alias: str) -> str:
    return ", ".join(
        f"sum({alias}.{m}) AS {m}, bool_or({alias}.{m} IS NULL) AS {m}_hn"
        for m in METRIC_COLUMNS
    )


def fact_coordinates(table: str) -> tuple[str, str, str]:
    """同一选择计划的来源坐标；derived 永远不追认为直接观测。"""
    level = (
        "adgroup"
        if table.startswith("adgroup")
        else "ad"
        if table.startswith("ad_")
        else "campaign"
    )
    parent = table.endswith("_selected_day")
    direct = "s.last_success_at IS NOT NULL" if parent else "TRUE"
    source = f"CASE WHEN {direct} THEN 'PLATFORM_DIRECT' ELSE 'CHILD_DERIVED' END"
    row_revision = "row_evidence.evidence->>'contract_revision'"
    revision = f"CASE WHEN {direct} THEN CASE WHEN s.active_contract='{NATIVE_CONTRACT}' AND {row_revision} IS NOT NULL AND {row_revision} IS DISTINCT FROM s.active_contract THEN 'CONTRACT_MISMATCH' ELSE COALESCE(s.active_contract,'LEGACY_UNKNOWN') END ELSE COALESCE({row_revision},'LEGACY_UNKNOWN') END"
    if not parent:
        revision = f"CASE WHEN row_evidence.advertiser_id IS NOT NULL THEN CASE WHEN row_evidence.evidence->'publication'->>'active_contract' IS DISTINCT FROM {row_revision} AND row_evidence.evidence->'publication' IS NOT NULL THEN 'CONTRACT_MISMATCH' ELSE COALESCE({row_revision},'LEGACY_UNKNOWN') END ELSE COALESCE(s.active_contract,'LEGACY_UNKNOWN') END"
    return level, source, revision


def _facts_cte(
    table: str,
    id_column: str | None,
    *,
    extra_group: Sequence[str] = (),
    selected_ids: bool = False,
    include_metadata: bool = True,
    period_scan: bool = False,
    contribution: bool = False,
) -> str:
    meta = ", ".join(
        (
            f"(array_agg(t.{name} ORDER BY t.stat_date DESC, t.fetched_at DESC) "
            f"FILTER (WHERE t.{name} IS NOT NULL))[1] AS f_{name}"
        )
        if include_metadata
        else f"NULL::text AS f_{name}"
        for name in _FACT_META[table]
        if name != id_column
    )
    meta = f"{meta}," if meta else ""

    def identity(column: str) -> str:
        return column

    adv = identity("t.advertiser_id")
    rid = identity(f"t.{id_column}" if id_column else "t.advertiser_id")
    currency = identity("t.currency")
    level, source_kind, revision = fact_coordinates(
        "ad_report_day" if contribution else table
    )
    group = ", ".join(
        [
            adv,
            rid,
            *(identity(f"t.{c}") for c in extra_group),
            currency,
            source_kind,
            revision,
        ]
    )
    extra_select = "".join(f", {identity(f't.{c}')} AS {c}" for c in extra_group)
    metric_sums = _metric_sums("t")
    if table in {
        "account_report_day",
        "adgroup_report_day",
        "adgroup_selected_day",
        "campaign_metric_day",
        "adgroup_metric_day",
        "ad_metric_day",
    }:
        metric_sums = ", ".join(
            f"sum(t.{m}) AS {m}, bool_or(t.{m}_has_null OR t.{m} IS NULL) AS {m}_hn"
            for m in METRIC_COLUMNS
        )
    predicate = """t.tenant_id = :tenant AND t.advertiser_id = ANY(:advs)
          AND t.stat_date BETWEEN :start AND :end"""
    source = f"{'ad_report_day' if contribution else table} t"
    if (
        table.endswith("_selected_day") or table in {"ad_report_day", "ad_metric_day"}
    ) and not contribution:
        # 已发布空桶对目录对象仍有直接权威；补 NULL 对象日，而非恢复旧派生值或补零。
        wanted = " AND obj.remote_id=ANY(:ids)" if selected_ids and id_column else ""
        wanted_adv = (
            " AND obj.advertiser_id=ANY(:ids)" if selected_ids and not id_column else ""
        )
        source = f"""(
            SELECT * FROM {table}
            UNION ALL
            SELECT empty.* FROM ad_object obj
            JOIN advertiser_account account ON account.tenant_id=obj.tenant_id
                AND account.advertiser_id=obj.advertiser_id
            JOIN report_sync_state pub ON pub.tenant_id=obj.tenant_id
                AND pub.advertiser_id=obj.advertiser_id AND pub.report_kind='{level}_day'
                AND pub.last_success_at IS NOT NULL AND pub.active_contract IS NOT NULL
                AND pub.stat_date BETWEEN :start AND :end
            CROSS JOIN LATERAL jsonb_populate_record(NULL::{table},jsonb_build_object(
                'tenant_id',obj.tenant_id,'advertiser_id',obj.advertiser_id,
                '{level}_id',obj.remote_id,'stat_date',pub.stat_date,
                'currency',COALESCE(pub.publication_coordinates->>'currency','UNKNOWN'),
                'timezone',COALESCE(pub.publication_coordinates->>'timezone','UNKNOWN'))) empty
            WHERE obj.tenant_id=:tenant AND obj.advertiser_id=ANY(:advs)
                AND obj.kind='{level}' {wanted} {wanted_adv}
                AND NOT EXISTS (SELECT 1 FROM {table} present
                    WHERE present.tenant_id=obj.tenant_id AND present.advertiser_id=obj.advertiser_id
                    AND present.{level}_id=obj.remote_id AND present.stat_date=pub.stat_date)
        ) t"""
    state_join = f""" LEFT JOIN report_sync_state s ON s.tenant_id=t.tenant_id
        AND s.advertiser_id=t.advertiser_id AND s.stat_date=t.stat_date
        AND s.report_kind='{level}_day'"""
    publication = "to_jsonb(s)"
    parent = table.endswith("_selected_day") and not contribution
    if parent:
        # 父级派生候选按真实子事实分桶；不把多个观测伪装成一次父级刷新。
        state_join += f""" LEFT JOIN LATERAL (
            SELECT raw.evidence, raw.advertiser_id, NULL::jsonb AS child_fact,
                   raw.remote_id AS observation_id
            FROM parent_report_day raw WHERE s.last_success_at IS NOT NULL
                AND raw.tenant_id=t.tenant_id AND raw.advertiser_id=t.advertiser_id
                AND raw.stat_date=t.stat_date AND raw.level='{level}' AND raw.remote_id=t.{level}_id
            UNION ALL
            SELECT child.evidence, child.advertiser_id, to_jsonb(child), child.ad_id
            FROM ad_report_day child WHERE s.last_success_at IS NULL
                AND child.tenant_id=t.tenant_id AND child.advertiser_id=t.advertiser_id
                AND child.stat_date=t.stat_date AND child.{level}_id=t.{level}_id
        ) row_evidence ON TRUE"""
        for name in METRIC_COLUMNS:
            metric_sums = (
                metric_sums.replace(
                    f"sum(t.{name})",
                    f"sum(CASE WHEN s.last_success_at IS NULL THEN (row_evidence.child_fact->>'{name}')::numeric ELSE t.{name} END)",
                )
                .replace(
                    f"bool_or(t.{name}_has_null OR t.{name} IS NULL)",
                    f"bool_or(CASE WHEN s.last_success_at IS NULL THEN row_evidence.child_fact->>'{name}' IS NULL ELSE t.{name}_has_null OR t.{name} IS NULL END)",
                )
                .replace(
                    f"bool_or(t.{name} IS NULL)",
                    f"bool_or(CASE WHEN s.last_success_at IS NULL THEN row_evidence.child_fact->>'{name}' IS NULL ELSE t.{name} IS NULL END)",
                )
            )
        publication = "CASE WHEN s.last_success_at IS NULL THEN row_evidence.evidence->'publication' ELSE to_jsonb(s) END"
    else:
        state_join += """ LEFT JOIN ad_report_day row_evidence ON
            row_evidence.tenant_id=t.tenant_id AND row_evidence.advertiser_id=t.advertiser_id
            AND row_evidence.stat_date=t.stat_date AND row_evidence.ad_id=t.ad_id"""
        # 真实行没有自有观测时保持未知；只有无返回行的完整空桶可以使用账户发布。
        publication = "CASE WHEN row_evidence.advertiser_id IS NOT NULL THEN row_evidence.evidence->'publication' ELSE to_jsonb(s) END"
    observation_id = "row_evidence.observation_id" if parent else f"t.{level}_id"

    def fact_value(m: str) -> str:
        return (
            f"CASE WHEN s.last_success_at IS NULL THEN row_evidence.child_fact->>'{m}' ELSE t.{m}::text END"
            if parent
            else f"t.{m}::text"
        )

    # 当前目录只能解释这一修订；未知直接合同留在独立桶，绝不回退子级。
    if selected_ids:
        predicate += f" AND {rid}=ANY(:ids)"
    if contribution:
        metric_sums = _metric_sums("t")
        predicate += " AND t.ad_id=ANY(:contribution_ids)"
    for name in METRIC_COLUMNS:
        if parent:
            metric_sums = metric_sums.replace(
                f"sum(CASE WHEN s.last_success_at IS NULL THEN (row_evidence.child_fact->>'{name}')::numeric ELSE t.{name} END)",
                f"sum(CASE WHEN ({revision}) IN ('{NATIVE_CONTRACT}','LEGACY_UNKNOWN') THEN CASE WHEN s.last_success_at IS NULL THEN (row_evidence.child_fact->>'{name}')::numeric ELSE t.{name} END END)",
            )
        metric_sums = metric_sums.replace(
            f"sum(t.{name})",
            f"sum(CASE WHEN ({revision}) IN ('{NATIVE_CONTRACT}','LEGACY_UNKNOWN') THEN t.{name} END)",
        )
    if not contribution and period_scan and table.endswith("_metric_day"):
        source = f"""(
            SELECT * FROM {table}
            WHERE tenant_id=:tenant AND stat_date BETWEEN :start AND :end OFFSET 0
        ) t"""
    observation = f"""jsonb_agg(jsonb_build_object(
        'advertiser_id',t.advertiser_id,'remote_id',{observation_id},'level','{level}',
        'source_kind',{source_kind},'contract_revision',{revision},
        'local_day',t.stat_date,'timezone',t.timezone,'currency',t.currency,
        'fact',jsonb_build_object({",".join(f"'{m}',{fact_value(m)}" for m in METRIC_COLUMNS)}),
        'returned',row_evidence.advertiser_id IS NOT NULL,
        'fields',COALESCE(row_evidence.evidence->'fields','{{}}'::jsonb),
        'publication',COALESCE({publication},'{{}}'::jsonb))) AS observations,"""
    return f"""facts AS (
        SELECT {adv} AS adv, {rid} AS rid{extra_select}, {currency} AS currency,
            CASE WHEN min(t.timezone) = max(t.timezone) THEN min(t.timezone) ELSE 'MIXED' END AS tz,
            '{level}'::text AS level, {source_kind} AS source_kind, {revision} AS contract_revision,
            {meta}
            {observation}
            {metric_sums}
        FROM {source}{state_join} WHERE {predicate}
        GROUP BY {group}
    )"""


def _common_ctes(cov_kind: str, *, account: bool = False) -> str:
    period = ""
    if account:
        state = "SELECT tenant_id,advertiser_id,stat_date,status FROM account_report_day_state"
    elif cov_kind in PARENT_KINDS:
        state = state_sql(cov_kind)
    elif cov_kind == "creative_material":
        from app.modules.reporting.creative_query import PERIOD_SQL

        period = f"material_period AS MATERIALIZED ({PERIOD_SQL}),"
        state = "SELECT tenant_id,advertiser_id,stat_date,status FROM material_period"
    else:
        state = f"SELECT tenant_id,advertiser_id,stat_date,status FROM report_sync_state WHERE report_kind='{cov_kind}'"
    return f"""acc AS NOT MATERIALIZED (
        SELECT advertiser_id AS adv, NULLIF(name, '') AS name, currency, timezone AS tz
        FROM advertiser_account WHERE tenant_id = :tenant AND advertiser_id = ANY(:advs)
    ),
    {period}
    cov AS (
        SELECT a.adv, count(s.stat_date) FILTER (WHERE s.status = 'COMPLETE') = :ndays AS complete
        FROM acc a LEFT JOIN ({state}) s
          ON s.tenant_id = :tenant AND s.advertiser_id = a.adv
         AND s.stat_date BETWEEN :start AND :end
        GROUP BY a.adv
    )"""


_PROJ_COLUMNS = "provider_label, drama_name, status"


def _proj_cte() -> str:
    """当前投影已有账户/系列唯一主键，直接连接才能保留唯一性与索引。

    旧实现为当前表再收集全量候选、逐个LIMIT1；错误的小基数估计会使
    筛选后的每条系列反复扫描整个候选集合，退化成平方复杂度。
    """
    return f"""proj AS NOT MATERIALIZED (
        SELECT advertiser_id AS adv,campaign_remote_id AS cid,{_PROJ_COLUMNS}
        FROM campaign_name_current WHERE tenant_id=:tenant AND advertiser_id=ANY(:advs)
    )"""


def _obj_select(condition: str) -> str:
    return f"""SELECT advertiser_id AS adv, kind, remote_id AS rid, parent_remote_id AS parent,
            NULLIF(name, '') AS name, ad_type, operation_status, review_status, delivery_status,
            budget_mode,budget_value,target_roas,roas_bid,created_local_date
        FROM ad_entity_read
        WHERE tenant_id = :tenant AND {condition}"""


def _obj_ctes(filters: ReportingFilter, chain: Sequence[tuple[str, str | None]]) -> str:
    """目录对象 obj。

    列出无数据对象时读取所需层级的全部目录对象；默认只列有数据的对象，
    此时只按身份逐个按主键读取页面行涉及的对象及其上级。不能写成
    (账户, ID) IN (子查询)：规划器会把它做成整租户目录扫描再半连接，
    大租户十几万条广告仍要十几秒。
    chain 自下而上，每级为 (层级, 额外身份 SQL)；上一级对象的父 ID 自动并入下一级。
    """
    if filters.include_empty:
        return f"obj AS ({_obj_select('advertiser_id = ANY(:advs) AND kind = ANY(:kinds)')})"
    parts: list[str] = []
    previous: str | None = None
    for kind, seeds in chain:
        name = f"obj_{kind}"
        ids = " UNION ".join(
            sql
            for sql in (
                seeds,
                f"SELECT adv, parent FROM {previous} WHERE parent IS NOT NULL"
                if previous
                else None,
            )
            if sql
        )
        lookup = _obj_select(
            f"advertiser_id = k.adv AND kind = '{kind}' AND remote_id = k.rid"
        )
        parts.append(f"""{name} AS (
        SELECT o.* FROM (SELECT DISTINCT adv, rid FROM ({ids}) x(adv, rid) WHERE rid IS NOT NULL) k
        CROSS JOIN LATERAL ({lookup} OFFSET 0) o
    )""")
        previous = name
    union = " UNION ALL ".join(f"SELECT * FROM obj_{kind}" for kind, _ in chain)
    return ",\n".join([*parts, f"obj AS ({union})"])


def _search_terms(filters: ReportingFilter) -> list[str]:
    return [word.lower() for word in (filters.query or "").split()]


def _metrics_only(filters: ReportingFilter) -> bool:
    """快照成员只由报表事实决定、可跳过目录与名称投影的查询。

    默认口径下系列/广告组/广告的成员即有事实的对象；没有搜索、目录筛选、
    命名筛选，也不按名称或配置排序时，快照只需事实与指标。名称与上级在
    翻页补全时只为当前页读取（materialize_rows），页面显示不变。
    素材仍需冻结使用位置；这里只额外允许其无筛选汇总直接消费全部成员桶。
    """
    return (
        filters.dimension in ("campaign", "adgroup", "ad", "drama", "material")
        and not filters.include_empty
        and not filters.ids
        and not _search_terms(filters)
        and not filters.naming_status
        and _null_entity_matches(filters)
        and filters.sort_by not in NAME_SORT_FIELDS | CONFIG_SORT_FIELDS
    )


_EMPTY_PROJ = """proj AS (
        SELECT NULL::text AS adv, NULL::text AS cid, NULL::text AS provider_label,
            NULL::text AS drama_name, NULL::text AS status WHERE FALSE
    )"""


def _dir_hits(filters: ReportingFilter, params: _Params, kind: str) -> str | None:
    """搜索/ID 筛选命中的目录对象（含所选日期无数据的），默认口径下并入行身份。

    用户按名称或 ID 找对象时不应因为当天没有数据就找不到。ID 匹配对象自身或
    直接上级（下钻只下一级）；关键词匹配对象自身名称或 ID（长数字按完整 ID），
    账户名、上级名等只对有数据的行生效。
    """
    if filters.include_empty:
        return None
    conditions = []
    if filters.ids:
        ids = params.add("ids", list(filters.ids))
        conditions.append(
            f"(remote_id = ANY(CAST({ids} AS text[])) "
            f"OR parent_remote_id = ANY(CAST({ids} AS text[])))"
        )
    tokens = _search_terms(filters)
    if tokens:
        # 粘贴的平台 ID（长数字）按主键精确查找；名称关键词只能扫描该层级目录
        # （库为 C 字符集，三元组索引会丢弃中文，帮不上忙）。
        platform_ids = [
            token for token in tokens if token.isdigit() and len(token) >= 10
        ]
        if platform_ids:
            conditions.append(
                f"remote_id = ANY(CAST({params.add('platform_ids', platform_ids)} AS text[]))"
            )
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM unnest(CAST({params.add('tokens', tokens)} AS text[])) "
            "AS q(token) WHERE strpos(lower(concat_ws(' ', remote_id, name)), q.token) = 0)"
        )
    if not conditions:
        return None
    return f"""dir_hits AS (
        SELECT advertiser_id AS adv, remote_id AS rid FROM ad_entity_read
        WHERE tenant_id = :tenant AND advertiser_id = ANY(:advs) AND kind = '{kind}'
          AND {" AND ".join(conditions)}
    )"""


def _member_buckets(
    members: str,
    *,
    unsupported: frozenset[str],
    campaign: bool = False,
    level: str = "campaign",
) -> str:
    """成员级指标桶：有事实取事实合计，否则按覆盖填 0 或空（用户裁定）。"""
    values = ", ".join(
        f"NULL::numeric AS {m}, false AS {m}_hn"
        if m in unsupported
        else (
            f"CASE WHEN f.adv IS NULL THEN {'NULL::numeric' if campaign else 'CASE WHEN c.complete THEN 0::numeric END'} ELSE f.{m} END AS {m}, "
            f"COALESCE(f.{m}_hn, false) AS {m}_hn"
        )
        for m in METRIC_COLUMNS
    )
    return f"""mb AS (
        SELECT m.row_key, m.adv, m.rid, COALESCE(f.level,'{level}') AS level, COALESCE(f.source_kind,'CHILD_DERIVED') AS source_kind, COALESCE(f.contract_revision,'LEGACY_UNKNOWN') AS contract_revision, COALESCE(f.currency, a.currency) AS currency,
            COALESCE(f.tz, a.tz) AS tz, NOT c.complete AS incomplete, f.observations, to_jsonb(f) AS selected_fact, {values}
        FROM {members} m
        JOIN acc a ON a.adv = m.adv
        JOIN cov c ON c.adv = m.adv
        LEFT JOIN facts f ON f.adv = m.adv AND f.rid = m.rid
    )"""


def _material_member_buckets() -> str:
    """已投影事实就是素材成员；避免先去重身份再与同一事实集自连接。"""
    fact_values = ", ".join(
        f"f.{m}, COALESCE(f.{m}_hn,false) AS {m}_hn" for m in METRIC_COLUMNS
    )
    empty_values = ", ".join(
        f"CASE WHEN c.complete THEN 0::numeric END AS {m},false AS {m}_hn"
        for m in METRIC_COLUMNS
    )
    return f"""mb AS (
        SELECT f.rid AS row_key,f.adv,COALESCE(f.currency,a.currency) AS currency,
            COALESCE(f.tz,a.tz) AS tz,(NOT c.complete OR f.source_incomplete) AS incomplete,{fact_values}
        FROM facts f JOIN acc a ON a.adv=f.adv JOIN cov c ON c.adv=f.adv
        UNION ALL
        SELECT ra.row_key,ra.adv,a.currency,a.tz,NOT c.complete AS incomplete,{empty_values}
        FROM row_advs ra JOIN acc a ON a.adv=ra.adv JOIN cov c ON c.adv=ra.adv
        WHERE NOT EXISTS (SELECT 1 FROM fk WHERE fk.row_key=ra.row_key AND fk.adv=ra.adv)
            AND NOT EXISTS (SELECT 1 FROM unallocated_rows u WHERE u.row_key=ra.row_key AND u.advertiser_id=ra.adv)
        UNION ALL
        SELECT DISTINCT u.row_key,u.advertiser_id,u.currency,u.timezone,true,
            {", ".join(f"NULL::numeric AS {m},true AS {m}_hn" for m in METRIC_COLUMNS)}
        FROM unallocated_rows u JOIN row_advs ra ON ra.row_key=u.row_key AND ra.adv=u.advertiser_id
        WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.rid=u.row_key AND f.adv=u.advertiser_id AND f.currency=u.currency AND (f.tz=u.timezone OR f.tz='MIXED'))
    )"""


def _row_buckets(*, scoped: bool = False) -> str:
    coordinates = ", level, source_kind, contract_revision" if scoped else ""
    return f"""rb AS (
        SELECT row_key, currency{coordinates}, {", ".join(f"sum({m}) AS {m}, bool_or({m}_hn OR {m} IS NULL) AS {m}_hn" for m in METRIC_COLUMNS)}
        FROM mb GROUP BY row_key, currency{coordinates}
    ),
    rv AS (
        -- 只有单一币种桶的行有排序值；多口径行与无值行统一排在末尾。
        SELECT row_key, count(*) AS bucket_count, min(currency) AS currency,
            {", ".join(f"min({m}) AS {m}, bool_or({m}_hn) AS {m}_hn" for m in METRIC_COLUMNS)}
        FROM rb GROUP BY row_key
    )"""


def _metric_expr(field: str, alias: str, *, strict: bool = True) -> str:
    if field in DERIVED_METRICS:
        numerator, denominator, scale = DERIVED_METRICS[field]
        if not strict:
            return f"({alias}.{numerator} / NULLIF({alias}.{denominator}, 0) * {scale})"
        return f"(CASE WHEN NOT {alias}.{denominator}_hn THEN {alias}.{numerator} / NULLIF({alias}.{denominator}, 0) * {scale} END)"
    return f"{alias}.{field}"


def _metric_bounds(filters: ReportingFilter, params: _Params) -> str | None:
    checks = []
    for field, low, high in (
        ("spend", filters.min_spend, filters.max_spend),
        ("roas_d0", filters.min_d0_roas, filters.max_d0_roas),
    ):
        expr = _metric_expr(field, "b", strict=filters.dimension != "material")
        if low is not None:
            checks.append(f"{expr} >= {params.add(f'min_{field}', low)}")
        if high is not None:
            checks.append(f"{expr} <= {params.add(f'max_{field}', high)}")
    if not checks:
        return None
    # 任一币种桶同时满足全部区间即通过，与 _passes_filter 一致。
    return f"r.row_key IN (SELECT b.row_key FROM rb b WHERE {' AND '.join(checks)})"


def _text_conditions(
    filters: ReportingFilter, params: _Params, *, ids: str, haystack: str
) -> list[str]:
    conditions = []
    if filters.ids:
        conditions.append(
            f"CAST({ids} AS text[]) && CAST({params.add('ids', list(filters.ids))} AS text[])"
        )
    tokens = [word.lower() for word in (filters.query or "").split()]
    if tokens:
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM unnest(CAST({params.add('tokens', tokens)} AS text[])) AS q(token) "
            f"WHERE strpos(lower({haystack}), q.token) = 0)"
        )
    return conditions


def _order_by(filters: ReportingFilter) -> str:
    field = filters.sort_by
    direction = "DESC" if filters.sort_direction == "desc" else "ASC"
    tie = 'r.row_key COLLATE "C" ASC'
    if field == "row_key":
        return f'r.row_key COLLATE "C" {direction}'
    if field in NAME_SORT_FIELDS:
        expr = f"lower(r.{field})"
        return f'({expr} IS NULL), {expr} COLLATE "C" {direction}, {tie}'
    if field in CONFIG_SORT_FIELDS:
        currency = "COALESCE(r.balance_currency, '')" if field == "balance" else "''"
        value = f"r.{field}"
        return f'({value} IS NULL), {currency} COLLATE "C" {direction}, {value} {direction}, {tie}'
    value = f"(CASE WHEN v.bucket_count = 1 THEN {_metric_expr(field, 'v', strict=filters.dimension != 'material')} END)"
    currency = (
        'v.currency COLLATE "C" ' + direction + ", "
        if field in MONEY_SORT_FIELDS
        else ""
    )
    return f"({value} IS NULL), {currency}{value} {direction}, {tie}"


def _metric_dimension_rows(
    filters: ReportingFilter, *, period_scan: bool = False
) -> tuple[str, str]:
    """纯指标候选本身就是事实成员，避免身份集合再回连同一份大聚合。"""
    dim = filters.dimension
    fact = _facts_cte(
        f"{dim}_selected_day"
        if query_kind(dim) in PARENT_KINDS
        else "ad_report_day"
        if dim == "ad"
        else f"{dim}_metric_day",
        f"{dim}_id",
        include_metadata=True,
        period_scan=period_scan,
        contribution=filters.contribution is not None,
    )
    values = ", ".join(f"f.{m}, f.{m}_hn" for m in METRIC_COLUMNS)
    return ",\n".join(
        [
            _common_ctes(query_kind(dim)),
            fact,
            f"""mb AS (
            SELECT f.adv || ':{dim}:' || f.rid AS row_key,f.adv,f.rid,f.currency,f.tz,
                NOT c.complete AS incomplete,f.level,f.source_kind,f.contract_revision,f.observations,to_jsonb(f) AS selected_fact,{values}
            FROM facts f JOIN acc a ON a.adv=f.adv JOIN cov c ON c.adv=f.adv
        )""",
            f"""base AS (
            SELECT DISTINCT f.adv || ':{dim}:' || f.rid AS row_key
            FROM facts f JOIN acc a ON a.adv=f.adv
        )""",
        ]
    ), "base"


def _drama_row_key(params: _Params, bc_id: str, naming: str, drama: str) -> str:
    return (
        f"CASE WHEN {naming} = 'VALID' THEN {params.add('drama_prefix', f'{bc_id}:drama:')} "
        f"|| '[' || COALESCE(to_json({drama})::text, 'null') || ']' "
        f"ELSE {params.add('external_prefix', f'{bc_id}:external:')} "
        "|| '[' || to_json(e.adv)::text || ',' || to_json(e.rid)::text || ']' END"
    )


def _drama_metric_rows(params: _Params, bc_id: str) -> tuple[str, str]:
    """剧目默认查询仅冻结分组与成员，展示目录留到当前页补全。"""
    key = _drama_row_key(params, bc_id, "p.status", "p.drama_name")
    return ",\n".join(
        [
            _common_ctes("campaign_day"),
            _facts_cte(
                "campaign_selected_day",
                "campaign_id",
                include_metadata=True,
                period_scan=len(params.values["advs"]) >= 1000,
                contribution="contribution_ids" in params.values,
            ),
            _proj_cte(),
            f"""members AS (
                SELECT {key} AS row_key,e.adv,e.rid
                FROM (SELECT DISTINCT adv,rid FROM facts) e
                JOIN acc a ON a.adv=e.adv
                LEFT JOIN proj p ON p.adv=e.adv AND p.cid=e.rid
            )""",
            """base AS (
                SELECT row_key,count(*)::int AS member_count,
                    jsonb_agg(jsonb_build_array(adv,rid)
                        ORDER BY adv COLLATE "C",rid COLLATE "C") AS member_keys
                FROM members GROUP BY row_key
            )""",
            _member_buckets("members", unsupported=frozenset(), campaign=True),
        ]
    ), "base"


def _dimension_rows(
    filters: ReportingFilter, params: _Params, bc_id: str
) -> tuple[str, str]:
    """返回维度 CTE 序列与行 CTE 名，冻结列由 _ctes 统一投影。"""
    if _metrics_only(filters):
        if filters.dimension == "drama":
            return _drama_metric_rows(params, bc_id)
        return _metric_dimension_rows(
            filters, period_scan=len(params.values["advs"]) >= 1000
        )
    dim = filters.dimension
    table, id_column = _FACT_TABLE[dim]
    kind = "campaign" if dim == "drama" else dim
    params.add(
        "kinds",
        {
            "account": ["campaign"],
            "campaign": ["campaign"],
            "drama": ["campaign"],
            "adgroup": ["campaign", "adgroup"],
            "ad": ["campaign", "adgroup", "ad"],
        }[dim],
    )
    entity_filter = _entity_conditions(filters, params, e="e", p="p")
    where = " AND ".join(entity_filter) or "TRUE"
    dir_hits = None if dim == "account" else _dir_hits(filters, params, kind)
    fact_meta = """fm AS (
        SELECT DISTINCT ON (adv, rid) * FROM facts ORDER BY adv, rid, currency
    )"""
    # 账户页的余额只参与余额排序，版权方只参与文字/命名筛选；展示由页补全读取。
    # ent 被成员与指标多次消费，会物化所有选择列，不能依赖外层投影消除无用查询。
    account_projection = bool(_search_terms(filters) or filters.naming_status)

    if dim == "account":
        idn = "idn AS (SELECT adv, adv AS rid FROM acc)"
        balance_columns = "NULL::numeric AS balance, NULL::text AS balance_currency"
        balance_join = ""
        if filters.sort_by == "balance":
            balance_columns = "bal.amount AS balance, bal.currency AS balance_currency"
            balance_join = """LEFT JOIN LATERAL (
                SELECT CASE WHEN o.availability = 'AVAILABLE' THEN o.amount END AS amount, o.currency
                FROM account_balance_observation o
                WHERE o.tenant_id = :tenant AND o.advertiser_id = i.adv
                ORDER BY o.observed_at DESC, o.id DESC LIMIT 1
            ) bal ON TRUE"""
        ent = f"""ent AS (
            SELECT i.adv, i.rid, COALESCE(a.name, i.adv) AS name,
                NULL::text AS campaign_id, NULL::text AS adgroup_id,
                NULL::text AS campaign_name, NULL::text AS adgroup_name, NULL::text AS ad_name,
                NULL::numeric AS target_roas, NULL::numeric AS campaign_budget,
                NULL::numeric AS adgroup_budget,
                {balance_columns}, p.provider_label
            FROM idn i JOIN acc a ON a.adv = i.adv
            LEFT JOIN fm ON fm.adv = i.adv AND fm.rid = i.rid
            LEFT JOIN proj p ON p.adv = i.adv AND p.cid = fm.f_campaign_id
            {balance_join}
            LEFT JOIN obj e ON FALSE
            WHERE {where}
        )"""
    else:
        metadata = [n for n in _FACT_META[table] if n != id_column]
        columns = ",".join(f"f_{n}" for n in metadata)
        missing = ",".join(f"NULL::text AS f_{n}" for n in metadata)
        extra = "obj" if filters.include_empty else "dir_hits" if dir_hits else None
        additions = (
            ""
            if extra is None
            else f""" UNION ALL
            SELECT d.adv,d.rid,{missing},FALSE AS has_facts FROM {extra} d
            WHERE {f"d.kind='{kind}' AND " if extra == "obj" else ""}
              NOT EXISTS(SELECT 1 FROM facts f WHERE f.adv=d.adv AND f.rid=d.rid)"""
        )
        # 把事实元数据携带到身份中，不再把去重后的大facts集二次连接。
        # 同一对象多币种的展示仍取currency最前的桶，与原fm一致。
        idn = f"""idn AS (
            SELECT adv,rid,{columns},TRUE AS has_facts FROM (
                SELECT DISTINCT ON (adv,rid) * FROM facts ORDER BY adv,rid,currency
            ) f {additions}
        )"""
        target = "e.target_roas"
        budget = "e.budget_value"
        if kind == "campaign":
            joins = "LEFT JOIN proj p ON p.adv = i.adv AND p.cid = i.rid"
            campaign_id = "i.rid"
            adgroup_id = "NULL::text"
            campaign_name = "COALESCE(e.name, i.f_campaign_name, i.rid)"
            adgroup_name = "NULL::text"
            ad_name = "NULL::text"
            name = campaign_name
        elif kind == "adgroup":
            joins = """LEFT JOIN LATERAL (SELECT COALESCE(e.parent, i.f_campaign_id) AS cid) h ON TRUE
                LEFT JOIN obj c ON c.adv = i.adv AND c.kind = 'campaign' AND c.rid = h.cid
                LEFT JOIN proj p ON p.adv = i.adv AND p.cid = h.cid"""
            campaign_id = "h.cid"
            adgroup_id = "i.rid"
            campaign_name = "CASE WHEN h.cid IS NOT NULL THEN COALESCE(c.name, i.f_campaign_name, h.cid) END"
            adgroup_name = "COALESCE(e.name, i.f_adgroup_name, i.rid)"
            ad_name = "NULL::text"
            name = adgroup_name
        else:
            joins = """LEFT JOIN LATERAL (SELECT COALESCE(e.parent, i.f_adgroup_id) AS gid) hg ON TRUE
                LEFT JOIN obj g ON e.adv IS NOT NULL AND g.adv = i.adv AND g.kind = 'adgroup'
                    AND g.rid = COALESCE(e.parent, '')
                LEFT JOIN LATERAL (SELECT COALESCE(g.parent, i.f_campaign_id) AS cid,
                    CASE WHEN g.adv IS NOT NULL THEN g.parent ELSE i.f_campaign_id END AS pcid) h ON TRUE
                LEFT JOIN obj gn ON gn.adv = i.adv AND gn.kind = 'adgroup' AND gn.rid = hg.gid
                LEFT JOIN obj c ON c.adv = i.adv AND c.kind = 'campaign' AND c.rid = h.cid
                LEFT JOIN proj p ON p.adv = i.adv AND p.cid = h.pcid"""
            campaign_id = "h.cid"
            adgroup_id = "hg.gid"
            campaign_name = "CASE WHEN h.cid IS NOT NULL THEN COALESCE(c.name, i.f_campaign_name, h.cid) END"
            adgroup_name = "CASE WHEN hg.gid IS NOT NULL THEN COALESCE(gn.name, i.f_adgroup_name, hg.gid) END"
            ad_name = "COALESCE(e.name, i.f_ad_name, i.rid)"
            name = ad_name
        deleted = (
            # 剧维度不列出所选日期没有数据的已删除系列（多为测试/探针系列）。
            " AND NOT (NOT i.has_facts AND e.adv IS NOT NULL AND COALESCE(e.delivery_status, '') LIKE '%DELETE')"
            if dim == "drama"
            else ""
        )
        ent = f"""ent AS (
            SELECT i.adv, i.rid, {name} AS name, {campaign_id} AS campaign_id, {adgroup_id} AS adgroup_id,
                {campaign_name} AS campaign_name, {adgroup_name} AS adgroup_name, {ad_name} AS ad_name,
                {target} AS target_roas,
                {budget if kind == "campaign" else "NULL::numeric"} AS campaign_budget,
                {budget if kind == "adgroup" else "NULL::numeric"} AS adgroup_budget,
                NULL::numeric AS balance, NULL::text AS balance_currency,
                p.provider_label, p.status AS naming, p.drama_name AS p_drama, i.has_facts,
                COALESCE(a.name, i.adv) AS account_name
            FROM idn i JOIN acc a ON a.adv = i.adv
            LEFT JOIN obj e ON e.adv = i.adv AND e.kind = '{kind}' AND e.rid = i.rid
            {joins}
            WHERE {where}{deleted}
        )"""
    # 窄投影保证 (tenant, advertiser, kind, remote_id) 唯一。保持基表可内联，
    # 让层级连接保留约束与统计；先 UNION 多层目录会把十万行误估为个位数，
    # 再只按账户 merge、逐行过滤 parent ID，宽搜索退化为账户内笛卡尔积。
    obj = f"obj AS NOT MATERIALIZED ({_obj_select('advertiser_id = ANY(:advs) AND kind = ANY(:kinds)')})"
    if dim == "drama" and not filters.include_empty:
        obj = _obj_ctes(filters, (("campaign", "SELECT adv,rid FROM idn"),))
    # 账户维度只在筛选需要时按首个事实系列取版权方；账户池可达数千个，
    # 无搜索的默认首屏不能为整个租户读取系列名称与历史事实名称数组。
    if dim == "account":
        proj = _proj_cte() if account_projection else _EMPTY_PROJ
    else:
        proj = _proj_cte()
    # 搜索、层级ID、目录条件及名称/配置排序保留历史名称与父级身份回退。
    facts = _facts_cte(
        table,
        id_column,
        include_metadata=account_projection if dim == "account" else True,
        contribution=filters.contribution is not None,
    )
    # 非递归 CTE 只能引用前面的 CTE：列出无数据对象时身份依赖完整目录，
    # 默认时目录按身份读取，身份需先算出。
    ctes = (
        [facts, obj, proj, *([fact_meta] if dim == "account" else []), idn, ent]
        if dim == "account" or filters.include_empty
        else [facts, *([dir_hits] if dir_hits else []), idn, obj, proj, ent]
    )
    if dim == "account":
        ctes.append("""members AS (
            SELECT e.adv || ':account:' || e.adv AS row_key, e.adv, e.rid FROM ent e
        )""")
        ctes.append("""account_refs AS (
            SELECT advertiser_id AS adv,version_id FROM account_membership_current
            WHERE tenant_id=:tenant AND advertiser_id=ANY(:advs)
        )""")
        ctes.append("""base AS (
            SELECT e.adv || ':account:' || e.adv AS row_key, e.name, e.name AS account_name,
                NULL::text AS campaign_name, NULL::text AS adgroup_name, NULL::text AS ad_name,
                NULL::text AS material_name, NULL::text AS drama_name,
                NULL::numeric AS target_roas, NULL::numeric AS campaign_budget, NULL::numeric AS adgroup_budget,
                e.balance, e.balance_currency,
                ARRAY[e.adv || ':account:' || e.adv, e.adv] AS ids,
                concat_ws(' ', e.adv || ':account:' || e.adv, e.adv, e.name, e.provider_label) AS haystack,
                -- 成员在同步事务中预存；快照只固定UUID，选择/导出展开原版本。
                '[]'::jsonb AS refs, ar.version_id AS account_membership_version_id,
                '[]'::jsonb AS material_uses, '[]'::jsonb AS member_keys,
                0::int AS member_count, ARRAY[]::uuid[] AS evidence_versions
            FROM ent e LEFT JOIN account_refs ar ON ar.adv = e.adv
        )""")
    elif dim == "drama":
        drama_key = _drama_row_key(params, bc_id, "e.naming", "e.p_drama")
        ctes.append(f"""members AS (
            SELECT {drama_key} AS row_key, e.*,
                row_number() OVER (PARTITION BY {drama_key} ORDER BY e.adv COLLATE "C", e.rid COLLATE "C") AS member_order
            FROM ent e
        )""")
        ctes.append("""base AS (
            SELECT m.row_key,
                -- 剧行展示取首个成员（按账户、系列排序），剧名对规范命名的成员一致。
                CASE WHEN f.naming = 'VALID' THEN f.p_drama ELSE f.name END AS name,
                f.account_name, f.campaign_name, NULL::text AS adgroup_name, NULL::text AS ad_name,
                NULL::text AS material_name,
                CASE WHEN f.naming = 'VALID' THEN f.p_drama ELSE f.name END AS drama_name,
                f.target_roas, f.campaign_budget, NULL::numeric AS adgroup_budget,
                NULL::numeric AS balance, NULL::text AS balance_currency,
                array_agg(m.rid) || ARRAY[m.row_key, f.adv] AS ids,
                concat_ws(' ', m.row_key, f.adv, string_agg(m.rid, ' '), f.account_name, f.campaign_name,
                    CASE WHEN f.naming = 'VALID' THEN f.p_drama ELSE f.name END,
                    string_agg(m.provider_label, ' ')) AS haystack,
                '[]'::jsonb AS refs, '[]'::jsonb AS material_uses,
                jsonb_agg(jsonb_build_array(m.adv,m.rid) ORDER BY m.member_order) AS member_keys,
                count(*)::int AS member_count, ARRAY[]::uuid[] AS evidence_versions
            FROM members m JOIN members f ON f.row_key = m.row_key AND f.member_order = 1
            GROUP BY m.row_key, f.naming, f.p_drama, f.name, f.account_name, f.campaign_name, f.adv,
                f.target_roas, f.campaign_budget
        )""")
    else:
        id_cols = {
            "campaign": ["e.rid"],
            "adgroup": ["e.campaign_id", "e.rid"],
            "ad": ["e.campaign_id", "e.adgroup_id", "e.rid"],
        }[kind]
        key = f"e.adv || ':{kind}:' || e.rid"
        ctes.append(
            f"""members AS (SELECT {key} AS row_key, e.adv, e.rid FROM ent e)"""
        )
        ctes.append(f"""base AS (
            SELECT {key} AS row_key, e.name, e.account_name, e.campaign_name, e.adgroup_name, e.ad_name,
                NULL::text AS material_name, NULL::text AS drama_name,
                e.target_roas, e.campaign_budget, e.adgroup_budget,
                NULL::numeric AS balance, NULL::text AS balance_currency,
                array_remove(ARRAY[{key}, e.adv, {", ".join(id_cols)}], NULL) AS ids,
                concat_ws(' ', {key}, e.adv, {", ".join(id_cols)}, e.account_name, e.campaign_name,
                    e.adgroup_name, e.ad_name, e.provider_label) AS haystack,
                '[]'::jsonb AS refs, '[]'::jsonb AS material_uses, '[]'::jsonb AS member_keys,
                1::int AS member_count, ARRAY[]::uuid[] AS evidence_versions
            FROM ent e
        )""")
    ctes.append(
        _member_buckets(
            "members",
            unsupported=frozenset(),
            campaign=query_kind(dim) in PARENT_KINDS,
            level="ad"
            if filters.contribution
            else "campaign"
            if kind == "account"
            else kind,
        )
    )
    return ",\n".join(
        [
            _common_ctes(
                query_kind(dim),
                account=False,
            ),
            *ctes,
        ]
    ), "base"


def _material_rows(filters: ReportingFilter, params: _Params) -> tuple[str, str]:
    """逐创意筛选后汇总；仅精确当前视频位置附带不可变动作证据。"""
    from app.modules.reporting.creative_query import filtered_ctes, material_name_sql

    values = ",".join(
        f"sum(f.{m}) AS {m},bool_or(f.{m}_has_null OR f.{m} IS NULL) AS {m}_hn"
        for m in METRIC_COLUMNS
    )
    ctes = [
        _common_ctes("creative_material"),
        filtered_ctes(filters, params),
        f"""facts AS (
            SELECT advertiser_id AS adv,row_key AS rid,currency,
                CASE WHEN min(timezone)=max(timezone) THEN min(timezone) ELSE 'MIXED' END AS tz,
                bool_or(source_status<>'COMPLETE' OR unallocated OR EXISTS (SELECT 1 FROM unallocated_rows u WHERE u.row_key=f.row_key AND u.advertiser_id=f.advertiser_id AND u.currency=f.currency)) AS source_incomplete,{values}
            FROM creative_filtered f GROUP BY advertiser_id,row_key,currency
        )""",
        f"""fk AS (SELECT DISTINCT adv,rid AS row_key FROM facts),
        row_advs AS (SELECT row_key,adv FROM fk UNION SELECT row_key,advertiser_id FROM creative_directory),
        names AS (
            SELECT row_key,min(name) AS name FROM (
                SELECT row_key,{material_name_sql("row_key", "material_name")} AS name FROM creative_filtered
                UNION ALL SELECT row_key,name FROM creative_directory
            ) n GROUP BY row_key
        ),
        use_list AS (
            SELECT row_key,array_agg(version_id ORDER BY advertiser_id,ad_remote_id) AS evidence_versions,
                count(*)::int AS member_count FROM creative_uses GROUP BY row_key
        ),
        rowsrc AS (
            SELECT ra.row_key,min(n.name) AS name,
                CASE WHEN count(DISTINCT ra.adv)=1 THEN min(COALESCE(a.name,ra.adv)) END AS account_name,
                COALESCE(ul.member_count,0) AS member_count,
                COALESCE(ul.evidence_versions,ARRAY[]::uuid[]) AS evidence_versions
            FROM row_advs ra JOIN acc a ON a.adv=ra.adv
            LEFT JOIN names n ON n.row_key=ra.row_key LEFT JOIN use_list ul ON ul.row_key=ra.row_key
            GROUP BY ra.row_key,ul.member_count,ul.evidence_versions
        )""",
        _material_member_buckets(),
        _row_buckets(),
    ]
    metric = _metric_bounds(filters, params) or "TRUE"
    ctes.append(f"""base AS (
        SELECT r.row_key,r.name,r.account_name,NULL::text AS campaign_name,NULL::text AS adgroup_name,
            NULL::text AS ad_name,r.name AS material_name,NULL::text AS drama_name,
            NULL::numeric AS target_roas,NULL::numeric AS campaign_budget,NULL::numeric AS adgroup_budget,
            NULL::numeric AS balance,NULL::text AS balance_currency,r.member_count,r.evidence_versions
        FROM rowsrc r WHERE {metric}
    )""")
    return ",\n".join(ctes), "base"


def _ctes(
    filters: ReportingFilter,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    bc_id: str,
    review_labels: bool = False,
) -> tuple[str, dict[str, Any]]:
    params = _Params()
    params.add("tenant", tenant_id)
    params.add("advs", sorted(advertisers))
    params.add("start", filters.start_date)
    params.add("end", filters.end_date)
    if filters.contribution:
        params.add("contribution_ids", list(filters.contribution.ids))
    params.add("ndays", (filters.end_date - filters.start_date).days + 1)
    # fr会被排序、汇总、coverage多次引用并物化；只冻结真正需要的列，
    # 否则未筛选/排序的名称与展示关联也会被强制计算到全部候选。
    retained = ["r.row_key"]
    if filters.dimension == "account":
        retained.append("r.account_membership_version_id")
    elif filters.dimension == "drama":
        retained += ["r.member_keys", "r.member_count"]
    elif filters.dimension == "material":
        retained += ["r.member_count", "r.evidence_versions"]
    if filters.sort_by in NAME_SORT_FIELDS | CONFIG_SORT_FIELDS:
        retained.append(f"r.{filters.sort_by}")
        if filters.sort_by == "balance":
            retained.append("r.balance_currency")
    if review_labels and filters.dimension == "material":
        retained.append("r.name")
    projection = ",".join(retained)
    if filters.dimension == "material":
        body, base = _material_rows(filters, params)
        filtered = f"fr AS (SELECT {projection} FROM {base} r)"
        buckets = ""
    else:
        body, base = _dimension_rows(filters, params, bc_id)
        conditions = _text_conditions(
            filters, params, ids="r.ids", haystack="r.haystack"
        )
        metric = _metric_bounds(filters, params)
        if metric:
            conditions.append(metric)
        buckets = f",\n{_row_buckets(scoped=True)}"
        filtered = f"fr AS (SELECT {projection} FROM {base} r WHERE {' AND '.join(conditions) or 'TRUE'})"
    version = (
        "r.account_membership_version_id"
        if filters.dimension == "account"
        else "NULL::uuid AS account_membership_version_id"
    )
    keys = "r.member_keys" if filters.dimension == "drama" else "'[]'::jsonb"
    count = "r.member_count" if filters.dimension in {"drama", "material"} else "1::int"
    evidence = (
        "r.evidence_versions" if filters.dimension == "material" else "ARRAY[]::uuid[]"
    )
    ordered = f"""ordered AS (
        SELECT r.row_key,'[]'::jsonb AS refs,'[]'::jsonb AS material_uses,
            {keys} AS member_keys,{count} AS member_count,{evidence} AS evidence_versions,{version},
            {"r.name AS review_name," if review_labels and filters.dimension == "material" else ""}
            (row_number() OVER (ORDER BY {_order_by(filters)}) - 1)::int AS seq
        FROM fr r LEFT JOIN rv v ON v.row_key = r.row_key
    )"""
    return f"WITH {body}{buckets},\n{filtered},\n{ordered}", params.values


def _summary_select(
    unsupported: frozenset[str],
    *,
    individual: bool = False,
    all_rows: bool = False,
    scoped: bool = False,
    per_row: bool = False,
) -> str:
    """汇总只算最终行：值为全部成员合计，部分合计规则同 merge_metric_buckets。"""
    columns = ", ".join(
        f"sum(x.{m})::text AS {m}, "
        f"bool_or(x.{m} IS NULL OR x.{m}_hn) AS {m}_missing, "
        f"(sum(x.{m}) IS NOT NULL AND (bool_or(x.row_incomplete) OR bool_or(x.{m} IS NULL) "
        f"OR bool_or(x.{m}_hn))) AS {m}_partial"
        for m in METRIC_COLUMNS
        if m not in unsupported
    )
    # 单对象只属于一个账户，同一行的所有币种覆盖相同，不需要大窗口排序。
    incomplete = (
        "mb.incomplete"
        if individual
        else "bool_or(mb.incomplete) OVER (PARTITION BY mb.row_key)"
    )
    coordinates = ", x.level, x.source_kind, x.contract_revision" if scoped else ""
    evidence = ", jsonb_agg(x.observations) AS observations" if scoped else ""
    source = "mb" if all_rows else "mb JOIN fr ON fr.row_key = mb.row_key"
    return f"""SELECT {"x.row_key, bool_or(x.row_incomplete) AS incomplete," if per_row else ""} x.currency{coordinates}{evidence},
            CASE WHEN count(DISTINCT x.tz) = 1 THEN min(x.tz) ELSE 'MIXED' END AS tz, {columns}
        FROM (SELECT mb.*, {incomplete} AS row_incomplete FROM {source}) x
        GROUP BY {"x.row_key," if per_row else ""} x.currency{coordinates} ORDER BY {"x.row_key," if per_row else ""} x.currency{coordinates}"""


def ordered_members(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    bc_id: str,
    filters: ReportingFilter,
) -> tuple[SnapshotMember, ...]:
    """按快照顺序返回全部成员（不写库），供测试与无快照场景使用。"""
    if not advertisers:
        return ()
    ctes, params = _ctes(
        filters, tenant_id=tenant_id, advertisers=advertisers, bc_id=bc_id
    )
    rows = (
        session.connection()
        .execute(
            text(
                f"{ctes} SELECT row_key, refs, material_uses,member_keys,member_count,evidence_versions,account_membership_version_id FROM ordered ORDER BY seq"
            ),
            params,
        )
        .all()
    )
    from app.modules.reporting.snapshot_members import members_from_data

    return members_from_data(
        session,
        tenant_id=tenant_id,
        advertisers=advertisers,
        dimension=filters.dimension,
        rows=[dict(r._mapping) for r in rows],
    )


@query_diagnostics.timed("snapshot_write")
def insert_snapshot_rows(
    session: Session,
    *,
    snapshot_id: UUID,
    tenant_id: UUID,
    advertisers: Sequence[str],
    bc_id: str,
    filters: ReportingFilter,
) -> SnapshotTotals:
    """一条语句写入精简快照行并返回汇总；快照行不存展示字段与指标。"""
    if not advertisers:
        return SnapshotTotals(0, (), (), (), True)
    ctes, params = _ctes(
        filters, tenant_id=tenant_id, advertisers=advertisers, bc_id=bc_id
    )
    params |= {"snapshot_id": snapshot_id, "dimension": filters.dimension}
    unsupported = frozenset()
    coverage_source = (
        "SELECT tenant_id,advertiser_id,'creative_material' AS report_kind,stat_date,status FROM material_period"
        if filters.dimension == "material"
        else "SELECT tenant_id,advertiser_id,report_kind,stat_date,status FROM report_sync_state"
    )
    if query_kind(filters.dimension) in PARENT_KINDS:
        coverage_source = state_sql(query_kind(filters.dimension))

    account_versions = (
        "(SELECT COALESCE(array_agg(account_membership_version_id),ARRAY[]::uuid[]) FROM fr)"
        if filters.dimension == "account"
        else "ARRAY[]::uuid[]"
    )
    all_rows = _metrics_only(filters) and all(
        value is None
        for value in (
            filters.min_spend,
            filters.max_spend,
            filters.min_d0_roas,
            filters.max_d0_roas,
        )
    )
    summary_sql = _summary_select(
        unsupported,
        individual=filters.dimension not in {"drama", "material"},
        all_rows=all_rows,
        scoped=filters.dimension != "material",
    )
    bad_source = "mb" if all_rows else "mb JOIN fr ON fr.row_key = mb.row_key"
    numeric_strings = ",".join(f"'{m}',mb.{m}::text" for m in METRIC_COLUMNS)
    projection_cte = (
        ""
        if filters.dimension == "material"
        else f"""
    frozen_projection AS MATERIALIZED (
        SELECT row_key, jsonb_agg(to_jsonb(mb) || jsonb_build_object({numeric_strings})) AS payload
        FROM mb JOIN fr USING (row_key) GROUP BY row_key
    ),"""
    )
    payload = (
        "'[]'::jsonb"
        if filters.dimension == "material"
        else "frozen_projection.payload"
    )
    selected = (
        "ordered"
        if filters.dimension == "material"
        else "ordered JOIN frozen_projection USING (row_key)"
    )
    statement = f"""{ctes}, {projection_cte}
    ins AS (
        INSERT INTO query_snapshot_row (snapshot_id, row_key, stable_sequence, dimension, display, refs,
            material_uses, metric_buckets, capabilities, directory_versions, membership_digest,account_membership_version_id,member_keys,member_count,evidence_versions)
        SELECT :snapshot_id, row_key, seq, :dimension, '{{}}'::jsonb, refs, material_uses,
            {payload}, '{{}}'::jsonb, '{{}}'::jsonb, '',account_membership_version_id,member_keys,member_count,evidence_versions
        FROM {selected}
        RETURNING snapshot_id,row_key
    ),
    bad AS (
        SELECT DISTINCT mb.adv FROM {bad_source} WHERE mb.incomplete
        {"UNION SELECT advertiser_id FROM unallocated_groups" if filters.dimension == "material" else ""}
    )
    SELECT
        (SELECT count(*) FROM ins) AS total,
        (SELECT COALESCE(jsonb_agg(to_jsonb(s)), '[]'::jsonb) FROM ({summary_sql}) s) AS buckets,
        (SELECT COALESCE(array_agg(adv ORDER BY adv), ARRAY[]::text[]) FROM bad) AS bad_advs,
        (SELECT COALESCE(array_agg(DISTINCT d.day::date ORDER BY d.day::date), ARRAY[]::date[])
           FROM bad b CROSS JOIN generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') d(day)
           WHERE NOT EXISTS (SELECT 1 FROM ({coverage_source}) s WHERE s.tenant_id = :tenant
               AND s.advertiser_id = b.adv AND s.report_kind = '{"creative_material" if filters.dimension == "material" else query_kind(filters.dimension)}'
               AND s.stat_date = d.day::date AND s.status = 'COMPLETE')) AS bad_dates,
        {account_versions} AS account_versions,
        {"(SELECT count(DISTINCT (advertiser_id,adgroup_id,currency,timezone)) FROM unallocated_groups)" if filters.dimension == "material" else "0"} AS unallocated_group_count
    """
    # 大范围目录条件会把复合身份JOIN误估为1行，进而对物化CTE做N²扫描。
    # 仅此集合聚合语句偏好Hash/Merge，不影响分页点查与连接池后续业务。
    # 保存点让事件异常、SQL异常时也恢复原设置；外层RR仍统一提交整个快照。
    with session.begin_nested():
        connection = session.connection()
        previous_join = connection.exec_driver_sql("SHOW enable_nestloop").scalar_one()
        previous_memory = connection.exec_driver_sql("SHOW work_mem").scalar_one()
        connection.exec_driver_sql("SET LOCAL enable_nestloop = off")
        # 仅本次集合聚合使用16MiB工作区，减少十几万行反复外排；不改部署或连接池默认值。
        connection.exec_driver_sql("SET LOCAL work_mem = '16MB'")
        row = connection.execute(text(statement), params).one()
        connection.execute(
            text("SELECT set_config('enable_nestloop',:previous,true)"),
            {"previous": previous_join},
        )
        connection.execute(
            text("SELECT set_config('work_mem',:previous,true)"),
            {"previous": previous_memory},
        )
    bad_dates: Sequence[date] = row.bad_dates
    return SnapshotTotals(
        total=row.total,
        buckets=tuple(row.buckets),
        incomplete_advertisers=tuple(row.bad_advs),
        incomplete_dates=tuple(day.isoformat() for day in bad_dates),
        complete=not row.bad_advs,
        account_versions=tuple(row.account_versions),
        unallocated_group_count=row.unallocated_group_count,
    )
