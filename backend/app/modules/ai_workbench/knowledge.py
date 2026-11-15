"""版本化公共方法/私有经验库；公共提炼必须由平台管理员明确共享审核。"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from typing import cast as typing_cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import Field as PydanticField
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    UniqueConstraint,
    exists,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import aliased
from sqlmodel import Field, Session, SQLModel, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant


class KnowledgeVersion(SQLModel, table=True):
    __tablename__ = "ai_knowledge_version"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_ai_knowledge_version"),
        CheckConstraint(
            "scope IN ('PUBLIC','TENANT','BC')", name="ck_ai_knowledge_scope"
        ),
        CheckConstraint(
            "(scope = 'PUBLIC' AND tenant_id IS NULL AND bc_id IS NULL) OR (scope = 'TENANT' AND tenant_id IS NOT NULL AND bc_id IS NULL) OR (scope = 'BC' AND tenant_id IS NOT NULL AND bc_id IS NOT NULL)",
            name="ck_ai_knowledge_owner",
        ),
        CheckConstraint(
            "status IN ('CONFIRMED','CANDIDATE','RETIRED')",
            name="ck_ai_knowledge_status",
        ),
        Index("ix_ai_knowledge_scope", "tenant_id", "bc_id", "scope", "created_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    asset_id: UUID = Field(default_factory=uuid4)
    version: int = 1
    key: str = Field(max_length=100)
    scope: str = Field(max_length=16)
    tenant_id: UUID | None = Field(default=None, foreign_key="tenant.id")
    bc_id: str | None = Field(default=None, max_length=128)
    title: str = Field(max_length=200)
    body: str
    source: str = Field(max_length=500)
    source_verified: bool = False
    applicability: str = ""
    tags: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    evidence_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    conflict_key: str | None = Field(default=None, max_length=100)
    status: str = Field(default="CONFIRMED", max_length=16)
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), index=False, unique=False, nullable=True
        ),
    )
    sharing_review: str | None = None
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=lambda: datetime.now(UTC),
        ),
    )


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = PydanticField(min_length=1, max_length=100)
    scope: Literal["PUBLIC", "TENANT", "BC"] = "TENANT"
    bc_id: str | None = PydanticField(default=None, max_length=128)
    title: str = PydanticField(min_length=1, max_length=200)
    body: str = PydanticField(min_length=1, max_length=12000)
    source: str = PydanticField(min_length=1, max_length=500)
    source_verified: bool = False
    applicability: str = PydanticField(default="", max_length=2000)
    tags: list[
        Literal[
            "builds", "rules", "materials", "business", "scale", "policy", "experience"
        ]
    ] = PydanticField(default_factory=list, max_length=7)
    evidence_ids: list[UUID] = PydanticField(default_factory=list, max_length=30)
    conflict_key: str | None = PydanticField(default=None, max_length=100)
    status: Literal["CONFIRMED", "CANDIDATE", "RETIRED"] = "CONFIRMED"
    expires_at: datetime | None = None
    sharing_review: str | None = PydanticField(default=None, max_length=2000)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("expiry must include timezone")
        return value


def _scope_clause(context, bc_id=None):
    return or_(
        col(KnowledgeVersion.scope) == "PUBLIC",
        (col(KnowledgeVersion.tenant_id) == context.tenant_id)
        & (
            (col(KnowledgeVersion.scope) == "TENANT")
            | (
                (col(KnowledgeVersion.scope) == "BC")
                & (col(KnowledgeVersion.bc_id) == bc_id)
            )
        ),
    )


def latest_query():
    newer = aliased(KnowledgeVersion)
    return select(KnowledgeVersion).where(
        ~exists(
            select(newer.id).where(
                newer.asset_id == col(KnowledgeVersion.asset_id),
                newer.version > col(KnowledgeVersion.version),
            )
        )
    )


def create_version(
    session: Session,
    *,
    context: TenantContext,
    body: KnowledgeInput,
    asset_id: UUID | None = None,
) -> KnowledgeVersion:
    authority = require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if body.scope == "PUBLIC" and authority.role != "platform_admin":
        raise DomainError("action_forbidden", "公共知识需平台管理员审核")
    if (body.scope == "BC") != bool(body.bc_id):
        raise DomainError("ai_knowledge_scope", "BC 范围必须明确且仅 BC 范围允许 BC")
    if body.bc_id:
        from app.modules.accounts.models import TenantBC

        if session.get(TenantBC, (context.tenant_id, body.bc_id)) is None:
            raise DomainError("ai_knowledge_scope", "BC 不属于当前租户")
    tenant_id = None if body.scope == "PUBLIC" else context.tenant_id
    number = 1
    if asset_id is None:
        # 初次导入按逻辑 key 串行并幂等；重复运行不能产生第二个公共知识资产。
        typing_cast(SQLAlchemySession, session).execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {
                "key": f"ai-knowledge-key:{body.scope}:{tenant_id}:{body.bc_id}:{body.key}"
            },
        )
        existing = session.exec(
            latest_query().where(
                col(KnowledgeVersion.scope) == body.scope,
                col(KnowledgeVersion.tenant_id) == tenant_id,
                col(KnowledgeVersion.bc_id) == body.bc_id,
                col(KnowledgeVersion.key) == body.key,
            )
        ).all()
        if existing:
            supplied = body.model_dump(mode="json")
            stored = {key: existing[0].model_dump(mode="json")[key] for key in supplied}
            if len(existing) == 1 and stored == supplied:
                return existing[0]
            raise DomainError(
                "ai_knowledge_key_exists", "已有同标识知识，请显式追加版本"
            )
    if asset_id:
        # 同一资产版本发布串行，避免并发覆盖；不允许借更新移动私有范围到公共库。
        typing_cast(SQLAlchemySession, session).execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"ai-knowledge:{asset_id}"},
        )
        previous = session.exec(
            latest_query().where(
                col(KnowledgeVersion.asset_id) == asset_id,
                _scope_clause(context, body.bc_id),
            )
        ).one_or_none()
        if previous is None or (
            previous.scope,
            previous.tenant_id,
            previous.bc_id,
            previous.key,
        ) != (body.scope, tenant_id, body.bc_id, body.key):
            raise DomainError(
                "ai_knowledge_scope", "知识范围、标识不可通过版本更新改变"
            )
        number = previous.version + 1
    data = body.model_dump(exclude={"evidence_ids"})
    row = KnowledgeVersion(
        **data,
        evidence_ids=[str(e) for e in body.evidence_ids],
        tenant_id=tenant_id,
        asset_id=asset_id or uuid4(),
        version=number,
        created_by=context.actor_id,
    )
    session.add(row)
    session.flush()
    return row


def promote_public(
    session: Session,
    *,
    context: TenantContext,
    version_id: UUID,
    body: str,
    review: str,
) -> KnowledgeVersion:
    authority = require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if authority.role != "platform_admin":
        raise DomainError("action_forbidden", "共享提炼需平台管理员审核")
    original = session.exec(
        select(KnowledgeVersion).where(
            col(KnowledgeVersion.id) == version_id,
            col(KnowledgeVersion.tenant_id) == context.tenant_id,
        )
    ).one_or_none()
    if original is None or not review.strip() or body.strip() == original.body.strip():
        raise DomainError(
            "ai_knowledge_sharing_review", "提交独立脱敏正文及明确共享审核"
        )
    # 不复制私有来源、证据、标题、账户、商业条款；公开的内容仅是审核者重新提交的提炼。
    return create_version(
        session,
        context=context,
        body=KnowledgeInput.model_validate(
            {
                "key": f"general-{uuid4().hex}",
                "scope": "PUBLIC",
                "title": "已审核通用投放经验",
                "body": body,
                "source": "explicit-sharing-review",
                "sharing_review": review,
                "tags": original.tags,
                "source_verified": True,
            }
        ),
    )


def select_knowledge(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    tags: set[str],
    now: datetime | None = None,
) -> list[dict]:
    now = now or datetime.now(UTC)
    query = latest_query().where(
        _scope_clause(context, bc_id), col(KnowledgeVersion.status) == "CONFIRMED"
    )
    if tags:
        query = query.where(
            or_(
                col(KnowledgeVersion.tags) == [],
                or_(*(col(KnowledgeVersion.tags).contains([tag]) for tag in tags)),
            )
        )
    rows = session.exec(
        query.order_by(col(KnowledgeVersion.created_at).desc()).limit(100)
    ).all()
    selected = []
    budget = 32000
    for row in rows:
        if len(selected) >= 20 or len(row.body) > budget:
            continue
        budget -= len(row.body)
        selected.append(
            {
                "id": str(row.id),
                "asset_id": str(row.asset_id),
                "key": row.key,
                "version": row.version,
                "scope": row.scope,
                "title": row.title,
                "body": row.body,
                "source": row.source,
                "applicability": row.applicability,
                "tags": row.tags,
                "evidence_ids": row.evidence_ids,
                "sha256": sha256(row.body.encode()).hexdigest(),
                "eligibility": "EXPIRED"
                if row.expires_at and row.expires_at <= now
                else "VERIFIED"
                if row.source_verified
                else "UNVERIFIED",
                "conflict_key": row.conflict_key,
                "conflict": bool(
                    row.conflict_key
                    and sum(r.conflict_key == row.conflict_key for r in rows) > 1
                ),
            }
        )
    return selected
