"""较新的待执行补链已完整承接需求时，取消同范围的旧执行，保留续步。"""

import json
from datetime import datetime
from time import monotonic
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from app.modules.sync_control.report_demand import _strictly_unstarted
from app.modules.sync_control.schedule import record_report_outcome

IDENTITY_FIELDS = (
    "tenant_id",
    "bc_id",
    "actor_id",
    "owner_epoch",
    "kind",
    "start_date",
    "end_date",
    "advertiser_ids",
    "campaign_ids",
    "frozen_route",
)
MAX_REFERENCES = 512


class _Skip(Exception):
    pass


def _require(condition: bool) -> None:
    if not condition:
        raise _Skip


def _compact(
    session: Session, *, job_id: UUID, now: datetime, replacement_id: UUID | None
) -> UUID:
    old = session.get(ReportSyncJob, job_id, populate_existing=True)
    _require(old is not None)
    assert old is not None
    _require(old.contract_version == 2 and old.kind == "material_links")
    _require(old.status in {"QUEUED", "WAITING"})
    _require(old.claim_token is None and old.claimed_until is None)
    # 先找最新活跃执行；正在运行或承接不完整时整组跳过，不能退选次新执行。
    predicates = [
        getattr(ReportSyncJob, field) == getattr(old, field)
        for field in IDENTITY_FIELDS
    ]

    def latest_id() -> UUID | None:
        # 分开使用 pending-scope 与 running 部分索引，避免 OR 扩大全表扫描。
        candidates = []
        for statuses in (["QUEUED", "WAITING"], ["RUNNING"]):
            row = session.exec(
                select(ReportSyncJob.created_at, ReportSyncJob.id)
                .where(
                    *predicates,
                    col(ReportSyncJob.contract_version) == 2,
                    col(ReportSyncJob.status).in_(statuses),
                )
                .order_by(
                    col(ReportSyncJob.created_at).desc(), col(ReportSyncJob.id).desc()
                )
                .limit(1)
            ).first()
            if row is not None:
                candidates.append((row[0], row[1]))
        return max(candidates)[1] if candidates else None

    identity = latest_id()
    newest = session.get(ReportSyncJob, identity) if identity is not None else None
    _require(newest is not None and newest.id != old.id)
    assert newest is not None
    newest_id = newest.id
    _require(replacement_id is None or newest_id == replacement_id)
    locked = session.exec(
        select(ReportSyncJob)
        .where(col(ReportSyncJob.id).in_([old.id, newest_id]))
        .order_by(col(ReportSyncJob.id))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    _require(len(locked) == 2)
    by_id = {job.id: job for job in locked}
    old, newer = by_id[job_id], by_id[newest_id]
    _require(
        all(getattr(old, field) == getattr(newer, field) for field in IDENTITY_FIELDS)
    )
    _require((newer.created_at, newer.id) > (old.created_at, old.id))
    _require(old.contract_version == newer.contract_version == 2)
    _require(
        old.status in {"QUEUED", "WAITING"}
        and old.claim_token is None
        and old.claimed_until is None
    )
    _require(newer.status in {"QUEUED", "WAITING"})
    _require(newer.claim_token is None and newer.claimed_until is None)
    _require(newer.due_at <= max(old.due_at, now))
    if newer.claim_generation == 0:
        # 零代数仍必须是真正未开始，不能把不明观察/缓存伪装成新任务。
        _require(newer.status == "QUEUED" and newer.request_count == 0)
        _require(
            session.exec(
                select(ReportSyncJob.id).where(
                    col(ReportSyncJob.id) == newer.id, *_strictly_unstarted()
                )
            ).first()
            == newer.id
        )
    else:
        # 已续步的等待任务可承接重复执行；其进度与冻结引用只读，不重置重试。
        # RUNNING、父租约和有效调度头租约仍由独立保护拒绝，不触碰在途任务。
        _require(newer.claim_generation > 0)
    completion_observed_at = now
    if "snapshot_observed_at" in newer.progress:
        # 正常完成优先使用父任务冻结快照时间；不能只看子引用的版本/观察字段。
        # 旧快照即使保留了新引用，也可能 COMPLETE 而无法满足冻结需求。
        try:
            completion_observed_at = datetime.fromisoformat(
                newer.progress["snapshot_observed_at"]
            )
        except (TypeError, ValueError):
            raise _Skip from None
        _require(
            completion_observed_at.tzinfo is not None
            and completion_observed_at.utcoffset() is not None
            and completion_observed_at <= now
        )
    # 完整读取两边引用，超限整组跳过；不以截断的前512条证明整个旧执行可删。
    refs = session.exec(
        select(SyncExecutionScope)
        .where(col(SyncExecutionScope.report_job_id).in_([old.id, newer.id]))
        .order_by(
            col(SyncExecutionScope.scope_id),
            col(SyncExecutionScope.requirement_id),
            col(SyncExecutionScope.id),
        )
        .limit(MAX_REFERENCES * 2 + 1)
    ).all()
    _require(0 < len(refs) <= MAX_REFERENCES * 2)
    _require(
        all(
            sum(ref.report_job_id == identity for ref in refs) <= MAX_REFERENCES
            for identity in [old.id, newer.id]
        )
    )
    scope_ids = {ref.scope_id for ref in refs}
    scopes = session.exec(
        select(SyncScope)
        .where(col(SyncScope.id).in_(scope_ids))
        .order_by(
            col(SyncScope.tenant_id), col(SyncScope.advertiser_id), col(SyncScope.id)
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    _require(len(scopes) == len(scope_ids))
    _require(
        all(
            scope.tenant_id == old.tenant_id
            and scope.actor_id == old.actor_id
            and scope.bc_id == old.bc_id
            and scope.owner_epoch == old.owner_epoch
            and scope.frozen_route == old.frozen_route
            for scope in scopes
        )
    )
    locked_refs = session.exec(
        select(SyncExecutionScope)
        .where(col(SyncExecutionScope.id).in_([ref.id for ref in refs]))
        .order_by(
            col(SyncExecutionScope.scope_id),
            col(SyncExecutionScope.requirement_id),
            col(SyncExecutionScope.id),
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    _require(len(locked_refs) == len(refs))
    requirement_ids = {ref.requirement_id for ref in locked_refs}
    requirements = session.exec(
        select(SyncRequirement)
        .where(col(SyncRequirement.id).in_(requirement_ids))
        .order_by(col(SyncRequirement.id))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    _require(len(requirements) == len(requirement_ids))
    requirement_by_id = {r.id: r for r in requirements}
    expected_heads = session.exec(
        select(SyncSchedule).where(col(SyncSchedule.scope_id).in_(scope_ids))
    ).all()
    heads = session.exec(
        select(SyncSchedule)
        .where(col(SyncSchedule.scope_id).in_(scope_ids))
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    _require(len(heads) == len(expected_heads))
    _require(
        not any(
            h.lease_execution_id in {old.id, newer.id}
            and h.leased_until is not None
            and h.leased_until > now
            for h in heads
        )
    )
    # scope与requirement锁也阻止正常生产者在证明期间更新需求/插入承接引用。
    latest = latest_id()
    _require(latest == newer.id)
    pending = [
        r for r in locked_refs if r.report_job_id == old.id and r.status == "PENDING"
    ]
    successors = {
        r.requirement_id: r
        for r in locked_refs
        if r.report_job_id == newer.id and r.status == "PENDING"
    }
    _require(bool(pending))
    for child in pending:
        successor = successors.get(child.requirement_id)
        _require(successor is not None)
        assert successor is not None
        requirement = requirement_by_id[child.requirement_id]
        _require(
            child.tenant_id
            == successor.tenant_id
            == requirement.tenant_id
            == old.tenant_id
        )
        _require(child.scope_id == successor.scope_id == requirement.scope_id)
        _require(child.owner_epoch == successor.owner_epoch == old.owner_epoch)
        _require(
            child.execution_generation == old.claim_generation
            and successor.execution_generation == newer.claim_generation
        )
        _require(successor.published_version is None)
        if newer.claim_generation == 0:
            _require(successor.observed_at is None)
        _require(
            successor.frozen_revision >= child.frozen_revision
            and successor.frozen_requested_after >= child.frozen_requested_after
        )
        _require(completion_observed_at >= successor.frozen_requested_after)
        _require(requirement.desired_revision >= successor.frozen_revision)
    before = {
        r.id: (r.desired_revision, r.satisfied_revision, r.requested_after)
        for r in requirements
    }
    for child in pending:
        child.status = "SUPERSEDED"
    old.status, old.error_code, old.finished_at = "CANCELLED", "pending_superseded", now
    old.claim_token = old.claimed_until = None
    session.flush()
    record_report_outcome(session, old)
    session.flush()
    _require(
        before
        == {
            r.id: (r.desired_revision, r.satisfied_revision, r.requested_after)
            for r in requirements
        }
    )
    return newer.id


def supersede_pending_report(
    session: Session,
    *,
    job_id: UUID,
    now: datetime,
    replacement_id: UUID | None = None,
) -> UUID | None:
    """调用方独立短事务提交/回滚；任一条件不足即释放本次全部锁。"""
    try:
        with session.begin_nested():
            session.execute(text("SET LOCAL transaction_timeout='6s'"))
            session.execute(text("SET LOCAL statement_timeout='3s'"))
            session.execute(text("SET LOCAL lock_timeout='100ms'"))
            return _compact(
                session, job_id=job_id, now=now, replacement_id=replacement_id
            )
    except _Skip:
        return None


def discover_pending_report_pairs(
    session: Session, *, limit: int = 5000
) -> tuple[list[tuple[UUID, UUID]], bool]:
    """只读发现相同业务范围；摘要仅筛候选，取消前仍逐字段、逐引用复验。"""
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be 1..10000")
    # 使用现有 active job_key 部分唯一索引游标分页，避免全表聚合拖慢生产库。
    groups: dict[str, list[tuple[datetime, UUID]]] = {}
    cursor = ""
    scanned = 0
    exhausted = False
    deadline = monotonic() + 20
    columns = ",".join("j." + name for name in IDENTITY_FIELDS)
    while monotonic() < deadline and scanned < 50000:
        rows = (
            session.execute(
                text(
                    "SELECT j.id,j.job_key,j.created_at,j.contract_version,"
                    + columns
                    + " FROM report_sync_job j"
                    " WHERE j.status IN ('QUEUED','WAITING','RUNNING')"
                    " AND j.job_key>:cursor ORDER BY j.job_key LIMIT 256"
                ),
                {"cursor": cursor},
            )
            .mappings()
            .all()
        )
        scanned += len(rows)
        for row in rows:
            if row["kind"] != "material_links" or row["contract_version"] != 2:
                continue
            key = json.dumps(
                [row[name] for name in IDENTITY_FIELDS], sort_keys=True, default=str
            )
            groups.setdefault(key, []).append((row["created_at"], row["id"]))
        if len(rows) < 256:
            exhausted = True
            break
        cursor = rows[-1]["job_key"]
    pairs: list[tuple[UUID, UUID]] = []
    for group in groups.values():
        group.sort(reverse=True)
        pairs.extend((old_id, group[0][1]) for _, old_id in group[1:])
    pairs.sort()
    # 不完整扫描明确标记；发现结果只是候选，执行时仍查真实最新任务并完整复验。
    return pairs[:limit], not exhausted or len(pairs) > limit
