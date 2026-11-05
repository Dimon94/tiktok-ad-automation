"""分页逐剧汇总；候选、引用、单元分别聚合，避免多对多连接放大计数。"""

from sqlalchemy import text

from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.resolver import decode_cursor, encode_cursor

from .image_composition_service import _draft
from .image_composition_state import ImageCompositionState

SUMMARY_SQL = """
WITH dramas AS (
 SELECT d.* FROM draft_drama d WHERE d.tenant_id=:tenant AND d.draft_id=:draft
 AND EXISTS (SELECT 1 FROM draft_input i WHERE i.tenant_id=d.tenant_id AND i.draft_id=d.draft_id AND i.drama_id=d.drama_id AND i.kind='drama')
), candidates AS (
 SELECT c.drama_id, count(*) candidate_count,
 count(*) FILTER (WHERE e.excluded IS TRUE) excluded_count,
 count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM draft_creative_unit_item i
   WHERE i.tenant_id=c.tenant_id AND i.draft_id=c.draft_id AND i.generation=:generation
   AND i.drama_id=c.drama_id AND i.material_id=c.material_id)) unused_count
 FROM image_candidate_item c LEFT JOIN image_candidate_exclusion e
 ON e.tenant_id=c.tenant_id AND e.draft_id=c.draft_id AND e.drama_id=c.drama_id AND e.material_id=c.material_id
 WHERE c.tenant_id=:tenant AND c.draft_id=:draft AND c.snapshot_id=:snapshot GROUP BY c.drama_id
), units AS (
 SELECT drama_id,count(*) unit_count,
 count(*) FILTER (WHERE music_status='READY') ready_count,
 count(*) FILTER (WHERE music_status='NEEDS_SELECTION') selection_count,
 count(*) FILTER (WHERE music_status IN ('PENDING','RUNNING')) pending_count,
 count(*) FILTER (WHERE music_status='FAILED') failed_count,
 count(*) FILTER (WHERE composition_protected OR music_protected) protected_count
 FROM draft_creative_unit WHERE tenant_id=:tenant AND draft_id=:draft AND generation=:generation GROUP BY drama_id
), images AS (
 SELECT drama_id,count(*) reference_count,count(DISTINCT material_id) used_image_count
 FROM draft_creative_unit_item WHERE tenant_id=:tenant AND draft_id=:draft AND generation=:generation GROUP BY drama_id
), summaries AS (
 SELECT d.drama_id,d.link_id,d.title,d.first_line,d.material_state,d.matched_count,
 coalesce((SELECT i.manual_link->>'material_identifier' FROM draft_input i
  WHERE i.tenant_id=d.tenant_id AND i.draft_id=d.draft_id AND i.kind='drama'
  AND i.line_no=d.first_line),'') material_identifier,
 coalesce(c.candidate_count,0) candidate_count,coalesce(c.excluded_count,0) excluded_count,coalesce(c.unused_count,0) unused_count,
 coalesce(u.unit_count,0) unit_count,coalesce(u.ready_count,0) ready_count,coalesce(u.selection_count,0) selection_count,
 coalesce(u.pending_count,0) pending_count,coalesce(u.failed_count,0) failed_count,coalesce(u.protected_count,0) protected_count,
 coalesce(i.reference_count,0) reference_count,coalesce(i.used_image_count,0) used_image_count
 FROM dramas d LEFT JOIN candidates c USING(drama_id) LEFT JOIN units u USING(drama_id) LEFT JOIN images i USING(drama_id)
)
"""


def drama_summaries(session, scope, *, issue="all", cursor=None, limit=50):
    draft = _draft(session, scope, verify=False)
    state = session.get(ImageCompositionState, (scope.tenant_id, scope.draft_id))
    filters = {
        "all": "TRUE",
        "music": "selection_count+pending_count+failed_count>0",
        "unused": "unused_count>0",
        "protected": "protected_count>0",
    }
    if issue not in filters or type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("image_edit_invalid", "图片摘要筛选无效")
    owner = {
        "tenant": str(scope.tenant_id),
        "draft": str(scope.draft_id),
        "bc": scope.bc_id,
        "kind": f"image-dramas:{draft.revision}:{draft.active_image_generation}:{issue}",
    }
    after = decode_cursor(cursor, scope=owner)
    try:
        offset = int(after or 0)
        if offset < 0:
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    args = {
        "tenant": scope.tenant_id,
        "draft": scope.draft_id,
        "snapshot": state.snapshot_id if state else None,
        "generation": draft.active_image_generation,
        "offset": offset,
        "limit": limit + 1,
    }
    where = " WHERE " + filters[issue]
    total = session.execute(
        text(SUMMARY_SQL + "SELECT count(*) FROM summaries" + where), args
    ).scalar_one()
    rows = (
        session.execute(
            text(
                SUMMARY_SQL
                + "SELECT * FROM summaries"
                + where
                + " ORDER BY first_line,drama_id OFFSET :offset LIMIT :limit"
            ),
            args,
        )
        .mappings()
        .all()
    )
    return Page(
        items=[dict(r) for r in rows[:limit]],
        total=total,
        next_cursor=encode_cursor(scope=owner, last_id=str(offset + limit))
        if len(rows) > limit
        else None,
    )
