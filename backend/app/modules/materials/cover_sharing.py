"""源封面到目标账户的 IMAGE 独立批次；发送后只能读回实际目标图片。"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Any
from typing import cast as type_cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import Engine, String, and_, cast, func, or_, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.core.context import TenantContext
from app.core.diagnostics import emit, error_fields
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as types
from app.integrations.tiktok.contracts.common import TRANSIENT_NOT_SENT, RemoteCallError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch

from . import cover_sdk as api
from . import covers
from .batch_distribution import rectangle
from .content_identity import content_key, material_content_key_expression
from .cover_models import MaterialCoverJob, MaterialCoverShareBatch
from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .routes import load_material_route, require_material_route

logger = logging.getLogger(__name__)

SCAN_PAGES_PER_SLICE = 4


class _PlanningAccessChecks(covers._CoverAccessChecks):
    """来源上传权限按固定路由与账户复用，生命周期仍限于当前规划事务。"""

    def __init__(self, session: Session, context: TenantContext):
        super().__init__(session, context)
        self.source_uploads: set[tuple[str, str, str]] = set()

    def source_upload(self, target: MaterialCoverJob, source: MaterialCoverJob) -> None:
        self._require_transaction(self.session, self.context)
        route = load_material_route(
            target.frozen_route, context=self.context, bc_id=target.bc_id
        )
        key = (route.model_dump_json(), target.bc_id, source.advertiser_id)
        if key not in self.source_uploads:
            require_material_route(
                self.session,
                context=self.context,
                route=route,
                bc_id=target.bc_id,
                advertiser_id=source.advertiser_id,
                capability="upload",
            )
            self.source_uploads.add(key)


def _content_material_ids(
    db: Session, context: TenantContext, target: MaterialCoverJob
) -> SelectOfScalar[UUID]:
    material = db.get(MaterialFile, target.material_id, populate_existing=True)
    if (
        material is None
        or material.media_type != "VIDEO"
        or material.tenant_id != context.tenant_id
        or material.video_md5 != target.video_md5
    ):
        raise DomainError("cover_video_changed", "目标视频内容身份已变化")
    # 只合并已核实的 SHA/MD5/大小；未知摘要仍按素材 ID 独立，不能按名字猜来源。
    return select(MaterialFile.id).where(
        MaterialFile.tenant_id == context.tenant_id,
        material_content_key_expression() == content_key(material),
    )


def _source(
    db: Session,
    context: TenantContext,
    target: MaterialCoverJob,
    *,
    checks: _PlanningAccessChecks | None = None,
) -> MaterialCoverJob | None:
    # 历史实际上传的 READY 图片也属于有效源；共享得到的目标图片不冒充所有者。
    rows = db.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == target.tenant_id,
            MaterialCoverJob.bc_id == target.bc_id,
            col(MaterialCoverJob.material_id).in_(
                _content_material_ids(db, context, target)
            ),
            MaterialCoverJob.video_md5 == target.video_md5,
            MaterialCoverJob.id != target.id,
            MaterialCoverJob.status == "READY",
            col(MaterialCoverJob.superseded_by_id).is_(None),
            or_(
                col(MaterialCoverJob.request_armed_at).is_not(None),
                col(MaterialCoverJob.purpose) == "SOURCE",
            ),
            or_(
                col(MaterialCoverJob.known_image_id).is_not(None),
                col(MaterialCoverJob.candidate_image_id).is_not(None),
            ),
            col(MaterialCoverJob.share_batch_id).is_(None),
        )
        .order_by(col(MaterialCoverJob.updated_at).desc(), col(MaterialCoverJob.id))
        .limit(20)
    ).all()
    for source in rows:
        if (
            covers.verified_cover_image_id(source) is None
            or not api._signature(source.signature)
            or not api._dimension(source.width)
            or not api._dimension(source.height)
        ):
            continue
        try:
            covers._access(db, context, source, checks=checks)
            (checks or _PlanningAccessChecks(db, context)).source_upload(target, source)
        except DomainError:
            continue
        return source
    return None


def _snapshot(job: MaterialCoverJob, source: MaterialCoverJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "material_id": str(job.material_id),
        "asset_id": str(job.asset_id),
        "advertiser_id": job.advertiser_id,
        "video_id": job.video_id,
        "video_md5": job.video_md5,
        "source_job_id": str(source.id),
        "source_video_id": source.video_id,
        "source_image_id": covers.verified_cover_image_id(source),
        # 已保存的真实源 MID 随图片身份冻结；仅缺失或非法时再请求平台详情。
        "source_mid": source.image_mid
        if source.image_mid and re.fullmatch(r"[0-9]+", source.image_mid)
        else None,
        "signature": source.signature,
        "width": source.width,
        "height": source.height,
        "file_name": source.remote_name,
    }


def _required[T](db: Session, model: type[T], identity: UUID) -> T:
    row = db.get(model, identity)
    if row is None:
        raise DomainError("cover_claim_lost", "封面持久身份已变化")
    return row


def _start_recorded_source(
    db: Session, context: TenantContext, target: MaterialCoverJob
) -> bool:
    """旧库存或成功事件尚未消费时，从真实成功上传位置补排源封面。"""
    remote = col(MaterialAssetOperation.remote_response)
    receipt_vid_sql = func.nullif(remote["upload_video_id"].as_string(), "")
    verified_vid_sql = func.nullif(remote["verified_upload_video_id"].as_string(), "")
    operations = db.exec(
        select(MaterialAssetOperation)
        .join(
            AccountMaterial,
            and_(
                col(AccountMaterial.tenant_id) == col(MaterialAssetOperation.tenant_id),
                col(AccountMaterial.bc_id) == col(MaterialAssetOperation.bc_id),
                col(AccountMaterial.material_id)
                == col(MaterialAssetOperation.material_id),
                col(AccountMaterial.advertiser_id)
                == col(MaterialAssetOperation.advertiser_id),
                col(AccountMaterial.video_id) == remote["video_id"].as_string(),
                cast(col(AccountMaterial.connection_id), String)
                == col(MaterialAssetOperation.frozen_route)[
                    "connection_id"
                ].as_string(),
            ),
        )
        .where(
            MaterialAssetOperation.tenant_id == context.tenant_id,
            MaterialAssetOperation.bc_id == target.bc_id,
            col(MaterialAssetOperation.material_id).in_(
                _content_material_ids(db, context, target)
            ),
            col(AccountMaterial.status) == "available",
            col(AccountMaterial.verified_at).is_not(None),
            func.length(func.trim(col(AccountMaterial.video_id))) > 0,
            # 先排除不属于当前实际映射的历史及无所有权证据的派生，再限制候选数量。
            or_(
                col(MaterialAssetOperation.path) == "upload_original",
                and_(
                    col(MaterialAssetOperation.path) == "share_source",
                    remote["transport"].as_string() == "url_relay",
                    remote["content_md5"].as_string() == target.video_md5,
                    func.coalesce(remote["conflicting_video_id"].as_string(), "") == "",
                    func.coalesce(receipt_vid_sql, verified_vid_sql)
                    == col(AccountMaterial.video_id),
                    or_(
                        receipt_vid_sql.is_(None),
                        verified_vid_sql.is_(None),
                        receipt_vid_sql == verified_vid_sql,
                    ),
                ),
            ),
            MaterialAssetOperation.status == "succeeded",
        )
        .order_by(col(MaterialAssetOperation.id))
        .limit(20)
    ).all()
    for operation in operations:
        evidence = operation.remote_response
        receipt_vid = evidence.get("upload_video_id")
        verified_vid = evidence.get("verified_upload_video_id")
        owned_vid = receipt_vid or verified_vid
        if operation.path == "share_source" and (
            evidence.get("transport") != "url_relay"
            or not owned_vid
            or owned_vid != evidence.get("video_id")
            or (receipt_vid and verified_vid and receipt_vid != verified_vid)
            or evidence.get("content_md5") != target.video_md5
            or evidence.get("conflicting_video_id")
        ):
            # 上传原回执和丢回执后的严格读回分开取证；有矛盾不能以其中之一覆盖另一份。
            # 跨 BC URL relay 是实际上传；native_share 或不匹配的回执不证明所有权。
            continue
        asset = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == target.bc_id,
                AccountMaterial.material_id == operation.material_id,
                AccountMaterial.advertiser_id == operation.advertiser_id,
                AccountMaterial.video_id == operation.remote_response.get("video_id"),
                AccountMaterial.status == "available",
            )
        ).first()
        if asset is None:
            continue
        route = load_material_route(
            operation.frozen_route, context=context, bc_id=target.bc_id
        )
        if asset.connection_id != route.connection_id:
            continue
        result = covers.ensure_source_cover(
            db,
            context=context,
            bc_id=target.bc_id,
            material_id=operation.material_id,
            advertiser_id=asset.advertiser_id,
            task_key=f"cover-source:{operation.id}",
            route=route,
        )
        if result.state == "blocked" and result.task_id:
            source = covers._job(db, context, result.task_id, lock=True)
            if (
                source.status == "BLOCKED"
                and source.error_code in covers.COVER_READ_TRANSIENT_CODES
                and source.failure_count < covers.COVER_READ_RETRY_LIMIT
                and source.request_armed_at is None
                and source.known_image_id is None
                and source.dispatch_id is None
                and not (source.claimed_until and source.claimed_until > covers._now())
            ):
                # 有真实原件上传证据的新搭建可接续旧读取失败，不受后台两小时
                # 扫描窗口限制；保留失败计数，不能因每个目标到来而重置预算。
                failures = source.failure_count
                result = covers.request_cover_retry(
                    db, context=context, job_id=source.id
                )
                source.failure_count = failures
        return result.state in {"queued", "ready"}
    return False


def _prepare(
    db: Session, context: TenantContext, first: MaterialCoverJob, nonce: UUID
) -> tuple[MaterialCoverShareBatch, dict[UUID, UUID]] | None:
    # 同固定授权只锁领取事务，远端调用完全在事务外运行。
    identity = (
        str(first.tenant_id),
        first.bc_id,
        str(first.actor_id),
        first.frozen_route,
    )
    lock = int.from_bytes(
        sha256(json.dumps(identity, sort_keys=True).encode()).digest()[:8],
        "big",
        signed=True,
    )
    type_cast(SASession, db).execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock}
    )
    anchor = covers._job(db, context, first.id)
    if (
        anchor.claim_token != nonce
        or not anchor.claimed_until
        or anchor.claimed_until <= covers._now()
    ):
        raise DomainError("cover_claim_lost", "封面执行权已变化")
    checks = _PlanningAccessChecks(db, context)
    source = _source(db, context, anchor, checks=checks)
    if source is None:
        db.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_(_content_material_ids(db, context, anchor)),
            )
            .order_by(col(MaterialFile.id))
            .with_for_update()
        ).all()
        locked_anchor = covers._fenced(db, context, first.id, nonce)
        if locked_anchor is None:
            raise DomainError("cover_claim_lost", "封面执行权已变化")
        anchor = locked_anchor
        started = _start_recorded_source(db, context, anchor)
        preparing = db.exec(
            select(MaterialCoverJob.id)
            .where(
                MaterialCoverJob.tenant_id == first.tenant_id,
                MaterialCoverJob.bc_id == first.bc_id,
                col(MaterialCoverJob.material_id).in_(
                    _content_material_ids(db, context, first)
                ),
                MaterialCoverJob.purpose == "SOURCE",
                col(MaterialCoverJob.superseded_by_id).is_(None),
                col(MaterialCoverJob.status).in_(["PENDING", "PREPARING", "VERIFYING"]),
            )
            .limit(1)
        ).first()
        if preparing or started:
            # 自动提升可能沿用持有图片候选的当前 job；续跑不能绕过库存只读核查。
            if anchor.purpose == "SOURCE":
                covers._queue(
                    db,
                    anchor,
                    read=bool(
                        anchor.request_armed_at
                        or anchor.known_image_id
                        or anchor.candidate_image_id
                    ),
                )
            else:
                # 目标等待来源时不再次投递自己，否则同队列中的来源核验会
                # 被大量目标轮询挤压。来源落定后由持久恢复检查唤醒原任务。
                anchor.status, anchor.error_code = "PENDING", "cover_source_pending"
                anchor.claim_token = anchor.claimed_until = anchor.dispatch_id = None
                anchor.updated_at = covers._now()
                anchor.repair_after = covers._now() + timedelta(
                    seconds=covers.CLAIM_SECONDS
                )
        else:
            covers._stop(anchor, "cover_source_unavailable", unknown=False)
        return None
    from .cover_candidates import candidate_query

    # 一次远端共享最多20项×10账户。只检查包含当前锚点的一组候选；
    # 其余任务保留原投递，避免积压越大、每次领取的本地SQL越多而永远超时。
    rows = db.exec(candidate_query(first)).all()
    # 同一平台图片可属于多个本地视频；矩形领取按本地素材保留全部目标 job。
    pairs = {(str(anchor.material_id), anchor.advertiser_id): (anchor, source)}
    sources: dict[UUID, MaterialCoverJob | None] = {anchor.material_id: source}
    for job in rows:
        if job.material_id not in sources:
            sources[job.material_id] = _source(db, context, job, checks=checks)
        candidate = sources[job.material_id]
        if candidate and (candidate.advertiser_id, candidate.frozen_route) == (
            source.advertiser_id,
            source.frozen_route,
        ):
            pairs[(str(job.material_id), job.advertiser_id)] = (job, candidate)
    selected = rectangle(set(pairs), (str(anchor.material_id), anchor.advertiser_id))
    materials = db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_(
                {job.material_id for pair in selected for job in pairs[pair]}
            ),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if covers._fenced(db, context, first.id, nonce) is None:
        raise DomainError("cover_claim_lost", "封面执行权已变化")
    claims = _claim_members(
        db,
        context,
        first.id,
        nonce,
        [pairs[pair][0] for pair in sorted(selected)],
        materials,
        checks,
    )
    members = []
    for pair in sorted(selected):
        job, selected_source = pairs[pair]
        members.append(_snapshot(job, selected_source))
    batch = MaterialCoverShareBatch(
        tenant_id=first.tenant_id,
        bc_id=first.bc_id,
        actor_id=first.actor_id,
        source_advertiser_id=source.advertiser_id,
        source_route=type_cast(dict[str, Any], source.frozen_route),
        target_route=type_cast(dict[str, Any], first.frozen_route),
        members=members,
        wake_job_id=first.id,
    )
    db.add(batch)
    db.flush()
    for member in members:
        job = _required(db, MaterialCoverJob, UUID(member["job_id"]))
        job.share_batch_id = batch.id
        if job.id != first.id:
            job.dispatch_id = None
        job.width, job.height, job.signature = (
            member["width"],
            member["height"],
            member["signature"],
        )
    db.flush()
    db.expunge(batch)
    return batch, claims


def _claim_members(
    db: Session,
    context: TenantContext,
    anchor_id: UUID,
    anchor_nonce: UUID,
    jobs: list[MaterialCoverJob],
    materials: Sequence[MaterialFile],
    checks: covers._CoverAccessChecks,
) -> dict[UUID, UUID]:
    """短事务批读领取；任一成员失效即回滚整个矩形，不生成部分共享。"""
    # 先保存候选消息身份：populate_existing 会原地刷新同一个 ORM 对象。
    expected = {job.id: (job.dispatch_id, job.revision) for job in jobs}
    locked = db.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            col(MaterialCoverJob.id).in_(expected),
        )
        .order_by(col(MaterialCoverJob.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(locked) != len(expected):
        raise DomainError("cover_claim_lost", "封面成员已被领取")
    # 保持 material → anchor → 同伴的持锁顺序；共同权限/窗口仅在本事务复用。
    dispatches = {
        dispatch.id: dispatch
        for dispatch in db.exec(
            select(PendingDispatch)
            .where(
                PendingDispatch.tenant_id == context.tenant_id,
                col(PendingDispatch.id).in_(
                    {
                        value[0]
                        for identity, value in expected.items()
                        if identity != anchor_id and value[0] is not None
                    }
                ),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    assets = {
        asset.id: asset
        for asset in db.exec(
            select(AccountMaterial)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                col(AccountMaterial.id).in_({job.asset_id for job in locked}),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    material_by_id = {material.id: material for material in materials}
    checks.preload_admission(list(locked))
    claims = {anchor_id: anchor_nonce}
    for job in locked:
        if job.id == anchor_id:
            if (
                job.superseded_by_id is not None
                or job.claim_token != anchor_nonce
                or not job.claimed_until
                or job.claimed_until <= covers._now()
            ):
                raise DomainError("cover_claim_lost", "封面执行权已变化")
            continue
        dispatch_id, revision = expected[job.id]
        dispatch = dispatches.get(dispatch_id) if dispatch_id else None
        if job.claimed_until and job.claimed_until > covers._now():
            # 候选读取后另一个 worker 合法领取了同伴，是正常竞争且尚未发送；
            # 整个矩形回滚后让锚点稍后重排，不能把无辜锚点终态判失败。
            raise DomainError("cover_claim_lost", "封面成员已被领取", retryable=True)
        if (
            job.superseded_by_id is not None
            or job.error_code == "cover_receipt_ambiguous"
            or job.actor_id != context.actor_id
            or job.dispatch_id != dispatch_id
            or job.revision != revision
            or job.status not in {"PENDING", "PREPARING", "VERIFYING"}
            or dispatch is None
            or dispatch.actor_id != job.actor_id
            or dispatch.task_name != "materials.prepare_cover"
            or dispatch.task_key != f"cover:{job.id}:{job.revision}"
            or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
            or job.request_armed_at is not None
            or (job.purpose == "BUILD" and not checks.admitted(db, context, job))
        ):
            raise DomainError("cover_claim_lost", "封面成员已被领取")
        try:
            checks.check(db, context, job, upload=True)
        except DomainError:
            # 单成员领取会_stop后返回None；调用方随后抛错，整个事务回滚。
            raise DomainError("cover_claim_lost", "封面成员已被领取") from None
        material = material_by_id.get(job.material_id)
        asset = assets.get(job.asset_id)
        # 与普通领取的_digest_error/_mapping保持相同内容身份要求，不能按缓存VID猜测。
        if (
            not job.video_md5
            or material is None
            or material.tenant_id != job.tenant_id
            or material.video_md5 != job.video_md5
            or asset is None
            or not asset.verified_at
            or (
                asset.tenant_id,
                asset.bc_id,
                asset.material_id,
                asset.advertiser_id,
                asset.connection_id,
                asset.video_id,
                asset.status,
            )
            != (
                job.tenant_id,
                job.bc_id,
                job.material_id,
                job.advertiser_id,
                job.connection_id,
                job.video_id,
                "available",
            )
        ):
            raise DomainError("cover_claim_lost", "封面成员已被领取")
    # 所有读验完成再修改ORM，避免后续账户权限查询触发逐成员autoflush。
    for job in locked:
        if job.id == anchor_id:
            continue
        claim = uuid4()
        job.claim_token = claim
        job.claimed_until = covers._now() + timedelta(seconds=covers.CLAIM_SECONDS)
        job.status, job.repair_after = "PREPARING", job.claimed_until
        claims[job.id] = claim
    return claims


def _resume(
    db: Session, context: TenantContext, first: MaterialCoverJob, nonce: UUID
) -> tuple[MaterialCoverShareBatch, dict[UUID, UUID]]:
    batch = db.exec(
        select(MaterialCoverShareBatch)
        .where(
            MaterialCoverShareBatch.id == first.share_batch_id,
            MaterialCoverShareBatch.tenant_id == context.tenant_id,
            MaterialCoverShareBatch.bc_id == first.bc_id,
            MaterialCoverShareBatch.actor_id == context.actor_id,
        )
        # 批次主键不可变；NO KEY UPDATE仍互斥发送者，允许成员更新的
        # 外键KEY SHARE检查，消除batch→job与retry job→FK batch的锁环。
        .with_for_update(key_share=True)
    ).one()
    members = {UUID(member["job_id"]): member for member in batch.members}
    # 前置_claim已独立提交；这里保持批次→按ID锁成员的次序，不反向获取素材锁。
    # 锁齐并校验后才集中改ORM，避免每个db.get触发上一成员的autoflush。
    jobs = db.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            col(MaterialCoverJob.id).in_(members),
        )
        .order_by(col(MaterialCoverJob.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(jobs) != len(members) or first.id not in members:
        raise DomainError("cover_claim_lost", "共享核查成员范围已变化")
    now = covers._now()
    pending = []
    for job in jobs:
        if job.status == "READY" and job.id != first.id:
            continue
        if (
            job.superseded_by_id is not None
            or job.bc_id != batch.bc_id
            or job.actor_id != context.actor_id
            or job.share_batch_id != batch.id
            or job.advertiser_id != members[job.id]["advertiser_id"]
            or job.frozen_route != batch.target_route
        ):
            raise DomainError("cover_claim_lost", "共享核查成员范围已变化")
        if job.id == first.id:
            if (
                job.claim_token != nonce
                or not job.claimed_until
                or job.claimed_until <= now
                or job.dispatch_id != first.dispatch_id
                or job.revision != first.revision
            ):
                raise DomainError("cover_claim_lost", "共享核查执行权已变化")
            continue
        if job.claimed_until and job.claimed_until > now:
            raise DomainError("cover_claim_lost", "共享核查成员正在执行")
        pending.append(job)
    if batch.status == "READY":
        # 过期 READY 的显式复核必须重新读取，不能重放上轮已缓存的正向证据。
        batch.scan_state = {}
        batch.status = "VERIFYING"
    claims = {first.id: nonce}
    for job in pending:
        job.claim_token = nonce
        job.claimed_until = now + timedelta(seconds=covers.CLAIM_SECONDS)
        job.repair_after = job.claimed_until
        job.status = "VERIFYING" if batch.armed_at else "PREPARING"
        claims[job.id] = nonce
    db.flush()
    db.expunge(batch)
    return batch, claims


def _locked_batch(
    db: Session, context: TenantContext, batch: MaterialCoverShareBatch
) -> MaterialCoverShareBatch:
    # 修改既有批次统一先锁batch再锁job；正常autoflush保留调用方刚取得的回执/游标。
    current = db.exec(
        select(MaterialCoverShareBatch)
        .where(
            MaterialCoverShareBatch.id == batch.id,
            MaterialCoverShareBatch.tenant_id == context.tenant_id,
            MaterialCoverShareBatch.bc_id == batch.bc_id,
            MaterialCoverShareBatch.actor_id == context.actor_id,
        )
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if current is None:
        raise DomainError("cover_claim_lost", "共享续跑范围已变化")
    return current


def _continue(
    db: Session,
    context: TenantContext,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    *,
    delay: int = 0,
) -> None:
    """一个批次仅一条续跑 outbox；成员不独立重扫，也不由 repair 放大调度。"""
    current = _locked_batch(db, context, batch)
    members = {UUID(member["job_id"]): member for member in current.members}
    if (
        not claims
        or current.tenant_id != context.tenant_id
        or current.bc_id != batch.bc_id
        or current.actor_id != context.actor_id
        or not set(claims) <= members.keys()
    ):
        raise DomainError("cover_claim_lost", "共享续跑范围已变化")
    # 一次按序批锁替代逐_fenced读取；其租户/当前代/nonce/到期围栏逐项保留。
    jobs = db.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            col(MaterialCoverJob.id).in_(claims),
        )
        .order_by(col(MaterialCoverJob.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    now = covers._now()
    if len(jobs) != len(claims) or any(
        job.superseded_by_id is not None
        or job.bc_id != current.bc_id
        or job.actor_id != context.actor_id
        or job.share_batch_id != current.id
        or job.advertiser_id != members[job.id]["advertiser_id"]
        or job.frozen_route != current.target_route
        or job.claim_token != claims[job.id]
        or job.claimed_until is None
        or job.claimed_until <= now
        for job in jobs
    ):
        raise DomainError("cover_claim_lost", "共享续跑执行权已变化")
    wake = (
        current.wake_job_id
        if current.wake_job_id in claims
        else sorted(claims, key=str)[0]
    )
    current.wake_job_id = wake
    for job in jobs:
        read = bool(current.armed_at or job.request_armed_at or job.known_image_id)
        if job.id == wake:
            covers._queue(db, job, read=read, delay=delay)
        else:
            job.claim_token = job.claimed_until = job.dispatch_id = None
            job.status = "VERIFYING" if read else "PENDING"
            job.repair_after = covers._now() + timedelta(
                seconds=covers.CLAIM_SECONDS + delay
            )


def _check(
    db: Session,
    context: TenantContext,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    *,
    upload: bool = False,
) -> None:
    # 每次真实请求仍重新读取；同一短事务内批读映射并按账户复用授权核查。
    from app.modules.tenants.permissions import require_tenant

    from .batch_validation import BatchSourceVerifier

    current = db.get(MaterialCoverShareBatch, batch.id, populate_existing=True)
    if current is None or (
        current.tenant_id,
        current.bc_id,
        current.actor_id,
        current.target_route,
        current.source_route,
        current.members,
    ) != (
        batch.tenant_id,
        batch.bc_id,
        batch.actor_id,
        batch.target_route,
        batch.source_route,
        batch.members,
    ):
        raise DomainError("cover_claim_lost", "共享批次范围已变化")
    require_tenant(
        db, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    jobs = {
        row.id: row
        for row in db.exec(
            select(MaterialCoverJob)
            .where(
                MaterialCoverJob.tenant_id == context.tenant_id,
                col(MaterialCoverJob.id).in_(claims),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    source_ids = {UUID(member["source_job_id"]) for member in batch.members}
    sources = {
        row.id: row
        for row in db.exec(
            select(MaterialCoverJob)
            .where(
                MaterialCoverJob.tenant_id == context.tenant_id,
                col(MaterialCoverJob.id).in_(source_ids),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    # 批次 BC 约束实际视频/图片位置；共享内容可来自租户内任意原上传 BC。
    materials = db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_({job.material_id for job in jobs.values()}),
        )
        .execution_options(populate_existing=True)
    ).all()
    verifier = BatchSourceVerifier(
        db,
        context=context,
        source_bc_id=batch.bc_id,
        source_asset_ids={job.asset_id for job in (*jobs.values(), *sources.values())},
        materials=materials,
    )
    target_route = load_material_route(
        batch.target_route, context=context, bc_id=batch.bc_id
    )
    source_route = load_material_route(
        batch.source_route, context=context, bc_id=batch.bc_id
    )
    now = covers._now()

    def mapping(job: MaterialCoverJob) -> AccountMaterial:
        item = verifier.source(
            db,
            context=context,
            bc_id=batch.bc_id,
            material_id=job.material_id,
            source_asset_id=job.asset_id,
        )
        material = verifier.materials.get(job.material_id)
        if (
            item is None
            or material is None
            or material.video_md5 != job.video_md5
            or (item.advertiser_id, item.video_id, item.connection_id)
            != (job.advertiser_id, job.video_id, job.connection_id)
        ):
            raise DomainError("cover_video_changed", "视频映射或摘要已变化")
        return item

    for member in batch.members:
        identity = UUID(member["job_id"])
        if identity not in claims:
            continue
        job = jobs.get(identity)
        if (
            job is None
            or job.share_batch_id != batch.id
            or job.claim_token != claims[identity]
            or not job.claimed_until
            or job.claimed_until <= now
        ):
            raise DomainError("cover_claim_lost", "共享成员执行权已变化")
        if (
            str(job.material_id),
            str(job.asset_id),
            job.advertiser_id,
            job.video_id,
            job.video_md5,
            job.frozen_route,
        ) != (
            member["material_id"],
            member["asset_id"],
            member["advertiser_id"],
            member["video_id"],
            member["video_md5"],
            batch.target_route,
        ):
            raise DomainError("cover_video_changed", "目标视频或连接已变化")
        mapping(job)
        for capability in ("read", "build", "upload") if upload else ("read", "build"):
            verifier.require_route(
                db,
                context=context,
                route=target_route,
                bc_id=batch.bc_id,
                advertiser_id=job.advertiser_id,
                capability=capability,
            )
        source = sources.get(UUID(member["source_job_id"]))
        if source is None or (
            source.bc_id,
            source.advertiser_id,
            source.video_id,
            source.video_md5,
            source.frozen_route,
            covers.verified_cover_image_id(source),
            source.signature,
            source.width,
            source.height,
        ) != (
            batch.bc_id,
            batch.source_advertiser_id,
            member["source_video_id"],
            member["video_md5"],
            batch.source_route,
            member["source_image_id"],
            member["signature"],
            member["width"],
            member["height"],
        ):
            raise DomainError("cover_source_changed", "源封面证据已变化")
        if mapping(source).image_id != member["source_image_id"]:
            raise DomainError("cover_source_changed", "源图片映射已变化")
        # source_job_id 保留真实来源身份；别名仅共享内容，绝不改写来源 job/operation。
        if content_key(verifier.materials[source.material_id]) != content_key(
            verifier.materials[job.material_id]
        ):
            raise DomainError("cover_source_changed", "源与目标的可信内容身份已变化")
        if member.get("source_mid") and source.image_mid != member["source_mid"]:
            raise DomainError("cover_source_changed", "源图片 MID 已变化")
        verifier.require_route(
            db,
            context=context,
            route=source_route,
            bc_id=batch.bc_id,
            advertiser_id=source.advertiser_id,
            capability="read",
        )
        verifier.require_route(
            db,
            context=context,
            route=target_route,
            bc_id=batch.bc_id,
            advertiser_id=source.advertiser_id,
            capability="upload" if upload else "read",
        )
    verifier.recheck_transaction()


def _matching_images(
    members: list[dict[str, Any]], rows: tuple[types.ImageRecord, ...]
) -> dict[str, dict[str, str]]:
    found = {}
    for member in members:
        for row in rows:
            if row.signature != member["signature"]:
                continue
            evidence = api.verified_image(
                {"list": [api.image_record_data(row)]},
                image_id=row.image_id,
                remote_name=member["file_name"],
                signature=member["signature"],
                width=member["width"],
                height=member["height"],
            )
            if evidence:
                found[member["job_id"]] = evidence
                break
    return found


def _scan(
    client: types.MaterialOperations,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    budget: Callable[[str], types.RemoteCallBudget],
    *,
    read_only_ids: set[UUID],
) -> tuple[dict[UUID, dict[str, str]], bool]:
    """每轮至多读一页；持久游标跨任务推进，避免大账户永远超时重头扫描。"""
    targets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for member in batch.members:
        if UUID(member["job_id"]) in claims:
            targets[member["advertiser_id"]].append(member)
    state = json.loads(json.dumps(batch.scan_state))
    for target, members in sorted(targets.items()):
        progress = state.setdefault(
            target, {"page": 1, "seen": [], "found": {}, "done": False}
        )
        first_share = (
            batch.armed_at is None
            and target != batch.source_advertiser_id
            and not any(UUID(member["job_id"]) in read_only_ids for member in members)
        )
        if not progress["done"] or (first_share and not progress.get("mid_probe_done")):
            # 查询只限定本次冻结源MID，不把不断变化的整座图库当成首次共享
            # 的前置依赖。已发送未知及来源自身仍只能从这里接受正证据。
            mids = [member.get("source_mid") for member in members]
            if not progress.get("mid_probe_done") and all(
                isinstance(mid, str) and re.fullmatch(r"[0-9]+", mid) for mid in mids
            ):
                result = client.search_images(
                    advertiser_id=target,
                    page=1,
                    material_ids=tuple(sorted(set(type_cast(list[str], mids)))),
                    budget=budget("materials.search_images"),
                )
                progress["mid_probe_done"] = True
                progress["found"].update(_matching_images(members, result.rows))
                progress["done"] = all(
                    member["job_id"] in progress["found"] for member in members
                )
                scoped_complete = (
                    result.total_pages <= 1
                    and result.total_number == len(result.rows)
                    and len(result.rows) <= len(set(mids))
                    and {row.image_id for row in result.rows}
                    <= {value["image_id"] for value in progress["found"].values()}
                )
                if first_share and scoped_complete:
                    # 缺项只允许原未armed账本的首次共享；不能将此结论用于
                    # UNKNOWN重发，也不能将同名拒绝直接发布为READY。
                    progress["done"] = True
                break
            page_number = progress["page"]
            page_size = progress.get("page_size", 100)
            result = client.search_images(
                advertiser_id=target,
                page=page_number,
                budget=budget("materials.search_images"),
                page_size=page_size,
            )
            ids = [row.image_id for row in result.rows]
            repeated = set(ids).intersection(progress["seen"])
            if "total" in progress and progress["total"] != result.total_number:
                # 并发共享会改变库存；废弃该目标旧分页证据，从首页重新完整
                # 核查，不能拼接不同总数后推断不存在。最多两次重新开始，
                # 持续变化仍明确阻断；已发送批次始终只读，绝不因此重发。
                restarts = progress.get("restarts", 0)
                if restarts >= 2:
                    raise DomainError("cover_search_incomplete", "图片分页范围持续变化")
                state[target] = {
                    "page": 1,
                    "seen": [],
                    "found": {},
                    "done": False,
                    "restarts": restarts + 1,
                    "mid_probe_done": progress.get("mid_probe_done", False),
                }
                break
            if (repeated and result.total_number is None) or len(set(ids)) != len(ids):
                raise DomainError("cover_search_incomplete", "图片分页范围已变化")
            # 平台按修改时间排序，同总数的相邻页也可能重叠。只累计唯一ID，
            # 达到完整库存计数才允许形成“尚未找到”的结论；不把重复行算成新素材。
            progress["seen"] = sorted(set(progress["seen"]).union(ids))
            progress["total"] = result.total_number
            if (
                result.total_number is not None
                and len(progress["seen"]) > result.total_number
            ):
                raise DomainError("cover_search_incomplete", "图片分页范围已变化")
            progress["found"].update(_matching_images(members, result.rows))
            progress["done"] = all(
                member["job_id"] in progress["found"] for member in members
            )
            # 同总数跨轮累计达到唯一库存数即已完整；补查轮无需再读尾页。
            if (
                result.total_number is not None
                and result.total_number < 10000
                and len(progress["seen"]) == result.total_number
            ):
                progress["done"] = True
            if not progress["done"] and page_number >= result.total_pages:
                if result.total_number is not None and result.total_number >= 10000:
                    raise DomainError(
                        "cover_search_incomplete", "目标图片超过平台可完整搜索范围"
                    )
                if (
                    result.total_number is not None
                    and len(progress["seen"]) < result.total_number
                ):
                    # 最多三轮补齐缺失ID，仍不完整就保留阻断，绝不反复自动共享。
                    rounds = progress.get("round", 1)
                    if rounds >= 3:
                        raise DomainError(
                            "cover_search_incomplete", "图片库存分页仍不完整"
                        )
                    progress["round"], progress["page"] = rounds + 1, 1
                    # modify_time相同的行在固定分页边界可永久重叠；只重复
                    # 100条页不能补齐。用不同合法页大小移开边界，仍要求
                    # 唯一ID总数完整，最多三轮；不能降低未知结果保护。
                    progress["page_size"] = 71 if rounds == 1 else 53
                    break
                progress["done"] = True
            if not progress["done"] and page_number * page_size >= 10000:
                raise DomainError(
                    "cover_search_incomplete", "目标图片超过平台可完整搜索范围"
                )
            progress["page"] = page_number + 1
            break
    batch.scan_state = state
    done = all(
        state.get(target, {}).get("done", False)
        and (
            batch.armed_at is not None
            or any(
                UUID(member["job_id"]) in read_only_ids for member in targets[target]
            )
            or target == batch.source_advertiser_id
            or state.get(target, {}).get("mid_probe_done", False)
        )
        for target in targets
    )
    found = {
        UUID(identity): evidence
        for progress in state.values()
        for identity, evidence in progress["found"].items()
        if UUID(identity) in claims
    }
    return found, done


def _publish(
    db: Session,
    context: TenantContext,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    found: dict[UUID, dict[str, str]],
) -> None:
    # 空探测只保存分页游标；没有结果可发布，不锁素材或重复整批鉴权。
    # 请求前的_check以及后续_continue的批次/成员租约围栏仍分别执行。
    if not found:
        return
    db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            or_(
                col(MaterialFile.id).in_(
                    {UUID(member["material_id"]) for member in batch.members}
                ),
                col(MaterialFile.id).in_(
                    select(MaterialCoverJob.material_id).where(
                        MaterialCoverJob.tenant_id == context.tenant_id,
                        col(MaterialCoverJob.id).in_(
                            {UUID(member["source_job_id"]) for member in batch.members}
                        ),
                    )
                ),
            ),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    # 与单项发布保持素材→batch→job次序；_resume仅锁batch→job，不反向拿素材锁。
    _locked_batch(db, context, batch)
    _check(db, context, batch, claims)
    checks = covers._CoverAccessChecks(db, context)
    for identity, evidence in found.items():
        job = covers._fenced(db, context, identity, claims[identity])
        if job is None:
            raise DomainError("cover_claim_lost", "图片核实发布执行权已变化")
        # 目标图片来自当前账户搜索；此时间表示批次确认，绝不伪造目标图片上传。
        job.request_armed_at = batch.armed_at or covers._now()
        covers._publish_result(db, context, job, evidence, checks=checks)


def _scan_slice(
    client: types.MaterialOperations,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    budget: Callable[[str], types.RemoteCallBudget],
    *,
    deadline: datetime,
    read_only_ids: set[UUID],
) -> tuple[dict[UUID, dict[str, str]], bool]:
    """至多4次读取；完整MID探测可接下一账户，完整库存仍有界续页。"""
    targets = sorted(
        {
            member["advertiser_id"]
            for member in batch.members
            if UUID(member["job_id"]) in claims
        }
    )
    for _ in range(SCAN_PAGES_PER_SLICE):
        target = next(
            (
                name
                for name in targets
                if not batch.scan_state.get(name, {}).get("done")
            ),
            None,
        )
        probed = {
            name
            for name, state in batch.scan_state.items()
            if state.get("mid_probe_done")
        }
        found, done = _scan(client, batch, claims, budget, read_only_ids=read_only_ids)
        if done or found or (deadline - covers._now()).total_seconds() < 20:
            # 已读到的正证据立即持久发布，不让下一账户的错误抹掉本次成果。
            break
        if any(
            name not in probed and state.get("mid_probe_done") and state.get("done")
            for name, state in batch.scan_state.items()
        ):
            # 精确定向查询已完整落定目标，余量足够时复用会话处理下一账户；
            # 每次请求仍重新检查授权，四次总上限不变，未知空结果不能进入此分支。
            continue
        progress = batch.scan_state.get(target, {}) if target is not None else {}
        # 不完整探测或总数变化仍让出；全库存读取保留同账户连续分页，
        # 不靠提高Worker期限或并发消化积压。
        if progress.get("done") or (
            progress.get("page", 1) == 1 and not progress.get("round")
        ):
            break
    return found, done


def run_shared_cover(
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    first: MaterialCoverJob,
    nonce: UUID,
    *,
    deadline: datetime,
) -> None:
    batch = None
    current: MaterialCoverShareBatch | None
    claims: dict[UUID, UUID] = {first.id: nonce}
    armed_this_turn = False
    started = monotonic()
    phase, outcome, error_code = "claim", "no_work", None
    check_count, check_seconds, found_count = 0, 0.0, 0
    batch_id = None
    try:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            prepared = (
                _resume(db, context, first, nonce)
                if first.share_batch_id
                else _prepare(db, context, first, nonce)
            )
            read_only_ids = (
                set(
                    db.exec(
                        select(MaterialCoverJob.id).where(
                            col(MaterialCoverJob.id).in_(prepared[1]),
                            or_(
                                col(MaterialCoverJob.request_armed_at).is_not(None),
                                col(MaterialCoverJob.known_image_id).is_not(None),
                            ),
                        )
                    ).all()
                )
                if prepared
                else set()
            )
        if prepared is None:
            return
        batch, claims = prepared
        batch_id = str(batch.id)
        phase, outcome = "open_gateway", "in_progress"

        def check() -> None:
            nonlocal check_count, check_seconds
            check_started = monotonic()
            check_count += 1
            try:
                with (
                    bounded_session(database_engine, task_deadline=deadline) as db,
                    db.begin(),
                ):
                    _check(db, context, batch, claims)
            finally:
                check_seconds += monotonic() - check_started

        def budget(operation: str) -> types.RemoteCallBudget:
            return types.RemoteCallBudget(
                deadline, covers.HARD_LIMIT, admission_policy(operation).lease_ms
            )

        route = load_material_route(
            batch.target_route, context=context, bc_id=batch.bc_id
        )
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check,
        ) as gateway:
            if batch.armed_at is None and not all(
                member.get("source_mid") for member in batch.members
            ):
                phase = "source_lookup"
                # 真正的图片 MID 只能来自源账户详情，不能把 image_id 或本地请求当证据。
                records = gateway.materials.read_images(
                    advertiser_id=batch.source_advertiser_id,
                    image_ids=tuple(
                        sorted({member["source_image_id"] for member in batch.members})
                    ),
                    budget=budget("materials.get_images"),
                )
                by_id = {row.image_id: row for row in records}
                members = []
                for member in batch.members:
                    row = by_id.get(member["source_image_id"])
                    if (
                        row is None
                        or row.signature != member["signature"]
                        or not row.mid
                        or not re.fullmatch(r"[0-9]+", row.mid)
                        or not api.verified_image(
                            {"list": [api.image_record_data(row)]},
                            image_id=row.image_id,
                            remote_name=member["file_name"],
                            signature=member["signature"],
                            width=member["width"],
                            height=member["height"],
                        )
                    ):
                        raise DomainError(
                            "cover_source_mid_missing", "源图片 MID 或内容无法核实"
                        )
                    members.append(
                        {
                            **member,
                            "source_mid": row.mid,
                            "file_name": row.file_name or member["file_name"],
                        }
                    )
                with (
                    bounded_session(database_engine, task_deadline=deadline) as db,
                    db.begin(),
                ):
                    current = _locked_batch(db, context, batch)
                    _check(db, context, batch, claims)
                    current.members = members
                    for member in members:
                        _required(
                            db, MaterialCoverJob, UUID(member["source_job_id"])
                        ).image_mid = member["source_mid"]
                batch.members = members
            phase = "scan"
            found, done = _scan_slice(
                gateway.materials,
                batch,
                claims,
                budget,
                deadline=deadline,
                read_only_ids=read_only_ids,
            )
            found_count = len(found)
            phase = "publish"
            # 完整读完但预算不足时，持久保存证据后下一次正式任务再发送。
            yield_for_write = (
                done
                and batch.armed_at is None
                and (deadline - covers._now()).total_seconds() < 15
            )
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                _publish(db, context, batch, claims, found)
                current = _required(db, MaterialCoverShareBatch, batch.id)
                current.scan_state = batch.scan_state
                claims = {
                    identity: claim
                    for identity, claim in claims.items()
                    if identity not in found
                }
                # 发布与恢复身份必须同一提交：任何崩溃点都保留一个可修复的 wake。
                if not claims:
                    current.status, current.scan_state = "READY", {}
                    outcome = "ready"
                elif not done or yield_for_write:
                    outcome = "continued"
                    _continue(db, context, batch, claims)
                elif batch.armed_at is not None:
                    outcome = "unresolved"
                    rejected = {
                        UUID(member["job_id"])
                        for member in batch.members
                        if member.get("source_mid")
                        in batch.failed_infos.get(member["advertiser_id"], [])
                    }
                    for identity, claim in claims.items():
                        job = covers._fenced(db, context, identity, claim)
                        if job:
                            covers._stop(
                                job,
                                "cover_share_rejected"
                                if identity in rejected
                                else "cover_result_unknown",
                                unknown=identity not in rejected,
                            )
                    current.status, current.scan_state = (
                        ("BLOCKED" if set(claims) <= rejected else "UNKNOWN"),
                        {},
                    )
                elif read_only_ids.intersection(claims):
                    outcome = "unresolved"
                    # 先前复用成功的图片即使批次没有POST也只能核查。
                    # 缺失者保持UNKNOWN，其余真正未发送成员另行正式续跑。
                    for identity in read_only_ids.intersection(claims):
                        job = covers._fenced(
                            db, context, identity, claims.pop(identity)
                        )
                        if job:
                            covers._stop(job, "cover_result_unknown", unknown=True)
                    # 仅拆回确实未发送的剩余成员，让原批次保留缺失复用证据；
                    # 否则下一次_resume又会把UNKNOWN成员领回，形成永久循环。
                    for identity, claim in claims.items():
                        job = covers._fenced(db, context, identity, claim)
                        if job:
                            job.share_batch_id = None
                            covers._queue(db, job, read=False)
                    current.members = [
                        member
                        for member in current.members
                        if UUID(member["job_id"]) not in claims
                    ]
                    claims = {}
                    current.status, current.scan_state = "UNKNOWN", {}
                    yield_for_write = True
                else:
                    current.wake_job_id = (
                        current.wake_job_id
                        if current.wake_job_id in claims
                        else sorted(claims, key=str)[0]
                    )
            if not claims or not done or yield_for_write or batch.armed_at is not None:
                return
            phase = "arm"
            # 已存在目标会使原矩形变稀疏；只发送剩余授权组合中的一个矩形。
            pending = [
                member for member in batch.members if UUID(member["job_id"]) in claims
            ]
            # 主账户本身使用内容别名时只能读回现有图片；库存未证实不能发 self share。
            if any(
                member["advertiser_id"] == batch.source_advertiser_id
                for member in pending
            ):
                raise DomainError(
                    "cover_source_inventory_unverified", "主账户源图片库存尚未核实"
                )
            pairs = {
                (member["source_mid"], member["advertiser_id"]) for member in pending
            }
            chosen = rectangle(pairs, sorted(pairs)[0])
            selected = [
                member
                for member in pending
                if (member["source_mid"], member["advertiser_id"]) in chosen
            ]
            request = types.AssetShare(
                batch.source_advertiser_id,
                tuple(sorted({mid for mid, _ in chosen})),
                tuple(sorted({target for _, target in chosen})),
                "IMAGE",
            )
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                current = _locked_batch(db, context, batch)
                _check(db, context, batch, claims, upload=True)
                for member in pending:
                    if member not in selected:
                        job = _required(db, MaterialCoverJob, UUID(member["job_id"]))
                        job.share_batch_id = None
                        covers._queue(db, job, read=False)
                # 已复用 READY 的 job 仍属于真实来源批次，保留只读成员以便过期核查。
                retained = [
                    {**member, "share_requested": member in selected}
                    for member in batch.members
                    if member not in pending or member in selected
                ]
                current.members = retained
                selected_ids = {UUID(member["job_id"]) for member in selected}
                current.wake_job_id = (
                    current.wake_job_id
                    if current.wake_job_id in selected_ids
                    else sorted(selected_ids, key=str)[0]
                )
                current.request_digest = sha256(
                    json.dumps(
                        {
                            "material_ids": request.material_ids,
                            "targets": request.shared_advertiser_ids,
                            "type": "IMAGE",
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                current.armed_at, current.status = covers._now(), "SENDING"
                current.scan_state = {}
                for member in selected:
                    _required(
                        db, MaterialCoverJob, UUID(member["job_id"])
                    ).request_armed_at = current.armed_at
                db.flush()
                batch.armed_at, batch.status, batch.members = (
                    current.armed_at,
                    current.status,
                    retained,
                )
            claims = {
                UUID(member["job_id"]): claims[UUID(member["job_id"])]
                for member in selected
            }
            armed_this_turn = True
            phase = "share"
            receipt = gateway.materials.share_assets(
                request, budget=budget("materials.share_assets")
            )
            with Session(database_engine) as db, db.begin():
                current = _locked_batch(db, context, batch)
                current.failed_infos = {
                    target: list(mids) for target, mids in receipt.failed_infos.items()
                }
                current.request_id = receipt.evidence.request_id
                current.status = "VERIFYING"
                outcome = "verification_queued"
                _continue(db, context, batch, claims)
    except Exception as error:
        outcome = "error"
        emit(
            "cover_share_failed",
            tenant_id=str(context.tenant_id),
            job_id=str(first.id),
            batch_id=batch_id,
            phase=phase,
            armed_this_turn=armed_this_turn,
            **error_fields(error),
        )
        error_code = (
            error.code if isinstance(error, DomainError) else type(error).__name__
        )
        # 即使清理客户端失败也不能重发已 armed 的批次；先保存读回调度。
        with Session(database_engine) as db, db.begin():
            current = _locked_batch(db, context, batch) if batch else None
            # 本次刚置 armed 后，本地准入可在 HTTP 前拒绝。保留 armed 会使未发送
            # 批次永远扫描不存在的图片。只凭本调用的明确未发送证据解除，历史
            # UNKNOWN、已收到回执、清理失败及丢失任一成员租约均不能走此分支。
            definitely_not_sent = (
                isinstance(error, AccountAdmissionDeferred)
                and error.admission_source == "local"
            ) or (isinstance(error, RemoteCallError) and error.effect == "NOT_SENT")
            if (
                current
                and batch is not None
                and armed_this_turn
                and phase == "share"
                and definitely_not_sent
                and current.status == "SENDING"
                and current.armed_at == batch.armed_at
                and current.request_id is None
                and not current.failed_infos
            ):
                unsent = [
                    covers._fenced(db, context, identity, claim)
                    for identity, claim in claims.items()
                ]
                if unsent and all(
                    job is not None
                    and job.share_batch_id == current.id
                    and job.request_armed_at == current.armed_at
                    and job.known_image_id is None
                    and job.candidate_image_id is None
                    for job in unsent
                ):
                    # 已 armed 的批次身份受数据库不可变约束保护。保留旧批次
                    # 和明确未发送结论；仅把当前仍持有租约的成员交回正常规划。
                    current.status, current.error_code = (
                        "BLOCKED",
                        "cover_share_not_sent",
                    )
                    delay = (
                        max(1, (error.retry_after_ms + 999) // 1000)
                        if isinstance(error, AccountAdmissionDeferred)
                        else 5
                    )
                    for job in unsent:
                        assert job is not None
                        job.request_armed_at = None
                        job.share_batch_id = None
                        covers._queue(db, job, read=False, delay=delay)
                    outcome = "retry_not_sent"
                    emit(
                        "cover_share_not_sent",
                        tenant_id=str(context.tenant_id),
                        batch_id=str(current.id),
                        member_count=len(unsent),
                        retry_after_seconds=delay,
                        **error_fields(error),
                    )
                    return
            unknown = bool(current and current.armed_at)
            code = (
                error.code if isinstance(error, DomainError) else "cover_share_unknown"
            )
            if current:
                current.status, current.error_code = (
                    ("UNKNOWN" if unknown else "BLOCKED"),
                    code,
                )
            active = {}
            for identity, claim in claims.items():
                job = covers._fenced(db, context, identity, claim)
                if job:
                    if not isinstance(error, AccountAdmissionDeferred):
                        job.failure_count += 1
                    active[identity] = job
            retry = (
                isinstance(error, AccountAdmissionDeferred)
                # 共享前后都可能遇到读取超时或索引缺页；只在实际读取阶段
                # 有限重试。已armed批次保留发送身份，绝不重新共享或放宽校验。
                or (
                    phase in {"source_lookup", "scan"}
                    and covers.read_transient(error)
                    and all(
                        job.failure_count < covers.COVER_READ_RETRY_LIMIT
                        for job in active.values()
                    )
                )
                or (isinstance(error, DomainError) and error.retryable and not unknown)
                or (
                    isinstance(error, RemoteCallError)
                    and error.code in TRANSIENT_NOT_SENT
                    and all(job.failure_count < 3 for job in active.values())
                    and (unknown or error.effect == "NOT_SENT")
                )
                # 领取/登记阶段的锁等待与本地期限耗尽：批次尚未发送，退避后整体重排。
                or (
                    covers.local_transient(error)
                    and not unknown
                    and all(
                        job.failure_count < covers.LOCAL_RETRY_LIMIT
                        for job in active.values()
                    )
                )
            )
            delay = (
                covers.local_retry_delay(
                    max((job.failure_count for job in active.values()), default=1)
                )
                if covers.local_transient(error)
                else 5
            )
            if current and active and (armed_this_turn or retry):
                current.status = "UNKNOWN" if unknown else "PREPARING"
                _continue(
                    db,
                    context,
                    current,
                    {identity: claims[identity] for identity in active},
                    delay=delay,
                )
                return
            if current:
                current.scan_state = {}
            for job in active.values():
                if retry:
                    covers._queue(db, job, read=False, delay=delay)
                else:
                    covers._stop(job, code, unknown=unknown)

    finally:
        # 关联真实业务身份和阶段；不记录请求正文、凭据、图片URL或原始异常。
        logger.info(
            "cover_share_slice %s",
            json.dumps(
                {
                    "tenant_id": str(context.tenant_id),
                    "bc_id": first.bc_id,
                    "job_id": str(first.id),
                    "dispatch_id": str(first.dispatch_id)
                    if first.dispatch_id
                    else None,
                    "revision": first.revision,
                    "batch_id": batch_id,
                    "phase": phase,
                    "outcome": outcome,
                    "error_code": error_code,
                    "members": len(claims),
                    "found": found_count,
                    "armed_this_turn": armed_this_turn,
                    "check_count": check_count,
                    "check_seconds": round(check_seconds, 3),
                    "elapsed_seconds": round(monotonic() - started, 3),
                },
                sort_keys=True,
            ),
        )
