"""仅canonical V2同步消息的不可续期投递身份；业务正文和平台准入保持原合同。"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import (
    _payload_digest,
    enqueue_after_commit,
    validate_dispatch_payload,
)
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import ReportBalanceRun, ReportSyncJob
from app.modules.sync_control.models import SyncSchedule

SYNC_EXECUTION_TASKS = frozenset(
    {
        "reporting.job_step",
        "reporting.backfill_step",
        "reporting.balance_step",
        "ads.sync_step",
        "ads.targeted_step",
        "ads.publish_step",
    }
)
LIFETIME = timedelta(days=7)
Execution = AdDirectoryRun | ReportBalanceRun | ReportSyncJob


def _execution(
    session: Session, task_name: str, payload: dict[str, Any], *, lock: bool = False
) -> Execution | None:
    if task_name not in SYNC_EXECUTION_TASKS:
        return None
    model: Any = (
        ReportSyncJob
        if task_name.startswith("reporting.") and task_name != "reporting.balance_step"
        else ReportBalanceRun
        if task_name == "reporting.balance_step"
        else AdDirectoryRun
    )
    try:
        identity = UUID(str(payload["job_id" if model is ReportSyncJob else "run_id"]))
    except KeyError, ValueError, TypeError:
        return None
    return cast(
        Execution | None,
        session.get(model, identity, with_for_update=lock, populate_existing=True),
    )


def _same_claim(run: Execution, payload: dict[str, Any]) -> bool:
    if isinstance(run, ReportSyncJob):
        return str(run.claim_token) == payload.get("claim_id")
    return run.claim_generation == payload.get("claim_generation") and (
        not isinstance(run, ReportBalanceRun)
        or str(run.claim_token) == payload.get("claim_id")
    )


def legacy_enqueue_allowed(
    session: Session,
    *,
    context: TenantContext,
    task_name: str,
    task_key: str,
    payload: dict[str, Any],
) -> bool:
    """原身份允许幂等回读；摘要已清后，旧producer不能借通用outbox重建V2消息。"""
    if task_name not in SYNC_EXECUTION_TASKS:
        return True
    existing = session.exec(
        select(PendingDispatch.id).where(
            PendingDispatch.tenant_id == context.tenant_id,
            PendingDispatch.task_key == task_key,
        )
    ).first()
    if existing is not None:
        return True
    run = _execution(session, task_name, payload)
    return (
        run is not None
        and run.contract_version != 2
        and run.tenant_id == context.tenant_id
        and run.actor_id == context.actor_id
    )


def planning_continuation_deadline(
    task_name: str, task_key: str, payload: dict[str, Any]
) -> datetime | None:
    """规划续点只有既有30秒绝对期限；不能通过新envelope获得额外七天。"""
    if task_name != "reporting.plan_continue" or set(payload) != {
        "continuation_id",
        "expires_at",
    }:
        return None
    try:
        identity = str(UUID(payload["continuation_id"]))
        deadline = datetime.fromisoformat(payload["expires_at"])
    except ValueError, TypeError, AttributeError:
        return None
    if (
        payload["continuation_id"] != identity
        or task_key != f"report-planning-continue:{identity}"
        or deadline.tzinfo is None
    ):
        return None
    return deadline


def enqueue_sync_dispatch(
    session: Session,
    *,
    context: TenantContext,
    task_name: str,
    task_key: str,
    payload: dict[str, Any],
    execution: Execution | None = None,
    now: datetime | None = None,
) -> UUID:
    """调用者持canonical执行锁；旧代只能复用原消息，不能重新签发有效期。"""
    now = now or datetime.now(UTC)
    payload = validate_dispatch_payload(payload)
    wake = (
        task_name == "reporting.plan_due"
        and task_key.startswith("sync-directory-wake:")
        and payload == {"dispatch_only": True}
    )
    planning_deadline = planning_continuation_deadline(task_name, task_key, payload)
    control = wake or planning_deadline is not None
    run = None if control else _execution(session, task_name, payload, lock=True)
    if not control and (
        run is None
        or execution is None
        or run.id != execution.id
        or run.contract_version != 2
        or run.tenant_id != context.tenant_id
        or run.actor_id != context.actor_id
    ):
        raise DomainError("dispatch_payload_invalid", "同步消息缺少有效canonical执行")
    existing = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == context.tenant_id,
            PendingDispatch.task_key == task_key,
        )
    ).first()
    if existing is not None:
        if (
            existing.dispatch_contract_version != 2
            or existing.task_name != task_name
            or existing.actor_id != context.actor_id
            or existing.payload_digest != _payload_digest(payload)
        ):
            raise DomainError("dispatch_key_conflict", "同步投递身份不一致")
        return existing.id
    if planning_deadline is not None and planning_deadline <= now:
        raise DomainError("dispatch_expired", "规划续点已过期")
    if planning_deadline is not None and planning_deadline > now + timedelta(
        seconds=30
    ):
        raise DomainError("dispatch_payload_invalid", "规划续点期限超出原30秒")
    if run is not None:
        head = session.exec(
            select(SyncSchedule.scope_id)
            .where(
                col(SyncSchedule.tenant_id) == run.tenant_id,
                col(SyncSchedule.lease_execution_id) == run.id,
                col(SyncSchedule.lease_execution_generation) == run.claim_generation,
                col(SyncSchedule.leased_until) > now,
            )
            .limit(1)
        ).first()
        if (
            head is None
            or run.status in {"COMPLETE", "FAILED", "CANCELLED", "STALE"}
            or not _same_claim(run, payload)
        ):
            raise DomainError("dispatch_payload_invalid", "旧同步领取不能重新签发消息")
    identity = enqueue_after_commit(
        session,
        context=context,
        task_name=task_name,
        task_key=task_key,
        payload=payload,
        _sync_contract=True,
    )
    row = session.get(PendingDispatch, identity)
    assert row is not None
    row.dispatch_contract_version = 2
    row.issued_at = now
    row.valid_until = planning_deadline or now + LIFETIME
    session.flush()
    return identity


def dispatch_envelope(row: PendingDispatch) -> dict[str, Any] | None:
    if row.dispatch_contract_version != 2:
        return None
    assert row.issued_at is not None and row.valid_until is not None
    return {
        "id": str(row.id),
        "version": 2,
        "issued_at": row.issued_at.isoformat(),
        "valid_until": row.valid_until.isoformat(),
    }


def consume_sync_dispatch(
    *,
    database_engine: Any,
    task_name: str,
    tenant_id: str,
    actor_id: str,
    payload: dict[str, Any],
    dispatch: dict[str, Any] | None,
) -> bool:
    """在成本登记/业务领取之前核验；缺失身份绝不凭迟到正文复活运行。"""
    with Session(database_engine) as session:
        if dispatch is None:
            # 既有legacy任务不套TTL；已接管V2运行必须携带明确的新合同。
            if task_name == "reporting.plan_due":
                return True
            if task_name == "reporting.plan_continue":
                key = f"report-planning-continue:{payload.get('continuation_id')}"
                deadline = planning_continuation_deadline(task_name, key, payload)
                if deadline is None or deadline <= datetime.now(UTC):
                    return False
                try:
                    tenant_uuid, actor_uuid = UUID(tenant_id), UUID(actor_id)
                except ValueError, TypeError:
                    return False
                legacy = session.exec(
                    select(PendingDispatch).where(
                        col(PendingDispatch.tenant_id) == tenant_uuid,
                        col(PendingDispatch.actor_id) == actor_uuid,
                        col(PendingDispatch.task_name) == task_name,
                        col(PendingDispatch.task_key) == key,
                        col(PendingDispatch.dispatch_contract_version).is_(None),
                        col(PendingDispatch.superseded_at).is_(None),
                    )
                ).first()
                return legacy is not None and legacy.payload_digest == _payload_digest(
                    payload
                )
            run = _execution(session, task_name, payload)
            return run is not None and run.contract_version != 2
        try:
            if (
                set(dispatch) != {"id", "version", "issued_at", "valid_until"}
                or type(dispatch["version"]) is not int
                or dispatch["version"] != 2
            ):
                return False
            identity = UUID(dispatch["id"])
            issued = datetime.fromisoformat(dispatch["issued_at"])
            until = datetime.fromisoformat(dispatch["valid_until"])
            if issued.tzinfo is None or until.tzinfo is None:
                return False
        except ValueError, TypeError, KeyError:
            return False
        row = session.get(PendingDispatch, identity)
        now = datetime.now(UTC)
        if (
            row is None
            or row.dispatch_contract_version != 2
            or str(row.tenant_id) != tenant_id
            or str(row.actor_id) != actor_id
            or row.task_name != task_name
            or row.issued_at != issued
            or row.valid_until != until
            or row.retired_at is not None
            or row.superseded_at is not None
            or until <= now
            or row.payload_digest != _payload_digest(payload)
        ):
            return False
        if task_name == "reporting.plan_due":
            return row.task_key.startswith("sync-directory-wake:") and payload == {
                "dispatch_only": True
            }
        if task_name == "reporting.plan_continue":
            return (
                planning_continuation_deadline(task_name, row.task_key, payload)
                == row.valid_until
            )
        run = _execution(session, task_name, payload)
        return (
            run is not None
            and run.contract_version == 2
            and str(run.tenant_id) == tenant_id
            and str(run.actor_id) == actor_id
            and _same_claim(run, payload)
        )


def can_retire_dispatch(
    session: Session, row: PendingDispatch, *, now: datetime
) -> bool:
    """消息寿命到期后仍不抢同代在途worker；过期需求由原head重新领取。"""
    if row.valid_until is None or row.valid_until > now:
        return False
    if row.task_name == "reporting.plan_due":
        return row.task_key.startswith("sync-directory-wake:") and row.payload == {
            "dispatch_only": True
        }
    if row.task_name == "reporting.plan_continue":
        return (
            planning_continuation_deadline(row.task_name, row.task_key, row.payload)
            == row.valid_until
        )
    run = _execution(session, row.task_name, row.payload, lock=True)
    if run is not None and run.claimed_until is not None and run.claimed_until > now:
        # 报表/余额消费会旋转queued token；token变化不代表同代worker已结束。
        # 报表旧payload没有generation，因此对任何该run的有效worker保守保留。
        if isinstance(run, ReportSyncJob) or payload_generation_matches(
            run, row.payload
        ):
            return False
    return True


def payload_generation_matches(run: Execution, payload: dict[str, Any]) -> bool:
    return run.claim_generation == payload.get("claim_generation")
