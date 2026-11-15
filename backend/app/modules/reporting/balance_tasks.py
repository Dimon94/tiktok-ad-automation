"""余额按 BC 分页独立派发；每页提交后续接，不阻断效果报表。"""

import hashlib
import json
import logging
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.jobs.outbox import (
    validate_dispatch_payload,
)
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_read_routes
from app.modules.ads.balances import persist_balances
from app.modules.reporting.planner import owned_accounts
from app.modules.reporting.store_models import AdSyncBc, ReportBalanceRun

logger = logging.getLogger(__name__)


class BalanceFailureFence(TypedDict, total=False):
    generation: int
    owner_epoch: int
    claim_token: UUID
    page: int
    advertiser: str
    provider_call_active: bool


TASK_NAME = "reporting.balance_step"
register_dispatch_task(TASK_NAME, "ads-directory")


def _enqueue_page(session: Session, run: ReportBalanceRun) -> None:
    from app.modules.sync_control.balance_demand import (
        balance_outcome,
        register_balance,
    )

    register_balance(session, run)
    balance_outcome(session, run)


def enqueue_balances(
    session: Session, plan: AdSyncBc, advertisers: Collection[str], now: datetime
) -> int:
    if not settings.ADS_SYNC_ENABLED or not advertisers:
        return 0
    route_key = hashlib.sha256(
        json.dumps([plan.frozen_route, str(plan.actor_id)], sort_keys=True).encode()
    ).hexdigest()
    key = f"balance:{route_key}:{int(now.timestamp()) // 1800}"
    from app.modules.sync_control.balance_demand import balance_scopes, register_balance
    from app.modules.sync_control.requirements import execution_owner_current

    # 周期tick只保证存在尚未完成的刷新，不给同一缺口每半小时再建一份执行。
    # 不改在途页码/冻结水位；整批owner失效时不能拿其余账户冒充有效覆盖。
    covered = {
        account
        for remaining in session.exec(
            select(col(ReportBalanceRun.remaining_advertiser_ids)).where(
                col(ReportBalanceRun.tenant_id) == plan.tenant_id,
                col(ReportBalanceRun.actor_id) == plan.actor_id,
                col(ReportBalanceRun.frozen_route) == plan.frozen_route,
                col(ReportBalanceRun.status) == "ACTIVE",
                execution_owner_current(ReportBalanceRun, "balance_run_id"),
            )
        ).all()
        for account in remaining
    }
    advertisers = sorted(set(advertisers) - covered)
    if not advertisers:
        return 0
    # 无新缺口时不再逐账户取得数千scope锁，避免周期计划阻塞余额回写。
    scopes = balance_scopes(
        session,
        tenant_id=plan.tenant_id,
        actor_id=plan.actor_id,
        route=plan.frozen_route,
        advertisers=advertisers,
    )
    count = 0
    for epoch in sorted({scope.owner_epoch for scope in scopes.values()}):
        accounts = sorted(
            account for account, scope in scopes.items() if scope.owner_epoch == epoch
        )
        account_key = hashlib.sha256(json.dumps(accounts).encode()).hexdigest()[:16]
        group_key = f"{key}:owner:{epoch}:{account_key}"
        if session.exec(
            select(col(ReportBalanceRun.id)).where(
                col(ReportBalanceRun.tenant_id) == plan.tenant_id,
                col(ReportBalanceRun.run_key) == group_key,
            )
        ).first():
            continue
        run = ReportBalanceRun(
            tenant_id=plan.tenant_id,
            actor_id=plan.actor_id,
            run_key=group_key,
            owner_epoch=epoch,
            frozen_route=plan.frozen_route,
            remaining_advertiser_ids=accounts,
            created_at=now,
        )
        session.add(run)
        session.flush()
        register_balance(session, run, scopes=scopes)
        count += 1
    return count


def plan_balances(*, database_engine: Engine, now: datetime | None = None) -> int:
    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = now or datetime.now(UTC)
    # 与报表筛选及其退避完全分离，低预算时仍保持独立的半小时计划。
    with Session(database_engine) as session:
        keys = session.exec(
            select(col(AdSyncBc.tenant_id), col(AdSyncBc.bc_id)).where(
                col(AdSyncBc.enabled).is_(True)
            )
        ).all()
    count = 0
    for tenant_id, bc_id in keys:
        with Session(database_engine) as session, session.begin():
            plan = session.exec(
                select(AdSyncBc)
                .where(
                    col(AdSyncBc.tenant_id) == tenant_id, col(AdSyncBc.bc_id) == bc_id
                )
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if plan is not None and plan.enabled:
                count += enqueue_balances(
                    session, plan, owned_accounts(session, plan), now
                )
    return count


@celery_app.task(name="reporting.plan_balances", time_limit=180, soft_time_limit=150)  # type: ignore[untyped-decorator]
def plan_balances_task() -> int:
    return plan_balances(database_engine=engine)


def run_balance(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    page: int,
    claim_id: UUID,
    claim_generation: int,
    failure_fence: BalanceFailureFence | None = None,
) -> str:
    if not settings.ADS_SYNC_ENABLED:
        return "WAITING"
    from app.modules.sync_control.costs import execution_cost_context

    cost_registered = False
    try:
        with execution_cost_context(
            database_engine,
            tenant_id=context.tenant_id,
            execution_id=run_id,
            generation=claim_generation,
            kind="balance",
        ):
            cost_registered = True
            return _run_balance(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                run_id=run_id,
                page=page,
                claim_id=claim_id,
                claim_generation=claim_generation,
                failure_fence=failure_fence,
            )
    except Exception:
        if not cost_registered:
            from app.modules.sync_control.balance_demand import (
                ENDPOINT,
                balance_reservation,
            )
            from app.modules.sync_control.occupancy import release_dispatch

            try:
                with Session(database_engine) as session, session.begin():
                    queued = session.exec(
                        select(ReportBalanceRun)
                        .where(
                            col(ReportBalanceRun.id) == run_id,
                            col(ReportBalanceRun.tenant_id) == context.tenant_id,
                            col(ReportBalanceRun.actor_id) == context.actor_id,
                            col(ReportBalanceRun.claim_generation) == claim_generation,
                            col(ReportBalanceRun.next_page) == page,
                            col(ReportBalanceRun.claim_token) == claim_id,
                        )
                        .with_for_update(skip_locked=True)
                    ).one_or_none()
                    if queued is not None and queued.remaining_advertiser_ids:
                        # 同代真实消费者会旋转令牌；旧消息既不清它的占位，也不写
                        # failure_fence触发终态。登记失败只释放这个未消费的queued身份。
                        release_dispatch(
                            redis_client,
                            reservation_id=balance_reservation(
                                queued, claim_generation
                            ),
                            app_scope=settings.TIKTOK_APP_ID,
                            endpoint=ENDPOINT,
                            rate_group=ENDPOINT,
                            tenant_id=queued.tenant_id,
                            advertiser_id=queued.remaining_advertiser_ids[0],
                        )
            except Exception:
                logger.warning("balance cost-registration queued cleanup deferred")
        raise


def _run_balance(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    page: int,
    claim_id: UUID,
    claim_generation: int,
    failure_fence: BalanceFailureFence | None = None,
) -> str:
    failure_fence = failure_fence if failure_fence is not None else {}
    if not settings.ADS_SYNC_ENABLED:
        return "WAITING"
    # A: 验权、冻结剩余账户及领取短租约；HTTP 不携带该 session。
    with Session(database_engine, expire_on_commit=False) as session, session.begin():
        run = session.exec(
            select(ReportBalanceRun)
            .where(col(ReportBalanceRun.id) == run_id)
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if run is None:
            return "IGNORED"
        if run.tenant_id != context.tenant_id or run.actor_id != context.actor_id:
            raise DomainError("dispatch_payload_invalid", "余额任务与冻结身份不符")
        if run.status != "ACTIVE" or run.next_page != page:
            return "IGNORED"
        now = datetime.now(UTC)
        if (
            run.claim_generation != claim_generation
            or run.claim_token != claim_id
            or run.claimed_until is None
            or run.claimed_until <= now
        ):
            return "IGNORED"
        failure_fence.update(
            {
                "generation": run.claim_generation,
                "owner_epoch": run.owner_epoch,
                "claim_token": claim_id,
                "page": page,
                "advertiser": run.remaining_advertiser_ids[0],
                "provider_call_active": False,
            }
        )
        from app.modules.sync_control.balance_demand import balance_fence

        balance_fence(session, run)
        route = FrozenTikTokRoute.model_validate(run.frozen_route)
        if route.channel != "OFFICIAL_API" or route.tenant_id != context.tenant_id:
            raise DomainError("api_binding_required", "余额同步需要当前租户的 API 绑定")
        advertisers = tuple(run.remaining_advertiser_ids)
        verify_read_routes(
            session, context=context, route=route, advertiser_ids=advertisers
        )
        generation, owner_epoch = run.claim_generation, run.owner_epoch
        # queued令牌只能消费一次；行锁内旋转后提交，重复消息不能进入HTTP。
        run.claim_token = claim_token = uuid4()
        run.claimed_until = now + timedelta(seconds=90)

    # A若验权失败会回滚旋转，失败围栏仍须指向原queued令牌；只在commit后换成worker令牌。
    failure_fence.update(
        {
            "generation": generation,
            "owner_epoch": owner_epoch,
            "claim_token": claim_token,
        }
    )

    from app.modules.sync_control.balance_demand import ENDPOINT, balance_reservation
    from app.modules.sync_control.occupancy import dispatch_context

    budget = dispatch_context(
        redis_client,
        reservation_id=balance_reservation(run, generation),
        scope={
            "app_scope": settings.TIKTOK_APP_ID,
            "endpoint": ENDPOINT,
            "rate_group": ENDPOINT,
            "tenant_id": run.tenant_id,
            "advertiser_id": advertisers[0],
        },
    )
    try:
        with (
            budget,
            open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=datetime.now(UTC) + timedelta(seconds=45),
            ) as gateway,
        ):
            failure_fence["provider_call_active"] = True
            balances, next_page = gateway.ads.read_balance_page(advertisers, page=page)
            failure_fence["provider_call_active"] = False
    except AccountAdmissionDeferred as error:
        return _record_balance_failure(
            database_engine=database_engine,
            context=context,
            run_id=run_id,
            failure_fence=failure_fence,
            error=error,
        )

    # B: 旧代回包、撤权或重绑不能写余额，也不能清除新worker的租约。
    with Session(database_engine) as session, session.begin():
        current = session.get(ReportBalanceRun, run_id, with_for_update=True)
        if (
            current is None
            or current.claim_token != claim_token
            or current.claim_generation != generation
            or current.owner_epoch != owner_epoch
            or current.status != "ACTIVE"
            or current.next_page != page
            or current.claimed_until is None
            or current.claimed_until <= datetime.now(UTC)
        ):
            return "IGNORED"
        from app.modules.sync_control.balance_demand import (
            balance_fence,
            balance_outcome,
            publish_balance_requirement,
        )

        balance_fence(session, current)
        verify_read_routes(
            session, context=context, route=route, advertiser_ids=advertisers
        )
        if not set(balances).issubset(advertisers):
            raise DomainError("ads_response_invalid", "余额返回了冻结范围外账户")
        persist_balances(session, context=context, route=route, balances=balances)
        confirmed = set()
        for advertiser, balance in balances.items():
            if publish_balance_requirement(session, current, advertiser, balance):
                confirmed.add(advertiser)
        current.remaining_advertiser_ids = sorted(set(advertisers) - confirmed)
        current.claim_token = current.claimed_until = None
        current.next_attempt_at = None
        if not current.remaining_advertiser_ids:
            current.status, current.error_code = "COMPLETE", None
            balance_outcome(session, current)
            return "COMPLETE"
        if next_page is not None:
            current.next_page = next_page
            current.error_code = None
            _enqueue_page(session, current)
            return "CONTINUE"
        # 末页遗漏的账户保留原需求，延后从首页核验，绝不补0或宣称完整。
        current.error_code = "balance_accounts_missing"
        current.next_page = 1
        current.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)
        balance_outcome(session, current)
        return "WAITING"


_PROVIDER_FAILURE_LIMIT = 6


def _record_balance_failure(
    *,
    database_engine: Engine,
    context: TenantContext,
    run_id: UUID,
    failure_fence: BalanceFailureFence,
    error: Exception,
) -> str:
    """只有仍持有本页执行令牌的调用能退避/终结；预算随执行持久保留。"""
    if not failure_fence:
        return "IGNORED"
    from app.modules.sync_control.balance_demand import balance_fence, balance_outcome

    with Session(database_engine) as session, session.begin():
        run = session.get(ReportBalanceRun, run_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            run is None
            or run.tenant_id != context.tenant_id
            or run.actor_id != context.actor_id
            or run.status != "ACTIVE"
            or run.next_page != failure_fence["page"]
            or run.claim_generation != failure_fence["generation"]
            or run.owner_epoch != failure_fence["owner_epoch"]
            or run.claim_token != failure_fence["claim_token"]
            or run.claimed_until is None
            or run.claimed_until <= now
        ):
            return "IGNORED"
        try:
            balance_fence(session, run)
        except DomainError as lost:
            if lost.code == "balance_claim_lost":
                return "IGNORED"
            raise
        if isinstance(error, AccountAdmissionDeferred):
            run.error_code = "admission_deferred"
            run.next_attempt_at = now + timedelta(
                milliseconds=max(1000, error.retry_after_ms)
            )
        else:
            retryable = not isinstance(error, DomainError) or error.code in {
                "tiktok_response_error",
                "read_deadline_exceeded",
                "tiktok_call_deadline_exceeded",
                "tiktok_local_resources_unavailable",
            }
            # gateway里的本地数据库超时会包装成DomainError；仍属本地竞争，
            # 不能误作永久失败或消耗平台失败预算。调用期限错误沿用有界重试。
            local = (
                not failure_fence.get("provider_call_active", False)
                or isinstance(error, (SQLAlchemyError, ValueError))
                or isinstance(error, DomainError)
                and error.code == "tiktok_local_resources_unavailable"
            )
            run.error_code = (
                error.code
                if isinstance(error, DomainError)
                else ("balance_local_error" if local else "balance_provider_error")
            )
            if not retryable or isinstance(error, ValueError):
                run.status = "FAILED"
                run.next_attempt_at = None
            elif local:
                # 数据库竞争/发送前准备失败不是平台失败；重派仍走新claim且不扣provider预算。
                run.next_attempt_at = now + timedelta(seconds=30)
            else:
                run.provider_failures += 1
                if run.provider_failures < _PROVIDER_FAILURE_LIMIT:
                    run.next_attempt_at = now + timedelta(
                        seconds=60 * 2 ** (run.provider_failures - 1)
                    )
                else:
                    run.status = "FAILED"
                    run.next_attempt_at = None
        run.claim_token = run.claimed_until = None
        balance_outcome(session, run)
        return "FAILED" if run.status == "FAILED" else "WAITING"


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TASK_NAME,
    time_limit=65,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def balance_step(
    *,
    tenant_id: str,
    actor_id: str,
    payload: dict[str, Any],
    dispatch: dict[str, Any] | None = None,
) -> str:
    from app.jobs.tasks import guard_sync_dispatch

    if not guard_sync_dispatch(
        database_engine=engine,
        task_name=TASK_NAME,
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
        dispatch=dispatch,
    ):
        return "IGNORED"
    validate_dispatch_payload(payload)
    if set(payload) != {"run_id", "page", "claim_id", "claim_generation"}:
        raise DomainError("dispatch_payload_invalid", "余额任务参数无效")
    try:
        run_id, claim_id = UUID(payload["run_id"]), UUID(payload["claim_id"])
        if type(payload["page"]) is not int or payload["page"] < 1:
            raise ValueError("invalid page")
        if (
            type(payload["claim_generation"]) is not int
            or payload["claim_generation"] < 1
        ):
            raise ValueError("invalid generation")
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "余额领取参数无效") from None
    context = TenantContext(
        tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
    )
    failure_fence: BalanceFailureFence = {}
    with Redis.from_url(settings.REDIS_URL) as redis:
        try:
            return run_balance(
                database_engine=engine,
                redis_client=redis,
                context=context,
                run_id=run_id,
                page=payload["page"],
                claim_id=claim_id,
                claim_generation=payload["claim_generation"],
                failure_fence=failure_fence,
            )
        except Exception as error:
            return _record_balance_failure(
                database_engine=engine,
                context=context,
                run_id=run_id,
                failure_fence=failure_fence,
                error=error,
            )
        finally:
            # 验权在HTTP上下文之前失败时也释放该旧claim的queued占位，不触碰新代。
            if failure_fence:
                from redis.exceptions import RedisError

                from app.modules.sync_control.balance_demand import ENDPOINT
                from app.modules.sync_control.occupancy import release_dispatch

                try:
                    release_dispatch(
                        redis,
                        reservation_id=f"balance:{run_id}:{failure_fence['generation']}:{failure_fence['page']}",
                        app_scope=settings.TIKTOK_APP_ID,
                        endpoint=ENDPOINT,
                        rate_group=ENDPOINT,
                        tenant_id=context.tenant_id,
                        advertiser_id=failure_fence["advertiser"],
                    )
                except RedisError:
                    # 已完成PG结果不能被临时Redis清理失败撤销；queued占位自身有TTL。
                    logger.warning(
                        "balance queued occupancy release deferred to expiry"
                    )
