"""创意素材行补全；分页、导出与快照使用相同的逐创意筛选。"""

import json
from collections import defaultdict
from hashlib import sha256

from sqlalchemy import text
from sqlmodel import select

from app.integrations.tiktok.contracts.ads import MaterialUseRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.creative_query import (
    UNALLOCATED_NOTE,
    creative_states,
    material_display_name,
    read_facts,
    read_unallocated,
)
from app.modules.reporting.dimension_query import (
    _coverage,
    empty_metric_values,
    merge_metric_buckets,
    metric_vector,
    scoped_rows,
)
from app.modules.reporting.read_models.coverage import (
    projected_states,
    projection_coverage,
)
from app.modules.reporting.read_models.models import CurrentAdMaterial
from app.modules.reporting.row_set import entity_ref
from app.modules.reporting.schemas import ReportRow
from app.modules.reporting.store_models import METRIC_COLUMNS


def _use(data):
    return MaterialUseRef(
        entity_ref(data["ad_ref"]),
        data["platform_material_id"],
        data.get("ad_material_id"),
        data["material_type"],
    )


def materialize_material_rows(
    session,
    *,
    tenant_id,
    filters,
    members,
    advertisers,
    exact_ad=None,
    selected_facts=None,
):
    scope = set(advertisers)
    facts = (
        selected_facts
        if selected_facts is not None
        else read_facts(
            session,
            tenant_id=tenant_id,
            advertisers=advertisers,
            filters=filters,
            row_keys=[m.row_key for m in members],
            exact_ad=exact_ad,
        )
    )
    directory = read_facts(
        session,
        tenant_id=tenant_id,
        advertisers=advertisers,
        filters=filters,
        row_keys=[m.row_key for m in members],
        exact_ad=exact_ad,
        directory=True,
    )
    missing = read_unallocated(
        session,
        tenant_id=tenant_id,
        advertisers=advertisers,
        filters=filters,
        row_keys=[m.row_key for m in members],
        exact_ad=exact_ad,
    )
    directory_by_key = defaultdict(list)
    for usage in directory:
        directory_by_key[usage["row_key"]].append(usage)
    by_key = defaultdict(list)
    for fact in facts:
        by_key[fact["row_key"]].append(fact)
    page_scope = sorted(
        scope
        & (
            {f["advertiser_id"] for f in facts}
            | {u["advertiser_id"] for u in directory}
            | {
                json.loads(m.row_key)[1]
                for m in members
                if json.loads(m.row_key)[0] != "material"
            }
            | {u["ad_ref"]["advertiser_id"] for m in members for u in m.material_uses}
        )
    )
    accounts = {
        a.advertiser_id: a
        for a in scoped_rows(
            session, AdvertiserAccount, tenant_id=tenant_id, advertisers=page_scope
        )
    }
    states = creative_states(
        session,
        tenant_id=tenant_id,
        advertisers=page_scope,
        start=filters.start_date,
        end=filters.end_date,
    )
    states_by_adv = defaultdict(list)
    for state in states:
        states_by_adv[state.advertiser_id].append(state)
    _, projections = projected_states(
        session,
        tenant_id=tenant_id,
        advertisers=page_scope,
        start=filters.start_date,
        end=filters.end_date,
        kind="creative_material",
    )
    projections_by_adv = defaultdict(list)
    for projection in projections:
        projections_by_adv[projection.advertiser_id].append(projection)
    # 当前目录仅补名称与版本；不可变快照证据仍决定动作对象，不重新扩大使用位置。
    videos = {
        json.loads(m.row_key)[2]
        for m in members
        if json.loads(m.row_key)[:2] == ["material", "VIDEO"]
    }
    current = (
        session.execute(
            select(CurrentAdMaterial).from_statement(
                text("""
        SELECT u.* FROM unnest(CAST(:videos AS text[])) v(id)
        CROSS JOIN LATERAL (
            SELECT * FROM current_ad_material WHERE tenant_id=:tenant AND platform_material_id=v.id
                AND complete AND material_type='VIDEO' OFFSET 0
        ) u WHERE u.advertiser_id=ANY(CAST(:advs AS text[]))
    """)
            ),
            {"tenant": tenant_id, "videos": sorted(videos), "advs": page_scope},
        )
        .scalars()
        .all()
        if videos
        else []
    )
    current_by_use = {
        (u.advertiser_id, u.ad_remote_id, u.platform_material_id, u.ad_material_id): u
        for u in current
    }
    result = []
    for member in members:
        key = json.loads(member.row_key)
        items = by_key[member.row_key]
        directory_items = directory_by_key[member.row_key]
        row_missing = [m for m in missing if m["row_key"] == member.row_key]
        uses = tuple(_use(u) for u in member.material_uses)
        row_advs = sorted(
            scope
            & (
                {f["advertiser_id"] for f in items}
                | {u["advertiser_id"] for u in directory_items}
                | ({key[1]} if key[0] != "material" else set())
                | {u.ad_ref.advertiser_id for u in uses}
            )
        )
        row_states = [s for adv in row_advs for s in states_by_adv[adv]]
        coverage = _coverage(
            row_states, advertisers=row_advs, filters=filters, kind="creative_material"
        )
        coverage["projection"] = projection_coverage(
            [p for adv in row_advs for p in projections_by_adv[adv]]
        )
        statuses = sorted({f["source_status"] for f in items})
        coverage["advertiser_ids"] = row_advs
        coverage["source_statuses"] = statuses
        coverage["attribution_statuses"] = sorted(
            {f["attribution_status"] for f in items}
        )
        coverage["assets"] = [
            {"creative_id": f["creative_id"], "assets": f["assets"]}
            for f in {f["creative_id"]: f for f in items if f["creative_id"]}.values()
        ]
        coverage["source_info"] = [f["source_info"] for f in items]
        if (
            any(s != "COMPLETE" for s in statuses)
            or any(f["unallocated"] for f in items)
            or row_missing
        ) and coverage["status"] == "COMPLETE":
            coverage["status"] = "INCOMPLETE"
        if key[0] == "unattributed":
            coverage["reason"] = "group_reconciliation_difference"
        elif key[0] == "unmapped_creative":
            coverage["reason"] = "creative_identity_unmapped"
        elif key[0] == "carousel":
            coverage["reason"] = "composite_creative_not_allocated_to_images"
        elif not uses:
            coverage["reason"] = (
                "usage_unknown" if key[1] == "VIDEO" else "material_actions_unavailable"
            )
        if (
            filters.ids
            or filters.operation_statuses
            or filters.review_statuses
            or filters.query
        ):
            coverage["unallocated_note"] = (
                "广告组对账差额仅按广告组保留，无法分摊到单条广告或素材。"
            )
        if row_missing:
            coverage["unallocated_note"] = UNALLOCATED_NOTE
            coverage["unallocated_group_count"] = len(
                {
                    (m["advertiser_id"], m["adgroup_id"], m["currency"], m["timezone"])
                    for m in row_missing
                }
            )
        buckets = []
        for f in items:
            incomplete = (
                _coverage(
                    states_by_adv[f["advertiser_id"]],
                    advertisers=[f["advertiser_id"]],
                    filters=filters,
                    kind="creative_material",
                )["status"]
                != "COMPLETE"
            )
            vector = metric_vector(
                f,
                currency=f["currency"],
                timezone=f["timezone"],
                partial={m for m in METRIC_COLUMNS if f[f"{m}_has_null"]},
            )
            buckets.append(
                (
                    (vector,),
                    incomplete
                    or f["source_status"] != "COMPLETE"
                    or f["unallocated"]
                    or any(
                        m["advertiser_id"] == f["advertiser_id"]
                        and m["currency"] == f["currency"]
                        for m in row_missing
                    ),
                )
            )
        # 先保留每个账户的缺失贡献，不能用共享 VID 另一账户的同币种事实抵消。
        for _, currency, timezone in {
            (m["advertiser_id"], m["currency"], m["timezone"]) for m in row_missing
        } - {(f["advertiser_id"], f["currency"], f["timezone"]) for f in items}:
            buckets.append(
                (
                    (
                        metric_vector(
                            dict.fromkeys(METRIC_COLUMNS),
                            currency=currency,
                            timezone=timezone,
                        ),
                    ),
                    True,
                )
            )
        for adv in (
            set(row_advs)
            - {f["advertiser_id"] for f in items}
            - {m["advertiser_id"] for m in row_missing}
        ):
            account = accounts[adv]
            buckets.append(
                (
                    (
                        metric_vector(
                            empty_metric_values(
                                states_by_adv[adv],
                                advertiser=adv,
                                filters=filters,
                                kind="creative_material",
                            ),
                            currency=account.currency,
                            timezone=account.timezone,
                        ),
                    ),
                    _coverage(
                        states_by_adv[adv],
                        advertisers=[adv],
                        filters=filters,
                        kind="creative_material",
                    )["status"]
                    != "COMPLETE",
                )
            )
        current_uses = [
            current_by_use[k]
            for u in uses
            if (
                k := (
                    u.ad_ref.advertiser_id,
                    u.ad_ref.remote_id,
                    u.platform_material_id,
                    u.ad_material_id,
                )
            )
            in current_by_use
        ]
        mid = key[2]
        name = min(
            [f["material_name"] for f in items if f["material_name"]]
            + [u["name"] for u in directory_items if u["name"]]
            + [u.name for u in current_uses if u.name],
            default=mid,
        )
        name = material_display_name(key, name)
        single = row_advs[0] if len(row_advs) == 1 else None
        result.append(
            ReportRow(
                row_key=member.row_key,
                display={
                    "name": name,
                    "material_name": name,
                    "main_material_id": mid,
                    "material_type": key[1] if key[0] == "material" else key[0].upper(),
                    "account_id": single,
                    "advertiser_id": single,
                    "account_name": (accounts[single].name or single)
                    if single
                    else None,
                    "account_count": str(len(row_advs)),
                },
                refs=tuple(entity_ref(r) for r in member.refs),
                material_uses=uses,
                metric_buckets=merge_metric_buckets(buckets),
                coverage=coverage,
                membership_digest=sha256(
                    json.dumps(member.material_uses, sort_keys=True).encode()
                ).hexdigest(),
                directory_versions={
                    str(u.id): u.published_version for u in current_uses
                },
            )
        )
    return tuple(result)


def freeze_instance_tracer(session, *, snapshot, filters, reports, facts):
    """首切仅发布精确单creative实例；复杂身份仍保留原行覆盖，交由后票扩展。"""
    from app.core.context import TenantContext
    from app.modules.reporting.dimension_query import materialize_rows
    from app.modules.reporting.filters import authorized_grants
    from app.modules.reporting.row_set import SnapshotMember
    from app.modules.reporting.schemas import (
        MaterialInstanceResult,
        ParentAdControl,
        ReportingFilter,
    )

    context = TenantContext(
        tenant_id=snapshot.tenant_id, actor_id=snapshot.actor_id, role="operator"
    )
    grants = authorized_grants(
        session,
        context=context,
        bc_id=snapshot.bc_id,
        advertiser_ids=tuple(snapshot.advertiser_ids),
    )
    connections = defaultdict(set)
    for grant in grants:
        connections[grant.advertiser_id].add(str(grant.connection_id))
    grouped = defaultdict(list)
    for f in facts:
        if f["attribution_status"] == "RESOLVED" and f["creative_id"]:
            grouped[
                (
                    f["row_key"],
                    f["advertiser_id"],
                    f["ad_remote_id"],
                    f["ad_material_id"],
                    f["material_type"],
                    f["platform_material_id"],
                )
            ].append(f)

    def digest(payload):
        return sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    result = []
    for report in reports:
        instances, controls = [], {}
        for (row_key, adv, parent, mid, kind, vid), items in sorted(
            grouped.items(), key=lambda pair: str(pair[0])
        ):
            if (
                row_key != report.row_key
                or len({f["creative_id"] for f in items}) != 1
                or len(connections[adv]) != 1
            ):
                continue
            key = {
                "tenant_id": str(snapshot.tenant_id),
                "bc_id": snapshot.bc_id,
                "connection_id": next(iter(connections[adv])),
                "advertiser_id": adv,
                "parent_ad_id": parent,
                "platform_material_id": vid,
                "ad_material_id": mid,
                "material_type": kind,
            }
            vectors = merge_metric_buckets(
                [
                    (
                        (
                            metric_vector(
                                dict(f)
                                | {
                                    "level": "creative",
                                    "source_kind": "PLATFORM_DIRECT",
                                    "contract_revision": "creative-observed-v1",
                                },
                                currency=f["currency"],
                                timezone=f["timezone"],
                                partial={
                                    m for m in METRIC_COLUMNS if f[f"{m}_has_null"]
                                },
                            ),
                        ),
                        f["source_status"] != "COMPLETE",
                    )
                    for f in items
                ]
            )
            observations = []
            for f in sorted(
                items, key=lambda item: (str(item["stat_date"]), item["creative_id"])
            ):
                # 原事实自己的观测坐标；派生完整水位不能替代实际creative观测。
                observations.append(
                    {
                        "tenant_id": str(snapshot.tenant_id),
                        "advertiser_id": adv,
                        "creative_id": f["creative_id"],
                        "fact_key": json.dumps(
                            [
                                str(snapshot.tenant_id),
                                adv,
                                f["creative_id"],
                                str(f["stat_date"]),
                            ]
                        ),
                        "local_day": str(f["stat_date"]),
                        "observed_at": f["fact_observed_at"].isoformat(),
                        "sync_job_id": str(f["fact_sync_job_id"]),
                        "source_info": f["source_info"],
                        "source_status": f["source_status"],
                    }
                )
            payload = {
                "key": key,
                "creative_ids": tuple(sorted({f["creative_id"] for f in items})),
                "fact_keys": tuple(sorted(o["fact_key"] for o in observations)),
                "metric_buckets": vectors,
                "coverage": dict(report.coverage)
                | {
                    "metric_semantics": "UNCONFIRMED",
                    "field_evidence": "LEGACY_UNKNOWN",
                },
                "observations": observations,
            }
            instance = MaterialInstanceResult(
                **payload,
                evidence_digest=digest(
                    MaterialInstanceResult(**payload, evidence_digest="").model_dump(
                        mode="json"
                    )
                ),
            )
            instances.append(instance)
            parent_key = (adv, parent)
            if parent_key not in controls:
                # 父控制只有父ID和完整日期范围，绝不继承素材子集筛选。
                parent_filters = ReportingFilter(
                    dimension="ad",
                    start_date=filters.start_date,
                    end_date=filters.end_date,
                    advertiser_ids=(adv,),
                    ids=(parent,),
                )
                parent_rows = materialize_rows(
                    session,
                    tenant_id=snapshot.tenant_id,
                    filters=parent_filters,
                    advertisers=[adv],
                    members=[
                        SnapshotMember(
                            f"{adv}:ad:{parent}",
                            (
                                {
                                    "tenant_id": str(snapshot.tenant_id),
                                    "advertiser_id": adv,
                                    "kind": "ad",
                                    "remote_id": parent,
                                },
                            ),
                            (),
                        )
                    ],
                )
                parent_row = parent_rows[0]
                parent_payload = {
                    "key": {
                        k: v
                        for k, v in key.items()
                        if k
                        in {
                            "tenant_id",
                            "bc_id",
                            "connection_id",
                            "advertiser_id",
                            "parent_ad_id",
                        }
                    },
                    "scope": "FULL_PARENT_AD",
                    "metric_buckets": parent_row.metric_buckets,
                    "coverage": parent_row.coverage,
                }
                controls[parent_key] = ParentAdControl(
                    **parent_payload,
                    evidence_digest=digest(
                        ParentAdControl(
                            **parent_payload, evidence_digest=""
                        ).model_dump(mode="json")
                    ),
                )
        for instance in instances:
            for observation in instance.observations:
                coordinate = dict(observation) | {
                    "report_kind": "creative_day",
                    "observation_source": "SELECTED_FACT",
                }
                if coordinate not in snapshot.publication_map:
                    snapshot.publication_map = [*snapshot.publication_map, coordinate]
        payload = report.model_dump(mode="json") | {
            "coverage": dict(report.coverage)
            | {
                "instance_count": len(instances),
                "creative_count": len({c for i in instances for c in i.creative_ids}),
                "instance_scope": "EXACT_SINGLE_CREATIVE",
                "metric_semantics": "UNCONFIRMED",
            },
            "material_instances": [i.model_dump(mode="json") for i in instances],
            "parent_controls": [c.model_dump(mode="json") for c in controls.values()],
        }
        result.append(
            ReportRow.model_validate(payload | {"evidence_digest": digest(payload)})
        )
    return result
