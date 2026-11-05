"""搭建输入中的素材匹配依据；补充标识不依赖推广链接。"""

from sqlmodel import Session, select

from .models import BuildDraft, DraftDrama, DraftInput


def material_match_text(session: Session, draft: BuildDraft, drama: DraftDrama) -> str:
    details = session.exec(
        select(DraftInput.manual_link).where(
            DraftInput.tenant_id == draft.tenant_id,
            DraftInput.draft_id == draft.id,
            DraftInput.kind == "drama",
            DraftInput.line_no == drama.first_line,
        )
    ).first()
    # 非空标识完全替代剧名；匹配零结果也不改用剧名扩大范围。
    return (details or {}).get("material_identifier", "").strip() or drama.title
