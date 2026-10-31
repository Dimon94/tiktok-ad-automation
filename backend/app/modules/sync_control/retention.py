"""同步投递正文回收：终态和无待发布子桶均有证据，幂等摘要仍保留。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import DateTime, Uuid, and_, case, cast, func, literal, or_, tuple_
from sqlmodel import Session, col, select

from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import ReportBalanceRun, ReportSyncJob
from app.modules.sync_control.models import SyncExecutionScope

SYNC_TASKS = (
    "reporting.job_step",
    "reporting.backfill_step",
    "ads.sync_step",
    "ads.targeted_step",
    "ads.publish_step",
    "reporting.balance_step",
)
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _legacy_candidates(
    session: Session,
    *,
    limit: int,
    cutoff: datetime,
    cursors: dict[str, list[str]],
    tasks: tuple[str, ...] = SYNC_TASKS,
    published_only: bool = False,
    cursor_prefix: str = "",
) -> list[UUID]:
    """先沿时间索引读窄ID，业务所有权只在有界候选内核验；游标不是删除证明。"""
    identities: list[UUID] = []
    for name in (
        ("published_at",) if published_only else ("published_at", "superseded_at")
    ):
        cursor_key = cursor_prefix + name
        timestamp = getattr(PendingDispatch, name)
        conditions = [
            timestamp <= cutoff,
            col(PendingDispatch.compacted_at).is_(None),
            col(PendingDispatch.dispatch_contract_version).is_(None),
            # 固定合同字面量让长连接的generic plan也能证明部分索引谓词。
            col(PendingDispatch.task_name).in_(
                [literal(task, literal_execute=True) for task in tasks]
            ),
        ]
        if cursor := cursors.get(cursor_key):
            conditions.append(
                tuple_(timestamp, col(PendingDispatch.id))
                > (datetime.fromisoformat(cursor[0]), UUID(cursor[1]))
            )
        rows = session.exec(
            select(timestamp, col(PendingDispatch.id))
            .where(*conditions)
            .order_by(timestamp, col(PendingDispatch.id))
            .limit(limit)
        ).all()
        identities.extend(row[1] for row in rows)
        if len(rows) == limit:
            cursors[cursor_key] = [rows[-1][0].isoformat(), str(rows[-1][1])]
        else:
            # 扫至末尾下一轮从头复查，先前未终态/被锁的任务不会永久遗漏。
            cursors.pop(cursor_key, None)
    return identities


def compact_sync_dispatches(
    session: Session,
    *,
    limit: int,
    cutoff: datetime,
    cursors: dict[str, list[str]] | None = None,
) -> int:
    from app.jobs.outbox import _payload_digest

    if not 1 <= limit <= 5000:
        raise ValueError("invalid sync compaction limit")
    cursors = cursors if cursors is not None else {}
    candidates = _legacy_candidates(
        session,
        limit=limit,
        cutoff=cutoff,
        cursors=cursors,
    )
    total = 0
    specs = (
        (
            ReportSyncJob,
            ("reporting.job_step", "reporting.backfill_step"),
            "job_id",
            SyncExecutionScope.report_job_id,
            ("COMPLETE", "FAILED", "CANCELLED"),
        ),
        (
            AdDirectoryRun,
            ("ads.sync_step", "ads.targeted_step", "ads.publish_step"),
            "run_id",
            SyncExecutionScope.directory_run_id,
            ("COMPLETE", "FAILED", "CANCELLED", "STALE"),
        ),
        (
            ReportBalanceRun,
            ("reporting.balance_step",),
            "run_id",
            SyncExecutionScope.balance_run_id,
            ("COMPLETE", "FAILED", "CANCELLED"),
        ),
    )
    now = datetime.now(UTC)
    for model, tasks, key, execution_column, terminal in specs:
        if total == limit or not candidates:
            break
        raw_id = col(PendingDispatch.payload)[key].astext
        # CASE保证非法旧正文不触发UUID转换错误，join仍可按业务主键定位。
        owner_id = case((raw_id.op("~")(_UUID), cast(raw_id, Uuid)), else_=None)
        pending_child = (
            select(SyncExecutionScope.id)
            .where(
                execution_column == col(model.id),
                col(SyncExecutionScope.status) == "PENDING",
            )
            .exists()
        )
        rows = session.exec(
            select(PendingDispatch)
            .join(
                model,
                and_(
                    col(model.id) == owner_id,
                    col(model.tenant_id) == col(PendingDispatch.tenant_id),
                    col(model.actor_id) == col(PendingDispatch.actor_id),
                ),
            )
            .where(
                # 候选边界必须约束外层；放进NOT EXISTS会扩大扫描并绕过子任务保护。
                col(PendingDispatch.id).in_(candidates),
                or_(
                    col(PendingDispatch.published_at) <= cutoff,
                    col(PendingDispatch.superseded_at) <= cutoff,
                ),
                col(PendingDispatch.compacted_at).is_(None),
                col(PendingDispatch.dispatch_contract_version).is_(None),
                col(PendingDispatch.task_name).in_(tasks),
                col(model.status).in_(terminal),
                col(model.contract_version) == 2,
                col(model.claim_token).is_(None),
                ~pending_child,
            )
            .order_by(
                func.coalesce(
                    col(PendingDispatch.published_at),
                    col(PendingDispatch.superseded_at),
                ),
                col(PendingDispatch.id),
            )
            .limit(limit - total)
            .with_for_update(of=(PendingDispatch, model), skip_locked=True)
        ).all()
        for row in rows:
            row.payload_digest = row.payload_digest or _payload_digest(row.payload)
            row.payload = {}
            row.compacted_at = now
        total += len(rows)
    # 精确纯唤醒没有业务正文所有权；旧request/health前缀只压缩正文，
    # 不补造有效期或删除幂等摘要。普通规划任务及附带其它参数的消息保持原样。
    if total < limit:
        # JSON合同核验必须在窄ID页之后，不能为了凑满一页重扫全部旧唤醒正文。
        wake_candidates = _legacy_candidates(
            session,
            limit=limit - total,
            cutoff=cutoff,
            cursors=cursors,
            tasks=("reporting.plan_due",),
            published_only=True,
            cursor_prefix="plan_due:",
        )
        wakes = session.exec(
            select(PendingDispatch)
            .where(
                col(PendingDispatch.id).in_(wake_candidates),
                col(PendingDispatch.task_name)
                == literal("reporting.plan_due", literal_execute=True),
                or_(
                    col(PendingDispatch.task_key).startswith("sync-directory-wake:"),
                    col(PendingDispatch.task_key).startswith(
                        "directory-request-refill:"
                    ),
                    col(PendingDispatch.task_key).startswith("sync-health:"),
                ),
                col(PendingDispatch.payload) == {"dispatch_only": True},
                col(PendingDispatch.published_at) <= cutoff,
                col(PendingDispatch.compacted_at).is_(None),
                col(PendingDispatch.dispatch_contract_version).is_(None),
            )
            .order_by(col(PendingDispatch.published_at), col(PendingDispatch.id))
            .limit(limit - total)
            .with_for_update(skip_locked=True)
        ).all()
        for row in wakes:
            row.payload_digest = row.payload_digest or _payload_digest(row.payload)
            row.payload = {}
            row.compacted_at = now
        total += len(wakes)
    if total < limit:
        continuation_candidates = _legacy_candidates(
            session,
            limit=limit - total,
            cutoff=cutoff,
            cursors=cursors,
            tasks=("reporting.plan_continue",),
            published_only=True,
            cursor_prefix="plan_continue:",
        )
        # 旧规划续点自身已带30秒绝对期限。只清精确合同且期限也已超过保留期的
        # 已发送正文；非法/无时区时间不做强制转换，不补造V2身份或删除旧摘要。
        payload = col(PendingDispatch.payload)
        raw_id, raw_deadline = (
            payload["continuation_id"].astext,
            payload["expires_at"].astext,
        )
        deadline = case(
            (
                func.pg_input_is_valid(raw_deadline, "timestamp with time zone"),
                cast(raw_deadline, DateTime(timezone=True)),
            ),
            else_=None,
        )
        rows = session.exec(
            select(PendingDispatch)
            .where(
                col(PendingDispatch.id).in_(continuation_candidates),
                col(PendingDispatch.task_name)
                == literal("reporting.plan_continue", literal_execute=True),
                col(PendingDispatch.task_key) == "report-planning-continue:" + raw_id,
                raw_id.op("~")(_UUID),
                raw_deadline.op("~")(r"(Z|[+-][0-9]{2}:[0-9]{2})$"),
                payload.op("-")("continuation_id").op("-")("expires_at") == {},
                deadline <= cutoff,
                col(PendingDispatch.published_at) <= cutoff,
                col(PendingDispatch.compacted_at).is_(None),
                col(PendingDispatch.dispatch_contract_version).is_(None),
            )
            .order_by(col(PendingDispatch.published_at), col(PendingDispatch.id))
            .limit(limit - total)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.payload_digest = row.payload_digest or _payload_digest(row.payload)
            row.payload = {}
            row.compacted_at = now
        total += len(rows)
    session.flush()
    return total


def prune_sync_dispatches(
    session: Session, *, now: datetime, limit: int = 1000
) -> dict[str, int]:
    """只治理显式V2寿命；先退役，再7天正文/30天摘要，小批次不删除业务需求。"""
    from app.jobs.outbox import _payload_digest
    from app.modules.sync_control.dispatch_envelope import can_retire_dispatch

    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("invalid sync retention limit")
    result = {"retired": 0, "compacted": 0, "deleted": 0}
    # 先不锁outbox：统一执行→消息的锁顺序，与canonical生产者一致。
    candidates = session.exec(
        select(PendingDispatch)
        .where(
            col(PendingDispatch.dispatch_contract_version) == 2,
            col(PendingDispatch.retired_at).is_(None),
            col(PendingDispatch.valid_until) <= now,
        )
        .order_by(col(PendingDispatch.valid_until), col(PendingDispatch.id))
        .limit(limit)
    ).all()
    for candidate in candidates:
        if not can_retire_dispatch(session, candidate, now=now):
            continue
        row = session.exec(
            select(PendingDispatch)
            .where(
                col(PendingDispatch.id) == candidate.id,
                col(PendingDispatch.retired_at).is_(None),
                col(PendingDispatch.valid_until) <= now,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).first()
        if row is not None:
            row.retired_at = now
            result["retired"] += 1
    remaining = limit - sum(result.values())
    if remaining:
        rows = session.exec(
            select(PendingDispatch)
            .where(
                col(PendingDispatch.dispatch_contract_version) == 2,
                col(PendingDispatch.retired_at) <= now - timedelta(days=7),
                col(PendingDispatch.valid_until) <= now,
                col(PendingDispatch.compacted_at).is_(None),
            )
            .order_by(col(PendingDispatch.retired_at), col(PendingDispatch.id))
            .limit(remaining)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.payload_digest = row.payload_digest or _payload_digest(row.payload)
            row.payload = {}
            row.compacted_at = now
        result["compacted"] = len(rows)
    remaining = limit - sum(result.values())
    if remaining:
        rows = session.exec(
            select(PendingDispatch)
            .where(
                col(PendingDispatch.dispatch_contract_version) == 2,
                col(PendingDispatch.retired_at) <= now - timedelta(days=30),
                col(PendingDispatch.valid_until) <= now,
                col(PendingDispatch.compacted_at).is_not(None),
            )
            .order_by(col(PendingDispatch.retired_at), col(PendingDispatch.id))
            .limit(remaining)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            session.delete(row)
        result["deleted"] = len(rows)
    session.flush()
    return result
