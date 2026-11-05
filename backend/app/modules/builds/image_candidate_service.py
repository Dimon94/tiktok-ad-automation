"""候选构建只推进一页；快照截止点、剧目输入和游标与 outbox 同事务持久化。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text, update
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.builds.image_composition_state import (
    ImageCandidateSnapshot,
)
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.materials.models import MaterialFile
from app.modules.materials.repository import material_visible

from .drama_details import material_match_text

CANDIDATE_PAGE_SIZE = 100


def start_candidate_snapshot(session, scope, draft, *, preparation_id=None):
    from .image_composition_service import _dramas, _state

    pending = session.exec(
        select(ImageCandidateSnapshot.id).where(
            ImageCandidateSnapshot.tenant_id == scope.tenant_id,
            ImageCandidateSnapshot.draft_id == draft.id,
            ImageCandidateSnapshot.status == "PENDING",
        )
    ).first()
    if pending:
        raise DomainError("image_candidates_busy", "图片候选正在构建，请等待或取消")
    state = _state(session, draft)
    snapshot = ImageCandidateSnapshot(
        tenant_id=scope.tenant_id,
        draft_id=draft.id,
        bc_id=scope.bc_id,
        base_revision=draft.revision,
        status="PENDING",
        actor_id=scope.actor_id,
        preparation_id=preparation_id,
        source_snapshot_id=state.snapshot_id,
        route=scope.route.model_dump(mode="json"),
        dramas=[
            # 快照 title 仅用于文件名检索，冻结实际依据；展示剧名仍读取 DraftDrama。
            {
                "drama_id": str(d.drama_id),
                "title": material_match_text(session, draft, d),
            }
            for d in _dramas(session, scope)
        ],
        repair_after=datetime.now(UTC) + timedelta(seconds=120),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def snapshot_progress(snapshot):
    return {
        "snapshot_id": str(snapshot.id),
        "revision": snapshot.base_revision,
        "status": snapshot.status,
        "generation": snapshot.generation,
        "processed": snapshot.candidate_count,
        "candidate_count": snapshot.candidate_count,
        "completed_drama_count": snapshot.drama_cursor,
        "total_drama_count": len(snapshot.dramas),
        "added": snapshot.summary.get("added"),
        "removed": snapshot.summary.get("removed"),
        "error_code": snapshot.error_code,
        "cutoff": snapshot.created_at.isoformat(),
    }


def scoped_snapshot(session, scope, snapshot_id):
    snapshot = session.get(ImageCandidateSnapshot, UUID(str(snapshot_id)))
    if snapshot is None or (snapshot.tenant_id, snapshot.draft_id, snapshot.bc_id) != (
        scope.tenant_id,
        scope.draft_id,
        scope.bc_id,
    ):
        raise DomainError("image_composition_not_found", "候选快照不存在")
    return snapshot


def require_completed_snapshot(session, scope, snapshot_id):
    snapshot = scoped_snapshot(session, scope, snapshot_id)
    if snapshot.status != "READY":
        raise DomainError(
            "image_candidates_incomplete", "候选尚未完整构建，暂不能查看或采纳"
        )
    return snapshot


def _complete_snapshot(session, snapshot):
    # 差异在数据库里聚合，不为了完成一次任务把所有候选再次读入 Python。
    parameters = {
        "tenant": snapshot.tenant_id,
        "new": snapshot.id,
        "old": snapshot.source_snapshot_id,
    }
    result = (
        session.execute(
            text("""
SELECT
 (SELECT count(*) FROM image_candidate_item n WHERE n.tenant_id=:tenant AND n.snapshot_id=:new
  AND NOT EXISTS (SELECT 1 FROM image_candidate_item o WHERE o.tenant_id=n.tenant_id
    AND o.snapshot_id=:old AND o.drama_id=n.drama_id AND o.material_id=n.material_id)) added,
 (SELECT count(*) FROM image_candidate_item o WHERE o.tenant_id=:tenant AND o.snapshot_id=:old
  AND NOT EXISTS (SELECT 1 FROM image_candidate_item n WHERE n.tenant_id=o.tenant_id
    AND n.snapshot_id=:new AND n.drama_id=o.drama_id AND n.material_id=o.material_id)) removed
"""),
            parameters,
        )
        .mappings()
        .one()
    )
    snapshot.status = "READY"
    snapshot.summary = dict(result)
    snapshot.repair_after = None


def advance_candidate_snapshot(
    session: Session, snapshot_id: UUID | str, generation: int
):
    from .image_composition_service import ImageDraftScope, _append_candidates, _draft

    snapshot = session.get(ImageCandidateSnapshot, UUID(str(snapshot_id)))
    if snapshot is None:
        raise DomainError("image_composition_not_found", "候选快照不存在")
    # 与所有修改统一 parent→snapshot 锁顺序；终态/旧消息不需要再获取已撤销的授权。
    draft = session.exec(
        select(BuildDraft)
        .where(
            BuildDraft.tenant_id == snapshot.tenant_id,
            BuildDraft.id == snapshot.draft_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    session.refresh(snapshot)
    if snapshot.status != "PENDING" or snapshot.generation != generation:
        return snapshot_progress(snapshot)
    assert snapshot.actor_id is not None
    scope = ImageDraftScope(
        snapshot.tenant_id,
        snapshot.bc_id,
        snapshot.draft_id,
        snapshot.actor_id,
        FrozenTikTokRoute.model_validate(snapshot.route),
    )
    try:
        with session.begin_nested():
            # 持有行锁不等于权限；每一页必须重新核验当前 build 权限。
            _draft(session, scope, lock=True)
            if draft.revision != snapshot.base_revision:
                raise DomainError(
                    "draft_revision_conflict", "草稿已变更，请重新刷新候选"
                )
            if snapshot.preparation_id:
                prep = session.get(DraftPreparation, snapshot.preparation_id)
                if (
                    prep is None
                    or prep.status != "PENDING"
                    or prep.draft_revision != draft.revision
                ):
                    raise DomainError(
                        "draft_revision_conflict", "原候选准备已结束或失效"
                    )
            if snapshot.drama_cursor < len(snapshot.dramas):
                drama = snapshot.dramas[snapshot.drama_cursor]
                query = select(MaterialFile).where(
                    MaterialFile.tenant_id == snapshot.tenant_id,
                    MaterialFile.media_type == "IMAGE",
                    col(MaterialFile.created_at) <= snapshot.created_at,
                    material_visible(tenant_id=snapshot.tenant_id),
                    col(MaterialFile.file_name_folded).contains(
                        drama["title"].strip().casefold(), autoescape=True
                    ),
                )
                if snapshot.material_after:
                    query = query.where(MaterialFile.id > snapshot.material_after)
                rows = session.exec(
                    query.order_by(col(MaterialFile.id)).limit(CANDIDATE_PAGE_SIZE)
                ).all()
                _append_candidates(
                    session, scope, snapshot, UUID(drama["drama_id"]), rows
                )
                snapshot.candidate_count += len(rows)
                if len(rows) < CANDIDATE_PAGE_SIZE:
                    snapshot.drama_cursor += 1
                    snapshot.material_after = None
                else:
                    snapshot.material_after = rows[-1].id
            snapshot.generation += 1
            session.flush()
            if snapshot.drama_cursor == len(snapshot.dramas):
                _draft(session, scope, lock=True)
                _complete_snapshot(session, snapshot)
            elif snapshot.preparation_id is None:
                from .image_candidate_tasks import queue_candidate_snapshot

                queue_candidate_snapshot(session, snapshot)
            session.flush()
    except DomainError as error:
        snapshot.status, snapshot.error_code = "FAILED", error.code
        snapshot.repair_after = None
        session.flush()
    return snapshot_progress(snapshot)


def candidate_snapshot_progress(session, scope, snapshot_id):
    from .image_composition_service import _draft

    _draft(session, scope, verify=False)
    return snapshot_progress(scoped_snapshot(session, scope, snapshot_id))


def cancel_candidate_snapshot(
    session, scope, snapshot_id, request_id, expected_revision
):
    from .image_composition_service import _receipt, _request

    draft, fp, replay = _request(
        session,
        scope,
        request_id,
        "cancel",
        expected_revision,
        {"snapshot_id": str(snapshot_id)},
    )
    if replay is not None:
        return replay
    snapshot = scoped_snapshot(session, scope, snapshot_id)
    if snapshot.status == "PENDING":
        snapshot.status, snapshot.error_code, snapshot.repair_after = (
            "CANCELLED",
            "image_candidates_cancelled",
            None,
        )
        if snapshot.preparation_id:
            prep = session.get(DraftPreparation, snapshot.preparation_id)
            assert prep is not None
            if prep.status == "PENDING":
                prep.status, prep.error_code = "BLOCKED", "image_candidates_cancelled"
                draft.status = "BLOCKED"
    return _receipt(session, scope, request_id, fp, snapshot_progress(snapshot))


def fail_initial_candidate_snapshot(session, prep, code):
    session.execute(
        update(ImageCandidateSnapshot)
        .where(
            col(ImageCandidateSnapshot.tenant_id) == prep.tenant_id,
            col(ImageCandidateSnapshot.draft_id) == prep.draft_id,
            col(ImageCandidateSnapshot.preparation_id) == prep.id,
            col(ImageCandidateSnapshot.status) == "PENDING",
        )
        .values(status="FAILED", error_code=code, repair_after=None)
    )


def invalidate_candidate_snapshots(session, draft):
    # 用户修改使旧提案过期，初次准备也不能把永远无人续作的 PENDING 留下。
    session.execute(
        update(ImageCandidateSnapshot)
        .where(
            col(ImageCandidateSnapshot.tenant_id) == draft.tenant_id,
            col(ImageCandidateSnapshot.draft_id) == draft.id,
            col(ImageCandidateSnapshot.status) == "PENDING",
            col(ImageCandidateSnapshot.base_revision) < draft.revision,
        )
        .values(
            status="FAILED", error_code="draft_revision_conflict", repair_after=None
        )
    )
