"""素材维度行补全：一个素材库视频一行，跨账户、跨广告合计帖子事实。

成员、筛选与排序由 row_set 在数据库内完成；这里只为冻结的素材行补全展示与指标。
帖子到视频的归属见 material_links。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlmodel import Session, select

from app.integrations.tiktok.contracts.ads import MaterialUseRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    _aggregate,
    _coverage,
    empty_metric_values,
    fact_vector,
    merge_metric_buckets,
    metric_vector,
    scoped_rows,
)
from app.modules.reporting.read_models.coverage import (
    projected_states,
    projection_coverage,
)
from app.modules.reporting.read_models.material_names import material_names_sql
from app.modules.reporting.read_models.models import (
    CurrentAdMaterial as AdMaterialReference,
)
from app.modules.reporting.read_models.models import MaterialEntityDay
from app.modules.reporting.row_set import (
    MATERIAL_UNSUPPORTED,
    SnapshotMember,
    entity_ref,
)
from app.modules.reporting.schemas import ReportingFilter, ReportRow
from app.modules.reporting.store_models import ReportSyncState


def _use(data: dict[str, Any]) -> MaterialUseRef:
    return MaterialUseRef(
        entity_ref(data["ad_ref"]),
        data["platform_material_id"],
        data.get("ad_material_id"),
        data["material_type"],
    )


def materialize_material_rows(
    session: Session,
    *,
    tenant_id: UUID,
    filters: ReportingFilter,
    members: Sequence[SnapshotMember],
    advertisers: Sequence[str],
) -> tuple[ReportRow, ...]:
    """为冻结的素材行补全展示与指标；行身份与 row_set._material_rows 一致。

    视频行合计该视频在全部账户、全部广告的帖子事实；帖子行只合计这一条帖子。
    """
    scope = sorted(set(advertisers))
    keys = {member.row_key: json.loads(member.row_key) for member in members}
    names: dict[str, str] = (
        {
            row.row_key: row.name
            for row in session.connection()
            .execute(
                text(material_names_sql(page=True)),
                {
                    "tenant": tenant_id,
                    "advs": scope,
                    "start": filters.start_date,
                    "end": filters.end_date,
                    "row_keys": list(keys),
                },
            )
            .all()
        }
        if keys
        else {}
    )
    videos = {key[1] for key in keys.values() if key[0] == "material"}
    posts_by_row: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    # 当前页素材驱动索引探测；OFFSET 0保留相关子查询，避免大账户集合
    # 把计划改成按账户扫描整个目录。账户条件在外层SQL过滤，防止每个素材
    # 又按全部账户展开索引探测；全部合法使用位置仍返回，不加LIMIT或截断。
    usages = (
        session.execute(
            select(AdMaterialReference).from_statement(
                text("""SELECT u.* FROM unnest(CAST(:videos AS text[])) v(id)
                CROSS JOIN LATERAL (
                    SELECT * FROM current_ad_material
                    WHERE tenant_id=:tenant AND platform_material_id=v.id
                        AND complete AND material_type='VIDEO'
                    OFFSET 0
                ) u
                WHERE u.advertiser_id=ANY(CAST(:advertisers AS text[]))""")
            ),
            {"tenant": tenant_id, "videos": sorted(videos), "advertisers": scope},
        )
        .scalars()
        .all()
        if videos
        else []
    )
    usage_by_video: dict[str, list[AdMaterialReference]] = defaultdict(list)
    for usage in usages:
        usage_by_video[usage.platform_material_id].append(usage)
    data = (
        _aggregate(
            session,
            model=MaterialEntityDay,
            group_cols=["advertiser_id", "row_key", "main_material_type"],
            tenant_id=tenant_id,
            advertisers=scope,
            start=filters.start_date,
            end=filters.end_date,
            ids=list(keys),
        )
        if keys
        else []
    )
    facts: dict[tuple[str, str, str], list[RowMapping]] = defaultdict(list)
    for item in data:
        identity = (
            item["advertiser_id"],
            item["row_key"],
            item["main_material_type"],
        )
        row_key = item["row_key"]
        if row_key in keys:
            posts_by_row[row_key].add(identity)
            facts[identity].append(item)
    accounts = {
        a.advertiser_id: a
        for a in scoped_rows(
            session, AdvertiserAccount, tenant_id=tenant_id, advertisers=scope
        )
    }
    states, projection_states = projected_states(
        session,
        tenant_id=tenant_id,
        advertisers=scope,
        start=filters.start_date,
        end=filters.end_date,
    )
    # 先按账户分组；每次覆盖计算只遍历对应账户的日期，避免账户数的平方级扫描。
    states_by_adv: dict[str, list[ReportSyncState]] = defaultdict(list)
    for state in states:
        states_by_adv[state.advertiser_id].append(state)
    incomplete_by_adv = {
        adv: _coverage(
            states_by_adv[adv], advertisers={adv}, filters=filters, kind="material_day"
        )["status"]
        not in COMPLETE_COVERAGE_STATUSES
        for adv in scope
    }
    empty_by_adv = {
        adv: empty_metric_values(
            states_by_adv[adv], advertiser=adv, filters=filters, kind="material_day"
        )
        for adv in accounts
    }
    result = []
    for member in members:
        key = keys[member.row_key]
        posts = posts_by_row.get(member.row_key, set())
        row_usages = usage_by_video.get(key[1], []) if key[0] == "material" else []
        # 行涉及的账户与快照 row_advs 一致：有事实的账户 ∪ 使用位置所在账户。
        row_advs = sorted(
            {adv for adv, _, _ in posts}
            | {u.advertiser_id for u in row_usages}
            | {use.ad_ref.advertiser_id for use in map(_use, member.material_uses)}
        )
        row_advs = [adv for adv in row_advs if adv in accounts]
        items = [item for post in sorted(posts) for item in facts.get(post, [])]
        coverage = _coverage(
            [state for adv in row_advs for state in states_by_adv[adv]],
            advertisers=set(row_advs),
            filters=filters,
            kind="material_day",
        )
        coverage["projection"] = projection_coverage(
            [state for state in projection_states if state.advertiser_id in row_advs]
        )
        uses = tuple(_use(item) for item in member.material_uses)
        if not uses:
            coverage["reason"] = "usage_unknown"
        fact_advs = {item["advertiser_id"] for item in items}
        # 账户覆盖不完整时，该账户已同步部分的金额标 PARTIAL（与其他维度同一规则）。
        buckets = [
            (
                (fact_vector(item, unsupported=MATERIAL_UNSUPPORTED),),
                incomplete_by_adv[item["advertiser_id"]],
            )
            for item in items
        ] + [
            # 没有事实的账户按覆盖填 0 或空，与快照排序口径一致。
            (
                (
                    metric_vector(
                        empty_by_adv[adv],
                        currency=accounts[adv].currency,
                        timezone=accounts[adv].timezone,
                        unsupported=MATERIAL_UNSUPPORTED,
                    ),
                ),
                incomplete_by_adv[adv],
            )
            for adv in row_advs
            if adv not in fact_advs
        ]
        mid = key[1] if key[0] == "material" else key[2]
        # 与快照排序同一取值：目录名优先（各帖子一致），否则取素材日报名称。
        name = (
            min((u.name for u in row_usages if u.name), default=None)
            or names.get(member.row_key)
            or mid
        )
        single = row_advs[0] if len(row_advs) == 1 else None
        result.append(
            ReportRow(
                row_key=member.row_key,
                display={
                    "name": name,
                    "material_name": name,
                    "main_material_id": mid,
                    "material_type": (
                        items[0].get("main_material_type") if items else None
                    )
                    or (row_usages[0].material_type if row_usages else None),
                    "account_id": single,
                    "advertiser_id": single,
                    "account_name": (accounts[single].name or single)
                    if single
                    else None,
                    # 跨账户合计的视频行没有单一账户；页面用账户数提示合计范围。
                    "account_count": str(len(row_advs)),
                },
                refs=tuple(entity_ref(ref) for ref in member.refs),
                material_uses=uses,
                metric_buckets=merge_metric_buckets(buckets),
                coverage=coverage,
                membership_digest=sha256(
                    json.dumps(
                        [
                            (
                                u.ad_ref.advertiser_id,
                                u.ad_ref.remote_id,
                                u.platform_material_id,
                                u.ad_material_id,
                                u.material_type,
                            )
                            for u in uses
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                directory_versions={str(u.id): u.published_version for u in row_usages},
            )
        )
    return tuple(result)
