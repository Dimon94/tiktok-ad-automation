"""任务范围目录与冻结正文分页；成功返回后才持久授予引用资格。"""

import json
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.ai_workbench.review_store import (
    ReviewSource,
    archive_artifact,
    deliver,
    scoped,
)
from app.modules.ai_workbench.reviews import _digest

PAGE_SIZE = 10
MAX_RESPONSE_BYTES = 64000
CATEGORIES = (
    "plans",
    "build_occurrences",
    "rule_evidence",
    "attribution",
    "material_orders",
    "history",
    "business",
    "operation_logs",
    "knowledge_refs",
)


def query_for_task(
    session,
    *,
    task,
    category: str,
    offset: int = 0,
    advertiser_id=None,
    dimension=None,
    maturity_horizon=None,
):
    if offset < 0:
        raise HTTPException(422, "invalid_evidence_page")
    query = scoped(ReviewSource, task).where(ReviewSource.category == category)
    for name, value in (
        ("advertiser_id", advertiser_id),
        ("dimension", dimension),
        ("maturity_horizon", maturity_horizon),
    ):
        if value is not None:
            query = query.where(getattr(ReviewSource, name) == value)
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    rows = session.exec(
        query.order_by(
            ReviewSource.advertiser_id,
            ReviewSource.dimension,
            ReviewSource.maturity_horizon,
            ReviewSource.id,
        )
        .offset(offset)
        .limit(PAGE_SIZE)
    ).all()
    return {
        "items": [
            {
                "id": str(row.id),
                "category": row.category,
                "sha256": row.digest,
                "captured_at": row.captured_at.isoformat(),
                **row.source_metadata,
            }
            for row in rows
        ],
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "total": total,
        "cutoff": task.evidence["captured_at"],
    }


def read_for_task(session, *, task, evidence_id: UUID, offset: int = 0, limit: int = 1):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "invalid_evidence_page")
    source = session.exec(
        scoped(ReviewSource, task).where(ReviewSource.id == evidence_id)
    ).one_or_none()
    if source is None:
        raise HTTPException(404, "evidence_not_available_in_task_scope")
    if _digest(source.payload) != source.digest:
        raise DomainError("ai_evidence_changed", "冻结证据摘要不一致")
    payload = source.payload
    entry = source.source_metadata
    if isinstance(payload, list):
        rows, metadata = payload, {"truncated": entry.get("truncated", False)}
    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = payload["rows"]
        metadata = {k: v for k, v in payload.items() if k != "rows"}
        if source.category == "attribution":
            # 一条归因可含多个窗口/大量报表行，拆成精确身份+单窗口单行供分页。
            rows = [
                {
                    **mapping,
                    "reports": [{**report, "rows": [row] if row is not None else []}],
                }
                for mapping in rows
                for report in mapping.get("reports", [])
                for row in (report.get("rows") or [None])
            ]
    elif isinstance(payload, dict):
        # 历史/发布索引按单条记录分页，不能一次返回整个历史列表。
        groups = {k: v for k, v in payload.items() if isinstance(v, list)}
        rows = [
            {"section": name, "value": row}
            for name, values in groups.items()
            for row in values
        ]
        metadata = {k: v for k, v in payload.items() if k not in groups}
        if not groups:
            rows, metadata = [payload], {}
    else:
        rows, metadata = [payload], {}
    if offset and offset >= len(rows):
        raise HTTPException(422, "invalid_evidence_page")
    selected = rows[offset : offset + limit]
    # 只缩小页长，不截断单条正文；超大单条给出可操作错误，绝不把残缺对象当完整证据。
    while True:
        page_id = uuid5(
            task.id, f"{source.id}:{source.digest}:{offset}:{len(selected)}"
        )
        result = {
            "id": str(page_id),
            "source_id": str(source.id),
            "category": source.category,
            "cutoff": task.evidence["captured_at"],
            "captured_at": source.captured_at.isoformat(),
            "metadata": metadata,
            "rows": selected,
            "next_offset": offset + len(selected)
            if offset + len(selected) < len(rows)
            else None,
            "frozen_rows": len(rows),
        }
        if len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_RESPONSE_BYTES:
            break
        if len(selected) <= 1:
            raise DomainError(
                "ai_evidence_object_too_large",
                "单条证据超过 64KB；请用 evidence/analyze 聚合报表或读取其他类别。该对象未交付，不可引用",
            )
        selected = selected[: max(1, len(selected) // 2)]
    extra_ids = (
        [row["id"] for row in selected if "id" in row]
        if source.category == "rule_evidence"
        else []
    )
    return deliver(
        session,
        task=task,
        identity=page_id,
        source_id=source.id,
        digest=source.digest,
        payload=result,
        extra_ids=extra_ids,
    )


def write_note(session, *, task, key: str, body: str):
    if not key or len(key) > 100 or len(body) > 16000:
        raise HTTPException(422, "invalid_note")
    from app.modules.ai_workbench.review_store import ReviewArtifact

    previous = session.exec(
        scoped(ReviewArtifact, task)
        .where(ReviewArtifact.kind == "note", ReviewArtifact.key == key)
        .order_by(ReviewArtifact.created_at.desc(), ReviewArtifact.id.desc())
        .limit(1)
    ).first()
    if previous and previous.payload["body"] == body:
        return previous.payload
    return archive_artifact(
        session,
        task=task,
        kind="note",
        key=key,
        identity=uuid4(),
        payload={
            "key": key,
            "body": body,
            "verified_evidence": False,
            "authority": "untrusted_agent_scratch_note",
        },
        citable=False,
    )


def list_notes(session, *, task, offset=0, limit=10):
    from app.modules.ai_workbench.review_store import ReviewArtifact

    # 仅取每个 key 的最新版本。历史版本保留审计，不重新注入重复草稿。
    statement = (
        scoped(ReviewArtifact, task)
        .where(ReviewArtifact.kind == "note")
        .distinct(ReviewArtifact.key)
        .order_by(
            ReviewArtifact.key,
            ReviewArtifact.created_at.desc(),
            ReviewArtifact.id.desc(),
        )
    )
    rows = session.exec(statement.offset(offset).limit(limit + 1)).all()
    selected = rows[:limit]
    while (
        len(json.dumps([row.payload for row in selected], ensure_ascii=False).encode())
        > MAX_RESPONSE_BYTES
        and len(selected) > 1
    ):
        selected = selected[: len(selected) // 2]
    return {
        "items": [row.payload for row in selected],
        "next_offset": offset + len(selected) if len(rows) > len(selected) else None,
        "verified_evidence": False,
    }
