"""报表快照成员在数据库内一次算出：身份、筛选、排序、汇总都不经过 Python。

快照只冻结"有哪些行、按什么顺序、每行管理哪些对象"（row_key、refs、
material_uses）；行的展示字段与指标在翻页、导出时只对用到的行计算
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
from app.modules.reporting.read_models.material_names import material_names_sql
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
# 素材日报不提供 D3/D30 收入，统一标 UNSUPPORTED。
MATERIAL_UNSUPPORTED = frozenset({"revenue_d3", "revenue_d30"})
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
    "account": ("campaign_report_day", None),
    "campaign": ("campaign_report_day", "campaign_id"),
    "drama": ("campaign_report_day", "campaign_id"),
    "adgroup": ("adgroup_report_day", "adgroup_id"),
    "ad": ("ad_report_day", "ad_id"),
}
_FACT_META = {
    "campaign_report_day": ("campaign_id", "campaign_name"),
    "ad_report_day": (
        "campaign_id",
        "adgroup_id",
        "campaign_name",
        "adgroup_name",
        "ad_name",
    ),
    "material_report_day": ("main_material_name",),
    "material_entity_day": (),
    "adgroup_report_day": ("campaign_id", "campaign_name", "adgroup_name"),
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
    naming_campaigns: tuple[tuple[str, str], ...]


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


def _facts_cte(
    table: str,
    id_column: str | None,
    *,
    extra_group: Sequence[str] = (),
    include_metadata: bool = True,
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
        # 平台身份/类型/币种是精确键，不按人类语言排序规则分组。C排序同时让
        # 全日期聚合保留基表统计并自然顺序读取；无需先物化全部日事实到临时盘，
        # 也不会为沿用面向50行补全的page索引顺序而随机读取整个日期窗口。
        return f'{column} COLLATE "C"' if table == "material_entity_day" else column

    adv = identity("t.advertiser_id")
    rid = identity(f"t.{id_column}" if id_column else "t.advertiser_id")
    currency = identity("t.currency")
    group = ", ".join([adv, rid, *(identity(f"t.{c}") for c in extra_group), currency])
    extra_select = "".join(f", {identity(f't.{c}')} AS {c}" for c in extra_group)
    metric_sums = _metric_sums("t")
    if table in {"adgroup_report_day", "material_entity_day"}:
        # 素材D3/D30在成员桶恒为UNSUPPORTED；不为最终必然丢弃的值
        # 建立SUM/BOOL_OR聚合状态，列合同仍保留NULL。
        metric_sums = ", ".join(
            f"NULL::numeric AS {m}, false AS {m}_hn"
            if table == "material_entity_day" and m in MATERIAL_UNSUPPORTED
            else f"sum(t.{m}) AS {m}, bool_or(t.{m}_has_null OR t.{m} IS NULL) AS {m}_hn"
            for m in METRIC_COLUMNS
        )
    predicate = """t.tenant_id = :tenant AND t.advertiser_id = ANY(:advs)
          AND t.stat_date BETWEEN :start AND :end"""
    return f"""facts AS (
        SELECT {adv} AS adv, {rid} AS rid{extra_select}, {currency} AS currency,
            CASE WHEN min(t.timezone) = max(t.timezone) THEN min(t.timezone) ELSE 'MIXED' END AS tz,
            {meta}
            {metric_sums}
        FROM {table} t WHERE {predicate}
        GROUP BY {group}
    )"""


def _common_ctes(cov_kind: str) -> str:
    if cov_kind == "material_day":
        state = """SELECT tenant_id,advertiser_id,bucket AS stat_date,derived_status AS status
            FROM read_bucket_state WHERE kind='material_day' AND derived_revision>0"""
    else:
        state = f"SELECT tenant_id,advertiser_id,stat_date,status FROM report_sync_state WHERE report_kind='{cov_kind}'"
    return f"""acc AS (
        SELECT advertiser_id AS adv, NULLIF(name, '') AS name, currency, timezone AS tz
        FROM advertiser_account WHERE tenant_id = :tenant AND advertiser_id = ANY(:advs)
    ),
    cov AS (
        SELECT a.adv, count(s.stat_date) FILTER (WHERE s.status = 'COMPLETE') = :ndays AS complete
        FROM acc a LEFT JOIN ({state}) s
          ON s.tenant_id = :tenant AND s.advertiser_id = a.adv
         AND s.stat_date BETWEEN :start AND :end
        GROUP BY a.adv
    )"""


_PROJ_COLUMNS = "provider_label, drama_name, status"
_PROJ_ORDER = "grouping_revision DESC, name_revision DESC, observed_at DESC"


def _proj_cte(*, full: bool, fact_campaigns: str | None) -> str:
    """系列最新名称投影 proj（剧名、版权方、命名状态）。

    默认只为页面涉及的系列（目录系列、广告组的父系列、事实里的系列）逐个按主键取最新一条；
    full（列出无数据对象或账户维度）时整租户读取。fact_campaigns 为事实中系列 ID 的 SQL。
    """
    if full:
        return f"""proj AS (
        SELECT DISTINCT ON (advertiser_id, campaign_remote_id)
            advertiser_id AS adv, campaign_remote_id AS cid, {_PROJ_COLUMNS}
        FROM campaign_name_current
        WHERE tenant_id = :tenant AND advertiser_id = ANY(:advs)
        ORDER BY advertiser_id, campaign_remote_id, {_PROJ_ORDER}
    )"""
    keys = " UNION ".join(
        sql
        for sql in (
            "SELECT adv, rid FROM obj WHERE kind = 'campaign'",
            "SELECT adv, parent FROM obj WHERE kind = 'adgroup' AND parent IS NOT NULL",
            fact_campaigns,
        )
        if sql
    )
    return f"""proj AS (
        SELECT k.adv, k.cid, p.provider_label, p.drama_name, p.status
        FROM (SELECT DISTINCT adv, cid FROM ({keys}) x(adv, cid) WHERE cid IS NOT NULL) k
        CROSS JOIN LATERAL (
            SELECT {_PROJ_COLUMNS} FROM campaign_name_current
            WHERE tenant_id = :tenant AND advertiser_id = k.adv AND campaign_remote_id = k.cid
            ORDER BY {_PROJ_ORDER} LIMIT 1
        ) p
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
        CROSS JOIN LATERAL ({lookup}) o
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
    """
    return (
        filters.dimension in ("campaign", "adgroup", "ad")
        and not filters.include_empty
        and not filters.ids
        and not _search_terms(filters)
        and not filters.naming_status
        and _null_entity_matches(filters)
        and filters.sort_by not in NAME_SORT_FIELDS | CONFIG_SORT_FIELDS
    )


_EMPTY_OBJ = f"obj AS ({_obj_select('FALSE')})"
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


def _member_buckets(members: str, *, unsupported: frozenset[str]) -> str:
    """成员级指标桶：有事实取事实合计，否则按覆盖填 0 或空（用户裁定）。"""
    values = ", ".join(
        f"NULL::numeric AS {m}, false AS {m}_hn"
        if m in unsupported
        else (
            f"CASE WHEN f.adv IS NULL THEN CASE WHEN c.complete THEN 0::numeric END ELSE f.{m} END AS {m}, "
            f"COALESCE(f.{m}_hn, false) AS {m}_hn"
        )
        for m in METRIC_COLUMNS
    )
    return f"""mb AS (
        SELECT m.row_key, m.adv, COALESCE(f.currency, a.currency) AS currency,
            COALESCE(f.tz, a.tz) AS tz, NOT c.complete AS incomplete, {values}
        FROM {members} m
        JOIN acc a ON a.adv = m.adv
        JOIN cov c ON c.adv = m.adv
        LEFT JOIN facts f ON f.adv = m.adv AND f.rid = m.rid
    )"""


def _material_member_buckets() -> str:
    """已投影事实就是素材成员；避免先去重身份再与同一事实集自连接。"""
    fact_values = ", ".join(
        f"NULL::numeric AS {m}, false AS {m}_hn"
        if m in MATERIAL_UNSUPPORTED
        else f"f.{m}, COALESCE(f.{m}_hn,false) AS {m}_hn"
        for m in METRIC_COLUMNS
    )
    empty_values = ", ".join(
        f"NULL::numeric AS {m}, false AS {m}_hn"
        if m in MATERIAL_UNSUPPORTED
        else f"CASE WHEN c.complete THEN 0::numeric END AS {m},false AS {m}_hn"
        for m in METRIC_COLUMNS
    )
    return f"""mb AS (
        SELECT f.rid AS row_key,f.adv,COALESCE(f.currency,a.currency) AS currency,
            COALESCE(f.tz,a.tz) AS tz,NOT c.complete AS incomplete,{fact_values}
        FROM facts f JOIN acc a ON a.adv=f.adv JOIN cov c ON c.adv=f.adv
        UNION ALL
        SELECT ra.row_key,ra.adv,a.currency,a.tz,NOT c.complete AS incomplete,{empty_values}
        FROM row_advs ra JOIN acc a ON a.adv=ra.adv JOIN cov c ON c.adv=ra.adv
        WHERE NOT EXISTS (SELECT 1 FROM fk WHERE fk.row_key=ra.row_key AND fk.adv=ra.adv)
    )"""


def _row_buckets() -> str:
    return f"""rb AS (
        SELECT row_key, currency, {", ".join(f"sum({m}) AS {m}" for m in METRIC_COLUMNS)}
        FROM mb GROUP BY row_key, currency
    ),
    rv AS (
        -- 只有单一币种桶的行有排序值；多口径行与无值行统一排在末尾。
        SELECT row_key, count(*) AS bucket_count, min(currency) AS currency,
            {", ".join(f"min({m}) AS {m}" for m in METRIC_COLUMNS)}
        FROM rb GROUP BY row_key
    )"""


def _metric_expr(field: str, alias: str) -> str:
    if field in DERIVED_METRICS:
        numerator, denominator, scale = DERIVED_METRICS[field]
        return f"({alias}.{numerator} / NULLIF({alias}.{denominator}, 0) * {scale})"
    return f"{alias}.{field}"


def _metric_bounds(filters: ReportingFilter, params: _Params) -> str | None:
    checks = []
    for field, low, high in (
        ("spend", filters.min_spend, filters.max_spend),
        ("roas_d0", filters.min_d0_roas, filters.max_d0_roas),
    ):
        expr = _metric_expr(field, "b")
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
    value = f"(CASE WHEN v.bucket_count = 1 THEN {_metric_expr(field, 'v')} END)"
    currency = (
        'v.currency COLLATE "C" ' + direction + ", "
        if field in MONEY_SORT_FIELDS
        else ""
    )
    return f"({value} IS NULL), {currency}{value} {direction}, {tie}"


def _ref_json(adv: str, kind: str, rid: str) -> str:
    return (
        f"jsonb_build_object('tenant_id', CAST(:tenant AS text), 'advertiser_id', {adv}, "
        f"'kind', {kind}, 'remote_id', {rid})"
    )


def _dimension_rows(
    filters: ReportingFilter, params: _Params, bc_id: str
) -> tuple[str, str]:
    """返回 (维度 CTE 序列, 行 CTE 名)；行 CTE 列见 _ROW_COLUMNS。"""
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
    if dim == "account":
        idn = "idn AS (SELECT adv, adv AS rid FROM acc)"
        ent = f"""ent AS (
            SELECT i.adv, i.rid, COALESCE(a.name, i.adv) AS name,
                NULL::text AS campaign_id, NULL::text AS adgroup_id,
                NULL::text AS campaign_name, NULL::text AS adgroup_name, NULL::text AS ad_name,
                NULL::numeric AS target_roas, NULL::numeric AS campaign_budget,
                NULL::numeric AS adgroup_budget,
                bal.amount AS balance, bal.currency AS balance_currency, p.provider_label
            FROM idn i JOIN acc a ON a.adv = i.adv
            LEFT JOIN fm ON fm.adv = i.adv AND fm.rid = i.rid
            LEFT JOIN proj p ON p.adv = i.adv AND p.cid = fm.f_campaign_id
            LEFT JOIN LATERAL (
                SELECT CASE WHEN o.availability = 'AVAILABLE' THEN o.amount END AS amount, o.currency
                FROM account_balance_observation o
                WHERE o.tenant_id = :tenant AND o.advertiser_id = i.adv
                ORDER BY o.observed_at DESC, o.id DESC LIMIT 1
            ) bal ON TRUE
            LEFT JOIN obj e ON FALSE
            WHERE {where}
        )"""
    else:
        idn = (
            f"""idn AS (
            SELECT adv, rid FROM facts UNION SELECT adv, rid FROM obj WHERE kind = '{kind}'
        )"""
            if filters.include_empty
            # 默认只列所选日期有数据的对象，不把目录里无数据的历史对象逐一补 0；
            # 搜索/ID 筛选另外并入命中的无数据对象。
            else "idn AS (SELECT adv, rid FROM facts"
            + (" UNION SELECT adv, rid FROM dir_hits)" if dir_hits else ")")
        )
        target = "e.target_roas"
        budget = "e.budget_value"
        if kind == "campaign":
            joins = "LEFT JOIN proj p ON p.adv = i.adv AND p.cid = i.rid"
            campaign_id = "i.rid"
            adgroup_id = "NULL::text"
            campaign_name = "COALESCE(e.name, fm.f_campaign_name, i.rid)"
            adgroup_name = "NULL::text"
            ad_name = "NULL::text"
            name = campaign_name
        elif kind == "adgroup":
            joins = """LEFT JOIN LATERAL (SELECT COALESCE(e.parent, fm.f_campaign_id) AS cid) h ON TRUE
                LEFT JOIN obj c ON c.adv = i.adv AND c.kind = 'campaign' AND c.rid = h.cid
                LEFT JOIN proj p ON p.adv = i.adv AND p.cid = h.cid"""
            campaign_id = "h.cid"
            adgroup_id = "i.rid"
            campaign_name = "CASE WHEN h.cid IS NOT NULL THEN COALESCE(c.name, fm.f_campaign_name, h.cid) END"
            adgroup_name = "COALESCE(e.name, fm.f_adgroup_name, i.rid)"
            ad_name = "NULL::text"
            name = adgroup_name
        else:
            joins = """LEFT JOIN LATERAL (SELECT COALESCE(e.parent, fm.f_adgroup_id) AS gid) hg ON TRUE
                LEFT JOIN obj g ON e.adv IS NOT NULL AND g.adv = i.adv AND g.kind = 'adgroup'
                    AND g.rid = COALESCE(e.parent, '')
                LEFT JOIN LATERAL (SELECT COALESCE(g.parent, fm.f_campaign_id) AS cid,
                    CASE WHEN g.adv IS NOT NULL THEN g.parent ELSE fm.f_campaign_id END AS pcid) h ON TRUE
                LEFT JOIN obj gn ON gn.adv = i.adv AND gn.kind = 'adgroup' AND gn.rid = hg.gid
                LEFT JOIN obj c ON c.adv = i.adv AND c.kind = 'campaign' AND c.rid = h.cid
                LEFT JOIN proj p ON p.adv = i.adv AND p.cid = h.pcid"""
            campaign_id = "h.cid"
            adgroup_id = "hg.gid"
            campaign_name = "CASE WHEN h.cid IS NOT NULL THEN COALESCE(c.name, fm.f_campaign_name, h.cid) END"
            adgroup_name = "CASE WHEN hg.gid IS NOT NULL THEN COALESCE(gn.name, fm.f_adgroup_name, hg.gid) END"
            ad_name = "COALESCE(e.name, fm.f_ad_name, i.rid)"
            name = ad_name
        deleted = (
            # 剧维度不列出所选日期没有数据的已删除系列（多为测试/探针系列）。
            " AND NOT (fm.adv IS NULL AND e.adv IS NOT NULL AND COALESCE(e.delivery_status, '') LIKE '%DELETE')"
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
                p.provider_label, p.status AS naming, p.drama_name AS p_drama, fm.adv IS NOT NULL AS has_facts,
                COALESCE(a.name, i.adv) AS account_name
            FROM idn i JOIN acc a ON a.adv = i.adv
            LEFT JOIN obj e ON e.adv = i.adv AND e.kind = '{kind}' AND e.rid = i.rid
            LEFT JOIN fm ON fm.adv = i.adv AND fm.rid = i.rid
            {joins}
            WHERE {where}{deleted}
        )"""
    # 窄投影保证 (tenant, advertiser, kind, remote_id) 唯一。保持基表可内联，
    # 让层级连接保留约束与统计；先 UNION 多层目录会把十万行误估为个位数，
    # 再只按账户 merge、逐行过滤 parent ID，宽搜索退化为账户内笛卡尔积。
    metrics_only = _metrics_only(filters)
    if metrics_only:
        obj = _EMPTY_OBJ
    else:
        obj = f"obj AS NOT MATERIALIZED ({_obj_select('advertiser_id = ANY(:advs) AND kind = ANY(:kinds)')})"
    # 账户维度按账户的首个事实系列取版权方，整租户读取（账户数有限、行数小）。
    if dim == "account":
        proj = _proj_cte(full=True, fact_campaigns=None)
    elif metrics_only:
        proj = _EMPTY_PROJ
    else:
        proj = _proj_cte(
            full=filters.include_empty,
            fact_campaigns="SELECT adv, rid FROM facts"
            if table == "campaign_report_day"
            else "SELECT adv, f_campaign_id FROM facts",
        )
    # 纯指标筛选/排序不消费历史展示字段。保留列合同但不聚合每条事实
    # 的名称数组，否则facts物化会强迫长日期范围排序及反复读取宽字段。
    facts = _facts_cte(table, id_column, include_metadata=not metrics_only)
    # 非递归 CTE 只能引用前面的 CTE：列出无数据对象时身份依赖完整目录，
    # 默认时目录按身份读取，身份需先算出。
    ctes = (
        [facts, obj, proj, fact_meta, idn, ent]
        if dim == "account" or filters.include_empty
        else [facts, *([dir_hits] if dir_hits else []), idn, obj, proj, fact_meta, ent]
    )
    if dim == "account":
        ctes.append("""members AS (
            SELECT e.adv || ':account:' || e.adv AS row_key, e.adv, e.rid FROM ent e
        )""")
        ctes.append(f"""account_refs AS (
            SELECT o.adv, jsonb_agg({_ref_json("o.adv", "'campaign'", "o.rid")} ORDER BY o.rid COLLATE "C") AS refs
            FROM obj o WHERE o.kind = 'campaign' GROUP BY o.adv
        )""")
        ctes.append("""base AS (
            SELECT e.adv || ':account:' || e.adv AS row_key, e.name, e.name AS account_name,
                NULL::text AS campaign_name, NULL::text AS adgroup_name, NULL::text AS ad_name,
                NULL::text AS material_name, NULL::text AS drama_name,
                NULL::numeric AS target_roas, NULL::numeric AS campaign_budget, NULL::numeric AS adgroup_budget,
                e.balance, e.balance_currency,
                ARRAY[e.adv || ':account:' || e.adv, e.adv] AS ids,
                concat_ws(' ', e.adv || ':account:' || e.adv, e.adv, e.name, e.provider_label) AS haystack,
                -- 账户行冻结其下全部系列，供跨页选择展开；列表响应不返回。
                COALESCE(ar.refs, '[]'::jsonb) AS refs,
                '[]'::jsonb AS material_uses
            FROM ent e LEFT JOIN account_refs ar ON ar.adv = e.adv
        )""")
    elif dim == "drama":
        drama_key = (
            f"CASE WHEN e.naming = 'VALID' THEN {params.add('drama_prefix', f'{bc_id}:drama:')} "
            "|| '[' || COALESCE(to_json(e.p_drama)::text, 'null') || ']' "
            f"ELSE {params.add('external_prefix', f'{bc_id}:external:')} "
            "|| '[' || to_json(e.adv)::text || ',' || to_json(e.rid)::text || ']' END"
        )
        ctes.append(f"""members AS (
            SELECT {drama_key} AS row_key, e.*,
                row_number() OVER (PARTITION BY {drama_key} ORDER BY e.adv COLLATE "C", e.rid COLLATE "C") AS member_order
            FROM ent e
        )""")
        ctes.append(f"""base AS (
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
                jsonb_agg({_ref_json("m.adv", "'campaign'", "m.rid")} ORDER BY m.member_order) AS refs,
                '[]'::jsonb AS material_uses
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
                jsonb_build_array({_ref_json("e.adv", f"'{kind}'", "e.rid")}) AS refs,
                '[]'::jsonb AS material_uses
            FROM ent e
        )""")
    ctes.append(_member_buckets("members", unsupported=frozenset()))
    return ",\n".join([_common_ctes("ad_day"), *ctes]), "base"


_USE_COLUMNS = """u.id, u.advertiser_id AS adv, u.ad_remote_id, u.platform_material_id,
                CASE WHEN count(*) OVER (PARTITION BY u.tenant_id,u.advertiser_id,u.ad_remote_id,
                    u.platform_material_id,u.material_type)>1 THEN NULL ELSE u.ad_material_id END AS ad_material_id,
                u.material_type, NULLIF(u.name, '') AS name"""


def _material_identity_ctes(filters: ReportingFilter) -> list[str]:
    """素材维度的帖子归属 fk 与目录使用位置 uses。

    列出无数据视频时读取全部使用位置与帖子对照。默认只列有数据的帖子/视频：
    已归属视频只读这些视频的使用位置；无法归属视频的帖子在cand_raw中
    按准确广告身份读取，避免全量uses反复连接和重复帖子影响歧义计数。
    """
    fk = """fk AS (
        SELECT DISTINCT f.adv,f.rid,f.main_material_type AS mtype,f.rid AS row_key,
            CASE WHEN (f.rid::jsonb)->>0='material' THEN (f.rid::jsonb)->>1 ELSE (f.rid::jsonb)->>2 END AS mid,
            CASE WHEN (f.rid::jsonb)->>0='material' THEN (f.rid::jsonb)->>1 END AS vid
        FROM facts f
    )"""
    if filters.include_empty:
        return [
            fk,
            f"""uses AS (
            SELECT {_USE_COLUMNS} FROM current_ad_material u
            WHERE u.tenant_id=:tenant AND u.advertiser_id=ANY(:advs) AND u.material_type='VIDEO'
        )""",
        ]
    return [
        fk,
        f"""uses AS (
        SELECT {_USE_COLUMNS}
        FROM (SELECT DISTINCT vid FROM fk WHERE vid IS NOT NULL) k
        JOIN current_ad_material u ON u.tenant_id=:tenant AND u.platform_material_id=k.vid
            AND u.advertiser_id=ANY(:advs) AND u.material_type='VIDEO'
    )""",
    ]


def _material_rows(filters: ReportingFilter, params: _Params) -> tuple[str, str]:
    """素材维度：一个素材库视频一行，跨账户、跨广告合计。

    素材日报的主素材是帖子（Spark 每条广告一条），先经 material_post_link 与广告目录
    归到视频；对不上的帖子各自一行，保证金额不丢。勾选“显示无数据对象”时，
    目录里有、所选日期无数据的视频也列出，按账户覆盖填 0 或空。
    """
    params.add("kinds", ["campaign", "adgroup", "ad"])
    entity_filter = _entity_conditions(filters, params, e="ae", p="p")
    has_entity_filter = bool(entity_filter)
    use_match = " AND ".join(entity_filter) or "TRUE"
    null_ok = "TRUE" if _null_entity_matches(filters) else "FALSE"
    use_ids = "ARRAY[u.adv, u.campaign_id, u.adgroup_id, u.ad_remote_id]"
    use_text = "concat_ws(' ', u.adv, u.account_name, u.campaign_id, u.campaign_name, u.adgroup_id, u.adgroup_name, u.ad_remote_id, u.ad_name)"
    row_ids = "array_remove(ARRAY[r.row_key, r.mid] || r.post_ids || r.advs || r.ref_ids, NULL)"
    row_text = "concat_ws(' ', r.row_key, r.mid, array_to_string(r.post_ids, ' '), array_to_string(r.advs, ' '), array_to_string(r.ref_ids, ' '), r.name, r.account_name)"
    plain = _text_conditions(filters, params, ids=row_ids, haystack=row_text)
    text_ok = " AND ".join(plain) or "TRUE"
    if plain:
        # 搜索/层级筛选可命中某个使用位置（行展示 + 该位置的层级），但金额仍是整份素材合计。
        hier = " AND ".join(
            _text_conditions(
                filters,
                params,
                ids=f"array_remove(ARRAY[r.row_key, r.mid] || r.post_ids || r.advs || r.ref_ids || {use_ids}, NULL)",
                haystack=f"concat_ws(' ', {row_text}, {use_text})",
            )
        )
        text_ok = (
            f"(({text_ok}) OR r.row_key IN (SELECT hit.row_key FROM hier_hits hit))"
        )
    else:
        hier = None
    use_order = (
        'u.adv COLLATE "C", u.ad_remote_id COLLATE "C", u.platform_material_id COLLATE "C", '
        'COALESCE(u.ad_material_id, \'\') COLLATE "C", u.material_type COLLATE "C"'
    )
    video_key = "'[\"material\", ' || to_json({vid})::text || ']'"
    ctes = [
        _common_ctes("material_day"),
        _facts_cte(
            "material_entity_day",
            "row_key",
            extra_group=("main_material_type",),
        ),
        *_material_identity_ctes(filters),
        f"""mkey AS (
            SELECT row_key, min(mid) AS mid FROM (
                SELECT row_key, mid FROM fk
                {
            ""
            if not filters.include_empty
            else f"UNION ALL SELECT {video_key.format(vid='u.platform_material_id')}, u.platform_material_id FROM uses u"
        }
            ) k GROUP BY row_key
        )""",
        f"fname AS ({material_names_sql()})",
        f"""cand_raw AS (
            -- 视频行管理该视频的全部使用位置；帖子行只有对照上的那条广告位置。
            SELECT {video_key.format(vid="u.platform_material_id")} AS row_key, u.*
            FROM uses u
            -- 默认只列有数据的视频：视频行只带出本次有事实的视频的使用位置。
            {"" if filters.include_empty else "WHERE u.platform_material_id IN (SELECT mid FROM fk)"}
            UNION ALL
            SELECT k.row_key, u.*
            FROM fk k
            JOIN material_post_link l ON l.tenant_id = :tenant AND l.advertiser_id = k.adv
                AND l.main_material_id = (k.rid::jsonb)->>2 AND l.main_material_type = k.mtype
            CROSS JOIN LATERAL (
                -- 先在真实目录身份内判定歧义，再匹配帖子。多个帖子引用同一
                -- 使用位置不会凭空增加目录候选数，也不再重复扫描全量uses。
                SELECT {_USE_COLUMNS} FROM current_ad_material u
                WHERE u.tenant_id=:tenant AND u.advertiser_id=l.advertiser_id
                    AND u.ad_remote_id=l.ad_remote_id AND u.material_type='VIDEO'
            ) u
            WHERE k.vid IS NULL AND u.ad_material_id = l.ad_material_id
        )""",
        _obj_ctes(
            filters,
            (
                ("ad", "SELECT adv, ad_remote_id FROM cand_raw"),
                ("adgroup", None),
                ("campaign", None),
            ),
        ),
        _proj_cte(full=filters.include_empty, fact_campaigns=None),
        f"""cand AS (
            SELECT DISTINCT ON (u.row_key, u.adv, u.ad_remote_id, u.platform_material_id, u.ad_material_id, u.material_type)
                u.*, g.parent AS campaign_id, ae.parent AS adgroup_id,
                CASE WHEN ae.adv IS NOT NULL THEN COALESCE(ae.name, u.ad_remote_id) ELSE u.ad_remote_id END AS ad_name,
                CASE WHEN g.adv IS NOT NULL THEN COALESCE(g.name, g.rid) ELSE ae.parent END AS adgroup_name,
                CASE WHEN g.parent IS NULL THEN NULL WHEN c.adv IS NOT NULL THEN COALESCE(c.name, c.rid)
                    ELSE g.parent END AS campaign_name,
                COALESCE(a.name, u.adv) AS account_name,
                ({use_match}) AS matched
            FROM cand_raw u
            JOIN acc a ON a.adv = u.adv
            LEFT JOIN obj ae ON ae.adv = u.adv AND ae.kind = 'ad' AND ae.rid = u.ad_remote_id
            LEFT JOIN obj g ON g.adv = u.adv AND g.kind = 'adgroup' AND g.rid = COALESCE(ae.parent, '')
            LEFT JOIN obj c ON c.adv = u.adv AND c.kind = 'campaign' AND c.rid = g.parent
            LEFT JOIN proj p ON p.adv = u.adv AND p.cid = g.parent
            ORDER BY u.row_key, u.adv, u.ad_remote_id, u.platform_material_id, u.ad_material_id, u.material_type, u.id
        )"""
        if has_entity_filter or hier
        else """cand AS (
            SELECT DISTINCT ON (u.row_key,u.adv,u.ad_remote_id,u.platform_material_id,u.ad_material_id,u.material_type)
                u.*, COALESCE(a.name,u.adv) AS account_name, TRUE AS matched
            FROM cand_raw u JOIN acc a ON a.adv=u.adv
            ORDER BY u.row_key,u.adv,u.ad_remote_id,u.platform_material_id,u.ad_material_id,u.material_type,u.id
        )""",
        f"""muse AS (SELECT * FROM cand WHERE {"matched" if has_entity_filter else "TRUE"})""",
        """cand_names AS (SELECT row_key, min(name COLLATE "C") AS name FROM cand GROUP BY row_key)""",
        f"""use_refs AS (
            -- 管理引用按排序后的使用位置首次出现顺序去重，与选择展开一致。
            SELECT x.row_key, jsonb_agg(x.ref ORDER BY x.pos) AS refs,
                array_agg(x.ad_remote_id) AS ref_ids
            FROM (
                SELECT u.row_key, u.ad_remote_id, {_ref_json("u.adv", "'ad'", "u.ad_remote_id")} AS ref,
                    min(u.pos) AS pos
                FROM (SELECT u.*, row_number() OVER (PARTITION BY u.row_key ORDER BY {use_order}) AS pos
                      FROM muse u) u
                GROUP BY u.row_key, u.adv, u.ad_remote_id
            ) x
            GROUP BY x.row_key
        )""",
        f"""use_list AS (
            SELECT u.row_key, jsonb_agg(jsonb_build_object(
                    'ad_ref', {_ref_json("u.adv", "'ad'", "u.ad_remote_id")},
                    'platform_material_id', u.platform_material_id,
                    'ad_material_id', u.ad_material_id, 'material_type', u.material_type)
                ORDER BY {use_order}) AS uses
            FROM muse u GROUP BY u.row_key
        )""",
        """row_advs AS (
            -- 行涉及的账户：有事实的账户 ∪ 目录使用位置所在账户；覆盖与空值按这些账户算。
            SELECT row_key, adv FROM fk
            UNION
            SELECT row_key, adv FROM cand
        )""",
        """adv_list AS (
            SELECT ra.row_key, array_agg(ra.adv ORDER BY ra.adv COLLATE "C") AS advs,
                CASE WHEN count(*) = 1 THEN COALESCE(min(a.name), min(ra.adv)) END AS account_name
            FROM row_advs ra JOIN acc a ON a.adv = ra.adv GROUP BY ra.row_key
        )""",
        """post_list AS (
            SELECT d.row_key,array_agg(DISTINCT p.mid) AS post_ids
            FROM material_entity_day d CROSS JOIN LATERAL unnest(d.post_ids) p(mid)
            WHERE d.tenant_id=:tenant AND d.advertiser_id=ANY(:advs) AND d.stat_date BETWEEN :start AND :end
            GROUP BY d.row_key
        )""",
        """rowsrc AS (
            SELECT k.row_key, k.mid,
                -- 视频名优先取目录（各帖子一致），否则取素材日报里的名称。
                COALESCE(cn.name, fn.name, k.mid) AS name,
                al.account_name, al.advs,
                COALESCE(pl.post_ids, ARRAY[]::text[]) AS post_ids,
                COALESCE(ur.ref_ids, ARRAY[]::text[]) AS ref_ids,
                COALESCE(ur.refs, '[]'::jsonb) AS refs,
                COALESCE(ul.uses, '[]'::jsonb) AS material_uses,
                ul.row_key IS NOT NULL AS has_uses
            FROM mkey k
            -- mkey来自同一授权fk/uses，必有row_advs；此处只补展示信息，
            -- LEFT JOIN使无名称/账户搜索时可裁掉不需要的全候选聚合。
            LEFT JOIN adv_list al ON al.row_key = k.row_key
            LEFT JOIN fname fn ON fn.row_key = k.row_key
            LEFT JOIN cand_names cn ON cn.row_key = k.row_key
            LEFT JOIN post_list pl ON pl.row_key = k.row_key
            LEFT JOIN use_refs ur ON ur.row_key = k.row_key
            LEFT JOIN use_list ul ON ul.row_key = k.row_key
        )""",
        _material_member_buckets(),
        _row_buckets(),
    ]
    if hier:
        ctes.append(f"""hier_hits AS (
            SELECT DISTINCT r.row_key FROM rowsrc r JOIN muse u ON u.row_key = r.row_key WHERE {hier}
        )""")
    metric_ok = _metric_bounds(filters, params) or "TRUE"
    ctes.append(f"""base AS (
        SELECT r.row_key, r.name, r.account_name, NULL::text AS campaign_name, NULL::text AS adgroup_name,
            NULL::text AS ad_name, r.name AS material_name, NULL::text AS drama_name,
            NULL::numeric AS target_roas, NULL::numeric AS campaign_budget, NULL::numeric AS adgroup_budget,
            NULL::numeric AS balance, NULL::text AS balance_currency, r.refs, r.material_uses
        FROM rowsrc r
        WHERE (r.has_uses OR {null_ok}) AND {text_ok} AND {metric_ok}
    )""")
    return ",\n".join(ctes), "base"


def _ctes(
    filters: ReportingFilter, *, tenant_id: UUID, advertisers: Sequence[str], bc_id: str
) -> tuple[str, dict[str, Any]]:
    params = _Params()
    params.add("tenant", tenant_id)
    params.add("advs", sorted(advertisers))
    params.add("start", filters.start_date)
    params.add("end", filters.end_date)
    params.add("ndays", (filters.end_date - filters.start_date).days + 1)
    # fr会被排序、汇总、coverage多次引用并物化；只冻结真正需要的列，
    # 否则未筛选/排序的名称与展示关联也会被强制计算到全部候选。
    retained = ["r.row_key", "r.refs", "r.material_uses"]
    if filters.sort_by in NAME_SORT_FIELDS | CONFIG_SORT_FIELDS:
        retained.append(f"r.{filters.sort_by}")
        if filters.sort_by == "balance":
            retained.append("r.balance_currency")
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
        buckets = f",\n{_row_buckets()}"
        filtered = f"fr AS (SELECT {projection} FROM {base} r WHERE {' AND '.join(conditions) or 'TRUE'})"
    ordered = f"""ordered AS (
        SELECT r.row_key, r.refs, r.material_uses,
            (row_number() OVER (ORDER BY {_order_by(filters)}) - 1)::int AS seq
        FROM fr r LEFT JOIN rv v ON v.row_key = r.row_key
    )"""
    return f"WITH {body}{buckets},\n{filtered},\n{ordered}", params.values


def _summary_select(unsupported: frozenset[str]) -> str:
    """汇总只算最终行：值为全部成员合计，部分合计规则同 merge_metric_buckets。"""
    columns = ", ".join(
        f"sum(x.{m})::text AS {m}, "
        f"(sum(x.{m}) IS NOT NULL AND (bool_or(x.row_incomplete) OR bool_or(x.{m} IS NULL) "
        f"OR bool_or(x.{m}_hn))) AS {m}_partial"
        for m in METRIC_COLUMNS
        if m not in unsupported
    )
    return f"""SELECT x.currency,
            CASE WHEN count(DISTINCT x.tz) = 1 THEN min(x.tz) ELSE 'MIXED' END AS tz, {columns}
        FROM (SELECT mb.*, bool_or(mb.incomplete) OVER (PARTITION BY mb.row_key) AS row_incomplete
              FROM mb JOIN fr ON fr.row_key = mb.row_key) x
        GROUP BY x.currency ORDER BY x.currency"""


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
    rows = session.connection().execute(
        text(f"{ctes} SELECT row_key, refs, material_uses FROM ordered ORDER BY seq"),
        params,
    )
    return tuple(
        SnapshotMember(r.row_key, tuple(r.refs), tuple(r.material_uses)) for r in rows
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
        return SnapshotTotals(0, (), (), (), True, ())
    ctes, params = _ctes(
        filters, tenant_id=tenant_id, advertisers=advertisers, bc_id=bc_id
    )
    params |= {"snapshot_id": snapshot_id, "dimension": filters.dimension}
    unsupported = (
        MATERIAL_UNSUPPORTED if filters.dimension == "material" else frozenset()
    )
    coverage_source = (
        "SELECT tenant_id,advertiser_id,kind AS report_kind,bucket AS stat_date,derived_status AS status FROM read_bucket_state"
        if filters.dimension == "material"
        else "SELECT tenant_id,advertiser_id,report_kind,stat_date,status FROM report_sync_state"
    )
    statement = f"""{ctes},
    ins AS (
        INSERT INTO query_snapshot_row (snapshot_id, row_key, stable_sequence, dimension, display, refs,
            material_uses, metric_buckets, capabilities, directory_versions, membership_digest)
        SELECT :snapshot_id, row_key, seq, :dimension, '{{}}'::jsonb, refs, material_uses,
            '[]'::jsonb, '{{}}'::jsonb, '{{}}'::jsonb, ''
        FROM ordered
        RETURNING 1
    ),
    bad AS (
        SELECT DISTINCT mb.adv FROM mb JOIN fr ON fr.row_key = mb.row_key WHERE mb.incomplete
    )
    SELECT
        (SELECT count(*) FROM ins) AS total,
        (SELECT COALESCE(jsonb_agg(to_jsonb(s)), '[]'::jsonb) FROM ({_summary_select(unsupported)}) s) AS buckets,
        (SELECT COALESCE(array_agg(adv ORDER BY adv), ARRAY[]::text[]) FROM bad) AS bad_advs,
        (SELECT COALESCE(array_agg(DISTINCT d.day::date ORDER BY d.day::date), ARRAY[]::date[])
           FROM bad b CROSS JOIN generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') d(day)
           WHERE NOT EXISTS (SELECT 1 FROM ({coverage_source}) s WHERE s.tenant_id = :tenant
               AND s.advertiser_id = b.adv AND s.report_kind = '{"material_day" if filters.dimension == "material" else "ad_day"}'
               AND s.stat_date = d.day::date AND s.status = 'COMPLETE')) AS bad_dates,
        (SELECT COALESCE(array_agg(DISTINCT ARRAY[r ->> 'advertiser_id', r ->> 'remote_id']), ARRAY[]::text[][])
           FROM fr, jsonb_array_elements(fr.refs) r WHERE r ->> 'kind' = 'campaign') AS campaigns
    """
    row = session.connection().execute(text(statement), params).one()
    bad_dates: Sequence[date] = row.bad_dates
    return SnapshotTotals(
        total=row.total,
        buckets=tuple(row.buckets),
        incomplete_advertisers=tuple(row.bad_advs),
        incomplete_dates=tuple(day.isoformat() for day in bad_dates),
        complete=not row.bad_advs,
        naming_campaigns=tuple((a, c) for a, c in row.campaigns),
    )
