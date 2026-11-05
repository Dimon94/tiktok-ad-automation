"""本地图片编排：冻结候选、只读结构试算、可恢复暂存与一次指针切换。

所有调用者提交/回滚自身事务；这里没有外部 IO。草稿锁是用户命令和 worker
共同的串行化点；计划中的 UUID 只分配一次，恢复绝不重新切片。
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, literal, or_, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.builds.drafts import _bump_revision, get_draft
from app.modules.builds.image_composition import (
    compose_images,
    music_rule_digest,
    pending_music_status,
    resolve_image_rule,
)
from app.modules.builds.image_composition_commands import CreativeEditCommand
from app.modules.builds.image_composition_models import (
    DraftCreativeUnit,
    DraftCreativeUnitItem,
)
from app.modules.builds.image_composition_schemas import (
    CreativeUnit,
    ImageCandidate,
    ImageRule,
    ImageRulePatch,
)
from app.modules.builds.image_composition_state import (
    ImageCandidateExclusion,
    ImageCandidateItem,
    ImageCandidateSnapshot,
    ImageCompositionPlan,
    ImageCompositionPlanUnit,
    ImageCompositionRequest,
    ImageCompositionRun,
    ImageCompositionState,
)
from app.modules.builds.models import BuildDraft, DraftDrama, DraftPreparation
from app.modules.materials.ingest_models import (
    IngestSession,
    IngestSessionFile,
    TemporaryMaterialObject,
)
from app.modules.materials.models import AccountMaterial, MaterialFile


@dataclass(frozen=True)
class ImageDraftScope:
    tenant_id: UUID
    bc_id: str
    draft_id: UUID
    actor_id: UUID
    route: FrozenTikTokRoute

    @property
    def context(self):
        return TenantContext(
            tenant_id=self.tenant_id, actor_id=self.actor_id, role="operator"
        )


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def resolve_image_scope(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    allow_frozen_read: bool = False,
) -> ImageDraftScope:
    draft = get_draft(session, context=context, draft_id=draft_id)
    if draft.media_kind != "CAROUSEL":
        raise DomainError("draft_media_kind_mismatch", "该操作只用于图片草稿")
    try:
        route = freeze_route(
            session,
            context=context,
            bc_id=draft.bc_id,
            connection_id=draft.execution_connection_id,
        )
    except DomainError:
        if not allow_frozen_read:
            raise
        # 授权撤销仍能读历史进度/取消本地生成，不借此执行新的准备操作。
        prior = session.exec(
            select(ImageCompositionPlan)
            .where(
                ImageCompositionPlan.tenant_id == context.tenant_id,
                ImageCompositionPlan.draft_id == draft.id,
            )
            .order_by(
                col(ImageCompositionPlan.base_revision).desc(),
                col(ImageCompositionPlan.id),
            )
            .limit(1)
        ).first()
        if prior is None:
            prior = session.exec(
                select(ImageCandidateSnapshot)
                .where(
                    ImageCandidateSnapshot.tenant_id == context.tenant_id,
                    ImageCandidateSnapshot.draft_id == draft.id,
                    col(ImageCandidateSnapshot.route) != {},
                )
                .order_by(col(ImageCandidateSnapshot.created_at).desc())
                .limit(1)
            ).first()
        if prior is None:
            raise
        route = FrozenTikTokRoute.model_validate(prior.route)
    return ImageDraftScope(
        context.tenant_id, draft.bc_id, draft.id, context.actor_id, route
    )


def _draft(session, scope, *, lock=False, verify=True):
    row = get_draft(session, context=scope.context, draft_id=scope.draft_id, lock=lock)
    if (
        row.bc_id != scope.bc_id
        or row.media_kind != "CAROUSEL"
        or (scope.route.tenant_id, scope.route.bc_id) != (scope.tenant_id, scope.bc_id)
    ):
        raise DomainError("frozen_route_scope_mismatch", "图片草稿范围不匹配")
    if verify:
        verify_route(
            session,
            context=scope.context,
            route=scope.route,
            advertiser_id=None,
            capability="read",
        )
    return row


def _state(session, draft):
    state = session.get(ImageCompositionState, (draft.tenant_id, draft.id))
    if state is None:
        state = ImageCompositionState(
            tenant_id=draft.tenant_id, draft_id=draft.id, bc_id=draft.bc_id
        )
        session.add(state)
        session.flush()
    return state


def _request(session, scope, request_id, operation, expected_revision, body):
    identity = UUID(str(request_id))
    fingerprint = digest(
        {
            "draft_id": scope.draft_id,
            "operation": operation,
            "expected_revision": expected_revision,
            "body": body,
        }
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"image-request:{scope.tenant_id}:{identity}"},
    )
    draft = _draft(session, scope, lock=True, verify=False)
    prior = session.get(ImageCompositionRequest, (scope.tenant_id, identity))
    if prior:
        if prior.digest != fingerprint or prior.draft_id != scope.draft_id:
            raise DomainError("idempotency_conflict", "请求标识已用于不同参数")
        return draft, fingerprint, prior.result
    if operation != "cancel":
        verify_route(
            session,
            context=scope.context,
            route=scope.route,
            advertiser_id=None,
            capability="read",
        )
    if type(expected_revision) is not int or draft.revision != expected_revision:
        raise DomainError("draft_revision_conflict", "草稿已更新")
    return draft, fingerprint, None


def _receipt(session, scope, request_id, fingerprint, result):
    result = json.loads(json.dumps(result, default=str))
    session.add(
        ImageCompositionRequest(
            tenant_id=scope.tenant_id,
            request_id=request_id,
            draft_id=scope.draft_id,
            bc_id=scope.bc_id,
            digest=fingerprint,
            result=result,
        )
    )
    session.flush()
    return result


def assert_no_image_run(session, draft):
    if (
        draft.media_kind == "CAROUSEL"
        and session.exec(
            select(col(ImageCompositionRun.id)).where(
                col(ImageCompositionRun.tenant_id) == draft.tenant_id,
                col(ImageCompositionRun.draft_id) == draft.id,
                ImageCompositionRun.status == "PENDING",
            )
        ).first()
    ):
        raise DomainError(
            "image_composition_busy", "草稿正在应用图片规则，请等待或取消"
        )


def _editable(session, draft):
    assert_no_image_run(session, draft)
    if draft.status == "DRAFT":
        raise DomainError("draft_not_ready", "请先解析当前剧目和账户")
    if session.exec(
        select(DraftPreparation.id).where(
            DraftPreparation.tenant_id == draft.tenant_id,
            DraftPreparation.draft_id == draft.id,
            DraftPreparation.status == "PENDING",
        )
    ).first():
        raise DomainError("draft_not_ready", "请等待账户和剧目解析完成")


def _dramas(session, scope):
    from .draft_catalog import current_image_drama_clause

    return session.exec(
        select(DraftDrama)
        .where(
            col(DraftDrama.tenant_id) == scope.tenant_id,
            col(DraftDrama.draft_id) == scope.draft_id,
            current_image_drama_clause(),
        )
        .order_by(col(DraftDrama.first_line), col(DraftDrama.drama_id))
    ).all()


def _exclusions(session, scope):
    return {
        (row.drama_id, row.material_id): row
        for row in session.exec(
            select(ImageCandidateExclusion).where(
                ImageCandidateExclusion.tenant_id == scope.tenant_id,
                ImageCandidateExclusion.draft_id == scope.draft_id,
            )
        ).all()
    }


def _exclusion(session, scope, drama_id, material_id):
    key = (scope.tenant_id, scope.draft_id, drama_id, material_id)
    row = session.get(ImageCandidateExclusion, key)
    if row is None:
        row = ImageCandidateExclusion(
            tenant_id=scope.tenant_id,
            draft_id=scope.draft_id,
            drama_id=drama_id,
            material_id=material_id,
            bc_id=scope.bc_id,
        )
        session.add(row)
    return row


def _snapshot_items(session, scope, snapshot_id, drama_id=None):
    query = select(ImageCandidateItem).where(
        col(ImageCandidateItem.tenant_id) == scope.tenant_id,
        col(ImageCandidateItem.draft_id) == scope.draft_id,
        col(ImageCandidateItem.snapshot_id) == snapshot_id,
    )
    if drama_id is not None:
        query = query.where(col(ImageCandidateItem.drama_id) == drama_id)
    return session.exec(
        query.order_by(
            col(ImageCandidateItem.drama_id), col(ImageCandidateItem.material_id)
        )
    ).all()


def _append_candidates(session, scope, snapshot, drama_id, rows):
    # 批次与顺序固定在候选快照，后续重命名/新增不会漂移。
    batch_rows = (
        session.exec(
            select(IngestSessionFile, IngestSession)
            .join(
                IngestSession,
                (col(IngestSession.id) == col(IngestSessionFile.session_id))
                & (col(IngestSession.tenant_id) == col(IngestSessionFile.tenant_id)),
            )
            .where(
                col(IngestSessionFile.tenant_id) == scope.tenant_id,
                col(IngestSessionFile.material_id).in_([r.id for r in rows]),
            )
            .distinct(col(IngestSessionFile.material_id))
            .order_by(
                col(IngestSessionFile.material_id),
                col(IngestSession.created_at),
                col(IngestSession.id),
                col(IngestSessionFile.client_index),
            )
        ).all()
        if rows
        else []
    )
    batches = {}
    for item, batch in batch_rows:
        batches.setdefault(item.material_id, (item, batch))
    for row in rows:
        pair = batches.get(row.id)
        candidate = ImageCandidate(
            material_id=row.id,
            file_name_snapshot=row.file_name,
            created_at=row.created_at,
            content_digest=row.sha256 or f"unverified:{row.id}",
            upload_batch_id=pair[1].id if pair else None,
            upload_batch_created_at=pair[1].created_at if pair else None,
            upload_sequence=pair[0].client_index if pair else None,
        )
        session.add(
            ImageCandidateItem(
                tenant_id=scope.tenant_id,
                draft_id=scope.draft_id,
                bc_id=scope.bc_id,
                snapshot_id=snapshot.id,
                drama_id=drama_id,
                material_id=row.id,
                candidate=candidate.model_dump(mode="json"),
                source={
                    "original_bc_id": row.bc_id,
                    "storage_state": row.storage_state,
                },
            )
        )


def build_candidate_snapshot(session, scope, draft, *, preparation_id=None):
    """启动候选并只推进首个 100 行页面；剩余页面有持久游标，不在此全量循环。"""
    from .image_candidate_service import (
        advance_candidate_snapshot,
        start_candidate_snapshot,
    )

    snapshot = start_candidate_snapshot(
        session, scope, draft, preparation_id=preparation_id
    )
    advance_candidate_snapshot(session, snapshot.id, snapshot.generation)
    return snapshot


def _sync_candidate_counts(session, scope, snapshot_id):
    counts = dict(
        session.exec(
            select(ImageCandidateItem.drama_id, func.count())
            .where(
                ImageCandidateItem.tenant_id == scope.tenant_id,
                ImageCandidateItem.snapshot_id == snapshot_id,
            )
            .group_by(col(ImageCandidateItem.drama_id))
        ).all()
    )
    for drama in _dramas(session, scope):
        drama.matched_count, drama.material_state = (
            counts.get(drama.drama_id, 0),
            "ready",
        )


def refresh_candidates(session, scope, request_id, expected_revision):
    draft, fp, replay = _request(
        session, scope, request_id, "refresh", expected_revision, {}
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    snapshot = build_candidate_snapshot(session, scope, draft)
    from .image_candidate_service import snapshot_progress

    return _receipt(session, scope, request_id, fp, snapshot_progress(snapshot))


def adopt_candidates(session, scope, request_id, expected_revision, snapshot_id):
    snapshot_id = UUID(str(snapshot_id))
    draft, fp, replay = _request(
        session,
        scope,
        request_id,
        "adopt",
        expected_revision,
        {"snapshot_id": snapshot_id},
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    snapshot = session.get(ImageCandidateSnapshot, snapshot_id)
    if snapshot is None or (snapshot.tenant_id, snapshot.draft_id, snapshot.bc_id) != (
        scope.tenant_id,
        scope.draft_id,
        scope.bc_id,
    ):
        raise DomainError("image_composition_not_found", "候选快照不存在")
    if snapshot.status != "READY":
        raise DomainError("image_candidates_incomplete", "候选尚未完整构建，暂不能采纳")
    if snapshot.base_revision != draft.revision:
        raise DomainError("draft_revision_conflict", "候选刷新后草稿已更新，请重新刷新")
    _state(session, draft).snapshot_id = snapshot.id
    _sync_candidate_counts(session, scope, snapshot.id)
    _bump_revision(session, draft, expected_revision)
    return _receipt(
        session,
        scope,
        request_id,
        fp,
        {"revision": draft.revision, "snapshot_id": snapshot.id},
    )


def resolve_candidate_conflict(
    session, scope, request_id, expected_revision, material_id, drama_ids
):
    material_id, drama_ids = (
        UUID(str(material_id)),
        tuple(UUID(str(i)) for i in drama_ids),
    )
    draft, fp, replay = _request(
        session,
        scope,
        request_id,
        "allocate",
        expected_revision,
        {"material_id": material_id, "drama_ids": drama_ids},
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    state = _state(session, draft)
    matches = {
        r.drama_id
        for r in _snapshot_items(session, scope, state.snapshot_id)
        if r.material_id == material_id
    }
    if (
        not drama_ids
        or len(set(drama_ids)) != len(drama_ids)
        or not set(drama_ids) <= matches
    ):
        raise DomainError(
            "image_edit_invalid", "请选择该图实际命中的剧目；多选即显式跨剧复用"
        )
    for drama_id in matches:
        exclusion = _exclusion(session, scope, drama_id, material_id)
        # 不借分配操作复活用户显式删除；恢复需要独立 RESTORE_CANDIDATES。
        if drama_id not in drama_ids:
            exclusion.excluded = True
        exclusion.cross_drama_allowed = drama_id in drama_ids
    _bump_revision(session, draft, expected_revision)
    return _receipt(session, scope, request_id, fp, {"revision": draft.revision})


def _candidates(session, scope, state):
    exclusions = _exclusions(session, scope)
    result = {}
    for row in _snapshot_items(session, scope, state.snapshot_id):
        excluded = exclusions.get((row.drama_id, row.material_id))
        result.setdefault(row.drama_id, []).append(
            ImageCandidate.model_validate(
                {**row.candidate, "enabled": not (excluded and excluded.excluded)}
            )
        )
    return result


def _cross_conflicts(session, scope, candidates):
    by_image = {}
    exclusions = _exclusions(session, scope)
    for drama_id, images in candidates.items():
        for image in images:
            if image.enabled:
                by_image.setdefault(image.material_id, []).append(drama_id)
    return [
        {
            "code": "CROSS_DRAMA_IMAGE",
            "material_id": str(material_id),
            "drama_ids": [str(d) for d in dramas],
        }
        for material_id, dramas in by_image.items()
        if len(dramas) > 1
        and not all(
            (r := exclusions.get((d, material_id))) and r.cross_drama_allowed
            for d in dramas
        )
    ]


def _units(session, scope, generation, drama_id=None):
    query = select(DraftCreativeUnit).where(
        col(DraftCreativeUnit.tenant_id) == scope.tenant_id,
        col(DraftCreativeUnit.draft_id) == scope.draft_id,
        col(DraftCreativeUnit.generation) == generation,
    )
    if drama_id:
        query = query.where(col(DraftCreativeUnit.drama_id) == drama_id)
    return session.exec(
        query.order_by(
            col(DraftCreativeUnit.drama_id), col(DraftCreativeUnit.position)
        ).execution_options(populate_existing=True)
    ).all()


def _unit_value(session, unit):
    items = session.exec(
        select(DraftCreativeUnitItem)
        .where(
            col(DraftCreativeUnitItem.tenant_id) == unit.tenant_id,
            col(DraftCreativeUnitItem.draft_id) == unit.draft_id,
            col(DraftCreativeUnitItem.generation) == unit.generation,
            DraftCreativeUnitItem.unit_id == unit.id,
        )
        .order_by(col(DraftCreativeUnitItem.position))
    ).all()
    keys = set(CreativeUnit.model_fields) - {"ordered_material_ids"}
    return CreativeUnit(
        **{k: getattr(unit, k) for k in keys},
        ordered_material_ids=tuple(i.material_id for i in items),
    )


def preview_composition(
    session: Session,
    scope: ImageDraftScope,
    request_id: UUID,
    expected_revision: int,
    rule: ImageRule,
    drama_patches: dict,
    mode="PRESERVE_MANUAL",
) -> dict[str, Any]:
    rule = ImageRule.model_validate(rule)
    patches = {
        str(k): ImageRulePatch.model_validate(v).model_dump(
            mode="json", exclude_unset=True
        )
        for k, v in drama_patches.items()
    }
    if mode not in {"PRESERVE_MANUAL", "REPLACE_ALL"}:
        raise DomainError("image_edit_invalid", "编排模式无效")
    body = {
        "rule": rule.model_dump(mode="json"),
        "drama_patches": patches,
        "mode": mode,
    }
    draft, fp, replay = _request(
        session, scope, request_id, "preview", expected_revision, body
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    state = _state(session, draft)
    if state.snapshot_id is None:
        raise DomainError("image_candidates_required", "请刷新并采纳图片候选")
    dramas = _dramas(session, scope)
    if not dramas or not set(patches) <= {str(d.drama_id) for d in dramas}:
        raise DomainError("image_edit_invalid", "逐剧规则不属于当前草稿")
    try:
        effective_rules = {
            d.drama_id: resolve_image_rule(rule, patches.get(str(d.drama_id), {}))
            for d in dramas
        }
    except ValueError:
        raise DomainError(
            "image_edit_invalid", "逐剧规则与整批规则组合无效，请核对单图模式和图片数量"
        ) from None
    if state.rule != body["rule"] or state.drama_patches != patches:
        state.rule, state.drama_patches = body["rule"], patches
        _bump_revision(session, draft, expected_revision)
    candidates = _candidates(session, scope, state)
    conflicts = _cross_conflicts(session, scope, candidates)
    available = _available_image_rows(
        session,
        scope,
        {c.material_id for group in candidates.values() for c in group if c.enabled},
    )
    for drama_id, images in candidates.items():
        for candidate in images:
            actual = available.get(candidate.material_id)
            if candidate.enabled and (
                actual is None or actual.sha256 != candidate.content_digest
            ):
                conflicts.append(
                    {
                        "code": "IMAGE_UNAVAILABLE",
                        "drama_id": str(drama_id),
                        "material_id": str(candidate.material_id),
                    }
                )
    plan = ImageCompositionPlan(
        tenant_id=scope.tenant_id,
        draft_id=scope.draft_id,
        bc_id=scope.bc_id,
        snapshot_id=state.snapshot_id,
        base_revision=draft.revision,
        input_digest=digest(
            {**body, "snapshot": state.snapshot_id, "revision": draft.revision}
        ),
        route=scope.route.model_dump(mode="json"),
    )
    session.add(plan)
    session.flush()
    from .image_diff_context import freeze_diff_context

    describe = freeze_diff_context(dramas, candidates, [])
    conflicts = [describe(c) for c in conflicts]
    ordinal, summaries = 0, []
    music_counts, image_uses = Counter(), Counter()
    new_count = removed_count = retained_count = protected_count = 0
    for drama in dramas:
        previous_rows = _units(
            session, scope, draft.active_image_generation, drama.drama_id
        )
        previous = tuple(_unit_value(session, u) for u in previous_rows)
        effective = effective_rules[drama.drama_id]
        diff = compose_images(
            tuple(candidates.get(drama.drama_id, [])), effective, previous, mode
        )
        details = diff.model_dump(mode="json", exclude={"units"})
        describe = freeze_diff_context(
            [drama],
            {drama.drama_id: candidates.get(drama.drama_id, [])},
            previous,
            {str(u.id): u.music_snapshot.get("display_name") for u in previous_rows},
        )
        for field in _DIFF_FIELDS:
            details[field] = [
                describe(item, drama.drama_id) for item in details.get(field, [])
            ]
        summaries.append(
            {"drama_id": str(drama.drama_id), "drama_title": drama.title, **details}
        )
        conflicts.extend(
            describe(c.model_dump(mode="json"), drama.drama_id) for c in diff.conflicts
        )
        removed_count += len(diff.removed_ids)
        retained_count += len(diff.retained_ids)
        for unit in diff.units:
            new_count += int(unit.id is None)
            protected_count += int(unit.composition_protected)
            music_counts[unit.music_status] += 1
            image_uses.update(unit.ordered_material_ids)
            ordinal += 1
            value = {**unit.model_dump(mode="json"), "id": str(unit.id or uuid4())}
            session.add(
                ImageCompositionPlanUnit(
                    tenant_id=scope.tenant_id,
                    draft_id=scope.draft_id,
                    bc_id=scope.bc_id,
                    plan_id=plan.id,
                    ordinal=ordinal,
                    drama_id=drama.drama_id,
                    unit=value,
                )
            )
    summary = {
        "plan_id": str(plan.id),
        "base_revision": draft.revision,
        "input_digest": plan.input_digest,
        "unit_count": ordinal,
        "added_unit_count": new_count,
        "removed_unit_count": removed_count,
        "retained_unit_count": retained_count,
        "protected_unit_count": protected_count,
        "music_counts": dict(music_counts),
        "image_reference_count": sum(image_uses.values()),
        "distinct_image_count": len(image_uses),
        "reused_image_count": sum(count > 1 for count in image_uses.values()),
        "conflicts": conflicts,
        "dramas": summaries,
        "mode": mode,
    }
    plan.summary = summary
    return _receipt(session, scope, request_id, fp, _public_plan(summary))


def _plan(session, scope, plan_id):
    plan = session.get(ImageCompositionPlan, UUID(str(plan_id)))
    if plan is None or (plan.tenant_id, plan.draft_id, plan.bc_id) != (
        scope.tenant_id,
        scope.draft_id,
        scope.bc_id,
    ):
        raise DomainError("image_composition_not_found", "编排计划不存在")
    return plan


def apply_composition(
    session: Session,
    scope: ImageDraftScope,
    plan_id: UUID | str,
    request_id: UUID,
    expected_revision: int,
) -> dict[str, Any]:
    plan_id = UUID(str(plan_id))
    draft, fp, replay = _request(
        session, scope, request_id, "apply", expected_revision, {"plan_id": plan_id}
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    plan = _plan(session, scope, plan_id)
    if plan.base_revision != draft.revision or plan.route != scope.route.model_dump(
        mode="json"
    ):
        raise DomainError("draft_revision_conflict", "试算或执行连接已变化，请重新试算")
    if plan.summary["conflicts"]:
        raise DomainError("image_composition_conflict", "请先解决试算中的冲突")
    state = _state(session, draft)
    generation = state.next_generation
    state.next_generation += 1
    run = ImageCompositionRun(
        tenant_id=scope.tenant_id,
        draft_id=scope.draft_id,
        bc_id=scope.bc_id,
        actor_id=scope.actor_id,
        plan_id=plan.id,
        generation=generation,
        base_revision=draft.revision,
        total=plan.summary["unit_count"],
    )
    session.add(run)
    session.flush()
    from app.modules.builds.image_composition_tasks import queue_run

    queue_run(session, run)
    return _receipt(session, scope, request_id, fp, _progress(run))


def _progress(run):
    return {
        "run_id": str(run.id),
        "plan_id": str(run.plan_id),
        "generation": run.generation,
        "status": run.status,
        "processed": run.cursor,
        "total": run.total,
        "cursor": run.cursor,
        "error_code": run.error_code,
    }


def _available_image_rows(session, scope, ids):
    from app.modules.accounts.access import usable_grants
    from app.modules.accounts.models import BCAccountAccess

    original = (
        select(TemporaryMaterialObject.material_id)
        .where(
            TemporaryMaterialObject.tenant_id == MaterialFile.tenant_id,
            TemporaryMaterialObject.material_id == MaterialFile.id,
            TemporaryMaterialObject.generation
            == MaterialFile.current_object_generation,
            TemporaryMaterialObject.status == "verified",
        )
        .exists()
    )
    grant = (
        usable_grants(tenant_id=scope.tenant_id, bc_id=scope.bc_id, action="read")
        .where(
            BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
            BCAccountAccess.connection_id == AccountMaterial.connection_id,
        )
        .exists()
    )
    asset = (
        select(AccountMaterial.id)
        .where(
            AccountMaterial.tenant_id == MaterialFile.tenant_id,
            AccountMaterial.material_id == MaterialFile.id,
            AccountMaterial.bc_id == scope.bc_id,
            AccountMaterial.connection_id == scope.route.connection_id,
            AccountMaterial.media_type == "IMAGE",
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
            col(AccountMaterial.is_carousel_usable).is_(True),
            col(AccountMaterial.frozen_route) == scope.route.model_dump(mode="json"),
            grant,
        )
        .exists()
    )
    return {
        r.id: r
        for r in session.exec(
            select(MaterialFile).where(
                MaterialFile.tenant_id == scope.tenant_id,
                col(MaterialFile.id).in_(ids),
                MaterialFile.media_type == "IMAGE",
                col(MaterialFile.sha256).is_not(None),
                or_(
                    and_(
                        col(MaterialFile.current_object_generation).is_(None),
                        col(MaterialFile.storage_state) == "stored",
                    ),
                    original,
                    asset,
                ),
            )
        ).all()
    }


def _validate_generation(session, scope, state, generation):
    # 切换前整批验证使用固定次数的查询；不逐单元重复扫描全剧候选/排除清单。
    members = session.exec(
        select(DraftCreativeUnitItem).where(
            DraftCreativeUnitItem.tenant_id == scope.tenant_id,
            DraftCreativeUnitItem.draft_id == scope.draft_id,
            DraftCreativeUnitItem.generation == generation,
        )
    ).all()
    candidates = {
        (r.drama_id, r.material_id): r
        for r in _snapshot_items(session, scope, state.snapshot_id)
    }
    exclusions = _exclusions(session, scope)
    material_rows = _available_image_rows(
        session, scope, {r.material_id for r in members}
    )
    for item in members:
        key = item.drama_id, item.material_id
        candidate, row, exclusion = (
            candidates.get(key),
            material_rows.get(item.material_id),
            exclusions.get(key),
        )
        if (
            candidate is None
            or row is None
            or (exclusion and exclusion.excluded)
            or row.sha256 != candidate.candidate["content_digest"]
        ):
            raise DomainError(
                "image_candidate_unavailable",
                "原定图片缺失或不可用；保留旧创意，不缩减成员",
            )


def _validate_images(session, scope, state, drama_id, ids):
    if not 1 <= len(ids) <= 35 or len(set(ids)) != len(ids):
        raise DomainError("image_edit_invalid", "创意必须包含 1–35 张不重复图片")
    candidates = {
        r.material_id: r
        for r in _snapshot_items(session, scope, state.snapshot_id, drama_id)
    }
    exclusions = _exclusions(session, scope)
    rows = _available_image_rows(session, scope, ids)
    for identity in ids:
        excluded = exclusions.get((drama_id, identity))
        if (
            identity not in candidates
            or identity not in rows
            or (excluded and excluded.excluded)
            or rows[identity].sha256 != candidates[identity].candidate["content_digest"]
        ):
            raise DomainError(
                "image_candidate_unavailable",
                "图片缺失、已排除或不属于当前剧目候选，请刷新或恢复候选",
            )
    return digest([[str(i), candidates[i].candidate["content_digest"]] for i in ids])


def _store_unit(session, scope, generation, drama_id, value, music_snapshot=None):
    unit = CreativeUnit.model_validate(value)
    row = DraftCreativeUnit(
        tenant_id=scope.tenant_id,
        draft_id=scope.draft_id,
        bc_id=scope.bc_id,
        generation=generation,
        drama_id=drama_id,
        **unit.model_dump(exclude={"ordered_material_ids"}),
        music_snapshot=music_snapshot or {},
    )
    session.add(row)
    session.flush()
    session.add_all(
        [
            DraftCreativeUnitItem(
                tenant_id=scope.tenant_id,
                draft_id=scope.draft_id,
                bc_id=scope.bc_id,
                generation=generation,
                drama_id=drama_id,
                unit_id=unit.id,
                position=index,
                material_id=identity,
                kind="CAROUSEL",
                media_type="IMAGE",
            )
            for index, identity in enumerate(unit.ordered_material_ids, 1)
        ]
    )
    session.flush()
    return row


def _inherit_music(old, proposed):
    return (
        old is not None
        and old.content_digest == proposed.content_digest
        and old.revision == proposed.revision
        and old.music_rule_digest == proposed.music_rule_digest
        and old.music_request_generation == proposed.music_request_generation
    )


def advance_composition(
    session: Session, run_id: UUID | str, generation: int, limit: int = 20
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise DomainError("invalid_page_size", "每片最多 20 个创意")
    run = session.get(ImageCompositionRun, UUID(str(run_id)))
    if run is None:
        raise DomainError("image_composition_not_found", "编排任务不存在")
    plan = session.get(ImageCompositionPlan, run.plan_id)
    assert plan is not None  # run→plan 的作用域 FK 保证存在。
    scope = ImageDraftScope(
        run.tenant_id,
        run.bc_id,
        run.draft_id,
        run.actor_id,
        FrozenTikTokRoute.model_validate(plan.route),
    )
    # 即使授权撤销，也先持有同一 draft 锁再记录失败；不重选新连接。
    draft = session.exec(
        select(BuildDraft)
        .where(BuildDraft.id == run.draft_id, BuildDraft.tenant_id == run.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    session.refresh(run)
    if run.status != "PENDING" or run.generation != generation:
        return _progress(run)
    try:
        with session.begin_nested():
            _draft(session, scope, lock=True)
            if draft.revision != run.base_revision:
                raise DomainError("draft_revision_conflict", "草稿已更新")
            state = _state(session, draft)
            rows = session.exec(
                select(ImageCompositionPlanUnit)
                .where(
                    col(ImageCompositionPlanUnit.tenant_id) == run.tenant_id,
                    ImageCompositionPlanUnit.plan_id == run.plan_id,
                    col(ImageCompositionPlanUnit.ordinal) > run.cursor,
                )
                .order_by(col(ImageCompositionPlanUnit.ordinal))
                .limit(limit)
            ).all()
            started = monotonic()
            for row in rows:
                value = CreativeUnit.model_validate(row.unit)
                _validate_images(
                    session, scope, state, row.drama_id, value.ordered_material_ids
                )
                old = session.get(
                    DraftCreativeUnit,
                    (
                        run.tenant_id,
                        run.draft_id,
                        draft.active_image_generation,
                        value.id,
                    ),
                )
                snapshot = None
                if old is not None and _inherit_music(old, value):
                    value = value.model_copy(
                        update={
                            key: getattr(old, key)
                            for key in ("music_id", "music_status", "music_revision")
                        }
                    )
                    snapshot = old.music_snapshot
                _store_unit(
                    session, scope, run.generation, row.drama_id, value, snapshot
                )
                run.cursor = row.ordinal
                if monotonic() - started >= 10:
                    break
            if run.cursor == run.total:
                _draft(session, scope, lock=True)
                # 再核对全部成员，并在切换瞬间继承刚完成的系统配乐；不跨内容/规则/请求代数。
                _validate_generation(session, scope, state, run.generation)
                previous = {
                    u.id: u
                    for u in _units(session, scope, draft.active_image_generation)
                }
                for unit in _units(session, scope, run.generation):
                    old = previous.get(unit.id)
                    if old is not None and _inherit_music(old, unit):
                        for key in (
                            "music_id",
                            "music_status",
                            "music_revision",
                            "music_snapshot",
                        ):
                            setattr(unit, key, getattr(old, key))
                draft.active_image_generation = run.generation
                run.status = "APPLIED"
                _bump_revision(session, draft, draft.revision)
                draft.applied_image_rule_revision = draft.revision
                from .carousel_music_tasks import queue_music_preparation

                queue_music_preparation(session, scope, draft)
            else:
                from app.modules.builds.image_composition_tasks import queue_run

                queue_run(session, run)
            session.flush()
    except DomainError as error:
        run.status, run.error_code = "FAILED", error.code
        session.flush()
    return _progress(run)


def cancel_composition(session, scope, run_id, request_id, expected_revision):
    draft, fp, replay = _request(
        session, scope, request_id, "cancel", expected_revision, {"run_id": str(run_id)}
    )
    if replay is not None:
        return replay
    run = _scoped_run(session, scope, run_id)
    if run.status == "PENDING":
        run.status = "CANCELLED"
    return _receipt(session, scope, request_id, fp, _progress(run))


def _scoped_run(session, scope, run_id):
    run = session.get(ImageCompositionRun, UUID(str(run_id)))
    if run is None or (run.tenant_id, run.draft_id, run.bc_id) != (
        scope.tenant_id,
        scope.draft_id,
        scope.bc_id,
    ):
        raise DomainError("image_composition_not_found", "任务不存在")
    return run


def composition_progress(session, scope, run_id):
    _draft(session, scope, verify=False)
    return _progress(_scoped_run(session, scope, run_id))


def _page(
    session,
    scope,
    query,
    *,
    kind,
    cursor=None,
    limit=50,
    transform=lambda x: x.model_dump(mode="json"),
):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "每页最多 100 条")
    owner = {
        "tenant": str(scope.tenant_id),
        "bc": scope.bc_id,
        "draft": str(scope.draft_id),
        "kind": kind,
    }
    after = decode_cursor(cursor, scope=owner)
    try:
        offset = int(after) if after else 0
        if offset < 0:
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    total = session.exec(
        select(func.count()).select_from(query.order_by(None).subquery())
    ).one()
    rows = session.exec(query.offset(offset).limit(limit + 1)).all()
    return Page(
        items=[transform(r) for r in rows[:limit]],
        total=total,
        next_cursor=encode_cursor(scope=owner, last_id=str(offset + limit))
        if len(rows) > limit
        else None,
    )


def list_creative_units(session, scope, *, cursor=None, limit=50, drama_id=None):
    draft = _draft(session, scope, verify=False)
    query = (
        select(DraftCreativeUnit)
        .where(
            col(DraftCreativeUnit.tenant_id) == scope.tenant_id,
            col(DraftCreativeUnit.draft_id) == scope.draft_id,
            col(DraftCreativeUnit.generation) == draft.active_image_generation,
        )
        .order_by(col(DraftCreativeUnit.drama_id), col(DraftCreativeUnit.position))
    )
    if drama_id is not None:
        query = query.where(DraftCreativeUnit.drama_id == drama_id)
    return _page(
        session,
        scope,
        query,
        kind=f"units:{draft.active_image_generation}:{draft.revision}:{drama_id}",
        cursor=cursor,
        limit=limit,
        transform=lambda r: {
            **_unit_value(session, r).model_dump(mode="json"),
            "drama_id": str(r.drama_id),
            "music_snapshot": r.music_snapshot,
        },
    )


def list_plan_units(session, scope, plan_id, *, cursor=None, limit=50):
    _draft(session, scope, verify=False)
    plan = _plan(session, scope, plan_id)
    query = (
        select(ImageCompositionPlanUnit)
        .where(
            col(ImageCompositionPlanUnit.tenant_id) == scope.tenant_id,
            ImageCompositionPlanUnit.plan_id == plan.id,
        )
        .order_by(col(ImageCompositionPlanUnit.ordinal))
    )
    titles = {
        row["drama_id"]: row.get("drama_title", "剧目名称未记录")
        for row in plan.summary["dramas"]
    }
    return _page(
        session,
        scope,
        query,
        kind=f"plan:{plan.id}",
        cursor=cursor,
        limit=limit,
        transform=lambda r: {
            **r.unit,
            "drama_id": str(r.drama_id),
            "drama_title": titles.get(str(r.drama_id), "已移除剧目"),
        },
    )


def list_candidates(
    session, scope, snapshot_id=None, *, cursor=None, limit=50, drama_id=None
):
    draft = _draft(session, scope, verify=False)
    state = session.get(ImageCompositionState, (scope.tenant_id, scope.draft_id))
    identity = (
        UUID(str(snapshot_id)) if snapshot_id else state.snapshot_id if state else None
    )
    if identity is not None:
        from .image_candidate_service import require_completed_snapshot

        require_completed_snapshot(session, scope, identity)
    exclusions = _exclusions(session, scope)
    query = (
        select(ImageCandidateItem)
        .where(
            col(ImageCandidateItem.tenant_id) == scope.tenant_id,
            col(ImageCandidateItem.draft_id) == scope.draft_id,
            col(ImageCandidateItem.snapshot_id) == identity,
        )
        .order_by(col(ImageCandidateItem.drama_id), col(ImageCandidateItem.material_id))
    )

    if drama_id is not None:
        query = query.where(ImageCandidateItem.drama_id == drama_id)

    def public(row):
        exclusion = exclusions.get((row.drama_id, row.material_id))
        return {
            **row.candidate,
            "drama_id": str(row.drama_id),
            "enabled": not (exclusion and exclusion.excluded),
            "cross_drama_allowed": bool(exclusion and exclusion.cross_drama_allowed),
        }

    return _page(
        session,
        scope,
        query,
        kind=f"candidates:{identity}:{draft.revision}:{drama_id}",
        cursor=cursor,
        limit=limit,
        transform=public,
    )


def edit_creative_units(
    session: Session,
    scope: ImageDraftScope,
    request_id: UUID,
    expected_revision: int,
    command: CreativeEditCommand,
) -> dict[str, Any]:
    draft, fp, replay = _request(
        session,
        scope,
        request_id,
        "edit",
        expected_revision,
        command.model_dump(mode="json"),
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    state = _state(session, draft)
    if state.snapshot_id is None:
        raise DomainError("image_candidates_required", "请先准备候选图片")
    if command.kind == "RESTORE_CANDIDATES":
        candidates = {
            r.material_id
            for r in _snapshot_items(
                session, scope, state.snapshot_id, command.drama_id
            )
        }
        if not set(command.material_ids) <= candidates:
            raise DomainError("image_candidate_unavailable", "图片不属于当前剧目快照")
        for identity in command.material_ids:
            _exclusion(session, scope, command.drama_id, identity).excluded = False
        _bump_revision(session, draft, expected_revision)
        return _receipt(
            session,
            scope,
            request_id,
            fp,
            {"revision": draft.revision, "affected_units": [], "pending_music_ids": []},
        )
    all_rows = _units(session, scope, draft.active_image_generation)
    rows = {u.id: u for u in all_rows}
    source_ids = (
        command.unit_ids
        if command.kind == "MERGE"
        else (command.unit_id,)
        if command.kind != "CREATE"
        else ()
    )
    if len(set(source_ids)) != len(source_ids) or any(
        i not in rows for i in source_ids
    ):
        raise DomainError("image_composition_not_found", "创意不属于当前草稿")
    drama_id = (
        command.drama_id if command.kind == "CREATE" else rows[source_ids[0]].drama_id
    )
    if any(rows[i].drama_id != drama_id for i in source_ids):
        raise DomainError("image_edit_invalid", "不能跨剧合并创意")
    effective_rule = resolve_image_rule(
        ImageRule.model_validate(state.rule or {}),
        state.drama_patches.get(str(drama_id), {}),
    )
    old_units = [_unit_value(session, r) for r in all_rows if r.drama_id == drama_id]
    by_id = {u.id: u for u in old_units}
    snapshots = {u.id: dict(rows[u.id].music_snapshot) for u in old_units}
    units = list(old_units)
    affected = []
    removed_images = set()

    def image_content(ids, *, allow_reuse=False, replacing=(), existing_reuse=()):
        content = _validate_images(session, scope, state, drama_id, ids)
        used = {
            i for u in units if u.id not in replacing for i in u.ordered_material_ids
        }
        # 已经明确建立的复用关系允许排序/删减；只有新增交集需要再次确认。
        if used.intersection(ids).difference(existing_reuse) and not allow_reuse:
            raise DomainError(
                "image_reuse_confirmation_required", "同图复用需要显式选择"
            )
        if _cross_conflicts(session, scope, _candidates(session, scope, state)):
            raise DomainError("image_composition_conflict", "请先解决跨剧图片冲突")
        return content

    def new_unit(ids, *, content, source=None):
        if source:
            value = source.model_copy(
                update={
                    "id": uuid4(),
                    "revision": 1,
                    "position": len(units) + 1,
                    "composition_protected": True,
                }
            )
            snapshots[value.id] = snapshots.get(source.id, {})
        else:
            value = CreativeUnit(
                id=uuid4(),
                ordered_material_ids=tuple(ids),
                content_digest=content,
                position=len(units) + 1,
                composition_protected=True,
                music_status=pending_music_status(effective_rule, len(ids)),
                music_rule_digest=music_rule_digest(effective_rule, len(ids)),
                music_request_generation=1,
            )
        affected.append(value.id)
        return value

    if command.kind == "CREATE":
        content = image_content(command.material_ids, allow_reuse=command.allow_reuse)
        units.append(new_unit(command.material_ids, content=content))
    elif command.kind == "DUPLICATE":
        old = by_id[command.unit_id]
        image_content(old.ordered_material_ids, allow_reuse=True)
        units.append(
            new_unit(old.ordered_material_ids, content=old.content_digest, source=old)
        )
    elif command.kind == "DELETE":
        old = by_id[command.unit_id]
        units.remove(old)
        affected.append(old.id)
        removed_images.update(old.ordered_material_ids)
    elif command.kind == "SET_IMAGES":
        old = by_id[command.unit_id]
        content = image_content(
            command.material_ids,
            allow_reuse=command.allow_reuse,
            replacing=(old.id,),
            existing_reuse=old.ordered_material_ids,
        )
        changed = old.ordered_material_ids != command.material_ids
        value = old.model_copy(
            update={
                "ordered_material_ids": command.material_ids,
                "content_digest": content,
                "composition_protected": True,
                "revision": old.revision + int(changed),
                "music_request_generation": old.music_request_generation + int(changed),
                "music_revision": old.music_revision + int(changed),
                "music_status": "READY"
                if old.music_id
                else pending_music_status(effective_rule, len(command.material_ids)),
                "music_rule_digest": old.music_rule_digest
                if old.music_id
                else music_rule_digest(effective_rule, len(command.material_ids)),
            }
        )
        if not changed:
            value = value.model_copy(
                update={
                    "music_status": old.music_status,
                    "music_rule_digest": old.music_rule_digest,
                }
            )
        if changed and old.music_id:
            snapshots[old.id] = {**snapshots[old.id], "images_changed": True}
        units[units.index(old)] = value
        removed_images.update(set(old.ordered_material_ids) - set(command.material_ids))
        affected.append(old.id)
    elif command.kind in {"MERGE", "SPLIT"}:
        sources = [by_id[i] for i in source_ids]
        if any(u.music_protected for u in sources):
            raise DomainError(
                "image_protected_music_conflict",
                "请先解除曲目保护；合并拆分不会猜选原曲",
            )
        if command.kind == "MERGE":
            ids = tuple(i for u in sources for i in u.ordered_material_ids)
            groups = [ids]
        else:
            assert command.kind == "SPLIT"
            ids = sources[0].ordered_material_ids
            cuts = command.split_positions
            if tuple(sorted(set(cuts))) != cuts or any(
                type(i) is not int or not 0 < i < len(ids) for i in cuts
            ):
                raise DomainError(
                    "image_edit_invalid", "拆分位置必须有序且位于图片之间"
                )
            bounds = (0, *cuts, len(ids))
            groups = [ids[a:b] for a, b in zip(bounds[:-1], bounds[1:], strict=True)]
        position = min(units.index(u) for u in sources)
        for u in sources:
            units.remove(u)
        made = [
            new_unit(group, content=image_content(group, allow_reuse=True))
            for group in groups
        ]
        units[position:position] = made
        affected.extend(source_ids)
    elif command.kind == "SET_PROTECTION":
        old = by_id[command.unit_id]
        changes = command.model_dump(exclude={"kind", "unit_id"}, exclude_none=True)
        if changes.get("music_protected") and not old.music_id:
            raise DomainError("image_edit_invalid", "未选择曲目不能保护曲目")
        units[units.index(old)] = old.model_copy(update=changes)
        affected.append(old.id)
    elif command.kind == "SET_MUSIC":
        old = by_id[command.unit_id]
        from .carousel_music import manual_selection

        units[units.index(old)] = old.model_copy(
            update=manual_selection(old, command.music_id)
        )
        snapshots[old.id] = {"selection_source": "MANUAL"}
        affected.append(old.id)
    # 删除的图片如果仍由另一显式副本使用，不禁用该副本；最后一个引用删除才排除。
    remaining = {i for u in units for i in u.ordered_material_ids}
    for identity in removed_images - remaining:
        _exclusion(session, scope, drama_id, identity).excluded = True
    generation = draft.active_image_generation
    if not generation:
        generation = state.next_generation
        state.next_generation += 1
        draft.active_image_generation = generation
    for model in (DraftCreativeUnitItem, DraftCreativeUnit):
        SASession.execute(
            session,
            delete(model).where(
                col(model.tenant_id) == scope.tenant_id,
                col(model.draft_id) == scope.draft_id,
                col(model.generation) == generation,
                col(model.drama_id) == drama_id,
            ),
            execution_options={"synchronize_session": "fetch"},
        )
    # 同一事务重新编号，避免位置唯一键交换时产生瞬时冲突。
    for index, unit in enumerate(units, 1):
        _store_unit(
            session,
            scope,
            generation,
            drama_id,
            unit.model_copy(update={"position": index}),
            snapshots.get(unit.id),
        )
    finish_creative_mutation(session, scope, draft, expected_revision)
    return _receipt(
        session,
        scope,
        request_id,
        fp,
        {
            "revision": draft.revision,
            "affected_units": affected,
            "pending_music_ids": [
                u.id for u in units if u.id in affected and u.music_status != "READY"
            ],
        },
    )


def finish_creative_mutation(session, scope, draft, expected_revision):
    """用户内容编辑统一推进草稿版本，并在同一事务登记真实配乐准备任务。"""
    was_applied = draft.applied_image_rule_revision == draft.revision
    _bump_revision(session, draft, expected_revision)
    if was_applied:
        draft.applied_image_rule_revision = draft.revision
    from .carousel_music_tasks import queue_music_preparation

    queue_music_preparation(session, scope, draft)


def prepare_image_candidates(session, context, draft, prep):
    """初次解析直接建立可用快照；再次准备保留旧快照，由刷新差异明确纳入。"""
    if prep.pending_links:
        prep.pending_links = False
        prep.phase = "links"
        return
    from .drafts import _preparation_route
    from .image_candidate_service import advance_candidate_snapshot

    scope = ImageDraftScope(
        context.tenant_id,
        draft.bc_id,
        draft.id,
        context.actor_id,
        _preparation_route(session, context, draft, prep),
    )
    state = _state(session, draft)
    if state.snapshot_id is None:
        snapshot = session.exec(
            select(ImageCandidateSnapshot).where(
                ImageCandidateSnapshot.tenant_id == scope.tenant_id,
                ImageCandidateSnapshot.draft_id == draft.id,
                ImageCandidateSnapshot.preparation_id == prep.id,
            )
        ).one_or_none()
        if snapshot is None:
            snapshot = build_candidate_snapshot(
                session, scope, draft, preparation_id=prep.id
            )
        elif snapshot.status == "PENDING":
            advance_candidate_snapshot(session, snapshot.id, snapshot.generation)
        if snapshot.status == "PENDING":
            return
        if snapshot.status != "READY":
            raise DomainError(
                snapshot.error_code or "image_candidates_incomplete",
                "图片候选准备未完成，请重新准备",
            )
        state.snapshot_id = snapshot.id
    _sync_candidate_counts(session, scope, state.snapshot_id)
    # 候选就绪后继续准备图片场景，读取小程序、身份和地区；不能在此提前结束。
    # 场景链路按 draft.media_kind 隔离缓存，不借用视频场景。
    prep.phase = "scenes"


def exclude_candidates(
    session, scope, request_id, expected_revision, drama_id, material_ids
):
    draft, fp, replay = _request(
        session,
        scope,
        request_id,
        "exclude",
        expected_revision,
        {"drama_id": drama_id, "material_ids": material_ids},
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    state = _state(session, draft)
    known = {
        r.material_id
        for r in _snapshot_items(session, scope, state.snapshot_id, drama_id)
    }
    if not material_ids or not set(material_ids) <= known:
        raise DomainError("image_candidate_unavailable", "图片不属于当前剧目候选")
    for identity in material_ids:
        _exclusion(session, scope, drama_id, identity).excluded = True
    _bump_revision(session, draft, expected_revision)
    return _receipt(session, scope, request_id, fp, {"revision": draft.revision})


def get_composition_plan(session, scope, plan_id):
    _draft(session, scope, verify=False)
    return _public_plan(_plan(session, scope, plan_id).summary)


def composition_state(session, scope):
    draft = _draft(session, scope, verify=False)
    state = session.get(ImageCompositionState, (scope.tenant_id, scope.draft_id))
    run = session.exec(
        select(ImageCompositionRun).where(
            col(ImageCompositionRun.tenant_id) == scope.tenant_id,
            col(ImageCompositionRun.draft_id) == scope.draft_id,
            ImageCompositionRun.status == "PENDING",
        )
    ).first()
    statuses = session.exec(
        select(col(DraftCreativeUnit.music_status), func.count())
        .where(
            col(DraftCreativeUnit.tenant_id) == scope.tenant_id,
            col(DraftCreativeUnit.draft_id) == scope.draft_id,
            col(DraftCreativeUnit.generation) == draft.active_image_generation,
        )
        .group_by(col(DraftCreativeUnit.music_status))
    ).all()
    from .image_candidate_service import snapshot_progress

    candidate_query = select(ImageCandidateSnapshot).where(
        ImageCandidateSnapshot.tenant_id == scope.tenant_id,
        ImageCandidateSnapshot.draft_id == scope.draft_id,
        ImageCandidateSnapshot.base_revision == draft.revision,
    )
    if state and state.snapshot_id:
        candidate_query = candidate_query.where(
            ImageCandidateSnapshot.id != state.snapshot_id
        )
    candidate_build = session.exec(
        candidate_query.order_by(col(ImageCandidateSnapshot.created_at).desc()).limit(1)
    ).first()
    from .creative_units import require_freeze_ready

    preview_reasons = []
    try:
        require_freeze_ready(session, draft, context=scope.context, route=scope.route)
        if not statuses or any(status != "READY" for status, _ in statuses):
            preview_reasons.append("music_not_ready")
    except DomainError as error:
        preview_reasons.append(error.code)
    return {
        "preview_ready": not preview_reasons,
        "preview_reason_codes": preview_reasons,
        "candidate_build": snapshot_progress(candidate_build)
        if candidate_build
        else None,
        "revision": draft.revision,
        "active_generation": draft.active_image_generation,
        "applied_rule_revision": draft.applied_image_rule_revision,
        "snapshot_id": str(state.snapshot_id) if state and state.snapshot_id else None,
        "rule": state.rule if state else {},
        "drama_patches": state.drama_patches if state else {},
        "run": _progress(run) if run else None,
        "music_counts": dict(statuses),
    }


def add_candidate_images(
    session, scope, request_id, expected_revision, drama_id, material_ids
):
    """手工加图先复制为新候选快照；不受文件名命中限制，但同样核验租户/来源。"""
    draft, fp, replay = _request(
        session,
        scope,
        request_id,
        "add-candidates",
        expected_revision,
        {"drama_id": drama_id, "material_ids": material_ids},
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    state = _state(session, draft)
    snapshot, added = include_candidate_images(
        session, scope, draft, state, drama_id, material_ids
    )
    _bump_revision(session, draft, expected_revision)
    return _receipt(
        session,
        scope,
        request_id,
        fp,
        {
            "revision": draft.revision,
            "snapshot_id": snapshot.id,
            "added": added,
        },
    )


def include_candidate_images(
    session, scope, draft, state, drama_id, material_ids, *, reuse_existing=False
):
    """事务内显式加入图片：公共加图和预设加载共用，调用者负责一次 CAS/回执。"""
    if drama_id not in {d.drama_id for d in _dramas(session, scope)}:
        raise DomainError("image_edit_invalid", "剧目不属于当前草稿")
    if not material_ids or len(set(material_ids)) != len(material_ids):
        raise DomainError("image_edit_invalid", "图片引用不能为空或重复")
    rows = _available_image_rows(session, scope, material_ids)
    if set(rows) != set(material_ids):
        raise DomainError(
            "image_candidate_unavailable", "图片不属于当前租户或尚无可用来源"
        )
    known = (
        {
            r.material_id
            for r in _snapshot_items(session, scope, state.snapshot_id, drama_id)
        }
        if state.snapshot_id
        else set()
    )
    if reuse_existing and set(material_ids) <= known:
        return session.get(ImageCandidateSnapshot, state.snapshot_id), 0
    snapshot = ImageCandidateSnapshot(
        tenant_id=scope.tenant_id,
        draft_id=scope.draft_id,
        bc_id=scope.bc_id,
        base_revision=draft.revision,
        actor_id=scope.actor_id,
        route=scope.route.model_dump(mode="json"),
        source_snapshot_id=state.snapshot_id,
    )
    session.add(snapshot)
    session.flush()
    if state.snapshot_id:
        # SQL 复制不可变事实，不从当前文件名重建既有候选，也不把整库送到前端。
        names = (
            "tenant_id",
            "snapshot_id",
            "drama_id",
            "material_id",
            "draft_id",
            "bc_id",
            "media_type",
            "candidate",
            "source",
        )
        session.execute(
            insert(ImageCandidateItem).from_select(
                names,
                select(
                    *[
                        literal(snapshot.id)
                        if name == "snapshot_id"
                        else col(getattr(ImageCandidateItem, name))
                        for name in names
                    ]
                ).where(
                    ImageCandidateItem.tenant_id == scope.tenant_id,
                    ImageCandidateItem.snapshot_id == state.snapshot_id,
                ),
            )
        )
    _append_candidates(
        session,
        scope,
        snapshot,
        drama_id,
        [rows[i] for i in material_ids if i not in known],
    )
    session.flush()
    snapshot.candidate_count = session.exec(
        select(func.count())
        .select_from(ImageCandidateItem)
        .where(
            ImageCandidateItem.tenant_id == scope.tenant_id,
            ImageCandidateItem.snapshot_id == snapshot.id,
        )
    ).one()
    snapshot.summary = {"added": len(set(material_ids) - known), "removed": 0}
    state.snapshot_id = snapshot.id
    _sync_candidate_counts(session, scope, snapshot.id)
    return snapshot, len(set(material_ids) - known)


_DIFF_FIELDS = (
    "excluded_images",
    "warnings",
    "removed_ids",
    "retained_ids",
    "protection_losses",
)


def _public_plan(summary):
    # 大批次只返回汇总和首屏冲突；图片/逐项差异通过受 scope 约束的分页 API 读取。
    dramas = []
    for row in summary["dramas"]:
        dramas.append(
            {
                **{
                    k: v
                    for k, v in row.items()
                    if k not in (*_DIFF_FIELDS, "conflicts")
                },
                **{
                    f"{key}_count": len(row.get(key, []))
                    for key in (*_DIFF_FIELDS, "conflicts")
                },
            }
        )
    return {
        **summary,
        "dramas": dramas,
        "conflicts": summary["conflicts"][:100],
        "conflict_count": len(summary["conflicts"]),
    }


def list_plan_differences(
    session, scope, plan_id, *, kind="conflicts", cursor=None, limit=50
):
    _draft(session, scope, verify=False)
    plan = _plan(session, scope, plan_id)
    if kind not in ("conflicts", *_DIFF_FIELDS):
        raise DomainError("image_edit_invalid", "差异类型无效")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "每页最多 100 条")
    owner = {
        "tenant": str(scope.tenant_id),
        "bc": scope.bc_id,
        "draft": str(scope.draft_id),
        "kind": f"plan-difference:{plan.id}:{kind}",
    }
    after = decode_cursor(cursor, scope=owner)
    try:
        offset = int(after) if after else 0
        if offset < 0:
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    items = (
        plan.summary["conflicts"]
        if kind == "conflicts"
        else [
            {
                "drama_id": drama["drama_id"],
                **(item if isinstance(item, dict) else {"unit_id": item}),
            }
            for drama in plan.summary["dramas"]
            for item in drama.get(kind, [])
        ]
    )
    return Page(
        items=items[offset : offset + limit],
        total=len(items),
        next_cursor=encode_cursor(scope=owner, last_id=str(offset + limit))
        if offset + limit < len(items)
        else None,
    )
