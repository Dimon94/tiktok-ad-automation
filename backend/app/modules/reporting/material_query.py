"""创意素材行补全；分页、导出与快照使用相同的逐创意筛选。"""

import json
from collections import defaultdict
from hashlib import sha256
from typing import Any

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
from app.modules.reporting.metric_catalog import CREATIVE_OBSERVED_CONTRACT
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
                dict(f)
                | {
                    "level": "creative",
                    "source_kind": "PLATFORM_DIRECT",
                    "contract_revision": CREATIVE_OBSERVED_CONTRACT,
                },
                currency=f["currency"],
                timezone=f["timezone"],
                partial={m for m in METRIC_COLUMNS if f[f"{m}_has_null"]},
                missing_denominators={m for m in METRIC_COLUMNS if f[f"{m}_has_null"]},
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
                metric_buckets=merge_metric_buckets(buckets, strict_denominators=True),
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
    """按直接事实唯一键冻结归属、特殊身份和逐字段证据；父控制独立。"""
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
        if f["creative_id"]:
            grouped[
                (
                    f["row_key"],
                    f["advertiser_id"],
                    f["ad_remote_id"],
                    f["ad_material_id"],
                    f["material_type"],
                    f["platform_material_id"],
                    f["creative_id"]
                    if f["attribution_status"] != "RESOLVED"
                    or f["material_type"] == "CAROUSEL"
                    or not f["platform_material_id"]
                    else None,
                )
            ].append(f)

    def digest(payload):
        return sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    result = []
    for report in reports:
        instances, controls = [], {}
        reconciliation = []
        for (row_key, adv, parent, mid, kind, vid, special), items in sorted(
            grouped.items(), key=lambda pair: str(pair[0])
        ):
            if row_key != report.row_key:
                continue
            # 同事实的引用、版本和连接不是新金额；以原始经济事实主键去重。
            items = list(
                {
                    (f["advertiser_id"], f["creative_id"], f["stat_date"]): f
                    for f in items
                }.values()
            )
            source_connections = {
                f["evidence"].get("source_connection_id")
                for f in items
                if f.get("evidence") and f["evidence"].get("source_connection_id")
            }
            candidates = source_connections or connections[adv]
            connection = (
                next(iter(candidates))
                if len(candidates) == 1 and candidates <= connections[adv]
                else None
            )
            source_bcs = {
                f["evidence"].get("source_bc_id")
                for f in items
                if f.get("evidence") and f["evidence"].get("source_bc_id")
            }
            source_mismatch = bool(source_bcs - {snapshot.bc_id})
            if source_mismatch:
                connection = None
            identity_status = items[0]["source_info"].get("identity_status")
            row_kind = (
                "COMPOSITE"
                if kind == "CAROUSEL" or json.loads(row_key)[0] == "carousel"
                else "UNMAPPED_CREATIVE"
                if special or not connection or not mid
                else "INSTANCE"
            )
            reason = (
                "SOURCE_SCOPE_MISMATCH"
                if source_mismatch
                else "CONNECTION_AMBIGUOUS"
                if not connection
                else "MISSING_AD_MATERIAL_ID"
                if row_kind == "UNMAPPED_CREATIVE"
                and not mid
                and identity_status == "RESOLVED"
                else (identity_status or "UNMAPPED")
                if row_kind != "INSTANCE"
                else None
            )
            key = {
                "tenant_id": str(snapshot.tenant_id),
                "bc_id": snapshot.bc_id,
                "connection_id": connection,
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
                                    "contract_revision": CREATIVE_OBSERVED_CONTRACT,
                                },
                                currency=f["currency"],
                                timezone=f["timezone"],
                                partial={
                                    m for m in METRIC_COLUMNS if f[f"{m}_has_null"]
                                },
                                missing_denominators={
                                    m for m in METRIC_COLUMNS if f[f"{m}_has_null"]
                                },
                            ),
                        ),
                        f["source_status"] != "COMPLETE",
                    )
                    for f in items
                ],
                strict_denominators=True,
            )
            # 与 MaterialInstanceResult.observations 一致，保留源事实的开放 JSON 证据。
            observations: list[dict[str, Any]] = []
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
                        "observed_at": (
                            (f.get("evidence") or {}).get("publication") or {}
                        ).get("observed_at")
                        or f["fetched_at"].isoformat(),
                        "sync_job_id": str(f["fact_sync_job_id"]),
                        "source_info": f["source_info"],
                        "source_status": f["source_status"],
                        "field_states": (f.get("evidence") or {}).get(
                            "field_states",
                            dict.fromkeys(METRIC_COLUMNS, "LEGACY_UNKNOWN"),
                        ),
                        "values": {
                            m: str(f[m]) if f[m] is not None else None
                            for m in METRIC_COLUMNS
                        },
                        "publisher": (f.get("evidence") or {}).get("publication"),
                        "evidence": f.get("evidence"),
                        "identity": {
                            "assets": f["assets"],
                            "status": f["source_info"].get("identity_status"),
                            "evidence": f["source_info"].get("identity_evidence"),
                        },
                    }
                )
            field_counts = {
                m: {
                    state: sum(
                        o["field_states"].get(m, "LEGACY_UNKNOWN") == state
                        for o in observations
                    )
                    for state in ("VALUE", "NULL", "DASH", "ABSENT", "LEGACY_UNKNOWN")
                }
                for m in METRIC_COLUMNS
            }
            payload: dict[str, Any] = {
                "row_kind": row_kind,
                "identity_reason": reason,
                "field_state_counts": field_counts,
                "key": key,
                "creative_ids": tuple(sorted({f["creative_id"] for f in items})),
                "fact_keys": tuple(sorted(o["fact_key"] for o in observations)),
                "metric_buckets": vectors,
                "coverage": dict(report.coverage)
                | {
                    "metric_semantics": "UNCONFIRMED",
                    "field_evidence": "MIXED"
                    if any(c["LEGACY_UNKNOWN"] for c in field_counts.values())
                    else "OBSERVED",
                    "field_state_counts": field_counts,
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
            if parent is not None and parent_key not in controls:
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
        for control in controls.values():
            reconciliation.extend(
                reconcile_parent(
                    session,
                    snapshot=snapshot,
                    filters=filters,
                    control=control,
                )
            )
        # 广告组残差保留原粒度，绝不能冒充父 Ad 差额或素材实例。
        if json.loads(report.row_key)[0] == "unattributed":
            reconciliation.append(
                {
                    "row_kind": "RECONCILIATION_DIFFERENCE",
                    "key": {"row_key": report.row_key},
                    "status": "NOT_COMPARABLE",
                    "reason": "ADGROUP_RESIDUAL_NOT_PARENT_CONTROL",
                    "scope": "ADGROUP_RESIDUAL",
                    # 保留原广告组有符号量及坐标；比率不是可加差额，不能重算。
                    "metric_buckets": [
                        v.model_copy(
                            update={
                                "values": {
                                    m: value
                                    for m, value in v.values.items()
                                    if m in METRIC_COLUMNS
                                },
                                "availability": {
                                    m: state
                                    for m, state in v.availability.items()
                                    if m in METRIC_COLUMNS
                                },
                            }
                        ).model_dump(mode="json")
                        for v in report.metric_buckets
                    ],
                    "coverage": report.coverage,
                    "observations": [
                        {
                            "source": "ADGROUP_RESIDUAL_PROJECTION",
                            "scope": "ADGROUP_RESIDUAL",
                            "metric_buckets": [
                                v.model_dump(mode="json") for v in report.metric_buckets
                            ],
                        }
                    ],
                    "evidence_digest": digest(report.model_dump(mode="json")),
                }
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
                "instance_count": sum(i.row_kind == "INSTANCE" for i in instances),
                "special_row_count": sum(i.row_kind != "INSTANCE" for i in instances),
                "creative_count": len({c for i in instances for c in i.creative_ids}),
                "instance_scope": "DIRECT_CREATIVE_FACTS",
                "metric_semantics": "UNCONFIRMED",
            },
            "material_instances": [i.model_dump(mode="json") for i in instances],
            "parent_controls": [c.model_dump(mode="json") for c in controls.values()],
            "reconciliation_rows": reconciliation,
        }
        result.append(
            ReportRow.model_validate(payload | {"evidence_digest": digest(payload)})
        )
    return result


def reconcile_parent(session, *, snapshot, filters, control):
    """独立完整父范围与选中事实对比；逐指标合同未知时不产生数字。"""
    from app.modules.reporting.metric_catalog import NATIVE_CONTRACT
    from app.modules.reporting.schemas import ReportingFilter

    adv, parent = control.key["advertiser_id"], control.key["parent_ad_id"]
    full_filter = ReportingFilter(
        dimension="material",
        start_date=filters.start_date,
        end_date=filters.end_date,
        advertiser_ids=(adv,),
    )
    full = read_facts(
        session,
        tenant_id=snapshot.tenant_id,
        advertisers=[adv],
        filters=full_filter,
        exact_ad=parent,
        observations=True,
    )
    selected = read_facts(
        session,
        tenant_id=snapshot.tenant_id,
        advertisers=[adv],
        filters=filters,
        exact_ad=parent,
        observations=True,
    )

    def fact_key(f):
        return (f["advertiser_id"], f["creative_id"], str(f["stat_date"]))

    full = list({fact_key(f): f for f in full if f["creative_id"]}.values())
    selected_keys = {fact_key(f) for f in selected if f["creative_id"]}
    output = []
    for bucket in control.metric_buckets:
        facts = [
            f
            for f in full
            if (f["currency"], f["timezone"]) == (bucket.currency, bucket.timezone)
        ]
        reasons = []
        if control.coverage.get("status") not in {"COMPLETE", "COMPLETE_EMPTY"}:
            reasons.append("PARENT_NOT_READY")
        if not facts or {fact_key(f) for f in facts} - selected_keys:
            reasons.append("SELECTED_MATERIAL_SUBSET_OR_MISSING_CREATIVE")
        if any(f["source_status"] != "COMPLETE" for f in facts):
            reasons.append("CREATIVE_COVERAGE_INCOMPLETE")
        if bucket.source_kind != "PLATFORM_DIRECT" or not bucket.provenance:
            reasons.append("PARENT_DIRECT_OBSERVATION_MISSING")
        if bucket.contract_revision != NATIVE_CONTRACT:
            reasons.append("PARENT_CONTRACT_UNCONFIRMED")
        # 只接受发布 owner 明确记载的完整账户日；不从投影水位或当前时钟制造证据。
        days = {str(f["stat_date"]) for f in facts}
        expected_days = set()
        from datetime import timedelta

        day = filters.start_date
        while day <= filters.end_date:
            expected_days.add(str(day))
            day += timedelta(days=1)
        if days != expected_days:
            reasons.append("LOCAL_DAY_COVERAGE_MISMATCH")
        if any(
            (f.get("evidence") or {}).get("contract_revision")
            != CREATIVE_OBSERVED_CONTRACT
            for f in facts
        ):
            reasons.append("CREATIVE_CONTRACT_UNCONFIRMED")
        if any(not (f.get("evidence") or {}).get("publication") for f in facts):
            reasons.append("CREATIVE_OBSERVATION_UNKNOWN")
        from datetime import datetime

        stamps = {
            str(b.stat_date): b for p in bucket.provenance.values() for b in p.buckets
        }
        for f in facts:
            publication = (f.get("evidence") or {}).get("publication") or {}
            stamp = stamps.get(str(f["stat_date"]))
            if (publication.get("scope") or {}).get("kind") != "ACCOUNT_DAY":
                reasons.append("CREATIVE_PUBLICATION_SCOPE_NOT_FULL")
                break
            observed = publication.get("observed_at")
            if (
                not stamp
                or not stamp.observed_at
                or not observed
                or stamp.last_attempt_status != "COMPLETE"
            ):
                reasons.append("OBSERVATION_COVERAGE_UNKNOWN")
                break
            if (
                abs(
                    (
                        stamp.observed_at - datetime.fromisoformat(observed)
                    ).total_seconds()
                )
                > 1800
            ):
                reasons.append("OBSERVATION_WINDOW_MISMATCH")
                break
        child = merge_metric_buckets(
            [
                (
                    (
                        metric_vector(
                            dict(f)
                            | {
                                "level": "creative",
                                "source_kind": "PLATFORM_DIRECT",
                                "contract_revision": CREATIVE_OBSERVED_CONTRACT,
                            },
                            currency=f["currency"],
                            timezone=f["timezone"],
                            partial={m for m in METRIC_COLUMNS if f[f"{m}_has_null"]},
                            missing_denominators={
                                m for m in METRIC_COLUMNS if f[f"{m}_has_null"]
                            },
                        ),
                    ),
                    False,
                )
                for f in facts
            ],
            strict_denominators=True,
        )
        values, metric_reasons = {}, {}
        for m in METRIC_COLUMNS:
            if reasons:
                metric_reasons[m] = ";".join(reasons)
            elif m.startswith("revenue_") or m == "ad_impression_events":
                metric_reasons[m] = (
                    "CREATIVE_NATIVE_WINDOW_TRAFFIC_MATURITY_UNCONFIRMED"
                )
            elif (
                bucket.availability.get(m) != "AVAILABLE"
                or child[0].availability.get(m) != "AVAILABLE"
            ):
                metric_reasons[m] = "FIELD_COVERAGE_INCOMPLETE"
            else:
                values[m] = bucket.values[m] - child[0].values[m]
        # 比率不能相减，也不能用金额差额重算；差额只包含可加原始量。
        from app.modules.reporting.metric_catalog import DERIVED_RATIOS

        difference_buckets = []
        if values:
            vector = metric_vector(
                values, currency=bucket.currency, timezone=bucket.timezone
            )
            ratios = {r[0] for r in DERIVED_RATIOS}
            vector = vector.model_copy(
                update={
                    "values": vector.values | dict.fromkeys(ratios),
                    "availability": vector.availability
                    | dict.fromkeys(ratios, "MISSING"),
                    "reasons": vector.reasons
                    | dict.fromkeys(ratios, "NON_ADDITIVE_DIFFERENCE"),
                }
            )
            difference_buckets.append(vector.model_dump(mode="json"))
        payload = {
            "row_kind": "RECONCILIATION_DIFFERENCE",
            "key": control.key,
            "scope": "FULL_PARENT_AD",
            "currency": bucket.currency,
            "timezone": bucket.timezone,
            "status": "COMPARABLE" if values else "NOT_COMPARABLE",
            "reason": ";".join(reasons) if reasons else "PER_METRIC_CONTRACT_CHECK",
            "metric_reasons": metric_reasons,
            "metric_buckets": difference_buckets,
            "coverage": {
                "parent": control.coverage,
                "selected_fact_count": len(selected_keys),
                "full_parent_fact_count": len(full),
                "metric_reasons": metric_reasons,
            },
            "observations": {
                "parent": bucket.model_dump(mode="json"),
                "creative": [dict(f.get("evidence") or {}) for f in facts],
            },
            "fact_keys": [
                json.dumps([str(snapshot.tenant_id), *fact_key(f)]) for f in facts
            ],
            "parent_digest": control.evidence_digest,
        }
        payload["evidence_digest"] = sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        output.append(payload)
    return output
