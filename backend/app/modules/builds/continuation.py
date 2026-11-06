"""同草稿续建：冻结兼容计划、复核来源与发送围栏，共用首次范围占用记录。"""

import hashlib
import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.builds.execution_models import (
    DraftUnitReservation,
    ExecutionStep,
    Submission,
)
from app.modules.builds.execution_state import DEFINITE_NO_EFFECT_HISTORY, UNARMED_RETRY
from app.modules.builds.frozen_requests import parent_request
from app.modules.builds.preview_models import (
    BuildPreview,
    BuildUnit,
    PlannedAd,
    PlannedGroup,
    PreviewAdCreativeUnit,
    PreviewCreativeUnit,
)
from app.modules.builds.receipt_completion import (
    confirmed_created_sql,
    obsolete_readback_sql,
)
from app.modules.builds.request_compiler import decode_intent

KEY = "continuation"
KINDS = ("CTA", "CAMPAIGN", "ADGROUP", "AD")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def superseded_sql(step: str = "s", unit: str = "u") -> str:
    if not all(re.fullmatch(r"[a-z_]+", alias) for alias in (step, unit)):
        raise ValueError("invalid SQL alias")
    return f"""EXISTS (SELECT 1 FROM build_submission original
 JOIN build_submission newer ON newer.tenant_id=original.tenant_id AND newer.draft_id=original.draft_id AND newer.ordinal>original.ordinal
 JOIN build_unit successor ON successor.tenant_id=newer.tenant_id AND successor.preview_id=newer.preview_id
 WHERE original.tenant_id={step}.tenant_id AND original.id={step}.submission_id
 AND successor.drama_id={unit}.drama_id AND successor.advertiser_id={unit}.advertiser_id
 AND successor.scene_snapshot->'{KEY}'->>'state'='CONTINUE')"""


def require_execution_owner(session: Session, step: ExecutionStep) -> None:
    superseded = SASession.execute(
        session,
        text(
            "SELECT "
            + superseded_sql()
            + " FROM execution_step s JOIN build_unit u ON u.tenant_id=s.tenant_id AND u.id=s.unit_id WHERE s.tenant_id=:tenant AND s.id=:step"
        ),
        {"tenant": step.tenant_id, "step": step.id},
    ).scalar_one()
    if superseded:
        raise DomainError("execution_superseded", "此范围已由新预览续建")


def _owner(session, preview, unit):
    # 首次占用仍由原不可变 reservation 负责；仅已受理的续建版本可成为它的后继。
    query = (
        select(Submission, BuildUnit)
        .join(
            BuildUnit,
            (col(BuildUnit.tenant_id) == Submission.tenant_id)
            & (col(BuildUnit.preview_id) == Submission.preview_id),
        )
        .where(
            col(Submission.tenant_id) == preview.tenant_id,
            Submission.draft_id == preview.draft_id,
            Submission.preview_id != preview.id,
            BuildUnit.drama_id == unit.drama_id,
            BuildUnit.advertiser_id == unit.advertiser_id,
        )
    )
    roots = select(DraftUnitReservation.unit_id).where(
        DraftUnitReservation.tenant_id == preview.tenant_id,
        DraftUnitReservation.draft_id == preview.draft_id,
        DraftUnitReservation.drama_id == unit.drama_id,
        DraftUnitReservation.advertiser_id == unit.advertiser_id,
    )
    owner = session.exec(
        query.where(
            col(BuildUnit.id).in_(roots)
            | (BuildUnit.scene_snapshot[KEY]["state"].astext == "CONTINUE")
        )
        .order_by(col(Submission.ordinal).desc())
        .limit(1)
    ).first()
    if owner is not None:
        return owner
    # 旧受理尚未展开也必须占位，不能在其 worker 开始之前抢走同一范围。
    return session.exec(
        query.where(
            col(BuildUnit.complete).is_(True),
            col(BuildUnit.readiness).in_(["READY", "PREPARING"]),
        )
        .order_by(col(Submission.ordinal))
        .limit(1)
    ).first()


def _steps(session, submission, unit, *, lock=False):
    # 连续版本也要检查更早的未知/迟到回执；不能只看最近一份任务而丢失外部效果。
    query = (
        select(ExecutionStep)
        .join(
            BuildUnit,
            (col(BuildUnit.tenant_id) == ExecutionStep.tenant_id)
            & (col(BuildUnit.id) == ExecutionStep.unit_id),
        )
        .join(Submission, col(Submission.id) == ExecutionStep.submission_id)
        .where(
            col(ExecutionStep.tenant_id) == unit.tenant_id,
            Submission.draft_id == submission.draft_id,
            Submission.ordinal <= submission.ordinal,
            BuildUnit.drama_id == unit.drama_id,
            BuildUnit.advertiser_id == unit.advertiser_id,
            BuildUnit.bc_id == unit.bc_id,
            col(ExecutionStep.kind).in_([*KINDS, "READBACK"]),
        )
        .order_by(col(ExecutionStep.id))
    )
    if lock:
        query = query.with_for_update(of=ExecutionStep)
    return list(session.exec(query.execution_options(populate_existing=True)).all())


def _physical_sources(session, preview, unit, steps):
    wanted = {
        UUID(s.resolved["reused_from_step_id"])
        for s in steps
        if s.resolved.get("reused_from_step_id")
    }
    roots = {}
    if wanted:
        rows = session.exec(
            select(ExecutionStep)
            .join(
                BuildUnit,
                (col(BuildUnit.tenant_id) == ExecutionStep.tenant_id)
                & (col(BuildUnit.id) == ExecutionStep.unit_id),
            )
            .join(Submission, col(Submission.id) == ExecutionStep.submission_id)
            .where(
                col(ExecutionStep.tenant_id) == preview.tenant_id,
                col(ExecutionStep.id).in_(wanted),
                Submission.draft_id == preview.draft_id,
                BuildUnit.bc_id == unit.bc_id,
                BuildUnit.drama_id == unit.drama_id,
                BuildUnit.advertiser_id == unit.advertiser_id,
                BuildUnit.connection_id == unit.connection_id,
            )
        ).all()
        roots = {s.id: s for s in rows}
    actual = {}
    for step in steps:
        value = step.resolved.get("reused_from_step_id")
        root = roots.get(UUID(value)) if value else step
        if root is not None and (root.kind, root.remote_id) == (
            step.kind,
            step.remote_id,
        ):
            actual[step.id] = root
    return actual


def _facts(session, steps, physical):
    ids = {s.id for s in steps} | {s.id for s in physical.values()}
    if not ids:
        return set(), set(), ""
    success = confirmed_created_sql()
    safe = f"(({success}) OR (s.status='FAILED' AND s.phase='DONE' AND NOT s.mismatch AND s.remote_id IS NULL AND ({UNARMED_RETRY} OR {DEFINITE_NO_EFFECT_HISTORY})))"
    rows = (
        SASession.execute(
            session,
            text(
                f"SELECT s.id, {success} success, {safe} safe, {obsolete_readback_sql()} obsolete FROM execution_step s WHERE s.tenant_id=:tenant AND s.id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"tenant": steps[0].tenant_id, "ids": list(ids)},
        )
        .mappings()
        .all()
    )
    successes = {r["id"] for r in rows if r["success"]}
    safe_ids = {r["id"] for r in rows if r["safe"] or r["obsolete"]}
    evidence = (
        SASession.execute(
            session,
            text(
                "SELECT step_id,count(*) n,max(observed_at) last FROM step_evidence WHERE tenant_id=:tenant AND step_id=ANY(CAST(:ids AS uuid[])) GROUP BY step_id ORDER BY step_id"
            ),
            {"tenant": steps[0].tenant_id, "ids": list(ids)},
        )
        .mappings()
        .all()
    )
    all_steps = {s.id: s for s in [*steps, *physical.values()]}
    baseline = digest(
        [
            [
                (
                    str(s.id),
                    s.status,
                    s.phase,
                    s.remote_id,
                    s.mismatch,
                    s.attempt,
                    s.attempt_id,
                    s.request_body_digest,
                    s.resolved,
                    s.dispatch_id,
                    s.lease_token,
                    s.lease_expires_at,
                )
                for s in sorted(all_steps.values(), key=lambda s: str(s.id))
                if s.kind != "READBACK"
            ],
            [dict(r) for r in evidence if all_steps[r["step_id"]].kind != "READBACK"],
        ]
    )
    return successes, safe_ids, baseline


def _layout(session, context, frozen):
    groups = session.exec(
        select(PlannedGroup)
        .where(
            PlannedGroup.tenant_id == context.tenant_id,
            PlannedGroup.unit_id == frozen.unit_id,
        )
        .order_by(col(PlannedGroup.group_no))
    ).all()
    group_map = {g.id: g for g in groups}
    ads = session.exec(
        select(PlannedAd)
        .where(
            PlannedAd.tenant_id == context.tenant_id,
            col(PlannedAd.group_id).in_(list(group_map)),
        )
        .order_by(col(PlannedAd.id))
    ).all()
    # 直接复用已冻结的内容摘要（含图片顺序与音乐），不把原片或整批成员拉入内存。
    rows = session.exec(
        select(PreviewAdCreativeUnit, PreviewCreativeUnit)
        .join(
            PreviewCreativeUnit,
            (col(PreviewCreativeUnit.tenant_id) == PreviewAdCreativeUnit.tenant_id)
            & (col(PreviewCreativeUnit.preview_id) == PreviewAdCreativeUnit.preview_id)
            & (col(PreviewCreativeUnit.id) == PreviewAdCreativeUnit.creative_unit_id),
        )
        .where(
            col(PreviewAdCreativeUnit.tenant_id) == context.tenant_id,
            col(PreviewAdCreativeUnit.preview_id) == frozen.preview_id,
            PreviewAdCreativeUnit.drama_id == frozen.drama_id,
        )
        .order_by(
            col(PreviewAdCreativeUnit.group_no),
            col(PreviewAdCreativeUnit.base_ad_no),
            col(PreviewAdCreativeUnit.position),
        )
    ).all()
    creative = {}
    for mapping, content in rows:
        creative.setdefault((mapping.group_no, mapping.base_ad_no), []).append(
            (content.kind, content.content_digest, content.music_id)
        )
    ad_intents = {}
    for ad in ads:
        group = group_map[ad.group_id]
        ad_intents[(group.group_no, ad.base_ad_no, ad.creative_no)] = [
            ad.text,
            ad.cta_option_ids,
            creative.get((group.group_no, ad.base_ad_no), []),
            frozen.url,
            frozen.scene_snapshot.get("creative_fields"),
            frozen.creation_status,
        ]
    return groups, ads, ad_intents


def _normalized_body(kind, body, frozen):
    # 与执行器恢复解码一致：request_id 是传输关联，不属于对象的业务配置。
    business = {key: value for key, value in body.items() if key != "request_id"}
    value = decode_intent(kind, business).model_dump(mode="json")
    value.pop("name", None)
    if (
        kind == "ADGROUP"
        and frozen.schedule_type == "SCHEDULE_FROM_NOW"
        and frozen.schedule_start_time is None
    ):
        value.pop("schedule_start_time", None)
    return value


def plan_continuation(session, *, context, preview, unit, lock=False):
    from .previews import load_frozen_unit, planned_unit_snapshot
    from .routes import load_preview_route

    owned = _owner(session, preview, unit)
    if owned is None:
        return None
    source, source_unit = owned
    old_preview = session.get(BuildPreview, source.preview_id)
    base = {
        "source_submission_id": str(source.id),
        "source_unit_id": str(source_unit.id),
        "source_batch": old_preview.batch_short_id,
    }

    def blocked(reason):
        return {**base, "state": "BLOCKED", "reason": reason}

    if not source.expanded or (source_unit.bc_id, source_unit.connection_id) != (
        unit.bc_id,
        unit.connection_id,
    ):
        return blocked("continuation_pending")
    if load_preview_route(
        session, context=context, preview_id=source.preview_id
    ) != load_preview_route(session, context=context, preview_id=preview.id):
        return blocked("continuation_route_changed")
    steps = _steps(session, source, source_unit, lock=lock)
    if not steps:
        return blocked("continuation_pending")
    physical = _physical_sources(session, preview, unit, steps)
    successes, safe, baseline = _facts(session, steps, physical)
    for step in steps:
        root = physical.get(step.id)
        if step.kind == "READBACK":
            if step.id not in safe and step.status != "SUCCEEDED":
                return blocked("continuation_pending")
        elif step.status == "SUCCEEDED":
            if root is None or root.id not in successes or step.mismatch:
                return blocked("continuation_pending")
        elif step.id not in safe:
            return blocked("continuation_pending")
    new = planned_unit_snapshot(session, context=context, preview=preview, unit=unit)
    groups, ads, intents = _layout(session, context, new)
    contexts = {}

    def source_context(step):
        if step.unit_id not in contexts:
            frozen = load_frozen_unit(session, context=context, unit_id=step.unit_id)
            old_preview = session.get(BuildPreview, step.preview_id)
            contexts[step.unit_id] = (
                frozen,
                old_preview,
                *_layout(session, context, frozen),
            )
        return contexts[step.unit_id]

    # 每个物理对象只评估一次，复用步骤不能被误当作新建回执。
    candidates = {
        root.id: root
        for step in steps
        if step.status == "SUCCEEDED"
        for root in [physical.get(step.id)]
        if root and root.id in successes
    }
    candidates = sorted(
        candidates.values(), key=lambda s: (s.updated_at, str(s.id)), reverse=True
    )
    reused = {}
    for step in candidates:
        if step.kind != "CAMPAIGN" or not step.request_body:
            continue
        old, old_preview, *_ = source_context(step)
        if old.contract_version != 2 or old_preview.config.get(
            "campaign_name_template"
        ) != preview.config.get("campaign_name_template"):
            continue
        if _normalized_body("CAMPAIGN", step.request_body, old) == _normalized_body(
            "CAMPAIGN",
            parent_request(new, kind="CAMPAIGN", name=new.campaign_name),
            new,
        ):
            reused["CAMPAIGN"] = str(step.id)
            campaign = step
            break
    for step in candidates:
        if step.kind != "CTA":
            continue
        old, _, _, old_ads, _ = source_context(step)
        if (
            old.scene_snapshot.get("cta_fields") == new.scene_snapshot.get("cta_fields")
            and old_ads
            and ads
            and old_ads[0].cta_option_ids == ads[0].cta_option_ids
        ):
            reused["CTA"] = str(step.id)
            break
    reused_groups = {}
    if "CAMPAIGN" in reused:
        for group in groups:
            expected = _normalized_body(
                "ADGROUP",
                parent_request(
                    new, kind="ADGROUP", name=group.name, parent_id=campaign.remote_id
                ),
                new,
            )
            for step in candidates:
                if step.kind != "ADGROUP" or not step.request_body:
                    continue
                old, old_preview, old_groups, _, _ = source_context(step)
                source_group = next(
                    (g for g in old_groups if g.id == step.group_id), None
                )
                if (
                    source_group is None
                    or source_group.group_no != group.group_no
                    or old_preview.config.get("adgroup_name_template")
                    != preview.config.get("adgroup_name_template")
                ):
                    continue
                if _normalized_body("ADGROUP", step.request_body, old) == expected:
                    reused[f"ADGROUP:{group.id}"] = str(step.id)
                    reused_groups[group.group_no] = step
                    break
    group_nos = {g.id: g.group_no for g in groups}
    if "CTA" in reused:
        for ad in ads:
            key = (group_nos[ad.group_id], ad.base_ad_no, ad.creative_no)
            parent = reused_groups.get(key[0])
            if parent is None:
                continue
            for step in candidates:
                if step.kind != "AD" or step.parent_step_id != parent.id:
                    continue
                old, old_preview, old_groups, old_ads, old_intents = source_context(
                    step
                )
                source_ad = next(
                    (a for a in old_ads if a.id == step.planned_ad_id), None
                )
                if (
                    source_ad is None
                    or (source_ad.base_ad_no, source_ad.creative_no) != key[1:]
                    or old_preview.config.get("ad_name_template")
                    != preview.config.get("ad_name_template")
                ):
                    continue
                # 真实 CTA ID 也必须相同，不能把另一个推荐组合当作兼容。
                cta_root = session.get(ExecutionStep, UUID(reused["CTA"]))
                if (
                    old_intents.get(key) == intents.get(key)
                    and step.request_body
                    and step.request_body.get("ad_configuration", {}).get(
                        "call_to_action_id"
                    )
                    == cta_root.remote_id
                ):
                    reused[f"AD:{ad.id}"] = str(step.id)
                    break
    counts = {
        "reused_campaign_count": int("CAMPAIGN" in reused),
        "reused_adgroup_count": len(reused_groups),
        "reused_ad_count": sum(k.startswith("AD:") for k in reused),
    }
    if counts == {
        "reused_campaign_count": 1,
        "reused_adgroup_count": len(groups),
        "reused_ad_count": len(ads),
    }:
        return {**base, "state": "BLOCKED", "reason": "continuation_unchanged"}
    return {
        **base,
        "state": "CONTINUE",
        "source_digest": baseline,
        "reused": reused,
        **counts,
    }


def freeze_continuation(session, *, context, preview, unit):
    if unit.readiness == "BLOCKED":
        return
    plan = plan_continuation(session, context=context, preview=preview, unit=unit)
    if plan:
        unit.scene_snapshot = {**unit.scene_snapshot, KEY: plan}
        if plan["state"] == "BLOCKED":
            unit.readiness = "BLOCKED"
            unit.reason_codes = sorted(set(unit.reason_codes) | {plan["reason"]})
        session.add(unit)


def validate_continuations(session, *, context, preview):
    after = None
    while True:
        query = select(BuildUnit).where(
            col(BuildUnit.tenant_id) == context.tenant_id,
            col(BuildUnit.preview_id) == preview.id,
            BuildUnit.scene_snapshot[KEY]["state"].astext == "CONTINUE",
        )
        if after:
            query = query.where(BuildUnit.id > after)
        units = session.exec(query.order_by(col(BuildUnit.id)).limit(100)).all()
        if not units:
            return
        for unit in units:
            expected = unit.scene_snapshot[KEY]
            actual = plan_continuation(
                session, context=context, preview=preview, unit=unit, lock=True
            )
            if actual != expected:
                raise DomainError(
                    "continuation_changed", "来源任务已变化，请刷新续建预览"
                )
        after = units[-1].id


def validate_execution_source(session, *, step):
    unit = session.get(BuildUnit, step.unit_id)
    expected = unit.scene_snapshot.get(KEY)
    if not expected or expected.get("state") != "CONTINUE":
        return
    preview = session.get(BuildPreview, step.preview_id)
    owner = _owner(session, preview, unit)
    if owner is None or str(owner[0].id) != expected["source_submission_id"]:
        raise DomainError("continuation_changed", "来源任务已变化")
    steps = _steps(session, owner[0], owner[1], lock=True)
    physical = _physical_sources(session, preview, unit, steps)
    _, _, actual = _facts(session, steps, physical)
    if actual != expected["source_digest"]:
        raise DomainError("continuation_changed", "来源任务已变化")


def reused_step_values(session, *, unit, key):
    plan = unit.scene_snapshot.get(KEY, {})
    identity = plan.get("reused", {}).get(key)
    if not identity:
        return {}
    root = session.get(ExecutionStep, UUID(identity))
    if root is None or root.tenant_id != unit.tenant_id:
        raise DomainError("continuation_changed", "复用来源不存在")
    proven = SASession.execute(
        session,
        text(
            "SELECT "
            + confirmed_created_sql()
            + " FROM execution_step s WHERE s.id=:step AND s.tenant_id=:tenant"
        ),
        {"step": root.id, "tenant": unit.tenant_id},
    ).scalar_one()
    if not proven:
        return {
            "status": "FAILED",
            "phase": "DONE",
            "error_code": "continuation_changed",
        }
    return {
        "status": "SUCCEEDED",
        "phase": "DONE",
        "remote_id": root.remote_id,
        "request_body": root.request_body,
        "request_body_digest": root.request_body_digest,
        "operation_status": root.operation_status,
        "review_status": root.review_status,
        "checked_at": root.checked_at,
        "resolved": {
            "reused_from_step_id": str(root.id),
            "source_submission_id": plan["source_submission_id"],
        },
    }


def record_reused_steps(session, *, identities):
    from .execution_state import evidence

    for identity in identities:
        step = session.get(ExecutionStep, identity)
        if step and step.resolved.get("reused_from_step_id"):
            evidence(
                session,
                step=step,
                claim=None,
                conclusion="REUSED",
                summary={
                    "remote_id": step.remote_id,
                    "source_step_id": step.resolved["reused_from_step_id"],
                },
            )


def preview_counts(session, *, tenant_id, preview_id):
    rows = (
        SASession.execute(
            session,
            text(f"""SELECT
 count(*) FILTER (WHERE scene_snapshot->'{KEY}'->>'state'='CONTINUE') continuation_unit_count,
 count(*) FILTER (WHERE scene_snapshot->'{KEY}'->>'state'='BLOCKED') continuation_blocked_count,
 coalesce(sum((scene_snapshot->'{KEY}'->>'reused_campaign_count')::int) FILTER (WHERE readiness IN ('READY','PREPARING')),0) reused_campaign_count,
 coalesce(sum((scene_snapshot->'{KEY}'->>'reused_adgroup_count')::int) FILTER (WHERE readiness IN ('READY','PREPARING')),0) reused_adgroup_count,
 coalesce(sum((scene_snapshot->'{KEY}'->>'reused_ad_count')::int) FILTER (WHERE readiness IN ('READY','PREPARING')),0) reused_ad_count
 FROM build_unit WHERE tenant_id=:tenant AND preview_id=:preview AND scene_snapshot ? '{KEY}'"""),
            {"tenant": tenant_id, "preview": preview_id},
        )
        .mappings()
        .one()
    )
    return {k: int(v) for k, v in rows.items()}


def submission_facts(session, submission):
    params = {
        "tenant": submission.tenant_id,
        "submission": submission.id,
        "preview": submission.preview_id,
        "draft": submission.draft_id,
        "ordinal": submission.ordinal,
    }
    reused = (
        SASession.execute(
            session,
            text("""SELECT
 count(*) FILTER (WHERE kind='CAMPAIGN') campaign_count,
 count(*) FILTER (WHERE kind='ADGROUP') adgroup_count,
 count(*) FILTER (WHERE kind='AD') ad_count
 FROM execution_step WHERE tenant_id=:tenant AND submission_id=:submission
 AND status='SUCCEEDED' AND resolved ? 'reused_from_step_id'"""),
            params,
        )
        .mappings()
        .one()
    )
    sources = (
        SASession.execute(
            session,
            text("""SELECT DISTINCT (scene_snapshot->'continuation'->>'source_submission_id')::uuid
 FROM build_unit WHERE tenant_id=:tenant AND preview_id=:preview AND scene_snapshot->'continuation'->>'state'='CONTINUE' LIMIT 20"""),
            params,
        )
        .scalars()
        .all()
    )
    successors = SASession.execute(
        session,
        text("""SELECT DISTINCT newer.id,newer.ordinal FROM build_submission newer
 JOIN build_unit successor ON successor.tenant_id=newer.tenant_id AND successor.preview_id=newer.preview_id
 WHERE newer.tenant_id=:tenant AND newer.draft_id=:draft AND newer.ordinal>:ordinal
 AND successor.scene_snapshot->'continuation'->>'state'='CONTINUE'
 AND EXISTS (SELECT 1 FROM build_unit original WHERE original.tenant_id=:tenant AND original.preview_id=:preview
 AND original.drama_id=successor.drama_id AND original.advertiser_id=successor.advertiser_id)
 ORDER BY newer.ordinal DESC LIMIT 20"""),
        params,
    ).all()
    return {
        "reused": dict(reused),
        "continuation_source_ids": list(sources),
        "continued_by_submission_ids": [r.id for r in successors],
    }
