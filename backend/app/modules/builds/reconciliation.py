"""One bounded read per delivery; immutable intent, scoped leases, append-only facts.

The caller owns scheduling/repair. needs_more means schedule another read (never
create). A terminal UNKNOWN needs an explicit later reconciliation request. Page
IDs live in per-page evidence; the mutable cursor and candidate remain bounded.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.adapters.build_results import known_no_effect_rejection
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.builds import (
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    CreateIntent,
)
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.builds.execution_admission import admitted_build_call
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.readback_compare import ID_KEYS
from app.modules.builds.reconciliation_scan import advance
from app.modules.builds.reconciliation_scan import (
    reconciliation_decision as reconciliation_decision,
)
from app.modules.builds.request_compiler import (
    decode_intent,
    read_arguments,
    remote_request_id,
    status_arguments,
)
from app.modules.builds.route_models import BuildAttemptContext
from app.modules.builds.routes import save_attempt_context, verify_unit_route
from app.modules.tenants.permissions import require_tenant

HARD_LIMIT = 45
CLAIM_SECONDS = 60
EMPTY_READBACK_DELAYS = (15, 30, 60)
SAFE_ERRORS = frozenset(
    {
        "action_forbidden",
        "tenant_unavailable",
        "account_not_in_bc",
        "account_ownership_conflict",
        "account_metadata_incomplete",
        "account_access_denied",
        "account_authorization_changed",
        "connection_unavailable",
        "credential_invalid",
        "tiktok_app_not_configured",
        "tiktok_app_incomplete",
        "connection_encryption_unconfigured",
        "admission_unconfigured",
        "admission_policy_invalid",
        "admission_unavailable",
        "readback_response_unknown",
        "readback_intent_incomplete",
        "route_authorization_changed",
        "route_contract_changed",
        "route_evidence_stale",
        "mcp_refresh_pending",
        "mcp_refresh_unknown",
        "mcp_refresh_reauth_required",
        "gateway_credentials_changed",
        "execution_lease_lost",
        "legacy_route_unverifiable",
    }
)


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    needs_more: bool = False
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class _Claim:
    step_id: UUID
    source_id: UUID
    tenant_id: UUID
    submission_id: UUID
    token: UUID
    attempt: int
    attempt_id: UUID
    revision: int
    source_attempt: int
    source_attempt_id: UUID | None
    source_revision: int
    connection_id: UUID
    advertiser_id: str
    bc_id: str
    kind: str
    body: dict[str, Any]
    digest: str
    progress: dict[str, Any]
    route: FrozenTikTokRoute
    intent: CreateIntent
    native_series: bool
    lease_expires_at: datetime


def require_bounded_worker() -> None:
    process = current_process()
    request = getattr(current_task, "request", None)
    limits = getattr(request, "timelimit", None)
    hard = limits[0] if isinstance(limits, (tuple, list)) and limits else None
    if hard is None:
        hard = getattr(current_task, "time_limit", None)
    if (
        not request
        or getattr(request, "called_directly", True)
        or getattr(request, "is_eager", True)
        or not process.daemon
        or not process.name.startswith("ForkPoolWorker-")
        or isinstance(hard, bool)
        or not isinstance(hard, (int, float))
        or not 0 < hard <= HARD_LIMIT
    ):
        raise DomainError("reconciliation_worker_unbounded", "结果核查需要有界后台任务")


def _locked(
    session: Session, context: TenantContext, step_id: UUID
) -> tuple[ExecutionStep, ExecutionStep, BuildUnit]:
    header = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.id == step_id, ExecutionStep.tenant_id == context.tenant_id
        )
    ).one_or_none()
    if header is None:
        raise DomainError("resource_not_found", "执行步骤不存在")
    source_id = header.parent_step_id if header.kind == "READBACK" else header.id
    source = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.id == source_id, ExecutionStep.tenant_id == context.tenant_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    step = (
        source
        if source_id == step_id
        else session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.id == step_id,
                ExecutionStep.tenant_id == context.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
    )
    if (
        source is None
        or step is None
        or source.kind not in ID_KEYS
        or source.submission_id != step.submission_id
        or source.unit_id != step.unit_id
    ):
        raise DomainError("resource_not_found", "执行步骤不存在")
    submission = session.get(Submission, step.submission_id)
    if submission is None or submission.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    unit = session.get(BuildUnit, step.unit_id)
    if (
        unit is None
        or unit.tenant_id != context.tenant_id
        or unit.preview_id != step.preview_id
    ):
        raise DomainError("resource_not_found", "执行组合不存在")
    return step, source, unit


def nonempty(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _authorize(
    session: Session, context: TenantContext, unit: BuildUnit, bc_id: str
) -> FrozenTikTokRoute:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    route = verify_unit_route(session, context=context, unit=unit, capability="read")
    if route.bc_id != bc_id:
        raise DomainError("account_authorization_changed", "冻结账户授权已变化")
    return route


def original_create_attempt(
    session: Session, source: ExecutionStep
) -> tuple[int, UUID]:
    rows = session.exec(
        select(BuildAttemptContext.attempt, BuildAttemptContext.attempt_id)
        .join(
            StepEvidence,
            (col(StepEvidence.tenant_id) == col(BuildAttemptContext.tenant_id))
            & (col(StepEvidence.step_id) == col(BuildAttemptContext.step_id))
            & (col(StepEvidence.attempt) == col(BuildAttemptContext.attempt)),
        )
        .where(
            BuildAttemptContext.tenant_id == source.tenant_id,
            BuildAttemptContext.step_id == source.id,
            StepEvidence.submission_id == source.submission_id,
            StepEvidence.conclusion == "REQUEST_ARMED",
            StepEvidence.summary["body_digest"].astext == source.request_body_digest,
        )
        .distinct()
        .limit(2)
    ).all()
    if len(rows) != 1:
        raise DomainError("readback_intent_incomplete", "原创建尝试证据不完整")
    return rows[0][0], rows[0][1]


def source_intent(
    session: Session, source: ExecutionStep, route: FrozenTikTokRoute
) -> CreateIntent:
    body = dict(source.request_body or {})
    # request_id 是传输信封字段，不属于冻结的业务创建意图。官方 API
    # 同样会把它保存进请求回执；回读重新解码前必须剥离，否则严格契约
    # 会把一次可安全核查的请求误判为 intent 不完整。
    request_id = body.pop("request_id", None)
    if route.channel == "OFFICIAL_MCP" and source.kind in {"CAMPAIGN", "ADGROUP"}:
        _, original_id = original_create_attempt(session, source)
        # 历史 UUID 请求仅允许读取核查；新发送一律使用官方 int64 格式。
        if request_id not in {
            remote_request_id(original_id),
            str(original_id),
        }:
            raise DomainError("readback_intent_incomplete", "原创建关联标识不匹配")
    try:
        return decode_intent(source.kind, body)
    except TypeError, ValueError:
        raise DomainError("readback_intent_incomplete", "原创建字段不完整") from None


def _known_ids(session: Session, source: ExecutionStep) -> set[str]:
    # At most two distinct receipts suffice to reject ambiguity. Never choose the
    # first late response when another attempt reports a conflicting object ID.
    values = session.exec(
        select(StepEvidence.summary["remote_id"].astext)
        .where(
            StepEvidence.tenant_id == source.tenant_id,
            StepEvidence.submission_id == source.submission_id,
            StepEvidence.step_id == source.id,
            col(StepEvidence.conclusion).in_(("CREATED", "LATE_CREATED", "RECONCILED")),
            StepEvidence.summary["remote_id"].astext.is_not(None),
        )
        .distinct()
        .limit(2)
    ).all()
    result = {value for value in values if nonempty(value)}
    if nonempty(source.remote_id):
        result.add(str(source.remote_id))
    return result


def _reusable_cta(
    session: Session, source: ExecutionStep, unit: BuildUnit
) -> ExecutionStep | None:
    """同一冻结预览、账户和连接内找完整相同的 CTA；候选还必须远端回读。"""
    candidates = session.exec(
        select(ExecutionStep)
        .join(
            BuildUnit,
            (col(BuildUnit.id) == ExecutionStep.unit_id)
            & (col(BuildUnit.tenant_id) == ExecutionStep.tenant_id),
        )
        .where(
            ExecutionStep.tenant_id == source.tenant_id,
            ExecutionStep.preview_id == source.preview_id,
            ExecutionStep.bc_id == source.bc_id,
            ExecutionStep.kind == "CTA",
            ExecutionStep.status == "SUCCEEDED",
            col(ExecutionStep.mismatch).is_(False),
            ExecutionStep.request_body_digest == source.request_body_digest,
            col(ExecutionStep.remote_id).is_not(None),
            BuildUnit.advertiser_id == unit.advertiser_id,
            BuildUnit.connection_id == unit.connection_id,
        )
        .order_by(col(ExecutionStep.id))
        .limit(20)
    ).all()
    for candidate in candidates:
        if candidate.request_body == source.request_body and _known_ids(
            session, candidate
        ) == {candidate.remote_id}:
            receipt = session.exec(
                select(StepEvidence.id)
                .where(
                    StepEvidence.tenant_id == source.tenant_id,
                    StepEvidence.step_id == candidate.id,
                    col(StepEvidence.conclusion).in_(("CREATED", "RECONCILED")),
                    StepEvidence.summary["remote_id"].astext == candidate.remote_id,
                )
                .limit(1)
            ).first()
            if receipt is not None:
                return candidate
    return None


def _evidence(
    session: Session,
    step: ExecutionStep,
    *,
    conclusion: str,
    summary: dict[str, Any],
    claim: _Claim | None = None,
    request_id: str | None = None,
) -> None:
    # 回读步骤和被核查的创建步骤具有独立计数，不能把两个 step 的 UUID 串联。
    count, identity = step.attempt, None
    if claim is not None:
        if step.id == claim.step_id:
            count, identity = claim.attempt, claim.attempt_id
        elif step.id == claim.source_id:
            count, identity = claim.source_attempt, claim.source_attempt_id
        else:
            raise DomainError("execution_attempt_changed", "证据不属于当前核查步骤")
    save_attempt_context(session, step=step, attempt=count, expected_id=identity)
    session.add(
        StepEvidence(
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            step_id=step.id,
            attempt=count,
            lease_token=claim.token if claim else step.lease_token,
            conclusion=conclusion,
            summary=summary,
            request_id=request_id,
        )
    )


def _unknown(step: ExecutionStep, code: str) -> None:
    if step.status != "SUCCEEDED" or not nonempty(step.remote_id):
        step.status = "UNKNOWN"
    else:
        step.mismatch = True
    step.error_code, step.updated_at = code, datetime.now(UTC)


def _recover_known_rejection(
    session: Session, *, step: ExecutionStep, source: ExecutionStep
) -> ReconciliationResult | None:
    """Close historical UNKNOWN rows when the original receipt already proved rejection."""
    if source.status != "UNKNOWN" or source.kind not in ID_KEYS:
        return None
    evidence_row = session.exec(
        select(StepEvidence)
        .where(
            StepEvidence.tenant_id == source.tenant_id,
            StepEvidence.submission_id == source.submission_id,
            StepEvidence.step_id == source.id,
            StepEvidence.conclusion == "RESULT_UNKNOWN",
        )
        .order_by(col(StepEvidence.observed_at).desc())
        .limit(1)
    ).first()
    summary = evidence_row.summary if evidence_row is not None else {}
    remote_code = summary.get("remote_code") if isinstance(summary, dict) else None
    # 历史41021保留原RESULT_UNKNOWN审计，走完整缺失回查及原request_id重放；
    # 不能只改为FAILED，否则历史未知证据会阻止安全重试。
    if remote_code == 41021:
        return None
    if not known_no_effect_rejection(source.kind, remote_code):
        return None
    now = datetime.now(UTC)
    source.status, source.phase = "FAILED", "DONE"
    source.error_code = "tiktok_business_error"
    source.lease_token = source.lease_expires_at = None
    source.due_at = now
    source.checked_at = now
    source.updated_at = now
    session.add(source)
    if step.id != source.id and step.status not in {"SUCCEEDED", "FAILED"}:
        step.status, step.phase = "FAILED", "DONE"
        step.error_code = "tiktok_business_error"
        step.lease_token = step.lease_expires_at = None
        step.due_at = now
        step.checked_at = now
        step.updated_at = now
        session.add(step)
    _evidence(
        session,
        source,
        conclusion="REMOTE_REJECTED_RECOVERED",
        summary={
            "reason_code": "tiktok_business_error",
            "remote_code": remote_code,
            "recovered_from": "RESULT_UNKNOWN",
        },
        request_id=evidence_row.request_id if evidence_row is not None else None,
    )
    session.flush()
    return ReconciliationResult("FAILED")


def _claim(
    session: Session, context: TenantContext, step_id: UUID, revision: int
) -> _Claim | ReconciliationResult:
    step, source, unit = _locked(session, context, step_id)
    from app.modules.builds.corrections import is_resolved
    from app.modules.builds.receipt_completion import is_obsolete_readback

    if is_resolved(session, source):
        return ReconciliationResult("VERIFIED_REPLACEMENT")
    now = datetime.now(UTC)
    if step.dispatch_revision != revision:
        return ReconciliationResult("STALE")
    delivered = step.resolved.get("reconciliation_delivery")
    if is_obsolete_readback(session, step) and not (
        step.status == "SUCCEEDED"
        and isinstance(delivered, dict)
        and delivered.get("revision") == revision
    ):
        # 旧消息只结束投递；不写 SUCCEEDED、checked_at 或远端核实证据。
        return ReconciliationResult("NOT_REQUIRED")
    if isinstance(delivered, dict) and delivered.get("revision") == revision:
        return ReconciliationResult(
            delivered["state"],
            delivered["needs_more"],
            delivered["retry_after_seconds"],
        )
    if step.due_at > now:
        return ReconciliationResult(
            "PENDING", True, max(1, int((step.due_at - now).total_seconds()) + 1)
        )
    if any(
        row.lease_expires_at is not None and row.lease_expires_at > now
        for row in (step, source)
    ):
        return ReconciliationResult("BUSY", True, CLAIM_SECONDS)
    recovered = _recover_known_rejection(session, step=step, source=source)
    if recovered is not None:
        return recovered
    if step.kind == "READBACK":
        if source.status != "SUCCEEDED" or not nonempty(source.remote_id):
            return ReconciliationResult("WAITING")
    elif source.status not in {"UNKNOWN", "SUCCEEDED"}:
        return ReconciliationResult("WAITING")
    try:
        route = _authorize(session, context, unit, source.bc_id)
        intent = source_intent(session, source, route)
    except DomainError as error:
        code = (
            error.code
            if error.code in SAFE_ERRORS
            else "readback_authorization_unknown"
        )
        _unknown(step, code)
        _evidence(
            session, step, conclusion="READBACK_BLOCKED", summary={"reason_code": code}
        )
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    body = source.request_body
    if not isinstance(body, dict) or body.get("advertiser_id") != unit.advertiser_id:
        _unknown(step, "readback_intent_incomplete")
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    digest = sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if digest != source.request_body_digest:
        _unknown(step, "readback_intent_incomplete")
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    known = _known_ids(session, source)
    reusable = (
        _reusable_cta(session, source, unit)
        if source.kind == "CTA" and not known
        else None
    )
    if len(known) > 1 or source.kind == "CTA" and not known and reusable is None:
        _unknown(
            step, "conflicting_remote_receipt" if len(known) > 1 else "cta_unknown_id"
        )
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    remote_id = next(iter(known), None) or (reusable.remote_id if reusable else None)
    progress = dict(step.resolved.get("reconciliation") or {})
    if (
        progress.get("digest") != digest
        or progress.get("known_id") != remote_id
        or progress.get("done")
        or not progress.get("scan_id")
    ):
        progress = {
            "scan_id": str(uuid4()),
            "digest": digest,
            "known_id": remote_id,
            "page": 1,
            "seen": 0,
            "matches": 0,
            "stage": "OBJECT",
        }
    if reusable is not None:
        progress["reused_cta_step_id"] = str(reusable.id)
    token = uuid4()
    step.attempt += 1
    attempt_id = save_attempt_context(session, step=step)
    for row in (source, step):
        row.lease_token, row.lease_expires_at = (
            token,
            now + timedelta(seconds=CLAIM_SECONDS),
        )
        row.updated_at = now
        session.add(row)
    if step.kind == "READBACK":
        step.status, step.phase = "RUNNING", "CLAIMED"
    step.resolved = {**step.resolved, "reconciliation": progress}
    session.flush()
    return _Claim(
        step.id,
        source.id,
        step.tenant_id,
        step.submission_id,
        token,
        step.attempt,
        attempt_id,
        revision,
        source.attempt,
        source.attempt_id,
        source.dispatch_revision,
        route.connection_id,
        unit.advertiser_id,
        source.bc_id,
        source.kind,
        dict(body),
        digest,
        progress,
        route,
        intent,
        "native_request" in unit.scene_snapshot,
        now + timedelta(seconds=CLAIM_SECONDS),
    )


def _active(step: ExecutionStep, source: ExecutionStep, claim: _Claim) -> bool:
    now = datetime.now(UTC)
    return (
        all(
            row.lease_token == claim.token
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
            for row in (step, source)
        )
        and step.attempt == claim.attempt
        and step.attempt_id == claim.attempt_id
        and step.dispatch_revision == claim.revision
        and source.attempt == claim.source_attempt
        and source.attempt_id == claim.source_attempt_id
        and source.dispatch_revision == claim.source_revision
    )


def _clear(step: ExecutionStep, source: ExecutionStep) -> None:
    for row in (source, step):
        row.lease_token = row.lease_expires_at = None
        row.updated_at = datetime.now(UTC)


def _advance(
    session: Session, claim: _Claim, page: BuildPage | AdGroupStatus
) -> tuple[dict[str, Any], str]:
    def seen_before(ids: tuple[str, ...]) -> bool:
        return bool(
            SASession.execute(
                session,
                text("""
            SELECT EXISTS(SELECT 1 FROM step_evidence
            WHERE tenant_id=:tenant AND submission_id=:submission AND step_id=:step
            AND conclusion='READBACK_PAGE' AND summary->>'scan_id'=:scan
            AND summary->>'stage'='OBJECT'
            AND (summary->'ids') ?| CAST(:ids AS text[]))
        """),
                {
                    "tenant": claim.tenant_id,
                    "submission": claim.submission_id,
                    "step": claim.step_id,
                    "scan": claim.progress["scan_id"],
                    "ids": list(ids),
                },
            ).scalar_one()
        )

    return advance(
        query=BuildReadQuery(
            intent=claim.intent,
            native_series=claim.native_series,
            remote_id=claim.progress.get("known_id"),
            page=claim.progress["page"],
        ),
        progress=claim.progress,
        page=page,
        seen_before=seen_before,
    )


def _remember(
    step: ExecutionStep, result: ReconciliationResult
) -> ReconciliationResult:
    step.resolved = {
        **step.resolved,
        "reconciliation_delivery": {
            "revision": step.dispatch_revision,
            "state": result.state,
            "needs_more": result.needs_more,
            "retry_after_seconds": result.retry_after_seconds,
        },
    }
    return result


def _empty_readback_delay(
    step: ExecutionStep,
    source: ExecutionStep,
    claim: _Claim,
    page: BuildPage | AdGroupStatus,
    progress: dict[str, Any],
    known: set[str],
) -> int:
    """仅已成功创建的精确ID空回读可短暂等索引；未知创建不获得新恢复权限。"""
    identity = claim.progress.get("known_id")
    if (
        step.kind != "READBACK"
        or source.status != "SUCCEEDED"
        or not nonempty(identity)
        or source.remote_id != identity
        or known != {identity}
        or not isinstance(page, BuildPage)
        or not page.complete
        or page.page != 1
        or page.rows
        or page.total_number != 0
        or page.total_pages not in {0, 1}
        or claim.progress.get("stage") != "OBJECT"
        or progress.get("total") != 0
        or progress.get("seen") != 0
        or progress.get("matches") != 0
    ):
        return 0
    budget = step.resolved.get("readback_empty_retry")
    if budget is None:
        retries = 0
    elif (
        isinstance(budget, dict)
        and budget.get("known_id") == identity
        and budget.get("body_digest") == claim.digest
        and type(budget.get("retries")) is int
    ):
        retries = budget["retries"]
    else:
        return 0
    if not 0 <= retries < len(EMPTY_READBACK_DELAYS):
        return 0
    # 与分页游标分开持久保存：新scan、消息重投、进程重启都不能刷新自动预算。
    step.resolved = {
        **step.resolved,
        "readback_empty_retry": {
            "known_id": identity,
            "body_digest": claim.digest,
            "retries": retries + 1,
        },
    }
    return EMPTY_READBACK_DELAYS[retries]


def _finish(
    database_engine: Engine,
    context: TenantContext,
    claim: _Claim,
    page: BuildPage | AdGroupStatus | None,
    *,
    error: str | None = None,
    delay: int = 0,
    call_evidence: CallEvidence | None = None,
) -> ReconciliationResult:
    with Session(database_engine) as session, session.begin():
        step, source, unit = _locked(session, context, claim.step_id)
        live = _active(step, source, claim)
        summary: dict[str, Any] = {
            "scan_id": claim.progress["scan_id"],
            "page": claim.progress["page"],
            "stage": claim.progress["stage"],
            "body_digest": claim.digest,
        }
        operation, arguments = (
            read_arguments(
                BuildReadQuery(
                    intent=claim.intent,
                    native_series=claim.native_series,
                    remote_id=claim.progress.get("known_id"),
                    page=claim.progress["page"],
                )
            )
            if claim.progress["stage"] == "OBJECT"
            else (
                "build.get_regular_adgroups",
                status_arguments(
                    advertiser_id=claim.advertiser_id,
                    adgroup_id=claim.progress["candidate"]["remote_id"],
                ),
            )
        )
        summary["query"] = {"operation": operation, "arguments": arguments}
        actual_evidence = page.evidence if page is not None else call_evidence
        if actual_evidence is not None:
            summary["call_evidence"] = asdict(actual_evidence)
        if page is not None:
            summary.update(
                ids=[record.remote_id for record in page.rows][:100]
                if isinstance(page, BuildPage)
                else [page.adgroup_id],
                total=page.total_number if isinstance(page, BuildPage) else 1,
            )
        if live:
            try:
                if _authorize(session, context, unit, claim.bc_id) != claim.route:
                    raise DomainError("route_authorization_changed", "原核查路由已改变")
            except DomainError as failure:
                error = (
                    failure.code
                    if failure.code in SAFE_ERRORS
                    else "readback_authorization_unknown"
                )
        if error:
            summary["reason_code"] = error
        if not live:
            _evidence(
                session,
                step,
                claim=claim,
                conclusion="LATE_READBACK",
                summary=summary,
                request_id=page.evidence.request_id if page else None,
            )
            return ReconciliationResult("STALE")
        if error or page is None:
            _evidence(
                session, step, claim=claim, conclusion="READBACK_ERROR", summary=summary
            )
            _unknown(step, error or "readback_response_unknown")
            _clear(step, source)
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("UNKNOWN", delay > 0, delay))
        known = _known_ids(session, source)
        try:
            progress, result = _advance(session, claim, page)
        except DomainError:
            progress, result = {**claim.progress, "done": True}, "UNKNOWN"
        if len(known) > 1 or known and known != {claim.progress.get("known_id")}:
            progress, result = {**progress, "done": True}, "UNKNOWN"
        retry_delay = (
            _empty_readback_delay(step, source, claim, page, progress, known)
            if result == "UNKNOWN"
            else 0
        )
        summary.update(candidate=progress.get("candidate"), result=result)
        if retry_delay:
            summary.update(
                empty_retry=step.resolved["readback_empty_retry"]["retries"],
                retry_after_seconds=retry_delay,
            )
        _evidence(
            session,
            step,
            claim=claim,
            conclusion="READBACK_PAGE",
            summary=summary,
            request_id=page.evidence.request_id,
        )
        step.resolved = {**step.resolved, "reconciliation": progress}
        _clear(step, source)
        if result == "MORE":
            _unknown(step, "readback_in_progress")
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("PENDING", True, 0))
        if retry_delay:
            _unknown(step, "readback_in_progress")
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("PENDING", True, retry_delay))
        candidate_value = progress.get("candidate")
        candidate: dict[str, Any] = (
            candidate_value if isinstance(candidate_value, dict) else {}
        )
        status = candidate.get("operation_status")
        # 复用不同创建步骤的 CTA 必须完全匹配；不能像原对象核查那样
        # 把不匹配对象记录为已创建后继续唤醒下游。
        if (
            claim.progress.get("reused_cta_step_id")
            and candidate.get("comparison") != "MATCH"
        ):
            _unknown(step, "cta_reuse_mismatch")
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("UNKNOWN"))
        if (
            result != "COMPLETE"
            or claim.kind != "CTA"
            and status not in {"ENABLE", "DISABLE", "FROZEN"}
        ):
            _unknown(step, "readback_inconclusive")
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("UNKNOWN"))
        mismatch = (
            candidate["comparison"] != "MATCH"
            or claim.kind != "CTA"
            and status != "ENABLE"
        )
        now = datetime.now(UTC)
        for row in (source, step):
            row.remote_id, row.status, row.phase = (
                candidate["remote_id"],
                "SUCCEEDED",
                "DONE",
            )
            row.mismatch, row.checked_at = mismatch, now
            row.operation_status, row.review_status = (
                status,
                candidate.get("review_status"),
            )
            row.error_code = "readback_mismatch" if mismatch else None
            session.add(row)
        # Receipt on the create step allows later independent READBACK delivery to
        # reuse the actual ID. The saved create request is never altered.
        _evidence(
            session,
            source,
            claim=claim,
            conclusion="RECONCILED",
            summary={
                "remote_id": candidate["remote_id"],
                "mismatch": mismatch,
                **(
                    {"reused_cta_step_id": claim.progress["reused_cta_step_id"]}
                    if claim.progress.get("reused_cta_step_id")
                    else {}
                ),
            },
            request_id=page.evidence.request_id,
        )
        from app.modules.builds.created_directory import publish_created_object

        publish_created_object(session, step=source, route=claim.route)
        from .execution_state import ensure_native_readback

        ensure_native_readback(session, source)
        return _remember(
            step, ReconciliationResult("MISMATCH" if mismatch else "SUCCEEDED")
        )


def process_reconciliation(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    step_id: UUID,
    revision: int,
) -> ReconciliationResult:
    require_bounded_worker()
    deadline = datetime.now(UTC) + timedelta(seconds=HARD_LIMIT)
    with Session(database_engine) as session, session.begin():
        claimed = _claim(session, context, step_id, revision)
    if isinstance(claimed, ReconciliationResult):
        return claimed
    claim = claimed
    deadline = min(deadline, claim.lease_expires_at)

    def before_request() -> None:
        with bounded_session(database_engine, task_deadline=deadline) as session:
            step, source, unit = _locked(session, context, step_id)
            if not _active(step, source, claim):
                raise DomainError("execution_lease_lost", "原核查claim已失效")
            if _authorize(session, context, unit, claim.bc_id) != claim.route:
                raise DomainError("route_authorization_changed", "原核查路由已改变")

    completed = None
    try:
        with admitted_build_call(
            redis_client, context=context, route=claim.route, task_deadline=deadline
        ):
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=claim.route,
                task_deadline=deadline,
                before_request=before_request,
            ) as gateway:
                page: BuildPage | AdGroupStatus
                if claim.progress["stage"] == "STATUS":
                    page = gateway.builds.read_adgroup_status(
                        advertiser_id=claim.advertiser_id,
                        adgroup_id=claim.progress["candidate"]["remote_id"],
                    )
                else:
                    page = gateway.builds.read_page(
                        query=BuildReadQuery(
                            intent=claim.intent,
                            native_series=claim.native_series,
                            remote_id=claim.progress.get("known_id"),
                            page=claim.progress["page"],
                        )
                    )
                # 完整读回结果先提交，客户端清理失败不能抹去已确认的原对象事实。
                completed = _finish(database_engine, context, claim, page)
        return completed
    except AccountAdmissionDeferred as error:
        if completed is not None:
            return completed
        return _finish(
            database_engine,
            context,
            claim,
            None,
            error="admission_deferred",
            delay=max(1, (error.retry_after_ms + 999) // 1000),
        )
    except Exception as error:
        if completed is not None:
            return completed
        code = (
            error.code
            if isinstance(error, DomainError) and error.code in SAFE_ERRORS
            else "readback_response_unknown"
        )
        return _finish(
            database_engine,
            context,
            claim,
            None,
            error=code,
            delay=30
            if code
            in {
                "admission_unavailable",
                "readback_response_unknown",
                "mcp_refresh_pending",
                "gateway_credentials_changed",
            }
            else 0,
            call_evidence=error.evidence
            if isinstance(error, RemoteCallError)
            else None,
        )
