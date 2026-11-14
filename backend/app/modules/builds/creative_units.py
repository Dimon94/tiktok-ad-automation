"""V2 冻结单元：内容身份、有序成员和曲目只在草稿锁内拍一次快照。"""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlmodel import col, select

from app.core.errors import DomainError

from .image_public import PublicMusicSnapshot
from .preview_models import (
    PreviewAdCreativeUnit,
    PreviewContract,
    PreviewCreativeUnit,
    PreviewCreativeUnitItem,
)


class FrozenCreativeMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    material_id: UUID
    content_digest: str | None
    file_name: str


class FrozenCreativeUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    unit_id: UUID
    kind: Literal["VIDEO", "CAROUSEL"]
    generation: int
    content_revision: int
    music_revision: int
    content_digest: str
    music_id: str | None
    music_snapshot: PublicMusicSnapshot
    members: tuple[FrozenCreativeMember, ...]


def creative_content_digest(kind, members, music_id):
    """同图不同音乐不合并；图片顺序是内容身份的一部分。"""
    value = [2, kind, [(str(i), sha) for i, sha in members], music_id]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def preview_contract(session, *, tenant_id, preview_id):
    value = session.get(PreviewContract, (tenant_id, preview_id))
    if value is None:
        raise DomainError("preview_contract_missing", "冻结合同描述缺失")
    return value


def require_freeze_ready(session, draft, *, context, route):
    """所有检查按当前代数/采用快照执行；数据库聚合不向请求进程拉整批图片。"""
    if context.tenant_id != draft.tenant_id or (route.tenant_id, route.bc_id) != (
        draft.tenant_id,
        draft.bc_id,
    ):
        raise DomainError("preview_scope_mismatch", "冻结范围与当前租户或 BC 不符")
    if draft.media_kind == "VIDEO":
        return
    from .image_composition_state import (
        ImageCandidateSnapshot,
        ImageCompositionRun,
        ImageCompositionState,
    )

    if (
        not draft.active_image_generation
        or draft.applied_image_rule_revision != draft.revision
    ):
        raise DomainError("image_rules_not_applied", "请先应用当前图片规则")
    for model in (ImageCandidateSnapshot, ImageCompositionRun):
        if session.exec(
            select(model.id).where(
                model.tenant_id == draft.tenant_id,
                model.draft_id == draft.id,
                model.base_revision == draft.revision,
                model.status == "PENDING",
            )
        ).first():
            raise DomainError("image_composition_pending", "图片候选或编排仍在准备中")
    music_pending = has_live_music_intent(session, draft)
    if music_pending:
        raise DomainError("music_preparation_pending", "配乐准备尚未完成")
    state = session.get(ImageCompositionState, (draft.tenant_id, draft.id))
    snapshot = (
        session.get(ImageCandidateSnapshot, state.snapshot_id)
        if state and state.snapshot_id
        else None
    )
    if snapshot is None or snapshot.status != "READY":
        raise DomainError("image_candidates_missing", "图片候选快照尚未完成")
    values = _snapshot_parameters(draft, snapshot_id=state.snapshot_id)
    invalid = session.execute(
        text(
            CANDIDATE_CTE
            + """
        SELECT CASE
          WHEN EXISTS (SELECT drama_id FROM current_dramas EXCEPT SELECT drama_id FROM candidates) THEN 'image_unresolved'
          WHEN EXISTS (SELECT material_id FROM candidates WHERE NOT excluded GROUP BY material_id HAVING count(DISTINCT drama_id)>1 AND NOT bool_and(cross_allowed)) THEN 'image_composition_conflict'
          WHEN EXISTS (SELECT drama_id,material_id FROM members EXCEPT SELECT drama_id,material_id FROM candidates WHERE NOT excluded) THEN 'image_unresolved'
          WHEN EXISTS (SELECT drama_id::text,material_id::text FROM candidates WHERE NOT excluded EXCEPT SELECT drama_id::text,material_id::text FROM members EXCEPT SELECT drama_id,material_id FROM remainders) THEN 'image_unresolved'
          ELSE NULL END
    """
        ),
        values,
    ).scalar_one()
    if invalid:
        raise DomainError(invalid, "存在未解决的图片候选、编排或跨剧冲突")


def has_live_music_intent(session, draft):
    # Match the worker's live intent fence, without loading all job subscribers.
    # PREPARE scanners can have no jobs yet; RETRY_FAILED is revision-bound.
    return session.execute(
        text("""
        SELECT EXISTS (
          SELECT 1 FROM music_preparation_run r
          WHERE r.tenant_id=:tenant AND r.draft_id=:draft AND r.active_generation=:generation
            AND r.status='PENDING' AND (r.action='PREPARE' OR r.base_revision=:revision)
        ) OR EXISTS (
          SELECT 1 FROM music_selection_job j
          CROSS JOIN LATERAL jsonb_array_elements(j.targets) WITH ORDINALITY t(target,ordinal)
          JOIN draft_creative_unit u ON u.tenant_id=j.tenant_id AND u.draft_id=j.draft_id
            AND u.generation=j.active_generation AND u.id::text=t.target->>'unit_id'
          WHERE j.tenant_id=:tenant AND j.draft_id=:draft AND j.active_generation=:generation
            AND j.status IN ('PENDING','RUNNING') AND t.ordinal>j.cursor AND NOT u.music_protected
            AND t.target=jsonb_build_object('unit_id',u.id::text,'revision',u.revision,
              'content_digest',u.content_digest,'rule_digest',u.music_rule_digest,
              'request_generation',u.music_request_generation)
        )
        """),
        _snapshot_parameters(draft, revision=draft.revision),
    ).scalar_one()


def _snapshot_parameters(draft, **extra):
    return {
        "tenant": draft.tenant_id,
        "draft": draft.id,
        "bc": draft.bc_id,
        "generation": 1
        if draft.media_kind == "VIDEO"
        else draft.active_image_generation,
        "kind": draft.media_kind,
        **extra,
    }


SOURCE_CTE = """
WITH sources AS MATERIALIZED (
  SELECT u.* FROM draft_creative_unit u
  WHERE u.tenant_id=:tenant AND u.draft_id=:draft AND u.generation=:generation
    AND EXISTS (SELECT 1 FROM draft_input d WHERE d.tenant_id=u.tenant_id AND d.draft_id=u.draft_id
      AND d.kind='drama' AND d.status='ready' AND d.drama_id=u.drama_id)
), content AS MATERIALIZED (
  -- 一次聚合整批成员；逐单元LATERAL会在冷统计下重复扫描同一万条成员。
  SELECT u.id,count(i.position) AS count,min(i.position) AS first,max(i.position) AS last,
    bool_and(f.media_type=CASE WHEN u.kind='VIDEO' THEN 'VIDEO' ELSE 'IMAGE' END) AS correct_type,
    bool_and(f.sha256 IS NOT NULL) AS has_digest,
    json_agg(json_build_array(f.id::text,f.sha256) ORDER BY i.position)
      FILTER (WHERE i.position IS NOT NULL) AS members
  FROM sources u LEFT JOIN draft_creative_unit_item i
    ON i.tenant_id=:tenant AND i.draft_id=:draft AND i.generation=:generation AND i.unit_id=u.id
  LEFT JOIN LATERAL (
    SELECT f.id,f.sha256,f.media_type FROM material_file f
    WHERE f.tenant_id=:tenant AND f.id=i.material_id OFFSET 0
  ) f ON true
  GROUP BY u.id
)

"""

CANDIDATE_CTE = """
WITH current_dramas AS (
  SELECT DISTINCT drama_id FROM draft_input WHERE tenant_id=:tenant AND draft_id=:draft AND kind='drama' AND status='ready'
), candidates AS (
  SELECT c.*, coalesce(e.excluded,false) AS excluded, coalesce(e.cross_drama_allowed,false) AS cross_allowed
  FROM image_candidate_item c JOIN current_dramas d ON d.drama_id=c.drama_id
  LEFT JOIN image_candidate_exclusion e ON e.tenant_id=c.tenant_id AND e.draft_id=c.draft_id AND e.drama_id=c.drama_id AND e.material_id=c.material_id
  WHERE c.tenant_id=:tenant AND c.draft_id=:draft AND c.snapshot_id=:snapshot_id
), members AS (
  SELECT i.* FROM draft_creative_unit_item i JOIN current_dramas d ON d.drama_id=i.drama_id
  WHERE i.tenant_id=:tenant AND i.draft_id=:draft AND i.generation=:generation
), remainders AS (
  SELECT drama->>'drama_id' AS drama_id, excluded->>'material_id' AS material_id
  FROM image_composition_run r JOIN image_composition_plan p ON p.id=r.plan_id AND p.tenant_id=r.tenant_id,
    LATERAL jsonb_array_elements(p.summary->'dramas') AS drama,
    LATERAL jsonb_array_elements(drama->'excluded_images') AS excluded
  WHERE r.tenant_id=:tenant AND r.draft_id=:draft AND r.generation=:generation AND r.status='APPLIED' AND excluded->>'reason'='REMAINDER'
)
"""


def snapshot_creative_units(session, *, preview, draft):
    """固定数量 SQL 拍下全批快照；草稿锁串行化音乐，原件共享锁固定字节事实。"""
    values = _snapshot_parameters(
        draft, preview=preview.id, music_fields=list(PublicMusicSnapshot.model_fields)
    )
    # 只返回 count；不把文件或单元全集拉到 Python，也不逐成员 flush。
    session.execute(
        text(
            SOURCE_CTE
            + """
      , lock_members AS MATERIALIZED (
        SELECT DISTINCT i.material_id FROM draft_creative_unit_item i JOIN sources u
          ON u.id=i.unit_id AND u.tenant_id=i.tenant_id AND u.draft_id=i.draft_id AND u.generation=i.generation
        WHERE i.tenant_id=:tenant AND i.draft_id=:draft AND i.generation=:generation
      )
      SELECT count(*) FROM lock_members m CROSS JOIN LATERAL (
        SELECT f.id FROM material_file f WHERE f.tenant_id=:tenant AND f.id=m.material_id
        OFFSET 0 FOR SHARE OF f
      ) locked
    """
        ),
        values,
    ).scalar_one()
    invalid = session.execute(
        text(
            SOURCE_CTE
            + """
      SELECT CASE
        WHEN :kind='CAROUSEL' AND NOT EXISTS (SELECT 1 FROM sources) THEN 'empty_creative_mapping'
        WHEN EXISTS (SELECT 1 FROM sources u JOIN content c ON c.id=u.id WHERE u.kind<>:kind OR NOT c.correct_type OR c.count NOT BETWEEN 1 AND 35 OR (u.kind='VIDEO' AND c.count<>1) OR c.first<>1 OR c.last<>c.count) THEN 'invalid_creative_unit'
        WHEN EXISTS (SELECT 1 FROM sources WHERE kind='CAROUSEL' AND (music_status<>'READY' OR music_id IS NULL OR length(trim(music_id))=0)) THEN 'music_not_ready'
        WHEN EXISTS (SELECT 1 FROM sources u JOIN content c ON c.id=u.id WHERE u.kind='CAROUSEL' AND NOT c.has_digest) THEN 'material_digest_missing'
        WHEN EXISTS (SELECT 1 FROM sources u JOIN content c ON c.id=u.id WHERE u.kind='CAROUSEL' AND u.content_digest<>encode(sha256(convert_to(replace(c.members::text, ', ', ','),'UTF8')),'hex')) THEN 'image_content_changed'
        ELSE NULL END
    """
        ),
        values,
    ).scalar_one()
    if invalid:
        raise DomainError(invalid, "创意成员、图片内容或配乐尚未准备完成")
    session.execute(
        text(
            SOURCE_CTE
            + """
      INSERT INTO preview_creative_unit
      (tenant_id,preview_id,bc_id,id,drama_id,kind,position,generation,content_revision,music_revision,content_digest,music_id,music_snapshot)
      SELECT u.tenant_id,:preview,u.bc_id,u.id,u.drama_id,u.kind,u.position,u.generation,u.revision,u.music_revision,
        encode(sha256(convert_to(json_build_array(2,u.kind,c.members,u.music_id)::text,'UTF8')),'hex'),u.music_id,coalesce((SELECT jsonb_object_agg(k,v) FROM jsonb_each(u.music_snapshot) AS fact(k,v) WHERE k=ANY(CAST(:music_fields AS text[]))), '{}'::jsonb)
      FROM sources u JOIN content c ON c.id=u.id
    """
        ),
        values,
    )
    session.execute(
        text("""
      INSERT INTO preview_creative_unit_item
      (tenant_id,preview_id,bc_id,creative_unit_id,position,drama_id,material_id,content_digest,file_name)
      SELECT i.tenant_id,:preview,i.bc_id,i.unit_id,i.position,i.drama_id,i.material_id,f.sha256,f.file_name
      FROM draft_creative_unit_item i
      JOIN preview_creative_unit u ON u.tenant_id=i.tenant_id AND u.preview_id=:preview AND u.id=i.unit_id AND u.generation=i.generation
      JOIN material_file f ON f.tenant_id=i.tenant_id AND f.id=i.material_id
      WHERE i.tenant_id=:tenant AND i.draft_id=:draft AND i.generation=:generation
    """),
        values,
    )
    if draft.media_kind == "CAROUSEL":
        from .image_composition_state import ImageCompositionState

        state = session.get(ImageCompositionState, (draft.tenant_id, draft.id))
        session.execute(
            text(
                CANDIDATE_CTE
                + """
          INSERT INTO preview_image_exclusion (tenant_id,preview_id,bc_id,drama_id,material_id,file_name,reason)
          SELECT tenant_id,:preview,bc_id,drama_id,material_id,candidate->>'file_name_snapshot',
            CASE WHEN excluded THEN 'DISABLED' ELSE 'REMAINDER' END
          FROM candidates c WHERE NOT EXISTS (SELECT 1 FROM members m WHERE m.drama_id=c.drama_id AND m.material_id=c.material_id)
        """
            ),
            values | {"snapshot_id": state.snapshot_id},
        )
    # 每行先摘要再按显式顺序聚合，SQL 返回固定大小摘要；不聚合整批 JSON 正文。
    hashes = {}
    for table, order in (
        ("preview_creative_unit", "drama_id,position,id"),
        ("preview_creative_unit_item", "creative_unit_id,position"),
        ("preview_image_exclusion", "drama_id,material_id"),
    ):
        hashes[table] = session.execute(
            text(f"""
          SELECT encode(sha256(convert_to(coalesce(string_agg(encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex'),'' ORDER BY {order}),''),'UTF8')),'hex')
          FROM {table} r WHERE tenant_id=:tenant AND preview_id=:preview
        """),
            values,
        ).scalar_one()
    return hashes


def frozen_ad_creative_units(
    session, *, tenant_id, preview_id, drama_id, group_no, base_ad_no
):
    contract = preview_contract(session, tenant_id=tenant_id, preview_id=preview_id)
    if contract.version != 2:
        raise DomainError("preview_contract_expired", "历史预览只支持原合同读取")
    rows = session.exec(
        select(PreviewCreativeUnit)
        .join(
            PreviewAdCreativeUnit,
            (col(PreviewAdCreativeUnit.tenant_id) == PreviewCreativeUnit.tenant_id)
            & (col(PreviewAdCreativeUnit.preview_id) == PreviewCreativeUnit.preview_id)
            & (col(PreviewAdCreativeUnit.creative_unit_id) == PreviewCreativeUnit.id),
        )
        .where(
            PreviewAdCreativeUnit.tenant_id == tenant_id,
            PreviewAdCreativeUnit.preview_id == preview_id,
            PreviewAdCreativeUnit.drama_id == drama_id,
            PreviewAdCreativeUnit.group_no == group_no,
            PreviewAdCreativeUnit.base_ad_no == base_ad_no,
        )
        .order_by(col(PreviewAdCreativeUnit.position))
    ).all()
    if not rows:
        raise DomainError("empty_creative_mapping", "冻结广告创意映射为空")
    all_members = session.exec(
        select(PreviewCreativeUnitItem)
        .where(
            PreviewCreativeUnitItem.tenant_id == tenant_id,
            PreviewCreativeUnitItem.preview_id == preview_id,
            col(PreviewCreativeUnitItem.creative_unit_id).in_([row.id for row in rows]),
        )
        .order_by(
            col(PreviewCreativeUnitItem.creative_unit_id),
            col(PreviewCreativeUnitItem.position),
        )
    ).all()
    by_unit = {}
    for member in all_members:
        by_unit.setdefault(member.creative_unit_id, []).append(member)
    result = []
    for row in rows:
        members = by_unit.get(row.id, [])
        if (
            not 1 <= len(members) <= 35
            or (row.kind == "VIDEO" and len(members) != 1)
            or [i.position for i in members] != list(range(1, len(members) + 1))
        ):
            raise DomainError("empty_creative_mapping", "冻结创意成员不完整")
        if (
            row.kind != contract.media_kind
            or row.content_digest
            != creative_content_digest(
                row.kind,
                [(i.material_id, i.content_digest) for i in members],
                row.music_id,
            )
        ):
            raise DomainError("creative_digest_mismatch", "冻结创意内容摘要不符")
        result.append(
            FrozenCreativeUnit(
                unit_id=row.id,
                kind=row.kind,
                generation=row.generation,
                content_revision=row.content_revision,
                music_revision=row.music_revision,
                content_digest=row.content_digest,
                music_id=row.music_id,
                music_snapshot=PublicMusicSnapshot.model_validate(row.music_snapshot),
                members=tuple(
                    FrozenCreativeMember(
                        material_id=i.material_id,
                        content_digest=i.content_digest,
                        file_name=i.file_name,
                    )
                    for i in members
                ),
            )
        )
    return tuple(result)
