"""任务级只读知识服务：按冻结截止检索，正文按需读取并归档，禁止越租户。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import exists, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.ai_workbench.knowledge import KnowledgeVersion, _scope_clause


def _query(task):
    context = TenantContext(tenant_id=task.tenant_id, actor_id=task.created_by, role="")
    cutoff = datetime.fromisoformat(task.evidence["captured_at"])
    newer = aliased(KnowledgeVersion)
    # 截止后发布的版本不替换旧任务的政策；同资产在截止前的退休版本会阻止旧版复活。
    return select(KnowledgeVersion).where(
        _scope_clause(context, task.evidence["optimizer"]["bc_id"]),
        KnowledgeVersion.created_at <= cutoff,
        KnowledgeVersion.status == "CONFIRMED",
        ~exists(
            select(newer.id).where(
                newer.asset_id == KnowledgeVersion.asset_id,
                newer.version > KnowledgeVersion.version,
                newer.created_at <= cutoff,
            )
        ),
    )


def _summary(row, cutoff):
    return {
        "id": str(row.id),
        "asset_id": str(row.asset_id),
        "version": row.version,
        "title": row.title,
        "scope": row.scope,
        "source": row.source,
        "applicability": row.applicability,
        "tags": row.tags,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "conflict_key": row.conflict_key,
        "eligibility": "EXPIRED"
        if row.expires_at and row.expires_at <= cutoff
        else "VERIFIED"
        if row.source_verified
        else "UNVERIFIED",
    }


def search_for_task(session: Session, *, task, query: str, offset: int = 0) -> dict:
    words = query.split()[:8]
    statement = _query(task)
    if words:
        statement = statement.where(
            or_(
                *(
                    column.icontains(word, autoescape=True)
                    for word in words
                    for column in (
                        KnowledgeVersion.title,
                        KnowledgeVersion.body,
                        KnowledgeVersion.applicability,
                    )
                )
            )
        )
    rows = session.exec(
        statement.order_by(col(KnowledgeVersion.created_at).desc(), KnowledgeVersion.id)
        .offset(offset)
        .limit(11)
    ).all()
    cutoff = datetime.fromisoformat(task.evidence["captured_at"])
    return {
        "items": [_summary(r, cutoff) for r in rows[:10]],
        "next_offset": offset + 10 if len(rows) > 10 else None,
        "as_of": cutoff.isoformat(),
        "policy": "keyword matching, latest confirmed scoped versions at cutoff; inspect applicability, expiry and conflicting sources before relying on claims",
    }


def read_for_task(session: Session, *, task, knowledge_id: UUID) -> dict:
    from fastapi import HTTPException

    from app.modules.ai_workbench.review_store import (
        ReviewArtifact,
        archive_artifact,
        scoped,
    )

    cached = session.exec(
        scoped(ReviewArtifact, task).where(
            ReviewArtifact.id == knowledge_id, ReviewArtifact.kind == "knowledge"
        )
    ).first()
    if cached:
        return cached.payload
    row = session.exec(
        _query(task).where(KnowledgeVersion.id == knowledge_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "knowledge_not_available_in_task_scope")
    cutoff = datetime.fromisoformat(task.evidence["captured_at"])
    conflict_count = (
        session.exec(
            _query(task)
            .where(KnowledgeVersion.conflict_key == row.conflict_key)
            .limit(2)
        ).all()
        if row.conflict_key
        else []
    )
    frozen = {
        **_summary(row, cutoff),
        "body": row.body,
        "sha256": sha256(row.body.encode()).hexdigest(),
        "conflict": len(conflict_count) > 1,
        "evidence_ids": row.evidence_ids,
    }
    return archive_artifact(
        session,
        task=task,
        kind="knowledge",
        key=str(knowledge_id),
        payload=frozen,
        identity=knowledge_id,
    )
