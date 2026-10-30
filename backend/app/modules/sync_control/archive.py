"""90天终态摘要归档：有界流式冻结、事务外上传、锁定复核后分批回收。"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from tempfile import TemporaryFile
from typing import Any, BinaryIO
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import (
    Table,
    Text,
    cast,
    delete,
    func,
    inspect,
    literal,
    literal_column,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting.store_models import (
    ReportBalanceRun,
    ReportSyncJob,
    ReportSyncPage,
)
from app.modules.sync_control.archive_storage import (
    MAX_ARCHIVE_BYTES,
    ArchiveReceipt,
    S3RunArchive,
    archive_key,
)
from app.modules.sync_control.models import (
    SyncArchiveFinalization,
    SyncCostLedger,
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)

Run = ReportSyncJob | AdDirectoryRun | ReportBalanceRun
type ArchiveRowModel = (
    ReportSyncJob
    | AdDirectoryRun
    | ReportBalanceRun
    | SyncExecutionScope
    | SyncCostLedger
    | SyncScope
    | SyncRequirement
)

SessionFactory = Callable[[], Session]

ONLINE_RETENTION = timedelta(days=90)
STREAM_ROWS = 256
DELETE_ROWS = 500
MAX_ARCHIVE_ROWS = 100_000
MAX_ROW_BYTES = 8 * 1024 * 1024
TERMINAL = {
    "report": ("COMPLETE", "FAILED", "CANCELLED"),
    "directory": ("COMPLETE", "FAILED", "CANCELLED", "STALE"),
    "balance": ("COMPLETE", "FAILED", "CANCELLED"),
}


class ArchiveDeferred(RuntimeError):
    """保留原因；源数据未移除。"""


@dataclass(frozen=True)
class FrozenArchive:
    tenant_id: UUID
    kind: str
    run_id: UUID
    payload: BinaryIO
    sha256: str
    size: int
    rows: int
    keep_reason: str | None

    def close(self) -> None:
        self.payload.close()


@dataclass(frozen=True)
class ArchiveResult:
    archived: bool
    deleted: bool
    reason: str
    bytes_archived: int = 0


def _spec(
    kind: str,
) -> tuple[
    type[ReportSyncJob] | type[AdDirectoryRun] | type[ReportBalanceRun], str, str, str
]:
    if kind == "report":
        return ReportSyncJob, "finished_at", "report_job_id", "job_id"
    if kind == "directory":
        return AdDirectoryRun, "completed_at", "directory_run_id", "run_id"
    if kind == "balance":
        return ReportBalanceRun, "completed_at", "balance_run_id", "run_id"
    raise ValueError("invalid archive kind")


def retention_candidates(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    now: datetime,
    cursor: tuple[datetime, UUID] | None = None,
    limit: int = 25,
) -> list[tuple[datetime, UUID]]:
    """租户+终态时间索引游标；未知终态时间绝不按创建时间猜测。"""
    if not 1 <= limit <= 100:
        raise ValueError("invalid archive batch limit")
    model, timestamp, _, _ = _spec(kind)
    time_col = getattr(model, timestamp)
    query = select(time_col, model.id).where(
        model.tenant_id == tenant_id,
        col(model.status).in_(TERMINAL[kind]),
        time_col < now - ONLINE_RETENTION,
    )
    if cursor:
        query = query.where(
            tuple_(time_col, col(model.id))
            > tuple_(literal(cursor[0]), literal(cursor[1]))
        )
    return list(
        session.exec(query.order_by(time_col, col(model.id)).limit(limit)).all()
    )


def _stream_batches[Row: ArchiveRowModel](
    session: Session,
    model: type[Row],
    predicate: ColumnElement[bool],
    *,
    lock: bool = False,
) -> Iterator[list[Row]]:
    cursor: UUID | None = None
    count = 0
    mapper = inspect(model)
    assert mapper is not None
    table = typing_cast(Table, mapper.local_table)
    while True:
        query = select(
            model.id,
            func.octet_length(cast(func.to_jsonb(table.table_valued()), Text)),
        ).where(predicate)
        if cursor is not None:
            query = query.where(model.id > cursor)
        sizes = [
            (key, int(size))
            for key, size in session.exec(
                query.order_by(col(model.id)).limit(STREAM_ROWS)
            ).all()
        ]
        if not sizes:
            return
        page_bytes = 0
        bounded: list[tuple[UUID, int]] = []
        for key, row_bytes in sizes:
            if bounded and page_bytes + row_bytes > MAX_ROW_BYTES:
                break
            bounded.append((key, row_bytes))
            page_bytes += row_bytes
        sizes = bounded
        count += len(sizes)
        if count > MAX_ARCHIVE_ROWS or any(size > MAX_ROW_BYTES for _, size in sizes):
            raise ArchiveDeferred("archive_budget_exceeded")
        # 流式页最多256行；大小预检按解压JSON计，不能用压缩后TOAST尺寸冒充。
        rows_query = (
            select(model)
            .where(col(model.id).in_([key for key, _ in sizes]))
            .order_by(col(model.id))
        )
        if lock:
            rows_query = rows_query.with_for_update()
        yield list(
            session.exec(rows_query.execution_options(populate_existing=True)).all()
        )
        cursor = sizes[-1][0]


def _stream_rows[Row: ArchiveRowModel](
    session: Session,
    model: type[Row],
    predicate: ColumnElement[bool],
    *,
    lock: bool = False,
) -> Iterator[Row]:
    for batch in _stream_batches(session, model, predicate, lock=lock):
        yield from batch


def _directory_cover_exists(
    session: Session, parent: AdDirectoryRun, *, lock: bool, full_only: bool = False
) -> bool:
    """只查可索引的充分证明，不为复杂集合包含关系扫描整户历史。"""
    from app.modules.ads.directory_supersession import superseding_directory_predicate

    route_digest = func.md5(cast(col(AdDirectoryRun.frozen_route), Text))
    frozen_digest = func.md5(cast(literal(parent.frozen_route, type_=JSONB), Text))
    common: tuple[ColumnElement[bool], ...] = (
        superseding_directory_predicate(parent, AdDirectoryRun),
        route_digest == frozen_digest,
        # 固定部分索引谓词，不能依赖generic plan猜参数化JSON键。
        literal_column("NOT (ad_directory_run.query ? 'material_child_v1')"),
    )
    sufficient: tuple[ColumnElement[bool], ...] = (
        col(AdDirectoryRun.partition_key) == parent.partition_key,
        literal_column(
            "coalesce(ad_directory_run.query->'ids','[]'::jsonb)='[]'::jsonb "
            "AND coalesce(ad_directory_run.query->'parent_ids','[]'::jsonb)='[]'::jsonb "
            "AND ad_directory_run.query->>'include_deleted'='true'"
        ),
    )
    for scope in sufficient[1:] if full_only else sufficient:
        query = (
            select(AdDirectoryRun.id)
            .where(*common, scope)
            .order_by(col(AdDirectoryRun.request_sequence).desc())
            .limit(1)
        )
        if lock:
            query = query.with_for_update(read=True, key_share=True)
        if session.exec(query).first() is not None:
            return True
    return False


def _assert_eligible(
    session: Session,
    parent: Run,
    *,
    kind: str,
    timestamp: str,
    payload_key: str,
    now: datetime,
    lock: bool,
) -> str | None:
    finished = getattr(parent, timestamp)
    if (
        parent.status not in TERMINAL[kind]
        or finished is None
        or finished >= now - ONLINE_RETENTION
    ):
        raise ArchiveDeferred("online_retention")
    if parent.claim_token is not None:
        raise ArchiveDeferred("live_claim")
    if session.exec(
        select(PendingDispatch.id)
        .where(
            PendingDispatch.tenant_id == parent.tenant_id,
            PendingDispatch.payload[payload_key].astext == str(parent.id),
            col(PendingDispatch.compacted_at).is_(None),
        )
        .limit(1)
    ).first():
        raise ArchiveDeferred("dispatch_not_preserved")
    if session.exec(
        select(SyncSchedule.scope_id)
        .where(
            SyncSchedule.tenant_id == parent.tenant_id,
            SyncSchedule.lease_execution_id == parent.id,
            col(SyncSchedule.lease_token).is_not(None),
        )
        .limit(1)
    ).first():
        raise ArchiveDeferred("schedule_reference")
    if kind != "balance":
        page_model, page_key = (
            (ReportSyncPage, "job_id")
            if kind == "report"
            else (AdDirectoryPage, "run_id")
        )
        if session.exec(
            select(getattr(page_model, page_key))
            .where(getattr(page_model, page_key) == parent.id)
            .limit(1)
        ).first():
            raise ArchiveDeferred("private_pages_remain")
    if isinstance(parent, AdDirectoryRun):
        # 普通目录与素材子运行使用各自冻结覆盖语义，不能放宽普通query谓词。
        if parent.published_version is not None or parent.status == "COMPLETE":
            from app.modules.ads.material_directory_children import is_material_child

            if is_material_child(parent):
                from app.modules.sync_control.archive_children import (
                    material_child_archive_covered,
                )

                reason = material_child_archive_covered(session, parent, lock=lock)
                if reason:
                    return reason
            elif not _directory_cover_exists(session, parent, lock=lock):
                return "directory_coverage_proof"
        # 与0092表达式索引完全一致；SQLAlchemy JSON下标新渲染会变成[]，
        # 且固定JSON路径不能参数化成generic plan无法证明的部分索引条件。
        source: ColumnElement[str] = literal_column(
            "(ad_directory_run.query->'material_child_v1'->>'source_run_id')"
        )
        if session.exec(
            select(AdDirectoryRun.id)
            .where(
                AdDirectoryRun.tenant_id == parent.tenant_id,
                col(AdDirectoryRun.query).op("?")(
                    literal_column("'material_child_v1'")
                ),
                source == str(parent.id),
            )
            .limit(1)
        ).first():
            return "directory_source_reference"
    return None


def freeze_archive(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    run_id: UUID,
    now: datetime,
    lock: bool = False,
) -> FrozenArchive:
    model, timestamp, execution_key, payload_key = _spec(kind)
    parents = list(
        _stream_rows(
            session,
            model,
            (col(model.id) == run_id) & (col(model.tenant_id) == tenant_id),
            lock=lock,
        )
    )
    if not parents:
        raise ArchiveDeferred("run_not_found")
    parent = parents[0]
    keep_reason = _assert_eligible(
        session,
        parent,
        kind=kind,
        timestamp=timestamp,
        payload_key=payload_key,
        now=now,
        lock=lock,
    )
    spool, digest, size, count = TemporaryFile(mode="w+b"), sha256(), 0, 0

    def write(record: dict[str, Any]) -> None:
        nonlocal size, count
        line = (
            json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
            + b"\n"
        )
        size, count = size + len(line), count + 1
        if size > MAX_ARCHIVE_BYTES or count > MAX_ARCHIVE_ROWS:
            raise ArchiveDeferred("archive_budget_exceeded")
        digest.update(line)
        spool.write(line)

    try:
        write(
            {
                "type": "summary",
                "schema_version": 1,
                "kind": kind,
                "tenant_id": str(tenant_id),
                "run_id": str(run_id),
                "summary": parent.model_dump(mode="json"),
                "audit_reference": {
                    "tenant_id": str(tenant_id),
                    "execution_kind": kind,
                    "execution_id": str(run_id),
                    "actor_id": str(parent.actor_id),
                },
                "dispatch_history": "separate_7_day_payload_30_day_identity_retention",
            }
        )
        for children in _stream_batches(
            session,
            SyncExecutionScope,
            getattr(SyncExecutionScope, execution_key) == run_id,
            lock=lock,
        ):
            scopes = {
                s.id: s
                for s in _stream_rows(
                    session,
                    SyncScope,
                    col(SyncScope.id).in_([c.scope_id for c in children]),
                )
            }
            requirements = {
                r.id: r
                for r in _stream_rows(
                    session,
                    SyncRequirement,
                    col(SyncRequirement.id).in_([c.requirement_id for c in children]),
                )
            }
            for child in children:
                if child.tenant_id != tenant_id or child.status == "PENDING":
                    raise ArchiveDeferred("pending_execution")
                scope, requirement = (
                    scopes.get(child.scope_id),
                    requirements.get(child.requirement_id),
                )
                if (
                    scope is None
                    or requirement is None
                    or scope.tenant_id != tenant_id
                    or requirement.tenant_id != tenant_id
                ):
                    raise ArchiveDeferred("scope_reference_mismatch")
                # owner/revision取执行冻结值，不抓当前需求或head造成永久冲突。
                write(
                    {
                        "type": "execution",
                        "execution": child.model_dump(mode="json"),
                        "scope": scope.model_dump(mode="json", exclude={"owner_epoch"}),
                        "bucket": requirement.bucket,
                        "bucket_key": requirement.bucket_key,
                    }
                )
        for cost in _stream_rows(
            session,
            SyncCostLedger,
            getattr(SyncCostLedger, execution_key) == run_id,
            lock=lock,
        ):
            if (
                cost.tenant_id != tenant_id
                or cost.receipt_version != cost.settled_version
                or (not cost.closed and cost.abandoned_at is None)
            ):
                raise ArchiveDeferred("unsettled_cost")
            write({"type": "cost", "cost": cost.model_dump(mode="json")})
        spool.seek(0)
        return FrozenArchive(
            tenant_id, kind, run_id, spool, digest.hexdigest(), size, count, keep_reason
        )
    except BaseException:
        spool.close()
        raise


def prune_archive(
    session: Session, *, frozen: FrozenArchive, receipt: ArchiveReceipt, now: datetime
) -> ArchiveResult:
    if (
        receipt.key
        != archive_key(frozen.tenant_id, frozen.kind, frozen.run_id, frozen.sha256)
        or receipt.sha256 != frozen.sha256
        or receipt.size != frozen.size
    ):
        raise ArchiveDeferred("archive_receipt_mismatch")
    current = freeze_archive(
        session,
        tenant_id=frozen.tenant_id,
        kind=frozen.kind,
        run_id=frozen.run_id,
        now=now,
        lock=True,
    )
    try:
        if (current.sha256, current.size, current.rows) != (
            frozen.sha256,
            frozen.size,
            frozen.rows,
        ):
            raise ArchiveDeferred("source_changed")
        if current.keep_reason:
            return ArchiveResult(False, False, current.keep_reason)
        # 临时outbox与源删除原子提交；崩溃后可凭intent找到已验证blob并补marker。
        session.connection().execute(
            pg_insert(SyncArchiveFinalization)
            .values(
                tenant_id=frozen.tenant_id,
                kind=frozen.kind,
                execution_id=frozen.run_id,
                digest=receipt.sha256,
                object_key=receipt.key,
                byte_length=receipt.size,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        intent = session.get(
            SyncArchiveFinalization,
            (frozen.tenant_id, frozen.kind, frozen.run_id),
            populate_existing=True,
        )
        if (
            intent is None
            or intent.digest != receipt.sha256
            or intent.object_key != receipt.key
            or intent.byte_length != receipt.size
        ):
            raise ArchiveDeferred("archive_finalization_pending")
        model, _, execution_key, _ = _spec(frozen.kind)
        # 全部行已被复核锁定；分批删除避免隐式大CASCADE及无界IN列表。
        for child_model in (SyncExecutionScope, SyncCostLedger):
            while True:
                ids = list(
                    session.exec(
                        select(child_model.id)
                        .where(
                            getattr(child_model, execution_key) == frozen.run_id,
                            child_model.tenant_id == frozen.tenant_id,
                        )
                        .order_by(col(child_model.id))
                        .limit(DELETE_ROWS)
                    ).all()
                )
                if not ids:
                    break
                session.connection().execute(
                    delete(child_model).where(col(child_model.id).in_(ids))
                )
        session.connection().execute(
            delete(model).where(
                col(model.id) == frozen.run_id, col(model.tenant_id) == frozen.tenant_id
            )
        )
        return ArchiveResult(True, True, "archived", receipt.size)
    finally:
        current.close()


def archive_run(
    session_factory: Callable[[], Session],
    storage: S3RunArchive,
    *,
    tenant_id: UUID,
    kind: str,
    run_id: UUID,
    now: datetime,
) -> ArchiveResult:
    frozen, receipt = None, None
    try:
        if kind == "directory":
            from app.modules.sync_control.archive_children import (
                register_legacy_owner_ceiling,
            )

            deferred = register_legacy_owner_ceiling(
                session_factory, tenant_id=tenant_id, run_id=run_id, now=now
            )
            if deferred:
                return ArchiveResult(False, False, deferred)
        with session_factory() as session:
            frozen = freeze_archive(
                session, tenant_id=tenant_id, kind=kind, run_id=run_id, now=now
            )
        if frozen.keep_reason:
            return ArchiveResult(False, False, frozen.keep_reason)
        # 此时数据库连接/快照已释放；存储网络不延长父任务锁或事务寿命。
        receipt = storage.persist(
            tenant_id=tenant_id,
            kind=kind,
            run_id=run_id,
            payload=frozen.payload,
            sha256_digest=frozen.sha256,
            size=frozen.size,
        )
        with session_factory() as session:
            result = prune_archive(session, frozen=frozen, receipt=receipt, now=now)
            session.commit()
        if not result.deleted:
            return result
        try:
            finalize_archive(
                session_factory, storage, tenant_id=tenant_id, kind=kind, run_id=run_id
            )
        except Exception:
            # blob已持久验证，intent仍可供审计定位；下轮独立补marker。
            return ArchiveResult(
                True, result.deleted, "finalization_pending", receipt.size
            )
        return result
    except ArchiveDeferred as error:
        return ArchiveResult(False, False, str(error))
    finally:
        if frozen:
            frozen.close()


def finalize_archive(
    session_factory: SessionFactory,
    storage: S3RunArchive,
    *,
    tenant_id: UUID,
    kind: str,
    run_id: UUID,
) -> bool:
    identity = (tenant_id, kind, run_id)
    with session_factory() as session:
        intent = session.get(SyncArchiveFinalization, identity)
        if intent is None:
            return False
        receipt = ArchiveReceipt(intent.object_key, intent.digest, intent.byte_length)
    storage.finalize(tenant_id=tenant_id, kind=kind, run_id=run_id, receipt=receipt)
    with session_factory() as session:
        session.connection().execute(
            delete(SyncArchiveFinalization).where(
                col(SyncArchiveFinalization.tenant_id) == tenant_id,
                col(SyncArchiveFinalization.kind) == kind,
                col(SyncArchiveFinalization.execution_id) == run_id,
                col(SyncArchiveFinalization.digest) == receipt.sha256,
            )
        )
        session.commit()
    return True


def resolve_archive(
    session: Session,
    storage: S3RunArchive,
    *,
    tenant_id: UUID,
    kind: str,
    run_id: UUID,
    cursor: str | None = None,
    limit: int = 25,
) -> tuple[list[ArchiveReceipt], str | None]:
    """审计UUID先查已提交DB intent，再查持久marker；不暴露未复核候选。"""
    _spec(kind)
    intent = session.get(SyncArchiveFinalization, (tenant_id, kind, run_id))
    if intent:
        return [
            ArchiveReceipt(intent.object_key, intent.digest, intent.byte_length)
        ], None
    return storage.committed(
        tenant_id=tenant_id, kind=kind, run_id=run_id, cursor=cursor, limit=limit
    )
