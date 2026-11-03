"""Current source/build covers and preserved history with bounded read-only recovery."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import Engine, and_, func, or_, tuple_, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.core.context import TenantContext
from app.core.diagnostics import emit, error_fields, span
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import TRANSIENT_NOT_SENT, RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred,
)
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.tenants.permissions import require_tenant

from . import cover_sdk as api
from .cover_models import (
    MaterialCoverJob,
    MaterialCoverJobPage,
    MaterialCoverReceipt,
    MaterialCoverShareBatch,
)
from .models import AccountMaterial, MaterialFile
from .repository import asset_public, require_material_scope
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
)
from .schemas import AssetPreparation

HARD_LIMIT = 45
SOFT_LIMIT = 40
CLAIM_SECONDS = 60
SOURCE_WAIT_SECONDS = 15
register_dispatch_task("materials.prepare_cover", "resources")
register_dispatch_task("materials.verify_cover", "resources")


def _now() -> datetime:
    return datetime.now(UTC)


def _mapping(session: Session, job: MaterialCoverJob) -> AccountMaterial | None:
    asset = session.get(AccountMaterial, job.asset_id, populate_existing=True)
    # VID 属于目标账户本身：封面任务按自己冻结的连接核验权限并上传，
    # 映射上的历史上传连接只是留档，不参与身份比较。同一 VID 即同一平台
    # 视频，封面依附于 VID；视频换代时 video_id 变化，迟到回执仍会被拦下。
    if (
        asset
        and (
            asset.tenant_id,
            asset.bc_id,
            asset.material_id,
            asset.advertiser_id,
            asset.video_id,
            asset.status,
        )
        == (
            job.tenant_id,
            job.bc_id,
            job.material_id,
            job.advertiser_id,
            job.video_id,
            "available",
        )
        and asset.verified_at
    ):
        return asset
    return None


class _CoverAccessChecks:
    """同一短事务复用共同授权；逐项内容仍检查，不跨 HTTP 保留权限结论。"""

    def __init__(self, session: Session, context: TenantContext):
        self.session, self.context = session, context
        self.transaction = session.get_transaction()
        self.checked: set[tuple[str, str, str, bool]] = set()
        self.admission: dict[UUID, bool] = {}

    def _require_transaction(self, session: Session, context: TenantContext) -> None:
        if (
            session is not self.session
            or context != self.context
            or self.transaction is None
            or session.get_transaction() is not self.transaction
            or not self.transaction.is_active
        ):
            raise DomainError("cover_claim_lost", "封面授权核查事务已变化")

    def preload_admission(self, jobs: list[MaterialCoverJob]) -> None:
        from app.modules.builds.execution_window import cover_jobs_admitted

        self._require_transaction(self.session, self.context)
        identities = {job.id for job in jobs}
        admitted = cover_jobs_admitted(
            self.session, tenant_id=self.context.tenant_id, job_ids=identities
        )
        # 只缓存本次持锁领取候选；不跨事务/HTTP复用窗口判断。
        self.admission.update(
            {identity: identity in admitted for identity in identities}
        )

    def admitted(
        self, session: Session, context: TenantContext, job: MaterialCoverJob
    ) -> bool:
        from app.modules.builds.execution_window import cover_job_admitted

        self._require_transaction(session, context)
        if job.id not in self.admission:
            self.admission[job.id] = cover_job_admitted(
                session, tenant_id=job.tenant_id, job_id=job.id
            )
        return self.admission[job.id]

    def check(
        self,
        session: Session,
        context: TenantContext,
        job: MaterialCoverJob,
        *,
        upload: bool,
    ) -> None:
        self._require_transaction(session, context)
        route = load_material_route(
            job.frozen_route,
            context=context,
            bc_id=job.bc_id,
            connection_id=job.connection_id,
        )
        action = "upload" if job.purpose == "SOURCE" else "build"
        key = (route.model_dump_json(), job.advertiser_id, action, upload)
        if key in self.checked:
            return
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=action,
        )
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=job.bc_id,
            advertiser_id=job.advertiser_id,
            capability="read",
        )
        if upload:
            require_material_route(
                session,
                context=context,
                route=route,
                bc_id=job.bc_id,
                advertiser_id=job.advertiser_id,
                capability="upload",
            )
        self.checked.add(key)


def _access(
    session: Session,
    context: TenantContext,
    job: MaterialCoverJob,
    *,
    upload: bool = False,
    checks: _CoverAccessChecks | None = None,
) -> None:
    if job.superseded_by_id is not None:
        raise DomainError("cover_superseded", "此封面任务已有明确授权的新代任务")
    # 未显式传入时仍逐次核查；批量只复用共同权限，不缓存映射和摘要。
    (checks or _CoverAccessChecks(session, context)).check(
        session, context, job, upload=upload
    )
    if code := _digest_error(session, job):
        raise DomainError(code, "原视频摘要缺失或已变化，请核实素材")
    if _mapping(session, job) is None:
        raise DomainError(
            "cover_video_changed", "目标视频或连接已变化，请重新准备当前素材"
        )


def _digest_error(session: Session, job: MaterialCoverJob) -> str | None:
    if job.video_md5 is None:
        return "cover_video_unverified"
    material = session.get(MaterialFile, job.material_id, populate_existing=True)
    if (
        material is None
        or material.tenant_id != job.tenant_id
        or material.video_md5 != job.video_md5
    ):
        return "cover_video_changed"
    return None


def verified_cover_image_id(job: MaterialCoverJob) -> str | None:
    """Only READY evidence is usable; candidate reuse never impersonates an upload."""
    if (
        job.superseded_by_id is not None
        or job.status != "READY"
        or job.search_ambiguous
        or job.error_code == "cover_receipt_ambiguous"
    ):
        return None
    if (
        job.known_image_id
        and job.candidate_image_id
        and job.known_image_id != job.candidate_image_id
    ):
        return None
    return api._identifier(job.known_image_id or job.candidate_image_id)


def _result(session: Session, job: MaterialCoverJob) -> AssetPreparation:
    if job.superseded_by_id is not None:
        return AssetPreparation(
            state="blocked", task_id=job.id, reason_code="cover_superseded"
        )
    if job.error_code == "cover_receipt_ambiguous":
        return AssetPreparation(
            state="blocked", task_id=job.id, reason_code="cover_receipt_ambiguous"
        )
    if code := _digest_error(session, job):
        return AssetPreparation(state="blocked", task_id=job.id, reason_code=code)
    mapping = _mapping(session, job)
    if mapping is None:
        return AssetPreparation(
            state="blocked", task_id=job.id, reason_code="cover_video_changed"
        )
    if (image_id := verified_cover_image_id(job)) and mapping.image_id == image_id:
        return AssetPreparation(
            state="ready", mapping=asset_public(mapping), task_id=job.id
        )
    # 错误码保留历史诊断，不代替当前状态。正式只读接续已是VERIFYING时
    # 消费者应等待核验，不能因旧unknown码把尚未发送的广告永久标失败。
    if job.status in {"UNKNOWN", "BLOCKED"}:
        return AssetPreparation(
            state="blocked",
            task_id=job.id,
            reason_code="cover_result_unknown"
            if job.status == "UNKNOWN"
            else job.error_code,
        )
    return AssetPreparation(state="queued", task_id=job.id, reason_code=job.error_code)


def _queue(
    session: Session, job: MaterialCoverJob, *, read: bool, delay: int = 0
) -> None:
    if job.superseded_by_id is not None:
        raise DomainError("cover_superseded", "历史封面任务不能重新排队")
    job.revision += 1
    job.claim_token = job.claimed_until = None
    job.status = "VERIFYING" if read else "PENDING"
    job.updated_at = _now()
    job.repair_after = _now() + timedelta(seconds=delay + CLAIM_SECONDS)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        ),
        task_name="materials.verify_cover" if read else "materials.prepare_cover",
        task_key=f"cover:{job.id}:{job.revision}",
        payload={"job_id": str(job.id), "revision": job.revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch
    dispatch.available_at = _now() + timedelta(seconds=delay)


def ensure_cover(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    task_key: str,
    route: FrozenTikTokRoute,
) -> AssetPreparation:
    return _ensure_cover(
        session,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
        task_key=task_key,
        route=route,
        purpose="BUILD",
    )


def ensure_source_cover(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    task_key: str,
    route: FrozenTikTokRoute,
) -> AssetPreparation:
    """上传成功的异步事件入口；素材账户无需广告创建权限。"""
    return _ensure_cover(
        session,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
        task_key=task_key,
        route=route,
        purpose="SOURCE",
    )


def _ensure_cover(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    task_key: str,
    route: FrozenTikTokRoute,
    purpose: str,
) -> AssetPreparation:
    # 普通调用方 task key 不能新增发送代；仅显式授权入口可替换 current。
    if not task_key or len(task_key) > 255:
        raise DomainError("invalid_asset_task", "封面任务标识无效")
    require_material_scope(session, context=context, bc_id=bc_id)
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="upload" if purpose == "SOURCE" else "build",
    )
    # 内容属于租户；原上传 BC 不限制实际账户副本的封面，目标授权仍按 bc_id 核实。
    material = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.id == material_id,
        )
        .with_for_update()
    ).first()
    if material is None:
        raise DomainError("material_not_found", "未找到当前租户素材")
    asset = session.exec(
        select(AccountMaterial)
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == bc_id,
            AccountMaterial.material_id == material_id,
            AccountMaterial.advertiser_id == advertiser_id,
        )
        .with_for_update()
    ).first()
    if (
        not asset
        or asset.status != "available"
        or not (asset.video_id or "").strip()
        or not asset.verified_at
    ):
        return AssetPreparation(state="blocked", reason_code="cover_video_not_ready")
    # A verified image on the current target-account asset is sufficient build
    # evidence.  Older cover jobs may retain a historical connection route;
    # that route must not prevent reusing the already verified target image.
    if asset.image_id and purpose == "BUILD":
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            capability="read",
        )
        return AssetPreparation(state="ready", mapping=asset_public(asset))
    jobs = session.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.asset_id == asset.id,
            MaterialCoverJob.video_id == asset.video_id,
            col(MaterialCoverJob.superseded_by_id).is_(None),
        )
        .with_for_update()
        .limit(2)
    ).all()
    if len(jobs) > 1:
        raise DomainError("material_route_unverified", "历史素材连接信息需要核实")
    job = jobs[0] if jobs else None
    if job:
        # A verified cover is a read-only asset fact.  Older material batches
        # may have been prepared through the MCP connection while the current
        # build is frozen to the API connection for the same BC.  Requiring
        # the historical upload route here incorrectly blocks an otherwise
        # valid build before any remote write is needed.
        current_mapping = _mapping(session, job)
        if verified_cover_image_id(job) and current_mapping is not None:
            if _digest_error(session, job):
                return AssetPreparation(
                    state="blocked", task_id=job.id, reason_code="cover_video_changed"
                )
            require_material_route(
                session,
                context=context,
                route=route,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                capability="read",
            )
            return AssetPreparation(
                state="ready", mapping=asset_public(current_mapping)
            )
        require_same_route(
            load_material_route(job.frozen_route, context=context, bc_id=bc_id), route
        )
        if (
            purpose == "SOURCE"
            and job.purpose == "BUILD"
            and job.share_batch_id is None
        ):
            # 成功事件可能晚于 BUILD 排队；沿用原 job/VID，绝不创建第二个上传身份。
            # 已发送上传/已有图片候选均只读核查；没有图片事实才可开始源准备。
            job.purpose = "SOURCE"
            if not job.claimed_until or job.claimed_until <= _now():
                _queue(
                    session,
                    job,
                    read=bool(
                        job.request_armed_at
                        or job.known_image_id
                        or job.candidate_image_id
                    ),
                )
        _access(session, context, job)
        return _result(session, job)
    assert asset.video_id is not None
    identity = uuid4()
    job = MaterialCoverJob(
        id=identity,
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        material_id=material_id,
        asset_id=asset.id,
        advertiser_id=advertiser_id,
        connection_id=route.connection_id,
        frozen_route=route.model_dump(mode="json"),
        actor_id=context.actor_id,
        video_id=asset.video_id,
        video_md5=material.video_md5,
        remote_name=f"cover-{identity.hex}.jpg",
        purpose=purpose,
        candidate_image_id=asset.image_id if purpose == "SOURCE" else None,
    )
    _access(session, context, job, upload=True)
    session.add(job)
    session.flush()
    _queue(session, job, read=bool(job.candidate_image_id))
    return _result(session, job)


def _job(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> MaterialCoverJob:
    statement = (
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.id == identity,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    job = session.exec(statement).first()
    if job is None:
        raise DomainError("material_not_found", "未找到当前租户封面任务")
    return job


def get_cover_status(
    session: Session, *, context: TenantContext, job_id: UUID
) -> AssetPreparation:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    job = _job(session, context, job_id)
    require_material_route(
        session,
        context=context,
        route=load_material_route(
            job.frozen_route,
            context=context,
            bc_id=job.bc_id,
            connection_id=job.connection_id,
        ),
        bc_id=job.bc_id,
        advertiser_id=job.advertiser_id,
        capability="read",
    )
    return _result(session, job)


def request_cover_reconciliation(
    session: Session, *, context: TenantContext, job_id: UUID
) -> AssetPreparation:
    job = _job(session, context, job_id, lock=True)
    _access(session, context, job)
    _access(
        session,
        TenantContext(tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"),
        job,
    )
    if job.error_code == "cover_receipt_ambiguous":
        # 已观察到互斥回执是持久冲突；普通只读重试不能抹掉这一事实。
        return _result(session, job)
    if job.status == "READY":
        # 只有调用方明确请求核查时才重新读取；已确认映射不按本地时间自动过期。
        _queue(session, job, read=True)
        return _result(session, job)
    if (
        job.candidate_image_id
        and job.request_armed_at is None
        and job.status in {"BLOCKED", "UNKNOWN"}
    ):
        # Failed reuse probes preserve their actual ID and can only be read again.
        job.failure_count = 0
        _queue(session, job, read=True)
        return _result(session, job)
    if (
        job.status in {"READY", "PENDING", "PREPARING", "VERIFYING"}
        or job.request_armed_at is None
    ):
        return _result(session, job)
    job.search_round, job.next_page, job.search_total = uuid4(), 1, None
    job.candidate_image_id, job.search_ambiguous = None, False
    job.failure_count = 0
    _queue(session, job, read=True)
    return _result(session, job)


def request_cover_retry(
    session: Session, *, context: TenantContext, job_id: UUID
) -> AssetPreparation:
    job = _job(session, context, job_id, lock=True)
    _access(session, context, job, upload=True)
    _access(
        session,
        TenantContext(tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"),
        job,
        upload=True,
    )
    has_receipt = (
        session.exec(
            select(MaterialCoverReceipt.id)
            .where(MaterialCoverReceipt.job_id == job.id)
            .limit(1)
        ).first()
        is not None
    )
    if (
        job.status not in {"BLOCKED", "PENDING"}
        or job.request_armed_at is not None
        or job.known_image_id is not None
        or has_receipt
        or job.dispatch_id is not None
        or (job.claimed_until and job.claimed_until > _now())
    ):
        raise DomainError("cover_retry_forbidden", "已发送或正在处理的封面任务只能核查")
    if job.status == "PENDING":
        # 共享依赖已恢复而搭建步骤仍失败时，只接回原等待链。
        # 保留原批次及唯一wake，由正式repair唤醒，不能重复投递成员。
        return _result(session, job)
    job.error_code = None
    job.failure_count = 0
    _queue(session, job, read=bool(job.candidate_image_id))
    return _result(session, job)


def _fenced(
    session: Session, context: TenantContext, identity: UUID, nonce: UUID
) -> MaterialCoverJob | None:
    job = _job(session, context, identity, lock=True)
    if (
        job.superseded_by_id is not None
        or job.claim_token != nonce
        or not job.claimed_until
        or job.claimed_until <= _now()
    ):
        return None
    return job


def _stop(job: MaterialCoverJob, code: str, *, unknown: bool) -> None:
    job.status = "UNKNOWN" if unknown else "BLOCKED"
    if job.error_code != "cover_receipt_ambiguous":
        job.error_code = code
    job.claim_token = job.claimed_until = job.dispatch_id = None
    job.updated_at = _now()


def _claim(
    database_engine: Engine,
    context: TenantContext,
    job_id: UUID,
    dispatch_id: UUID,
    revision: int,
    *,
    read: bool,
) -> tuple[MaterialCoverJob, UUID] | None:
    with Session(database_engine) as session, session.begin():
        claimed = _claim_in_session(
            session, context, job_id, dispatch_id, revision, read=read
        )
        session.flush()
        if claimed:
            session.expunge(claimed[0])
        return claimed


def _claim_in_session(
    session: Session,
    context: TenantContext,
    job_id: UUID,
    dispatch_id: UUID,
    revision: int,
    *,
    read: bool,
    checks: _CoverAccessChecks | None = None,
) -> tuple[MaterialCoverJob, UUID] | None:
    job = _job(session, context, job_id, lock=True)
    # 旧消息和看门狗不能将已获准替换的历史代重新领取或改写状态。
    if job.superseded_by_id is not None:
        return None
    if job.error_code == "cover_receipt_ambiguous":
        _stop(job, "cover_receipt_ambiguous", unknown=True)
        return None
    if (
        job.actor_id != context.actor_id
        or job.dispatch_id != dispatch_id
        or job.revision != revision
        or job.status not in {"PENDING", "PREPARING", "VERIFYING"}
    ):
        return None
    dispatch = session.get(PendingDispatch, dispatch_id)
    expected_task = "materials.verify_cover" if read else "materials.prepare_cover"
    if dispatch is None or (
        dispatch.tenant_id != job.tenant_id
        or dispatch.actor_id != job.actor_id
        or dispatch.task_name != expected_task
        or dispatch.task_key != f"cover:{job.id}:{job.revision}"
        or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
    ):
        return None
    if job.claimed_until and job.claimed_until > _now():
        return None
    if not read and job.purpose == "BUILD" and job.request_armed_at is None:
        if not (checks or _CoverAccessChecks(session, context)).admitted(
            session, context, job
        ):
            # 已排队的未来封面也必须遵守正式搭建窗口。只暂停未发送准备；
            # 来源封面和已发送/显式只读核查始终保留原恢复路径。
            job.status, job.error_code = "PENDING", "cover_window_wait"
            job.claim_token = job.claimed_until = job.dispatch_id = None
            job.updated_at = _now()
            job.repair_after = _now() + timedelta(seconds=CLAIM_SECONDS)
            return None
    if job.request_armed_at is not None and not read:
        _queue(session, job, read=True)
        return None
    try:
        _access(session, context, job, upload=not read, checks=checks)
    except DomainError as error:
        _stop(job, error.code, unknown=bool(job.request_armed_at))
        return None
    nonce = uuid4()
    job.claim_token, job.claimed_until = (
        nonce,
        _now() + timedelta(seconds=CLAIM_SECONDS),
    )
    job.status = "VERIFYING" if read else "PREPARING"
    job.repair_after = job.claimed_until
    return job, nonce


def _check_current(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    *,
    deadline: datetime,
    upload: bool,
) -> None:
    # 会话复用不缓存授权；每次物理发送重新检查 claim、目标视频及固定通道。
    with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
        current = _fenced(db, context, job.id, nonce)
        if current is None:
            raise DomainError("cover_claim_lost", "封面任务执行权已变化")
        _access(db, context, current, upload=upload)


def _call[T](
    database_engine: Engine,
    client: material_types.MaterialOperations,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    operation: str,
    invoke: Callable[
        [material_types.MaterialOperations, material_types.RemoteCallBudget], T
    ],
    *,
    deadline: datetime,
    arm: bool = False,
    receipt: Callable[[T], None] | None = None,
) -> T:
    policy = admission_policy(operation)
    budget = material_types.RemoteCallBudget(deadline, HARD_LIMIT, policy.lease_ms)
    budget.timeout(upload=arm)
    _check_current(database_engine, context, job, nonce, deadline=deadline, upload=arm)
    if arm:
        with (
            bounded_session(database_engine, task_deadline=deadline) as db,
            db.begin(),
        ):
            current = _fenced(db, context, job.id, nonce)
            if current is None:
                raise DomainError("cover_claim_lost", "封面任务执行权已变化")
            _access(db, context, current, upload=True)
            if current.request_armed_at is not None:
                raise DomainError("cover_result_unknown", "原上传结果需要核查")
            current.request_armed_at = _now()
    try:
        result = invoke(client, budget)
    except Exception as error:
        if arm and (
            isinstance(error, AccountAdmissionDeferred)
            or isinstance(error, RemoteCallError)
            and error.effect == "NOT_SENT"
        ):
            # 仅当前发送者得到可靠未发送证据时解除本次armed；旧claim和未知结果不能回退。
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                current = _fenced(db, context, job.id, nonce)
                if (
                    current is not None
                    and current.known_image_id is None
                    and db.exec(
                        select(MaterialCoverReceipt.id)
                        .where(MaterialCoverReceipt.job_id == current.id)
                        .limit(1)
                    ).first()
                    is None
                ):
                    current.request_armed_at = None
        raise
    if receipt:
        with span(
            "cover_receipt",
            tenant_id=str(context.tenant_id),
            job_id=str(job.id),
            operation=operation,
            image_id=getattr(result, "image_id", None),
        ):
            receipt(result)  # 实际image_id须先落库，再离开可能失败的HTTP客户端清理。
    return result


def _receipt_conflict(session: Session, job: MaterialCoverJob) -> bool:
    if job.error_code == "cover_receipt_ambiguous":
        return True
    receipts = session.exec(
        select(MaterialCoverReceipt)
        .where(MaterialCoverReceipt.job_id == job.id)
        .limit(2)
    ).all()
    if len(receipts) > 1:
        return True
    if not receipts:
        return False
    receipt = receipts[0]
    facts = receipt.receipt_facts
    if facts is None or (job.known_image_id and job.known_image_id != receipt.image_id):
        return True
    if job.signature and facts["signature"] and job.signature != facts["signature"]:
        return True
    job.known_image_id = receipt.image_id
    if job.signature is None:
        job.signature = facts["signature"]
    return False


def _invalidate_receipt(session: Session, job: MaterialCoverJob) -> None:
    if job.superseded_by_id is not None:
        return
    mapping = _mapping(session, job)
    if mapping and mapping.image_id == job.known_image_id:
        mapping.image_id = None
    _stop(job, "cover_receipt_ambiguous", unknown=True)


def _preserve_receipt(
    database_engine: Engine,
    context: TenantContext,
    job_id: UUID,
    value: material_types.ImageReceipt,
) -> None:
    with Session(database_engine) as session, session.begin():
        original = _job(session, context, job_id)
        session.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.id == original.material_id,
            )
            .with_for_update()
        ).one()
        current = _job(session, context, job_id, lock=True)
        # 迟到的真实回执不依赖旧actor仍有权限；material→job锁和发布事务同序。
        session.exec(
            insert(MaterialCoverReceipt)
            .values(
                id=uuid4(),
                tenant_id=context.tenant_id,
                job_id=job_id,
                image_id=value.image_id,
                receipt_facts={"signature": value.signature},
                observed_at=_now(),
            )
            .on_conflict_do_nothing(index_elements=["job_id", "image_id"])
        )
        saved = session.exec(
            select(MaterialCoverReceipt).where(
                MaterialCoverReceipt.job_id == job_id,
                MaterialCoverReceipt.image_id == value.image_id,
            )
        ).one()
        if current.superseded_by_id is not None:
            # 迟到回执仍按原发送身份留档，不能污染新代图片或改写旧代结论。
            return
        if saved.receipt_facts != {"signature": value.signature} or _receipt_conflict(
            session, current
        ):
            # 重复回执只能完全幂等；冲突不改写旧证据，立即撤下冲突图片的可用映射。
            _invalidate_receipt(session, current)


def _save_receipt(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    value: material_types.ImageReceipt,
) -> None:
    try:
        with Session(database_engine) as session, session.begin():
            # 与只读发布和迟到回执一致地按 material→job 加锁，发布前复核目标身份。
            session.exec(
                select(MaterialFile)
                .where(
                    MaterialFile.tenant_id == context.tenant_id,
                    MaterialFile.id == job.material_id,
                )
                .with_for_update()
            ).one()
            current = _fenced(session, context, job.id, nonce)
            if current is None:
                raise DomainError("cover_claim_lost", "封面回执需补充核查")
            current.known_image_id = value.image_id
            current.signature = value.signature
            # 上传成功已证明 ID 归属本次目标账户；完整事实还需匹配刚核实的视频比例。
            # 缺字段/畸形回执保留真实 ID，只排只读核查，不再次上传图片。
            evidence = (
                api.verified_image(
                    {
                        "list": [
                            {
                                "image_id": value.image_id,
                                "signature": value.signature,
                                "width": value.width,
                                "height": value.height,
                                "displayable": value.displayable,
                                "material_id": value.mid,
                            }
                        ]
                    },
                    image_id=value.image_id,
                    remote_name=job.remote_name,
                    signature=value.signature,
                    width=current.width,
                    height=current.height,
                )
                if value.signature is not None
                else None
            )
            if evidence is None:
                _queue(session, current, read=True)
            else:
                _access(session, context, current, upload=True)
                _publish_result(session, context, current, evidence)
    except Exception as error:
        emit(
            "cover_receipt_primary_failed",
            tenant_id=str(context.tenant_id),
            job_id=str(job.id),
            image_id=value.image_id,
            **error_fields(error),
        )
        with span(
            "cover_receipt_fallback",
            tenant_id=str(context.tenant_id),
            job_id=str(job.id),
            image_id=value.image_id,
        ):
            _preserve_receipt(database_engine, context, job.id, value)


def _read_result(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    evidence: dict[str, str] | None,
) -> None:
    with Session(database_engine) as session, session.begin():
        _read_result_in_session(session, context, job, nonce, evidence)


def _read_result_in_session(
    session: Session,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    evidence: dict[str, str] | None,
    *,
    checks: _CoverAccessChecks | None = None,
) -> None:
    session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.id == job.material_id,
        )
        .with_for_update()
    ).one()
    current = _fenced(session, context, job.id, nonce)
    if current is None:
        return
    _check_read_identity(current, job)
    _publish_result(session, context, current, evidence, checks=checks)


def _check_read_identity(current: MaterialCoverJob, expected: MaterialCoverJob) -> None:
    # 读取响应只属于发出该请求时的具体 VID/摘要/图片/路线，不能发布给中途替换的身份。
    for field in (
        "tenant_id",
        "bc_id",
        "actor_id",
        "advertiser_id",
        "connection_id",
        "frozen_route",
        "asset_id",
        "material_id",
        "video_id",
        "video_md5",
        "known_image_id",
        "candidate_image_id",
        "signature",
        "width",
        "height",
        "remote_name",
        "request_armed_at",
    ):
        if getattr(current, field) != getattr(expected, field):
            raise DomainError("cover_video_changed", "封面读取身份已变化")


def _publish_result(
    session: Session,
    context: TenantContext,
    current: MaterialCoverJob,
    evidence: dict[str, str] | None,
    *,
    checks: _CoverAccessChecks | None = None,
) -> None:
    if current.superseded_by_id is not None:
        return
    _access(session, context, current, checks=checks)
    if _receipt_conflict(session, current):
        _invalidate_receipt(session, current)
        return
    if (
        evidence is not None
        and current.signature is not None
        and current.signature != evidence["signature"]
    ):
        _invalidate_receipt(session, current)
        return
    if evidence is None:
        _stop(current, "cover_result_unknown", unknown=True)
        return
    # 成功上传已确认该账户的 image_id；MID 只在跨账户共享时需要，缺失时再发现。
    mapping = _mapping(session, current)
    assert mapping
    mapping.image_id = evidence["image_id"]
    if current.request_armed_at:
        current.known_image_id = evidence["image_id"]
    else:
        current.candidate_image_id = evidence["image_id"]
    current.signature = evidence["signature"]
    current.image_mid = evidence.get("material_id")
    current.status, current.error_code = "READY", None
    # 已取得新正证据后结束本轮连续故障；翌日核查拥有独立的有界恢复机会。
    current.failure_count = 0
    current.claim_token = current.claimed_until = current.dispatch_id = None
    current.updated_at = _now()


def _search_position(job: MaterialCoverJob) -> tuple[int, int, int]:
    """持久next_page是本次核查的读取序号，最多100/71/53三轮。

    第一轮与已有100条分页的序号一致；补齐轮移动边界，不覆写旧页证据。
    """
    remaining = job.next_page
    if job.search_total is None:
        return 1, remaining, 100
    for phase, size in enumerate((100, 71, 53), start=1):
        pages = max(1, ceil(job.search_total / size))
        if remaining <= pages:
            return phase, remaining, size
        remaining -= pages
    raise DomainError("cover_search_incomplete", "图片库存分页仍不完整")


def _search_result(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    rows: list[dict[str, Any]],
    last: bool,
    total: int,
) -> None:
    with Session(database_engine) as session, session.begin():
        current = _fenced(session, context, job.id, nonce)
        if current is None:
            return
        ids = [row["image_id"] for row in rows]
        if len(ids) != len(set(ids)) or len(ids) > 100 or total >= 10000:
            _stop(current, "cover_search_incomplete", unknown=True)
            return
        if current.search_total is not None and current.search_total != total:
            # 同账户并发入库会改变分页总数；旧轮证据保留但不能与新轮拼接。
            # 共用连续故障预算，最多三次后停止，绝不因重查重复发送图片。
            current.failure_count += 1
            if current.failure_count >= 3:
                _stop(current, "cover_search_incomplete", unknown=True)
                return
            current.search_round, current.next_page, current.search_total = (
                uuid4(),
                1,
                None,
            )
            current.candidate_image_id, current.search_ambiguous = None, False
            current.error_code = "cover_search_incomplete"
            _queue(session, current, read=True)
            return
        current.search_total = total
        session.add(
            MaterialCoverJobPage(
                tenant_id=current.tenant_id,
                job_id=current.id,
                search_round=current.search_round,
                page=current.next_page,
                total=total,
                image_ids=ids,
            )
        )
        for row in rows:
            if row.get("file_name") == current.remote_name:
                if (
                    current.candidate_image_id
                    and current.candidate_image_id != row["image_id"]
                ):
                    current.search_ambiguous = True
                current.candidate_image_id = row["image_id"]
        session.flush()
        # 跨页重复不是新增图片；只按同一总数、同一次核查的唯一ID计数。
        # 不因平台重叠页截断后续候选，也不将重复计数冒充完整库存。
        seen = (
            select(
                func.jsonb_array_elements_text(MaterialCoverJobPage.image_ids).label(
                    "image_id"
                )
            )
            .where(
                MaterialCoverJobPage.job_id == current.id,
                MaterialCoverJobPage.search_round == current.search_round,
            )
            .subquery()
        )
        observed = session.exec(
            select(func.count(func.distinct(seen.c.image_id)))
        ).one()
        phase, _, _ = _search_position(current)
        if observed > total or (last and phase == 3 and observed < total):
            _stop(current, "cover_search_incomplete", unknown=True)
            return
        if observed < total:
            current.next_page += 1
            _queue(session, current, read=True)
            return
        if not current.candidate_image_id or current.search_ambiguous:
            _stop(current, "cover_result_unknown", unknown=True)
            return
        current.known_image_id = current.candidate_image_id
        _queue(session, current, read=True)


def _prepare_read(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    *,
    deadline: datetime,
) -> bool:
    with (
        bounded_session(database_engine, task_deadline=deadline) as session,
        session.begin(),
    ):
        # Conflicting local receipts must stop before even opening a gateway.
        session.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.id == job.material_id,
            )
            .with_for_update()
        ).one()
        current = _fenced(session, context, job.id, nonce)
        if current is None:
            return False
        if _receipt_conflict(session, current):
            _invalidate_receipt(session, current)
            return False
        job.known_image_id = current.known_image_id
        job.signature = current.signature
        job.candidate_image_id = current.candidate_image_id
        return True


def _run_claimed_cover(
    database_engine: Engine,
    client: material_types.MaterialOperations,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    *,
    deadline: datetime,
    read: bool,
) -> None:
    if not read:
        if job.purpose != "SOURCE":
            raise DomainError("cover_source_required", "目标封面必须使用源图片共享")
        if job.video_md5 is None:
            raise DomainError("cover_video_unverified", "视频签名尚未核实")
        md5 = job.video_md5
        video = _call(
            database_engine,
            client,
            context,
            job,
            nonce,
            "materials.get_videos",
            lambda client, budget: client.read_video_cover(
                advertiser_id=job.advertiser_id,
                video_id=job.video_id,
                md5=md5,
                budget=budget,
            ),
            deadline=deadline,
        )
        url = video.url
        if not url:
            suggestion = _call(
                database_engine,
                client,
                context,
                job,
                nonce,
                "materials.get_suggested_covers",
                lambda client, budget: client.suggest_cover(
                    advertiser_id=job.advertiser_id,
                    video_id=job.video_id,
                    width=video.width,
                    height=video.height,
                    budget=budget,
                ),
                deadline=deadline,
            )
            url = suggestion.url if suggestion else None
        if not url:
            raise DomainError("cover_unavailable", "平台尚未提供可用的视频封面")
        with Session(database_engine) as session, session.begin():
            current = _fenced(session, context, job.id, nonce)
            if current is None:
                return
            current.width, current.height = video.width, video.height
        _call(
            database_engine,
            client,
            context,
            job,
            nonce,
            "materials.upload_image_url",
            lambda client, budget: client.upload_image_url(
                material_types.URLImageUpload(job.advertiser_id, url, job.remote_name),
                budget=budget,
            ),
            deadline=deadline,
            arm=True,
            receipt=lambda value: _save_receipt(
                database_engine, context, job, nonce, value
            ),
        )
        return
    if job.known_image_id or (job.candidate_image_id and job.request_armed_at is None):
        image_id = job.known_image_id or job.candidate_image_id
        assert image_id
        if job.purpose == "SOURCE" and (job.width is None or job.height is None):
            assert job.video_md5 is not None
            md5 = job.video_md5
            video = _call(
                database_engine,
                client,
                context,
                job,
                nonce,
                "materials.get_videos",
                lambda client, budget: client.read_video_cover(
                    advertiser_id=job.advertiser_id,
                    video_id=job.video_id,
                    md5=md5,
                    budget=budget,
                ),
                deadline=deadline,
            )
            with Session(database_engine) as db, db.begin():
                current = _fenced(db, context, job.id, nonce)
                if current is None:
                    return
                current.width, current.height = video.width, video.height
            job.width, job.height = video.width, video.height
        data = _call(
            database_engine,
            client,
            context,
            job,
            nonce,
            "materials.get_images",
            lambda client, budget: client.read_image(
                advertiser_id=job.advertiser_id, image_id=image_id, budget=budget
            ),
            deadline=deadline,
        )
        source_candidate = job.purpose == "SOURCE" and job.request_armed_at is None
        evidence = api.verified_image(
            {"list": [api.image_record_data(data)] if data else []},
            image_id=image_id,
            remote_name=job.remote_name,
            signature=job.signature
            or (data.signature if source_candidate and data else None),
            width=job.width,
            height=job.height,
        )
        if source_candidate:
            # 详情可读不等于本账户库存持有；既有候选必须另取 MID/内容的账户级正证据。
            # 同 MID 在两个接口可能返回不同 tos ID，不能以 ID 字面相等判归属。
            mid = evidence.get("material_id") if evidence else None
            if not mid or not mid.isascii() or not mid.isdigit():
                raise DomainError(
                    "cover_source_inventory_unverified", "源账户图片库存尚未核实"
                )
            inventory = _call(
                database_engine,
                client,
                context,
                job,
                nonce,
                "materials.search_images",
                lambda client, budget: client.search_images(
                    advertiser_id=job.advertiser_id,
                    page=1,
                    material_ids=(mid,),
                    budget=budget,
                ),
                deadline=deadline,
            )
            assert evidence is not None and data is not None
            if not any(
                row.mid == mid
                and row.width == data.width
                and row.height == data.height
                and api.verified_image(
                    {"list": [api.image_record_data(row)]},
                    image_id=row.image_id,
                    remote_name=job.remote_name,
                    signature=evidence["signature"],
                    width=job.width,
                    height=job.height,
                )
                for row in inventory.rows
            ):
                raise DomainError(
                    "cover_source_inventory_unverified", "源账户图片库存尚未核实"
                )
        _read_result(database_engine, context, job, nonce, evidence)
    else:
        _, page_number, page_size = _search_position(job)
        page_data = _call(
            database_engine,
            client,
            context,
            job,
            nonce,
            "materials.search_images",
            lambda client, budget: client.search_images(
                advertiser_id=job.advertiser_id,
                page=page_number,
                page_size=page_size,
                budget=budget,
            ),
            deadline=deadline,
        )
        rows, last, total = api.image_search_page(
            api.image_page_data(page_data), page=page_number, page_size=page_size
        )
        _search_result(database_engine, context, job, nonce, rows, last, total)


def run_cover(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    job_id: UUID,
    dispatch_id: UUID,
    revision: int,
    read: bool,
) -> None:
    deadline = _now() + timedelta(seconds=HARD_LIMIT - 5)
    claimed = _claim(database_engine, context, job_id, dispatch_id, revision, read=read)
    if claimed is None:
        return
    job, nonce = claimed
    try:
        completed_shared_read = False
        if read and job.known_image_id and job.share_batch_id:
            with bounded_session(database_engine, task_deadline=deadline) as db:
                completed_shared_read = (
                    db.exec(
                        _completed_cover_batches(job).where(
                            MaterialCoverShareBatch.id == job.share_batch_id
                        )
                    ).first()
                    is not None
                )
        if (
            job.purpose == "BUILD"
            and (
                job.share_batch_id
                or (not job.request_armed_at and not job.known_image_id)
            )
            and not completed_shared_read
        ):
            from .cover_sharing import run_shared_cover

            run_shared_cover(
                database_engine, redis_client, context, job, nonce, deadline=deadline
            )
            return
        if read and not _prepare_read(
            database_engine, context, job, nonce, deadline=deadline
        ):
            return
        if (
            read
            and job.known_image_id
            and _run_known_cover_group(
                database_engine, redis_client, context, job, nonce, deadline=deadline
            )
        ):
            return
        route = load_material_route(
            job.frozen_route,
            context=context,
            bc_id=job.bc_id,
            connection_id=job.connection_id,
        )
        # 本次执行独占同一 gateway；视频读取、建议和图片上传仍分别准入。
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=lambda: _check_current(
                database_engine,
                context,
                job,
                nonce,
                deadline=deadline,
                upload=not read,
            ),
        ) as gateway:
            _run_claimed_cover(
                database_engine,
                gateway.materials,
                context,
                job,
                nonce,
                deadline=deadline,
                read=read,
            )
    except AccountAdmissionDeferred as error:
        with Session(database_engine) as session, session.begin():
            current = _fenced(session, context, job.id, nonce)
            if current:
                _queue(
                    session,
                    current,
                    read=read,
                    delay=max(1, ceil(error.retry_after_ms / 1000)),
                )
    except Exception as error:
        emit(
            "cover_failed",
            tenant_id=str(context.tenant_id),
            job_id=str(job.id),
            dispatch_id=str(dispatch_id),
            revision=revision,
            read_only=read,
            **error_fields(error),
        )
        with Session(database_engine) as session, session.begin():
            current = _fenced(session, context, job.id, nonce)
            if current is None:
                return
            code = (
                error.code if isinstance(error, DomainError) else "cover_response_error"
            )
            current.failure_count += 1
            if current.request_armed_at and not read:
                current.error_code = "cover_result_unknown"
                _queue(session, current, read=True)
            elif local_transient(error) and current.failure_count < LOCAL_RETRY_LIMIT:
                # 走到这里时 upload 阶段必然尚未 armed（armed 已在上一分支转只读核查）。
                current.error_code = code
                _queue(
                    session,
                    current,
                    read=read or bool(current.candidate_image_id),
                    delay=local_retry_delay(current.failure_count),
                )
            elif (
                read_transient(error)
                and current.failure_count < COVER_READ_RETRY_LIMIT
                and (read or current.request_armed_at is None)
            ):
                # 视频/库存读取失败不能直接毒化后续搭建。只在尚未上传或只读
                # 核查阶段退避；已 armed 的上传在上方分支只能进入原身份核查。
                current.error_code = code
                _queue(
                    session,
                    current,
                    read=read or bool(current.candidate_image_id),
                    delay=local_retry_delay(current.failure_count),
                )
            elif (
                isinstance(error, RemoteCallError)
                and error.code in TRANSIENT_NOT_SENT
                and current.failure_count < 3
                and (
                    (error.effect == "NOT_SENT" and current.request_armed_at is None)
                    or (
                        error.effect in {"NOT_SENT", "UNKNOWN"}
                        and (
                            read
                            or (
                                # 首次prepare尚未切task名称，但持久候选阶段只会读图。
                                current.request_armed_at is None
                                and current.known_image_id is None
                                and current.candidate_image_id is not None
                                and session.exec(
                                    select(MaterialCoverReceipt.id)
                                    .where(MaterialCoverReceipt.job_id == current.id)
                                    .limit(1)
                                ).first()
                                is None
                            )
                        )
                    )
                )
            ):
                # 未发送瞬断与只读瞬断共用同job的三次上限；不重试权限/契约错误。
                # 候选一旦持久化只能核查；已armed身份绝不退回新的图片上传。
                current.error_code = code
                _queue(
                    session,
                    current,
                    read=read or bool(current.candidate_image_id),
                    delay=5 * 2 ** (current.failure_count - 1),
                )
            else:
                _stop(current, code, unknown=bool(current.request_armed_at))


# 本地短事务（锁等待、语句超时、任务期限在发送前耗尽）失败时请求从未发出，
# 属于搭建高峰的瞬时拥塞，不是封面本身的问题。按指数退避重排，单个任务最多
# LOCAL_RETRY_LIMIT 次（约 6 分钟）；不能像合同/权限错误一样落成需人工处理的终态。
LOCAL_TRANSIENT_CODES = frozenset(
    {"tiktok_local_resources_unavailable", "tiktok_call_deadline_exceeded"}
)
LOCAL_RETRY_LIMIT = 8

# SDK读取超时被统一包装为tiktok_response_error；库存索引短暂缺页会触发
# cover_schema_unsupported。二者只在读取阶段有限重试，不放宽字段校验。
COVER_READ_TRANSIENT_CODES = frozenset(
    {"tiktok_response_error", "cover_schema_unsupported"}
)
COVER_READ_RETRY_LIMIT = 3


def read_transient(error: Exception) -> bool:
    return isinstance(error, DomainError) and error.code in COVER_READ_TRANSIENT_CODES


def local_transient(error: Exception) -> bool:
    # 已发送的调用一律由适配器转成 RemoteCallError；普通 DomainError 只来自发送前的本地检查。
    return (
        isinstance(error, DomainError)
        and not isinstance(error, RemoteCallError)
        and error.code in LOCAL_TRANSIENT_CODES
    )


def local_retry_delay(failures: int) -> int:
    return min(60, 5 * 2 ** max(0, failures - 1))


def _read_failure(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    error: Exception,
) -> None:
    """批量只读失败仍按每项原 claim 恢复，绝不切回上传阶段。"""
    with Session(database_engine) as session, session.begin():
        current = _fenced(session, context, job.id, nonce)
        if current is None:
            return
        if isinstance(error, AccountAdmissionDeferred):
            _queue(
                session,
                current,
                read=True,
                delay=max(1, ceil(error.retry_after_ms / 1000)),
            )
            return
        current.failure_count += 1
        code = error.code if isinstance(error, DomainError) else "cover_response_error"
        if (
            (
                isinstance(error, RemoteCallError)
                and error.code in TRANSIENT_NOT_SENT
                and current.failure_count < 3
            )
            or (
                read_transient(error) and current.failure_count < COVER_READ_RETRY_LIMIT
            )
            or (local_transient(error) and current.failure_count < LOCAL_RETRY_LIMIT)
        ):
            current.error_code = code
            _queue(
                session,
                current,
                read=True,
                delay=local_retry_delay(current.failure_count),
            )
        else:
            _stop(current, code, unknown=bool(current.request_armed_at))


def _completed_cover_batches(
    first: MaterialCoverJob | type[MaterialCoverJob],
) -> SelectOfScalar[UUID]:
    # 已完成共享保留原账本，只刷新目标真实ID。未完成批次仍需要自己的
    # wake成员接续未知结果，不能被详情批读领取后遗留无唤醒的成员。
    member = aliased(MaterialCoverJob)
    identified = (
        select(func.count(col(member.id)))
        .where(
            member.share_batch_id == MaterialCoverShareBatch.id,
            member.tenant_id == MaterialCoverShareBatch.tenant_id,
            member.bc_id == MaterialCoverShareBatch.bc_id,
            member.actor_id == MaterialCoverShareBatch.actor_id,
            member.frozen_route == MaterialCoverShareBatch.target_route,
            col(member.known_image_id).is_not(None),
        )
        .correlate(MaterialCoverShareBatch)
        .scalar_subquery()
    )
    return select(MaterialCoverShareBatch.id).where(
        MaterialCoverShareBatch.tenant_id == first.tenant_id,
        MaterialCoverShareBatch.bc_id == first.bc_id,
        MaterialCoverShareBatch.actor_id == first.actor_id,
        MaterialCoverShareBatch.target_route == first.frozen_route,
        # 历史刷新失败可把账本标为UNKNOWN，但不能抹掉全部成员的已知ID。
        # 必须覆盖原成员总数；未发送成员被拆到另一批次时也不得误判完整。
        identified == func.jsonb_array_length(MaterialCoverShareBatch.members),
    )


def _known_cover_candidates(
    first: MaterialCoverJob,
) -> SelectOfScalar[MaterialCoverJob]:
    """索引支持按固定授权取下一小组，不能每轮加载或排序全部待核查素材。"""
    scoped = (
        select(MaterialCoverJob)
        .join(PendingDispatch, col(PendingDispatch.id) == MaterialCoverJob.dispatch_id)
        .where(
            MaterialCoverJob.tenant_id == first.tenant_id,
            MaterialCoverJob.bc_id == first.bc_id,
            MaterialCoverJob.actor_id == first.actor_id,
            MaterialCoverJob.advertiser_id == first.advertiser_id,
            MaterialCoverJob.connection_id == first.connection_id,
            MaterialCoverJob.frozen_route == first.frozen_route,
            MaterialCoverJob.status == "VERIFYING",
            col(MaterialCoverJob.superseded_by_id).is_(None),
            col(MaterialCoverJob.known_image_id).is_not(None),
            or_(
                col(MaterialCoverJob.share_batch_id).is_(None),
                _completed_cover_batches(first)
                .where(MaterialCoverShareBatch.id == MaterialCoverJob.share_batch_id)
                .correlate(MaterialCoverJob)
                .exists(),
            ),
            or_(
                col(MaterialCoverJob.claimed_until).is_(None),
                col(MaterialCoverJob.claimed_until) <= _now(),
            ),
            PendingDispatch.available_at <= _now(),
        )
    )
    # 任意单项 dispatch 都可作为游标；按主键范围读取，避免反复过滤前段已完成任务。
    # 两个分支分别有界，UNION ALL 外层仍只收49项，低位任务不会因此丢失。
    after = (
        scoped.where(col(MaterialCoverJob.id) > first.id)
        .order_by(col(MaterialCoverJob.id))
        .limit(49)
    )
    before = (
        scoped.where(col(MaterialCoverJob.id) < first.id)
        .order_by(col(MaterialCoverJob.id))
        .limit(49)
    )
    candidates = aliased(MaterialCoverJob, union_all(after, before).subquery())
    return select(candidates).limit(49)


def _run_known_cover_group(
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    first: MaterialCoverJob,
    first_nonce: UUID,
    *,
    deadline: datetime,
) -> bool:
    """从既有只读 outbox 合并同授权任务，保留每项永久身份与独立 claim。"""
    members = [(first, first_nonce)]
    with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
        checks = _CoverAccessChecks(db, context)
        candidates = db.exec(_known_cover_candidates(first)).all()
        completed_batches = set(
            db.exec(
                _completed_cover_batches(first).where(
                    col(MaterialCoverShareBatch.id).in_(
                        {row.share_batch_id for row in candidates if row.share_batch_id}
                    )
                )
            ).all()
        )
        # 批量 claim 与回执检查仍按 material→job 固定锁序，共用本组一个短会话。
        if candidates:
            db.exec(
                select(MaterialFile)
                .where(
                    MaterialFile.tenant_id == context.tenant_id,
                    col(MaterialFile.id).in_([row.material_id for row in candidates]),
                )
                .order_by(col(MaterialFile.id))
                .with_for_update()
            ).all()
        for candidate in candidates:
            candidate_id, candidate_dispatch, candidate_revision = (
                candidate.id,
                candidate.dispatch_id,
                candidate.revision,
            )
            current = _job(db, context, candidate_id, lock=True)
            dispatch = (
                db.get(PendingDispatch, candidate_dispatch, populate_existing=True)
                if candidate_dispatch
                else None
            )
            # 候选查询不锁任务；进入批次前必须在锁下再次匹配范围、阶段和到期时间。
            if (
                any(
                    getattr(current, field) != getattr(first, field)
                    for field in (
                        "tenant_id",
                        "bc_id",
                        "actor_id",
                        "advertiser_id",
                        "connection_id",
                        "frozen_route",
                    )
                )
                or current.status != "VERIFYING"
                or not current.known_image_id
                or (
                    current.share_batch_id is not None
                    and current.share_batch_id not in completed_batches
                )
                or dispatch is None
                or dispatch.available_at > _now()
            ):
                continue
            assert candidate_dispatch is not None
            claimed = _claim_in_session(
                db,
                context,
                candidate_id,
                candidate_dispatch,
                candidate_revision,
                read=True,
                checks=checks,
            )
            if claimed:
                job, nonce = claimed
                if _receipt_conflict(db, job):
                    _invalidate_receipt(db, job)
                else:
                    members.append(claimed)
        db.flush()
        for job, _ in members[1:]:
            db.expunge(job)
    if len(members) == 1:
        return False

    # 首个消费消息不一定是最小ID。广告刷新按job.id持锁，后续所有批量
    # HTTP前检查及结果发布也须同序，不能先锁首消息再回头锁较小ID。
    active = sorted(members, key=lambda member: member[0].id)

    def check_members() -> None:
        # 工厂会在每一次真实 HTTP 前调用，连同凭据/账户权限重新检查。
        # 同一回调共享短事务，避免逐项新建连接阻塞 MCP 流；不跨请求缓存权限。
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            checks = _CoverAccessChecks(db, context)
            for job, nonce in active:
                current = _fenced(db, context, job.id, nonce)
                if current is None:
                    raise DomainError("cover_claim_lost", "封面任务执行权已变化")
                _check_read_identity(current, job)
                _access(db, context, current, checks=checks)

    def budget(operation: str) -> material_types.RemoteCallBudget:
        return material_types.RemoteCallBudget(
            deadline, HARD_LIMIT, admission_policy(operation).lease_ms
        )

    try:
        route = load_material_route(
            first.frozen_route,
            context=context,
            bc_id=first.bc_id,
            connection_id=first.connection_id,
        )
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check_members,
        ) as gateway:
            # 视频正证据已由 _access 按身份和状态核对，不再因年龄先回读整组视频。
            check_members()
            images = gateway.materials.read_images(
                advertiser_id=first.advertiser_id,
                image_ids=tuple(
                    dict.fromkeys(
                        job.known_image_id for job, _ in active if job.known_image_id
                    )
                ),
                budget=budget("materials.get_images"),
            )
            by_image = {row.image_id: row for row in images}
            failures = []
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                checks = _CoverAccessChecks(db, context)
                db.exec(
                    select(MaterialFile)
                    .where(
                        MaterialFile.tenant_id == context.tenant_id,
                        col(MaterialFile.id).in_(
                            [job.material_id for job, _ in active]
                        ),
                    )
                    .order_by(col(MaterialFile.id))
                    .with_for_update()
                ).all()
                for job, nonce in active:
                    try:
                        with db.begin_nested():
                            assert job.known_image_id is not None
                            data = by_image.get(job.known_image_id)
                            evidence = api.verified_image(
                                {"list": [api.image_record_data(data)] if data else []},
                                image_id=job.known_image_id,
                                remote_name=job.remote_name,
                                signature=job.signature,
                                width=job.width,
                                height=job.height,
                            )
                            _read_result_in_session(
                                db, context, job, nonce, evidence, checks=checks
                            )
                    except Exception as error:
                        failures.append((job, nonce, error))
            for job, nonce, member_error in failures:
                _read_failure(database_engine, context, job, nonce, member_error)
    except Exception as error:
        for job, nonce in active:
            _read_failure(database_engine, context, job, nonce, error)
    return True


def repair_cover_dispatches(session: Session, *, limit: int = 100) -> int:
    from app.modules.builds.execution_window import cover_task_admission_condition

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Cover repair limit must be between 1 and 100")
    candidates_query = (
        select(MaterialCoverJob)
        .where(
            col(MaterialCoverJob.status).in_(["PENDING", "PREPARING", "VERIFYING"]),
            col(MaterialCoverJob.superseded_by_id).is_(None),
            or_(
                col(MaterialCoverJob.error_code).is_distinct_from("cover_window_wait"),
                cover_task_admission_condition(),
            ),
            or_(
                col(MaterialCoverJob.share_batch_id).is_(None),
                col(MaterialCoverJob.dispatch_id).is_not(None),
                col(MaterialCoverJob.id).in_(
                    select(MaterialCoverShareBatch.wake_job_id)
                ),
                # 全部成员已有目标ID后可独立批读；旧wake可能先完成。
                # 过期领取且无投递的其余成员必须由正式repair接续，
                # 未完整识别批次仍保持单wake，不能放大未知库存扫描。
                and_(
                    col(MaterialCoverJob.known_image_id).is_not(None),
                    _completed_cover_batches(MaterialCoverJob)
                    .where(
                        MaterialCoverShareBatch.id == MaterialCoverJob.share_batch_id
                    )
                    .correlate(MaterialCoverJob)
                    .exists(),
                ),
            ),
            MaterialCoverJob.repair_after <= _now(),
            or_(
                col(MaterialCoverJob.claimed_until).is_(None),
                col(MaterialCoverJob.claimed_until) <= _now(),
            ),
        )
        .order_by(col(MaterialCoverJob.repair_after), col(MaterialCoverJob.id))
        .limit(limit)
    )
    candidates = session.exec(candidates_query).all()
    if not candidates:
        return 0
    # 多次UPDATE投递身份时，外键检查也会隐式取得素材KEY SHARE锁。
    # 必须先按统一material→job顺序领取；忙碌素材留给下一轮，不能先锁
    # 住封面再阻塞视频批读。候选和所有锁均限制在本轮最多100项范围内。
    material_ids = session.exec(
        select(MaterialFile.id)
        .where(
            tuple_(col(MaterialFile.tenant_id), col(MaterialFile.id)).in_(
                {(job.tenant_id, job.material_id) for job in candidates}
            )
        )
        .order_by(col(MaterialFile.id))
        .with_for_update(skip_locked=True)
    ).all()
    if not material_ids:
        return 0
    jobs = session.exec(
        candidates_query.where(
            col(MaterialCoverJob.id).in_({job.id for job in candidates}),
            col(MaterialCoverJob.material_id).in_(material_ids),
        )
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    now = _now()
    for job in jobs:
        if job.error_code == "cover_window_wait":
            job.error_code = None
        if (
            job.status == "PENDING"
            and job.error_code == "cover_source_pending"
            and job.dispatch_id is None
        ):
            from .cover_sharing import _content_material_ids, _source

            context = TenantContext(
                tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
            )
            try:
                source = _source(session, context, job)
                preparing = session.exec(
                    select(MaterialCoverJob.id)
                    .where(
                        MaterialCoverJob.tenant_id == job.tenant_id,
                        MaterialCoverJob.bc_id == job.bc_id,
                        col(MaterialCoverJob.material_id).in_(
                            _content_material_ids(session, context, job)
                        ),
                        MaterialCoverJob.purpose == "SOURCE",
                        col(MaterialCoverJob.superseded_by_id).is_(None),
                        col(MaterialCoverJob.status).in_(
                            ["PENDING", "PREPARING", "VERIFYING"]
                        ),
                    )
                    .limit(1)
                ).first()
                if source is None and preparing is not None:
                    # 来源封面通常几十秒内就绪；短间隔复查，避免目标封面多等一整分钟。
                    job.repair_after = now + timedelta(seconds=SOURCE_WAIT_SECONDS)
                    continue
                # 来源已就绪或已落定失败：重新走原执行器的权限/来源检查，
                # 不把观察到的状态当成共享成功，也不重发已发送的来源上传。
                job.error_code = None
                _queue(
                    session,
                    job,
                    read=bool(
                        job.request_armed_at
                        or job.known_image_id
                        or job.candidate_image_id
                    ),
                )
            except DomainError as error:
                _stop(job, error.code, unknown=bool(job.request_armed_at))
            count += 1
            continue
        dispatch = session.exec(
            select(PendingDispatch)
            .where(PendingDispatch.id == job.dispatch_id)
            .with_for_update()
        ).first()
        read = bool(job.request_armed_at) or job.status == "VERIFYING"
        expected_task = "materials.verify_cover" if read else "materials.prepare_cover"
        if dispatch is not None and (
            dispatch.tenant_id != job.tenant_id
            or dispatch.actor_id != job.actor_id
            or dispatch.task_name
            not in {"materials.prepare_cover", "materials.verify_cover"}
            or dispatch.task_key != f"cover:{job.id}:{job.revision}"
            or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
        ):
            _stop(job, "dispatch_payload_invalid", unknown=bool(job.request_armed_at))
        elif dispatch is None or dispatch.task_name != expected_task:
            # A dead armed PREPARE can only advance to a new read-only phase.
            if job.request_armed_at:
                job.error_code = "cover_result_unknown"
            _queue(session, job, read=read)
        else:
            job.claim_token = job.claimed_until = None
            if (
                dispatch.published_at is not None
                and dispatch.published_at <= now - timedelta(seconds=CLAIM_SECONDS)
            ):
                dispatch.published_at = None
            # Preserve the broker's pending identity, attempts and backoff.
            job.repair_after = max(
                now, dispatch.available_at, dispatch.published_at or now
            ) + timedelta(seconds=CLAIM_SECONDS)
        count += 1
    return count


# 自动接续覆盖近期产生或刚开始发送的任务：排队超过两小时不应让
# 刚发出的共享失去恢复窗口；读取不会更新 armed 时间，仍不会无限续期。
# 再久的历史结果交给人工核查入口，
# 避免对早已废弃的素材无限读取。
AUTO_RECOVERY_AGE = timedelta(hours=2)
AUTO_RECOVERY_BACKOFF = timedelta(minutes=2)
AUTO_RETRY_BLOCKED_CODES = LOCAL_TRANSIENT_CODES | {"cover_claim_lost"}


def recover_stalled_covers(session: Session, *, limit: int = 100) -> int:
    """无人值守接续卡住的封面：UNKNOWN 只读核查，发送前瞬时失败的 BLOCKED 重新排队。

    搭建中的广告依赖这些封面；此前只能靠人工点“重试”接回，整批广告因此停在
    需要核查。这里沿用人工入口的同一规则：已 armed/有回执的只做只读核查，绝不重发。
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Cover recovery limit must be between 1 and 100")
    now = _now()
    candidates_query = (
        select(MaterialCoverJob)
        .where(
            col(MaterialCoverJob.superseded_by_id).is_(None),
            or_(
                col(MaterialCoverJob.created_at) >= now - AUTO_RECOVERY_AGE,
                col(MaterialCoverJob.request_armed_at) >= now - AUTO_RECOVERY_AGE,
            ),
            MaterialCoverJob.repair_after <= now,
            col(MaterialCoverJob.dispatch_id).is_(None),
            or_(
                col(MaterialCoverJob.claimed_until).is_(None),
                col(MaterialCoverJob.claimed_until) <= now,
            ),
            or_(
                and_(
                    col(MaterialCoverJob.status) == "UNKNOWN",
                    col(MaterialCoverJob.error_code).is_distinct_from(
                        "cover_receipt_ambiguous"
                    ),
                ),
                and_(
                    col(MaterialCoverJob.status) == "BLOCKED",
                    col(MaterialCoverJob.error_code).in_(AUTO_RETRY_BLOCKED_CODES),
                ),
            ),
        )
        .order_by(col(MaterialCoverJob.repair_after), col(MaterialCoverJob.id))
        .limit(limit)
    )
    candidates = session.exec(candidates_query).all()
    if not candidates:
        return 0
    # 与正式 repair 同一锁序：先素材后封面，忙碌的留给下一轮。
    material_ids = session.exec(
        select(MaterialFile.id)
        .where(
            tuple_(col(MaterialFile.tenant_id), col(MaterialFile.id)).in_(
                {(job.tenant_id, job.material_id) for job in candidates}
            )
        )
        .order_by(col(MaterialFile.id))
        .with_for_update(skip_locked=True)
    ).all()
    if not material_ids:
        return 0
    jobs = session.exec(
        candidates_query.where(
            col(MaterialCoverJob.id).in_({job.id for job in candidates}),
            col(MaterialCoverJob.material_id).in_(material_ids),
        )
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for job in jobs:
        context = TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        )
        try:
            with session.begin_nested():
                if job.status == "UNKNOWN" or job.request_armed_at is not None:
                    request_cover_reconciliation(
                        session, context=context, job_id=job.id
                    )
                else:
                    request_cover_retry(session, context=context, job_id=job.id)
        except DomainError:
            # 权限/内容已变化等确定性原因保留原状态，由人工入口处理。
            pass
        # 无论是否接续，都推后下一次自动检查，不在同一轮反复扫描同一任务。
        current = session.get(MaterialCoverJob, job.id)
        if current is not None and current.status in {"UNKNOWN", "BLOCKED"}:
            current.repair_after = now + AUTO_RECOVERY_BACKOFF
        count += 1
    return count
