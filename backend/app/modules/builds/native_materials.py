"""原生有序选材接线：沿素材 owner 准备，按当前 revision 消费正证据。"""

from copy import deepcopy
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.readiness import mapping_fresh

from .models import DraftPreparation
from .scene_job_models import DraftScenePreparation


def prepare_native_materials(
    session, *, context, draft, prep, route, checkpoint, plan_key
):
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import ensure_cover, get_cover_status
    from app.modules.materials.distribution import ensure_target_asset

    state = session.get(DraftScenePreparation, (context.tenant_id, prep.id))
    sources = checkpoint.selection.video_sources
    # 每轮至多20项，轮询复用原任务；先锁素材再锁映射，与素材 owner 同序。
    saved = (state.native_materials or {}).get("plans", {}).get(str(plan_key), {})
    previous = deepcopy(saved.get("items", []))
    cursor = saved.get("cursor", 0)
    selected = sources[cursor : cursor + 20]
    # 所有来源按固定 ID 锁序领取；输出仍严格使用选择顺序。
    ids = {source.material_id for source in selected} | {
        source.cover_material_id for source in selected if source.cover_material_id
    }
    files = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id, col(MaterialFile.id).in_(ids)
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    files = {row.id: row for row in files}
    items = deepcopy(previous) or [
        {"position": i, **source.model_dump(mode="json"), "state": "queued"}
        for i, source in enumerate(sources)
    ]
    for index, source in enumerate(selected, cursor):
        entry = {
            "position": index,
            **source.model_dump(mode="json"),
            "state": "blocked",
        }
        old = (
            previous[index]
            if index < len(previous)
            and previous[index].get("material_id") == str(source.material_id)
            else {}
        )
        items[index] = entry
        try:
            if (
                source.material_id not in files
                or files[source.material_id].media_type != "VIDEO"
            ):
                raise DomainError("native_video_source_missing", "选中视频不存在")
            if source.cover_material_id and (
                source.cover_material_id not in files
                or files[source.cover_material_id].media_type != "IMAGE"
            ):
                raise DomainError("native_cover_source_missing", "选中封面不存在")
            result = ensure_target_asset(
                session,
                context=context,
                bc_id=draft.bc_id,
                material_id=source.material_id,
                advertiser_id=checkpoint.selection.advertiser_id,
                task_key=f"native:{prep.id}:{plan_key}:{source.material_id}",
                route=route,
            )
            entry.update(
                video_task_id=str(result.task_id)
                if result.task_id
                else old.get("video_task_id"),
                state=result.state,
                reason_code=result.reason_code,
            )
            if result.state != "ready":
                continue
            video = result.mapping
            entry.update(
                video_id=video.video_id,
                video_evidence_id=str(video.asset_id),
                video_md5=files[source.material_id].video_md5,
            )
            if source.cover_material_id:
                cover = ensure_target_asset(
                    session,
                    context=context,
                    bc_id=draft.bc_id,
                    material_id=source.cover_material_id,
                    advertiser_id=checkpoint.selection.advertiser_id,
                    task_key=f"native-cover:{prep.id}:{plan_key}:{source.cover_material_id}",
                    route=route,
                )
            else:
                jobs = session.exec(
                    select(MaterialCoverJob).where(
                        MaterialCoverJob.tenant_id == context.tenant_id,
                        MaterialCoverJob.asset_id == video.asset_id,
                        MaterialCoverJob.video_id == video.video_id,
                        col(MaterialCoverJob.superseded_by_id).is_(None),
                    )
                ).all()
                if len(jobs) > 1:
                    raise DomainError("native_cover_mapping_ambiguous", "封面映射多义")
                if jobs and jobs[0].frozen_route == route.model_dump(mode="json"):
                    cover = get_cover_status(
                        session, context=context, job_id=jobs[0].id
                    )
                else:
                    cover = ensure_cover(
                        session,
                        context=context,
                        bc_id=draft.bc_id,
                        material_id=source.material_id,
                        advertiser_id=checkpoint.selection.advertiser_id,
                        task_key=f"native-cover:{prep.id}:{plan_key}:{source.material_id}",
                        route=route,
                    )
            entry.update(
                state=cover.state,
                reason_code=cover.reason_code,
                cover_task_id=str(cover.task_id)
                if cover.task_id
                else old.get("cover_task_id"),
            )
            if cover.state == "ready":
                entry.update(
                    image_id=cover.mapping.image_id,
                    cover_evidence_id=str(cover.mapping.asset_id),
                )
        except DomainError as error:
            entry.update(state="blocked", reason_code=error.code)
    entry_state = {
        "draft_revision": draft.revision,
        "scope_basis": checkpoint.scope_basis,
        "items": items,
        "cursor": cursor + 20 if cursor + 20 < len(sources) else 0,
    }
    state.native_materials = {
        "plans": {
            **(state.native_materials or {}).get("plans", {}),
            str(plan_key): entry_state,
        }
    }
    session.flush()
    return items


def read_native_materials(session: Session, *, context, draft, checkpoint, plan_key):
    """只读消费；不通过 ID 列表重排或去重来修复损坏的准备回执。"""
    prep = session.exec(
        select(DraftPreparation).where(
            DraftPreparation.tenant_id == context.tenant_id,
            DraftPreparation.draft_id == draft.id,
            DraftPreparation.draft_revision == draft.revision,
        )
    ).one_or_none()
    if prep is None or prep.status == "OBSOLETE":
        return [], []
    state = session.get(DraftScenePreparation, (context.tenant_id, prep.id))
    stored = state.native_materials if state else None
    # 旧 M1 回执只读规范化，不改写历史冻结数据。
    evidence = (
        stored.get("plans", {}).get(str(plan_key))
        if stored and "plans" in stored
        else stored
    )
    if not evidence:
        return [], []
    if (
        state.frozen_route != checkpoint.route
        or evidence.get("scope_basis") != checkpoint.scope_basis
        or evidence.get("draft_revision") != draft.revision
    ):
        return [], [{"reason_code": "native_material_scope_changed"}]
    sources = checkpoint.selection.video_sources
    entries = evidence.get("items", [])
    if len(entries) != len(sources):
        return [], [{"reason_code": "native_material_mapping_count"}]
    results, issues = [], []
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import verified_cover_image_id
    from app.modules.materials.image_distribution import image_ready

    for index, (source, entry) in enumerate(zip(sources, entries, strict=True)):
        issue = {"position": index, "material_id": str(source.material_id)}
        code = None
        if (
            entry.get("position") != index
            or entry.get("material_id") != str(source.material_id)
            or entry.get("cover_material_id")
            != (str(source.cover_material_id) if source.cover_material_id else None)
        ):
            code = "native_material_mapping_order"
        elif entry.get("state") != "ready":
            code = entry.get("reason_code") or "native_material_pending"
        else:
            video = (
                session.get(AccountMaterial, UUID(entry["video_evidence_id"]))
                if entry.get("video_evidence_id")
                else None
            )
            cover = (
                session.get(AccountMaterial, UUID(entry["cover_evidence_id"]))
                if entry.get("cover_evidence_id")
                else None
            )

            def scoped(asset, material_id):
                return asset and (
                    asset.tenant_id,
                    asset.bc_id,
                    asset.advertiser_id,
                    asset.material_id,
                ) == (
                    context.tenant_id,
                    draft.bc_id,
                    checkpoint.selection.advertiser_id,
                    material_id,
                )

            material = session.get(MaterialFile, source.material_id)
            if (
                not scoped(video, source.material_id)
                or not mapping_fresh(video)
                or video.video_id != entry.get("video_id")
                or material is None
                or material.video_md5 != entry.get("video_md5")
            ):
                code = "native_video_mapping_unverified"
            elif (
                not scoped(cover, source.cover_material_id or source.material_id)
                or cover.status != "available"
                or not cover.verified_at
                or not entry.get("image_id")
                or cover.image_id != entry["image_id"]
            ):
                code = "native_cover_mapping_unverified"
            elif source.cover_material_id and not image_ready(
                cover, FrozenTikTokRoute.model_validate(checkpoint.route)
            ):
                code = "native_cover_mapping_unverified"
            elif not source.cover_material_id:
                jobs = session.exec(
                    select(MaterialCoverJob).where(
                        MaterialCoverJob.tenant_id == context.tenant_id,
                        MaterialCoverJob.asset_id == video.id,
                        MaterialCoverJob.video_id == video.video_id,
                        col(MaterialCoverJob.superseded_by_id).is_(None),
                    )
                ).all()
                if len(jobs) > 1:
                    code = "native_cover_mapping_ambiguous"
                elif jobs and verified_cover_image_id(jobs[0]) != entry["image_id"]:
                    code = "native_cover_evidence_unverified"
                elif entry.get("cover_task_id") and (
                    not jobs or str(jobs[0].id) != entry["cover_task_id"]
                ):
                    code = "native_cover_evidence_unverified"
            if (
                code is None
                and entry.get("cover_task_id")
                and not source.cover_material_id
            ):
                job = session.get(MaterialCoverJob, UUID(entry["cover_task_id"]))
                if (
                    job is None
                    or job.asset_id != video.id
                    or job.video_id != video.video_id
                    or job.frozen_route != checkpoint.route
                    or verified_cover_image_id(job) != entry["image_id"]
                ):
                    code = "native_cover_evidence_unverified"
        if code:
            issues.append(
                {
                    **issue,
                    "reason_code": code,
                    "state": "PENDING"
                    if entry.get("state") == "queued"
                    and code == (entry.get("reason_code") or "native_material_pending")
                    else "BLOCKED",
                }
            )
        else:
            results.append({**entry, "preparation_id": prep.id})
    return ([] if issues else results), issues
