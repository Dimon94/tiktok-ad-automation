"""本地查询快照与冻结副本；数据库事务在一次构建完成后立即释放。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    Engine,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Session, SQLModel, col, select

from app.core.context import TenantContext
from app.modules.accounts.models import TenantBC
from app.modules.ads.models import AwareDateTime
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.schemas import MATERIAL_QUERY_CONTRACT
from app.modules.tenants.permissions import require_tenant


def _scope_constraints() -> tuple[Any, ...]:
    return (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        # 平台管理员可无 tenant_membership；每次读取必须重新 require_tenant。
        ForeignKeyConstraint(["actor_id"], ["user.id"]),
    )


class QueryScope(SQLModel):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class FrozenQueryScope(QueryScope):
    # 保存实际获准账户全集：分页/汇总读取时任何撤权都使旧快照不可读。
    advertiser_ids: list[str] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    filters: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    filter_digest: str = Field(max_length=64)
    publication_versions: dict[str, int] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    naming_versions: dict[str, int] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(minutes=15),
        sa_type=AwareDateTime,
    )


class QuerySnapshot(FrozenQueryScope, table=True):
    __tablename__ = "query_snapshot"
    __table_args__ = (
        *_scope_constraints(),
        CheckConstraint("expires_at > created_at", name="ck_query_snapshot_expiry"),
        CheckConstraint("total >= 0", name="ck_query_snapshot_total"),
        Index(
            "ix_query_snapshot_owner_expiry",
            "tenant_id",
            "bc_id",
            "actor_id",
            "expires_at",
        ),
        Index("ix_query_snapshot_expiry", "expires_at"),
    )
    result_contract: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    publication_map: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    total: int = 0
    summary: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    coverage: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    trends: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    dependency_digest: str = Field(default="", max_length=64)
    dependency_stamp: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )


class QuerySnapshotWaiter(QueryScope, table=True):
    __tablename__ = "query_snapshot_waiter"
    __table_args__ = (
        *_scope_constraints(),
        Index("ix_query_snapshot_waiter_key_expiry", "key", "expires_at"),
        Index("ix_query_snapshot_waiter_expiry", "expires_at"),
    )
    key: str = Field(max_length=64)
    expires_at: datetime = Field(sa_type=AwareDateTime)


class QuerySnapshotBuild(SQLModel, table=True):
    """短事务竞争构建租约；代数与token共同阻止失租构建者发布。"""

    __tablename__ = "query_snapshot_build"
    __table_args__ = (
        *_scope_constraints(),
        ForeignKeyConstraint(
            ["snapshot_id"], ["query_snapshot.id"], ondelete="SET NULL"
        ),
        CheckConstraint("generation > 0", name="ck_query_snapshot_build_generation"),
        Index("ix_query_snapshot_build_expiry", "lease_until"),
    )
    key: str = Field(primary_key=True, max_length=64)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID
    generation: int = Field(sa_type=BigInteger)
    token: UUID
    lease_until: datetime = Field(sa_type=AwareDateTime)
    snapshot_id: UUID | None = None
    dependency_digest: str = Field(default="", max_length=64)
    backend_pid: int | None = None


class QuerySnapshotRow(SQLModel, table=True):
    __tablename__ = "query_snapshot_row"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id"], ["query_snapshot.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "(dimension='account' AND account_membership_version_id IS NOT NULL AND refs='[]'::jsonb) OR "
            "(dimension<>'account' AND account_membership_version_id IS NULL AND refs='[]'::jsonb AND material_uses='[]'::jsonb)",
            name="ck_snapshot_account_membership",
        ),
        CheckConstraint("stable_sequence >= 0", name="ck_query_snapshot_row_sequence"),
        CheckConstraint(
            "dimension IN ('account','campaign','adgroup','ad','material','drama')",
            name="ck_query_snapshot_row_dimension",
        ),
        Index(
            "ix_query_snapshot_row_sequence",
            "snapshot_id",
            "stable_sequence",
            unique=True,
        ),
        Index(
            "ix_query_snapshot_row_account_membership_version_id",
            "account_membership_version_id",
            postgresql_where=text("account_membership_version_id IS NOT NULL"),
        ),
    )
    # 复合主键自身提供 snapshot + row_key 索引。
    snapshot_id: UUID = Field(primary_key=True)
    row_key: str = Field(primary_key=True, max_length=512)
    frozen_result: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    stable_sequence: int
    dimension: str = Field(max_length=32)
    member_keys: list[list[str]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    member_count: int = 0
    # 版本ID直接冻结；当前版本外键和历史保留水位共同保证生命周期。
    evidence_versions: list[UUID] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(PG_UUID(as_uuid=True)),
            nullable=False,
            server_default=text("'{}'::uuid[]"),
        ),
    )
    account_membership_version_id: UUID | None = Field(
        default=None, foreign_key="account_membership_version.id"
    )
    display: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    refs: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    material_uses: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    metric_buckets: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    capabilities: dict[str, bool] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    directory_versions: dict[str, int] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    membership_digest: str = Field(max_length=64)


class FrozenSelectionRecord(FrozenQueryScope, table=True):
    __tablename__ = "frozen_selection"
    __table_args__ = (
        *_scope_constraints(),
        UniqueConstraint("tenant_id", "id", name="uq_frozen_selection_tenant_id"),
        UniqueConstraint(
            "tenant_id", "bc_id", "id", name="uq_frozen_selection_scope_id"
        ),
        CheckConstraint("expires_at > created_at", name="ck_frozen_selection_expiry"),
        Index(
            "ix_frozen_selection_owner_expiry",
            "tenant_id",
            "bc_id",
            "actor_id",
            "expires_at",
        ),
    )
    # 仅保留来源 ID 而不加删除阻塞外键；冻结目标不依赖短期快照存活。
    snapshot_id: UUID
    refs: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    material_uses: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    membership_digest: str = Field(max_length=64)


class SavedReportView(QueryScope, table=True):
    __tablename__ = "saved_report_view"
    __table_args__ = (
        *_scope_constraints(),
        Index("ix_saved_report_view_owner", "tenant_id", "bc_id", "actor_id", "name"),
    )
    name: str = Field(max_length=128)
    filters: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    columns: list[str] = Field(default_factory=list, sa_type=JSONB, nullable=False)


class ReportExport(FrozenQueryScope, table=True):
    __tablename__ = "report_export"
    __table_args__ = (
        *_scope_constraints(),
        CheckConstraint("expires_at > created_at", name="ck_report_export_expiry"),
        Index(
            "ix_report_export_owner_status", "tenant_id", "bc_id", "actor_id", "status"
        ),
        Index("ix_report_export_expiry", "expires_at"),
        Index(
            "ix_report_export_idempotency",
            "tenant_id",
            "bc_id",
            "actor_id",
            "idempotency_key",
            unique=True,
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETE','FAILED','EXPIRED')",
            name="ck_report_export_status",
        ),
    )
    result_contract: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    publication_map: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    snapshot_id: UUID
    idempotency_key: str = Field(max_length=128)
    status: str = Field(default="QUEUED", max_length=16)
    frozen_rows: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    coverage: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    object_key: str | None = Field(default=None, max_length=512)
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=24),
        sa_type=AwareDateTime,
    )


def is_transaction_cleanup(statement: str) -> bool:
    """清理保存点必须绕过取消/预算hook，尤其SQL报错后不能先执行SET。"""
    return (
        statement.lstrip()
        .upper()
        .startswith(("ROLLBACK TO SAVEPOINT ", "RELEASE SAVEPOINT "))
    )


@contextmanager
def snapshot_transaction(
    engine: Engine,
    *,
    before_begin: Callable[[Connection], None] | None = None,
    deadline: float | None = None,
) -> Iterator[Session]:
    """B3 必须在此短事务内授权、聚合并保存行；禁止跨请求持有读视图。

    使用独立连接，隔离级别在第一条 SQL 前设置；不提交调用方的事务。
    30 秒 statement/idle timeout 限制异常构建；函数内不应访问外部服务。
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("report snapshots require PostgreSQL")
    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ"
    ) as connection:
        if before_begin is not None:
            before_begin(connection)
        with connection.begin():
            # 报表包含大量CTE但每次只执行一次；实测LLVM编译额外占约1秒，
            # 仅本读事务关闭JIT，不改变连接池后续业务或服务器全局参数。
            connection.execute(
                text("""SELECT set_config('statement_timeout','30s',true),
                set_config('idle_in_transaction_session_timeout','30s',true),
                set_config('jit','off',true)""")
            )

            def remaining_budget(
                _connection: Connection,
                cursor: Any,
                _statement: str,
                _parameters: Any,
                _context: Any,
                _many: bool,
            ) -> None:
                if deadline is None or is_transaction_cleanup(_statement):
                    return
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise HTTPException(503, detail="query_build_busy")
                # 单语句上限仍30秒，但不能在每条SQL/重试时重置请求总预算。
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{max(1, min(30000, int(remaining * 1000) + 1))}ms",),
                )

            event.listen(connection, "before_cursor_execute", remaining_budget)
            try:
                with Session(bind=connection, expire_on_commit=False) as session:
                    yield session
                    session.flush()
                    if deadline is not None and monotonic() >= deadline:
                        raise HTTPException(503, detail="query_build_busy")
            finally:
                event.remove(connection, "before_cursor_execute", remaining_budget)


def read_snapshot(
    session: Session, *, context: TenantContext, bc_id: str, snapshot_id: UUID
) -> QuerySnapshot:
    """所有快照消费者共用：所有者隔离、有效期及当前账户授权重新检查。"""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    snapshot = session.exec(
        select(QuerySnapshot)
        .where(
            QuerySnapshot.id == snapshot_id,
            QuerySnapshot.tenant_id == context.tenant_id,
            QuerySnapshot.bc_id == bc_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if snapshot is None:
        raise HTTPException(404, detail="query_snapshot_not_found")
    if snapshot.actor_id != context.actor_id:
        raise HTTPException(404, detail="query_snapshot_not_found")
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise HTTPException(403, detail="report_scope_forbidden")
    allowed = authorized_grants(session, context=context, bc_id=bc_id)
    if not set(snapshot.advertiser_ids) <= {grant.advertiser_id for grant in allowed}:
        raise HTTPException(403, detail="report_scope_forbidden")
    if (
        snapshot.filters.get("dimension") == "material"
        and snapshot.publication_versions.get("material_query_contract")
        != MATERIAL_QUERY_CONTRACT
    ):
        raise HTTPException(409, detail="query_snapshot_contract_changed")
    if snapshot.filters.get("dimension") != "material":
        from app.modules.reporting.frozen_result import RESULT_CONTRACT

        if snapshot.result_contract != RESULT_CONTRACT:
            raise HTTPException(409, detail="query_snapshot_contract_changed")
    if snapshot.expires_at <= datetime.now(UTC):
        raise HTTPException(409, detail="query_snapshot_expired")
    return snapshot


def read_snapshot_rows(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    snapshot_id: UUID,
    after_sequence: int = -1,
    limit: int = 100,
) -> tuple[QuerySnapshotRow, ...]:
    if not 1 <= limit <= 100 or after_sequence < -1:
        raise HTTPException(422, detail="invalid_snapshot_page")
    snapshot = read_snapshot(
        session, context=context, bc_id=bc_id, snapshot_id=snapshot_id
    )
    return tuple(
        session.exec(
            select(QuerySnapshotRow)
            .where(
                QuerySnapshotRow.snapshot_id == snapshot.id,
                col(QuerySnapshotRow.stable_sequence) > after_sequence,
            )
            .order_by(
                col(QuerySnapshotRow.stable_sequence), col(QuerySnapshotRow.row_key)
            )
            .limit(limit)
        ).all()
    )
