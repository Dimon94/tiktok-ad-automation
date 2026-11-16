"""任务独占的不可变冻结来源、已交付凭证与分析产物；不占模型初始输入。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel, select

from app.core.errors import DomainError


def _scope_fk(name):
    return ForeignKeyConstraint(
        ["tenant_id", "optimizer_id", "task_id"],
        [
            "ai_dsh_review_task.tenant_id",
            "ai_dsh_review_task.optimizer_id",
            "ai_dsh_review_task.id",
        ],
        name=name,
        ondelete="CASCADE",
    )


class ReviewSource(SQLModel, table=True):
    __tablename__ = "ai_review_source"
    __table_args__ = (
        _scope_fk("fk_ai_review_source_task"),
        Index(
            "ix_ai_review_source_lookup",
            "task_id",
            "category",
            "advertiser_id",
            "dimension",
            "maturity_horizon",
        ),
    )
    task_id: UUID = Field(primary_key=True)
    id: UUID = Field(primary_key=True)
    tenant_id: UUID
    optimizer_id: UUID
    category: str = Field(max_length=40)
    advertiser_id: str | None = Field(default=None, max_length=100)
    dimension: str | None = Field(default=None, max_length=20)
    maturity_horizon: str | None = Field(default=None, max_length=5)
    digest: str = Field(max_length=64)
    payload: Any = Field(sa_column=Column(JSONB, nullable=False))
    source_metadata: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    captured_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class ReviewReceipt(SQLModel, table=True):
    __tablename__ = "ai_review_receipt"
    __table_args__ = (_scope_fk("fk_ai_review_receipt_task"),)
    task_id: UUID = Field(primary_key=True)
    id: UUID = Field(primary_key=True)
    tenant_id: UUID
    optimizer_id: UUID
    source_id: UUID
    source_digest: str = Field(max_length=64)
    payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    citable_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
        ),
    )


class ReviewArtifact(SQLModel, table=True):
    __tablename__ = "ai_review_artifact"
    __table_args__ = (
        _scope_fk("fk_ai_review_artifact_task"),
        Index("ix_ai_review_artifact_lookup", "task_id", "kind", "key", "created_at"),
    )
    task_id: UUID = Field(primary_key=True)
    id: UUID = Field(primary_key=True)
    tenant_id: UUID
    optimizer_id: UUID
    kind: str = Field(max_length=30)
    key: str = Field(max_length=100)
    digest: str = Field(max_length=64)
    payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    # 草稿笔记永不获得证据资格；其他产物只在成功交付后登记凭证。
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
        ),
    )


def scoped(model, task):
    return select(model).where(
        model.task_id == task.id,
        model.tenant_id == task.tenant_id,
        model.optimizer_id == task.optimizer_id,
    )


def archive_source(
    session, *, task, category, payload, captured_at, metadata=None, identity=None
):
    from app.modules.ai_workbench.reviews import _digest

    digest = _digest(payload)
    aid = payload.get("advertiser_id") if isinstance(payload, dict) else None
    identity = identity or f"{category}:{aid}:{digest}"
    source_id = uuid5(task.id, str(identity))
    previous = session.get(ReviewSource, (task.id, source_id))
    if previous:
        if previous.digest != digest:
            raise DomainError("ai_evidence_changed", "冻结证据不可覆盖")
        return previous
    meta = dict(metadata or {})
    if isinstance(payload, dict):
        meta.update(
            {
                k: payload[k]
                for k in (
                    "advertiser_id",
                    "dimension",
                    "maturity_horizon",
                    "status",
                    "window",
                    "rows_truncated",
                    "total",
                    "metrics_observed_at",
                )
                if k in payload
            }
        )
    source = ReviewSource(
        task_id=task.id,
        id=source_id,
        tenant_id=task.tenant_id,
        optimizer_id=task.optimizer_id,
        category=category,
        advertiser_id=aid,
        dimension=meta.get("dimension"),
        maturity_horizon=meta.get("maturity_horizon"),
        digest=digest,
        payload=payload,
        source_metadata=meta,
        captured_at=captured_at,
    )
    session.add(source)
    session.flush()
    return source


def deliver(session, *, task, identity, source_id, digest, payload, extra_ids=()):
    previous = session.get(ReviewReceipt, (task.id, identity))
    if previous:
        return previous.payload
    receipt = ReviewReceipt(
        task_id=task.id,
        id=identity,
        tenant_id=task.tenant_id,
        optimizer_id=task.optimizer_id,
        source_id=source_id,
        source_digest=digest,
        payload=payload,
        citable_ids=[str(identity), *map(str, extra_ids)],
    )
    session.add(receipt)
    session.flush()
    return payload


def archive_artifact(session, *, task, kind, key, payload, identity=None, citable=True):
    from app.modules.ai_workbench.reviews import _digest

    digest = _digest(payload)
    identity = identity or uuid5(task.id, f"{kind}:{key}:{digest}")
    previous = session.get(ReviewArtifact, (task.id, identity))
    if previous:
        if previous.digest != digest:
            raise DomainError("ai_evidence_changed", "分析产物不可覆盖")
        result = previous.payload
    else:
        result = {**payload, "id": str(identity)}
        session.add(
            ReviewArtifact(
                task_id=task.id,
                id=identity,
                tenant_id=task.tenant_id,
                optimizer_id=task.optimizer_id,
                kind=kind,
                key=key,
                digest=digest,
                payload=result,
            )
        )
        session.flush()
    if citable:
        deliver(
            session,
            task=task,
            identity=identity,
            source_id=identity,
            digest=digest,
            payload=result,
        )
    return result


def delivered_evidence_ids(session, task):
    from app.modules.ai_workbench.reviews import frozen_evidence_ids

    if task.evidence.get("reading_contract") != "on-demand-v2":
        return frozen_evidence_ids(task.evidence)
    rows = session.exec(
        select(ReviewReceipt.citable_ids).where(
            ReviewReceipt.task_id == task.id,
            ReviewReceipt.tenant_id == task.tenant_id,
            ReviewReceipt.optimizer_id == task.optimizer_id,
        )
    ).all()
    return {value for row in rows for value in row}


def source_payloads(session, task, category):
    if task.evidence.get("reading_contract") != "on-demand-v2":
        yield from (
            s["payload"]
            for s in task.evidence_store.values()
            if s["category"] == category
        )
        return
    for row in session.exec(
        scoped(ReviewSource, task)
        .where(ReviewSource.category == category)
        .execution_options(yield_per=1)
    ):
        yield row.payload


def source_catalog(session, task):
    """只提供类别计数，提醒 Agent 空的摘要不等于账户来源不存在。"""
    from sqlalchemy import text

    rows = session.execute(
        text("""SELECT category, count(*) AS source_count,
            count(DISTINCT advertiser_id) AS account_count,
            CASE WHEN bool_and(COALESCE(jsonb_typeof(payload->'rows')='array', false)
                OR jsonb_typeof(payload)='array') THEN
            sum(CASE WHEN jsonb_typeof(payload->'rows')='array'
                THEN jsonb_array_length(payload->'rows')
                WHEN jsonb_typeof(payload)='array' THEN jsonb_array_length(payload)
                ELSE 0 END) ELSE NULL END AS row_count
            FROM ai_review_source WHERE task_id=:task AND tenant_id=:tenant
                AND optimizer_id=:optimizer GROUP BY category ORDER BY category"""),
        {"task": task.id, "tenant": task.tenant_id, "optimizer": task.optimizer_id},
    ).mappings()
    # runs/publications、reviews/proposals 等字典结构不冒充空列表。
    return [
        {
            **dict(row),
            "row_count_status": "KNOWN"
            if row["row_count"] is not None
            else "UNKNOWN_READ_SOURCE",
        }
        for row in rows
    ]
