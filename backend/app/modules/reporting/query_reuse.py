"""同操作者查询单飞；短事务租约与RR内实际依赖标签，等待者不占数据库连接。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg import Connection as PsycopgConnection
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.reporting import query_diagnostics
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotBuild,
    read_snapshot,
    snapshot_transaction,
)
from app.modules.reporting.schemas import ReportingFilter
from app.modules.tenants.permissions import require_tenant

BUILD_LEASE_SECONDS = 45
# 总请求50s可用于排队/接管；本代构建最多40s，保留5s登记/提交余量。
# 普通慢构建先被SQL预算终止，不能跨45s租约产出后反复丢弃结果。
MAX_BUILD_SECONDS = 40


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class QueryDependencyStamp:
    scope_digest: str
    permission_revision: str
    filter_digest: str
    fact_buckets_digest: str
    derived_buckets_digest: str
    directory_revision: str
    naming_revision: str
    link_revision: str
    metric_contract: str = "report-metrics-v1"

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


def query_key(context: TenantContext, bc_id: str, filters: ReportingFilter) -> str:
    return _digest(
        [context.tenant_id, bc_id, context.actor_id, filters.model_dump(mode="json")]
    )


@query_diagnostics.timed("dependencies")
def dependency_stamp(
    session: Session, *, context: TenantContext, bc_id: str, filters: ReportingFilter
) -> QueryDependencyStamp:
    authority = require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    grants = authorized_grants(
        session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
    )
    advertisers = sorted({g.advertiser_id for g in grants})
    if not advertisers or (
        filters.advertiser_ids and set(advertisers) != set(filters.advertiser_ids)
    ):
        raise HTTPException(403, detail="report_scope_forbidden")
    params = {
        "tenant": context.tenant_id,
        "bc": bc_id,
        "advertisers": advertisers,
        "start": filters.start_date,
        "end": filters.end_date,
    }
    connections = (
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT c.id,c.kind,c.status,c.authorization_revision,c.adapter_contract_revision,
            b.revision,b.authorization_revision,b.status
        FROM bc_account_access a JOIN tiktok_connection c ON c.tenant_id=a.tenant_id AND c.id=a.connection_id
        LEFT JOIN bc_connection_binding b ON b.tenant_id=a.tenant_id AND b.bc_id=a.bc_id AND b.connection_id=a.connection_id
        WHERE a.tenant_id=:tenant AND a.bc_id=:bc AND a.advertiser_id=ANY(:advertisers)
            AND a.in_bc AND a.authorized AND a.active ORDER BY c.id
    """),
            params,
        )
        .all()
    )
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT advertiser_id,kind,bucket,source_revision,derived_revision
        FROM read_bucket_state WHERE tenant_id=:tenant AND advertiser_id=ANY(:advertisers)
            AND (bucket BETWEEN :start AND :end OR bucket=DATE '1970-01-01')
        ORDER BY advertiser_id,kind,bucket
    """),
            params,
        )
        .all()
    )
    kind = "material_day" if filters.dimension == "material" else "ad_day"
    facts = [tuple(r[:4]) for r in rows if r.kind == kind]
    derived = [
        (r.advertiser_id, r.kind, r.bucket, r.derived_revision)
        for r in rows
        if r.kind == kind
    ]

    def revisions(kinds: Collection[str]) -> str:
        return _digest([tuple(r) for r in rows if r.kind in kinds])

    balances = []
    if filters.dimension == "account" and filters.sort_by == "balance":
        balances = [
            tuple(r)
            for r in session.connection().execute(
                text("""
            SELECT DISTINCT ON (advertiser_id) advertiser_id,id FROM account_balance_observation
            WHERE tenant_id=:tenant AND advertiser_id=ANY(:advertisers)
            ORDER BY advertiser_id,observed_at DESC,id DESC
        """),
                params,
            )
        ]
    return QueryDependencyStamp(
        scope_digest=_digest([context.tenant_id, bc_id, context.actor_id, advertisers]),
        permission_revision=_digest(
            [
                authority.role,
                [tuple(r) for r in connections],
                sorted(
                    (
                        g.advertiser_id,
                        str(g.connection_id),
                        g.permission_state,
                        g.can_upload,
                        g.can_build,
                    )
                    for g in grants
                ),
            ]
        ),
        filter_digest=_digest(filters.model_dump(mode="json")),
        fact_buckets_digest=_digest(facts),
        derived_buckets_digest=_digest(derived),
        directory_revision=_digest([revisions({"directory"}), balances]),
        naming_revision=revisions({"naming"}),
        link_revision=revisions({"link", "usage"})
        if filters.dimension == "material"
        else _digest([]),
    )


@dataclass(frozen=True)
class BuildClaim:
    generation: int
    token: UUID


def claim_build(
    session: Session,
    *,
    key: str,
    context: TenantContext,
    bc_id: str,
    lease_seconds: int = BUILD_LEASE_SECONDS,
) -> BuildClaim | None:
    token = uuid4()
    row = (
        session.connection()
        .execute(
            text("""
        INSERT INTO query_snapshot_build
            (key,tenant_id,bc_id,actor_id,generation,token,lease_until,snapshot_id,dependency_digest)
        VALUES (:key,:tenant,:bc,:actor,1,:token,clock_timestamp()+make_interval(secs=>:lease),NULL,'')
        ON CONFLICT (key) DO UPDATE SET generation=query_snapshot_build.generation+1,
            token=EXCLUDED.token,lease_until=EXCLUDED.lease_until
        WHERE query_snapshot_build.lease_until<=clock_timestamp()
        RETURNING generation,token
    """),
            {
                "key": key,
                "tenant": context.tenant_id,
                "bc": bc_id,
                "actor": context.actor_id,
                "token": token,
                "lease": lease_seconds,
            },
        )
        .first()
    )
    return BuildClaim(row.generation, row.token) if row else None


def complete_build(
    session: Session, *, key: str, claim: BuildClaim, snapshot: QuerySnapshot
) -> bool:
    return (
        session.connection()
        .execute(
            text("""
        UPDATE query_snapshot_build SET snapshot_id=:snapshot,dependency_digest=:digest,lease_until=clock_timestamp(),backend_pid=NULL
        WHERE key=:key AND generation=:generation AND token=:token AND lease_until>clock_timestamp()
        RETURNING generation
    """),
            {
                "key": key,
                "generation": claim.generation,
                "token": claim.token,
                "snapshot": snapshot.id,
                "digest": snapshot.dependency_digest,
            },
        )
        .first()
        is not None
    )


class BuildLeaseLost(Exception):
    pass


def register_waiter(
    session: Session, *, key: str, waiter_id: UUID, context: TenantContext, bc_id: str
) -> None:
    session.connection().execute(
        text("SELECT key FROM query_snapshot_build WHERE key=:key FOR UPDATE"),
        {"key": key},
    )
    session.connection().execute(
        text("""INSERT INTO query_snapshot_waiter
        (id,key,tenant_id,bc_id,actor_id,created_at,expires_at)
        VALUES (:id,:key,:tenant,:bc,:actor,clock_timestamp(),clock_timestamp()+interval '90 seconds')"""),
        {
            "id": waiter_id,
            "key": key,
            "tenant": context.tenant_id,
            "bc": bc_id,
            "actor": context.actor_id,
        },
    )


def cancel_waiter(engine: Engine, *, key: str, waiter_id: UUID) -> bool:
    """先锁构建身份再移除等待者；只取消仍属于本代且没有消费者的数据库执行。"""
    with Session(engine) as session:
        entry = (
            session.connection()
            .execute(
                text(
                    "SELECT backend_pid,token,lease_until FROM query_snapshot_build WHERE key=:key FOR UPDATE"
                ),
                {"key": key},
            )
            .first()
        )
        session.connection().execute(
            text("DELETE FROM query_snapshot_waiter WHERE id=:id AND key=:key"),
            {"id": waiter_id, "key": key},
        )
        waiting = (
            session.connection()
            .execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM query_snapshot_waiter WHERE key=:key AND expires_at>clock_timestamp())"
                ),
                {"key": key},
            )
            .scalar_one()
        )
        cancelled = False
        if (
            entry
            and entry.backend_pid
            and not waiting
            and entry.lease_until > datetime.now(UTC)
        ):
            # pid可能被连接池复用；事务级application_name必须仍精确匹配本代token。
            cancelled = bool(
                session.connection()
                .execute(
                    text("""SELECT pg_cancel_backend(pid)
                FROM pg_stat_activity WHERE pid=:pid AND application_name=:name"""),
                    {"pid": entry.backend_pid, "name": f"qrm:{entry.token}"},
                )
                .scalar()
            )
        session.commit()
    return cancelled


class QueryCancellation:
    """共享构建通过DB等待者计数取消；本页私有hydrate则直接取消本连接。"""

    def __init__(self) -> None:
        self.event = Event()
        self.waiter_id = uuid4()
        self.key: str | None = None
        self._driver: PsycopgConnection[Any] | None = None
        self._lock = Lock()

    def cancel(self, engine: Engine) -> None:
        self.event.set()
        if self.key:
            try:
                cancel_waiter(engine, key=self.key, waiter_id=self.waiter_id)
            except Exception:
                # 数据库暂时不可达时仍等待原线程退出；不能提前释放其Session。
                pass
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.cancel_safe(timeout=2)
                except Exception:
                    pass

    @contextmanager
    def private_query(self, session: Session) -> Iterator[None]:
        connection = session.connection()

        def before_sql(*_args: object) -> None:
            if self.event.is_set():
                raise HTTPException(499, detail="query_cancelled")

        with self._lock:
            if self.event.is_set():
                raise HTTPException(499, detail="query_cancelled")
            self._driver = cast(
                PsycopgConnection[Any], connection.connection.driver_connection
            )
            event.listen(connection, "before_cursor_execute", before_sql)
        try:
            yield
        finally:
            with self._lock:
                event.remove(connection, "before_cursor_execute", before_sql)
                self._driver = None


def prune_query_reuse(session: Session, *, limit: int = 500) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("invalid query cleanup batch")
    session.connection().execute(
        text("""DELETE FROM query_snapshot_waiter WHERE id IN (
        SELECT id FROM query_snapshot_waiter WHERE expires_at<=clock_timestamp()
        ORDER BY expires_at LIMIT :limit FOR UPDATE SKIP LOCKED)"""),
        {"limit": limit},
    )
    session.connection().execute(
        text("""DELETE FROM query_snapshot_build WHERE key IN (
        SELECT b.key FROM query_snapshot_build b LEFT JOIN query_snapshot s ON s.id=b.snapshot_id
        WHERE b.lease_until<=clock_timestamp() AND (s.id IS NULL OR s.expires_at<=clock_timestamp())
          AND NOT EXISTS(SELECT 1 FROM query_snapshot_waiter w WHERE w.key=b.key AND w.expires_at>clock_timestamp())
        ORDER BY b.lease_until LIMIT :limit FOR UPDATE OF b SKIP LOCKED)"""),
        {"limit": limit},
    )


class SnapshotBuilder(Protocol):
    def __call__(
        self,
        session: Session,
        *,
        context: TenantContext,
        bc_id: str,
        filters: ReportingFilter,
    ) -> QuerySnapshot: ...


def reuse_in_transaction(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    builder: SnapshotBuilder,
) -> QuerySnapshot:
    """已由调用方提供一致事务时复用该视图内快照，不提交或回滚调用方事务。"""
    stamp = dependency_stamp(session, context=context, bc_id=bc_id, filters=filters)
    snapshot = session.exec(
        select(QuerySnapshot)
        .where(
            QuerySnapshot.tenant_id == context.tenant_id,
            QuerySnapshot.bc_id == bc_id,
            QuerySnapshot.actor_id == context.actor_id,
            QuerySnapshot.dependency_digest == stamp.digest,
            QuerySnapshot.expires_at > datetime.now(UTC),
        )
        .order_by(col(QuerySnapshot.created_at).desc())
        .limit(1)
    ).first()
    if snapshot:
        query_diagnostics.count("reused", 1)
        return read_snapshot(
            session, context=context, bc_id=bc_id, snapshot_id=snapshot.id
        )
    snapshot = builder(session, context=context, bc_id=bc_id, filters=filters)
    snapshot.dependency_stamp = asdict(stamp)
    snapshot.dependency_digest = stamp.digest
    session.add(snapshot)
    session.flush()
    return snapshot


def find_or_build_snapshot(
    engine: Engine,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    builder: SnapshotBuilder,
    cancelled: Callable[[], bool] = lambda: False,
    wait_seconds: float = 50,
    waiter_id: UUID | None = None,
) -> QuerySnapshot:
    deadline = time.monotonic() + wait_seconds
    key = query_key(context, bc_id, filters)
    waiter_id = waiter_id or uuid4()
    with Session(engine) as session:
        register_waiter(
            session, key=key, waiter_id=waiter_id, context=context, bc_id=bc_id
        )
        session.commit()
    try:
        return _find_or_build_snapshot(
            engine,
            context=context,
            bc_id=bc_id,
            filters=filters,
            builder=builder,
            cancelled=cancelled,
            deadline=deadline,
        )
    finally:
        with Session(engine) as session:
            session.connection().execute(
                text("DELETE FROM query_snapshot_waiter WHERE id=:id"),
                {"id": waiter_id},
            )
            session.commit()


def _find_or_build_snapshot(
    engine: Engine,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    builder: SnapshotBuilder,
    cancelled: Callable[[], bool] = lambda: False,
    deadline: float,
) -> QuerySnapshot:
    key = query_key(context, bc_id, filters)
    # 默认请求预算50秒包含构建和等待；45秒租约允许故障接管，剩余预算
    # 用于复用赢家。SQL每次取剩余预算与30秒上限的较小值，重试不续时。
    delay = 0.025
    serialization_retries = 0
    while True:
        if time.monotonic() >= deadline:
            raise HTTPException(503, detail="query_build_busy")
        if cancelled():
            # 此调用仅退出等待，不碰另一构建者的数据库租约或SQL。
            raise HTTPException(499, detail="query_cancelled")
        with Session(engine, expire_on_commit=False) as session:
            stamp = dependency_stamp(
                session, context=context, bc_id=bc_id, filters=filters
            )
            entry = session.get(QuerySnapshotBuild, key)
            if entry and entry.snapshot_id and entry.dependency_digest == stamp.digest:
                snapshot = session.get(QuerySnapshot, entry.snapshot_id)
                if snapshot and snapshot.expires_at > datetime.now(UTC):
                    read_snapshot(
                        session, context=context, bc_id=bc_id, snapshot_id=snapshot.id
                    )
                    query_diagnostics.count("reused", 1)
                    return snapshot
            claim_started = time.monotonic()
            claim = claim_build(session, key=key, context=context, bc_id=bc_id)
            session.commit()
        if claim:
            attempt_deadline = min(deadline, claim_started + MAX_BUILD_SECONDS)
            try:

                def attach_backend(
                    connection: Connection, owned_claim: BuildClaim = claim
                ) -> None:
                    driver = cast(
                        PsycopgConnection[Any], connection.connection.driver_connection
                    )
                    pid = driver.info.backend_pid
                    # 此时RR构建尚未开始；同一连接短事务登记PID并提交，
                    # 让取消者可见，然后再打开真正的构建RR视图。不能持一条
                    # 池连接再等第二条：并发冷构建会把有限连接池互相堵死。
                    with connection.begin():
                        attached = connection.execute(
                            text("""UPDATE query_snapshot_build SET backend_pid=:pid
                            WHERE key=:key AND generation=:generation AND token=:token AND lease_until>clock_timestamp()
                            RETURNING generation"""),
                            {
                                "pid": pid,
                                "key": key,
                                "generation": owned_claim.generation,
                                "token": owned_claim.token,
                            },
                        ).first()
                    if attached is None:
                        raise BuildLeaseLost()

                with snapshot_transaction(
                    engine, before_begin=attach_backend, deadline=attempt_deadline
                ) as session:
                    session.connection().execute(
                        text("SELECT set_config('application_name',:name,true)"),
                        {"name": f"qrm:{claim.token}"},
                    )
                    # 注销可能发生在pid登记与SET application_name之间。此时构建SQL
                    # 尚未开始，直接退出RR，由外层归还连接后释放本代租约即可。
                    # 不能持着池内唯一连接再调用cancel_waiter申请第二条连接；也不能
                    # 删除其他消费者或取消它们的SQL，它们会接管已释放的构建。
                    if cancelled():
                        raise HTTPException(499, detail="query_cancelled")
                    # 最终标签与成员聚合消费同一RR视图，不能复用竞争锁前的旧stamp。
                    actual = dependency_stamp(
                        session, context=context, bc_id=bc_id, filters=filters
                    )
                    # 在争取租约前的读取可能刚好错过前任提交，拿到租约后必须再查。
                    entry = session.get(QuerySnapshotBuild, key)
                    snapshot = (
                        session.get(QuerySnapshot, entry.snapshot_id)
                        if entry and entry.snapshot_id
                        else None
                    )
                    if (
                        not snapshot
                        or snapshot.dependency_digest != actual.digest
                        or snapshot.expires_at <= datetime.now(UTC)
                    ):
                        snapshot = builder(
                            session, context=context, bc_id=bc_id, filters=filters
                        )
                    snapshot.dependency_stamp = asdict(actual)
                    snapshot.dependency_digest = actual.digest
                    session.add(snapshot)
                    session.flush()
                    if not complete_build(
                        session, key=key, claim=claim, snapshot=snapshot
                    ):
                        raise BuildLeaseLost()
                return snapshot
            except BuildLeaseLost:
                # 失租结果整个事务回滚，含刚写的快照；继续读取接管者的结果。
                pass
            except BaseException as error:
                # 此处已经退出snapshot_transaction，失败RR及其新快照全部回滚。
                # 清理只影响本token，绝不能提前释放接管者B的租约。
                with Session(engine) as session:
                    session.connection().execute(
                        text("""UPDATE query_snapshot_build SET lease_until=clock_timestamp(),backend_pid=NULL
                        WHERE key=:key AND generation=:generation AND token=:token"""),
                        {
                            "key": key,
                            "generation": claim.generation,
                            "token": claim.token,
                        },
                    )
                    session.commit()
                if time.monotonic() >= attempt_deadline:
                    raise HTTPException(503, detail="query_build_busy") from error
                if (
                    isinstance(error, DBAPIError)
                    and getattr(error.orig, "sqlstate", None) == "40001"
                ):
                    serialization_retries += 1
                    if serialization_retries >= 3:
                        raise HTTPException(503, detail="query_build_busy") from error
                    # 新事务重新检查权限/依赖及已完成快照；绝不在失败RR内重试。
                else:
                    raise
        if time.monotonic() >= deadline:
            raise HTTPException(503, detail="query_build_busy")
        with query_diagnostics.stage("reuse_wait"):
            time.sleep(delay)
        delay = min(delay * 1.5, 0.25)
