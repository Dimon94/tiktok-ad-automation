"""直接任务后台分块准备；行锁就是准备领取围栏，提交检查点后才释放。"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_many_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.expansion import _expand, _ref_dict, _use_dict
from app.modules.ad_management.expansion_cache import expansion_cache
from app.modules.ad_management.models import ManagementTask, ManagementTaskItem
from app.modules.ad_management.preparation_dispatch import preparation_watchdog_entry
from app.modules.ad_management.schemas import MutationSpec
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.tenants.permissions import require_tenant

PREPARE_CHUNK_SIZE = 200


def prepare_direct_chunk(session: Session, task_id: UUID) -> bool:
    """一次短事务原子保存最终值和游标；重复投递从上次已提交的位置继续。"""
    task = session.exec(
        select(ManagementTask)
        .where(col(ManagementTask.id) == task_id)
        .with_for_update()
    ).one_or_none()
    if task is None or task.source_kind != "DIRECT" or task.status != "PREPARING":
        return True
    context = require_tenant(
        session, actor_id=task.actor_id, tenant_id=task.tenant_id, action="ads_manage"
    )
    route = FrozenTikTokRoute.model_validate(task.route)
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    mutation = MutationSpec.model_validate(task.request["mutation"])
    raw = deepcopy(task.request["selection"])
    refs, uses = raw["refs"], raw["material_uses"]
    if uses and mutation.field == "status":
        refs = []  # 素材选择的广告引用仅作证据，不能在分块时变成广告启停。
    # 素材独立选择不把所属广告扩大成整条广告；改名一次检查整批的重名冲突。
    total = len(refs) + len(uses)
    size = PREPARE_CHUNK_SIZE
    begin, end = task.prepare_cursor, min(total, task.prepare_cursor + size)
    raw["refs"] = refs[begin : min(end, len(refs))] if begin < len(refs) else []
    raw["material_uses"] = uses[max(0, begin - len(refs)) : max(0, end - len(refs))]
    if not total:
        raw["filters"] = {}  # 空冻结集合不重新展开当前账户目录。
    record = FrozenSelectionRecord.model_validate(raw)
    with expansion_cache(session, context, task.bc_id, route, record, mutation):
        expansion = _expand(
            session,
            context,
            bc_id=task.bc_id,
            selection_id=record.id,
            mutation=mutation,
            require_capabilities=True,
            route=route,
            frozen_record=record,
        )
    # 联动父级/兄弟可能跨块重复出现：已准备的最终值永不重新覆盖。
    candidates = {
        (str(i.ref.advertiser_id), i.ref.kind, i.ref.remote_id) for i in expansion.items
    }
    from sqlalchemy import tuple_

    old = (
        session.exec(
            select(ManagementTaskItem).where(
                col(ManagementTaskItem.tenant_id) == task.tenant_id,
                col(ManagementTaskItem.task_id) == task.id,
                tuple_(
                    col(ManagementTaskItem.ref)["advertiser_id"].astext,
                    col(ManagementTaskItem.ref)["kind"].astext,
                    col(ManagementTaskItem.ref)["remote_id"].astext,
                ).in_(candidates),
            )
        ).all()
        if candidates
        else []
    )

    def identity(ref, use):
        return (
            ref["advertiser_id"],
            ref["kind"],
            ref["remote_id"],
            (use or {}).get("platform_material_id"),
            (use or {}).get("ad_material_id"),
            (use or {}).get("material_type"),
        )

    existing = {identity(i.ref, i.material_use): i for i in old}

    last_position = session.exec(
        select(col(ManagementTaskItem.position))
        .where(
            col(ManagementTaskItem.tenant_id) == task.tenant_id,
            col(ManagementTaskItem.task_id) == task.id,
        )
        .order_by(col(ManagementTaskItem.position).desc())
        .limit(1)
    ).first()
    position = 0 if last_position is None else last_position + 1
    counts = dict(task.counts)
    for item in expansion.items:
        ref, use = (
            _ref_dict(item.ref),
            _use_dict(item.material_use) if item.material_use else None,
        )
        key = identity(ref, use)
        previous = existing.get(key)
        promote = (
            previous is not None
            and previous.reason in {"linked_impact", "parent_disabled"}
            and item.execution_result == "PENDING"
        )
        if previous is not None and not promote:
            continue
        row = ManagementTaskItem(
            tenant_id=task.tenant_id,
            task_id=task.id,
            source_kind="DIRECT",
            position=position,
            ref=ref,
            material_use=use,
            original_value=str(item.original_value)
            if item.original_value is not None
            else None,
            final_value=str(item.final_value) if item.final_value is not None else None,
            reason=item.reason,
            membership_digest=record.membership_digest,
            execution_result=item.execution_result,
            grouping_revision=item.grouping_revision,
            parent_ref=_ref_dict(item.parent_ref) if item.parent_ref else None,
            capability={
                **item.capability,
                "route": task.route,
                "preview_digest": task.digest,
            },
        )
        if promote:
            # 先出现的联动提示不能遮住随后显式选中的同一对象。
            for name in (
                "original_value",
                "final_value",
                "reason",
                "execution_result",
                "grouping_revision",
                "parent_ref",
                "capability",
            ):
                setattr(previous, name, getattr(row, name))
            row = previous
            counts["skipped"] = max(0, counts.get("skipped", 0) - 1)
            counts["linked"] = max(0, counts.get("linked", 0) - 1)
        else:
            position += 1
        session.add(row)
        existing[key] = row
        metric = "targets" if item.execution_result == "PENDING" else "skipped"
        counts[metric] = counts.get(metric, 0) + 1
        if item.execution_result == "UNSUPPORTED":
            counts["unsupported"] = counts.get("unsupported", 0) + 1
        if item.linked:
            counts["linked"] = counts.get("linked", 0) + 1
    task.prepare_cursor = end
    counts["prepared"] = end + len(task.request.get("row_problems", []))
    counts["pending"] = counts.get("targets", 0)
    task.counts = counts
    task.updated_at = datetime.now(UTC)
    done = end >= total
    if done:
        session.flush()
        # 所有块完成后检查跨块冲突，尚未向平台发送；同一系列不同 ROAS、
        # 同账户多个系列最终同名都逐项跳过，不能让分块改变业务校验。
        if mutation.field in {"roas", "name"}:
            account = col(ManagementTaskItem.ref)["advertiser_id"].astext
            group = (
                col(ManagementTaskItem.parent_ref)["remote_id"].astext
                if mutation.field == "roas"
                else col(ManagementTaskItem.final_value)
            )
            candidates = session.exec(
                select(account, group)
                .where(
                    col(ManagementTaskItem.tenant_id) == task.tenant_id,
                    col(ManagementTaskItem.task_id) == task.id,
                    col(ManagementTaskItem.execution_result) == "PENDING",
                )
                .group_by(account, group)
                .having(
                    func.count(func.distinct(col(ManagementTaskItem.final_value))) > 1
                    if mutation.field == "roas"
                    else func.count() > 1
                )
            ).all()
            if candidates:
                from sqlalchemy import update

                changed = session.exec(
                    update(ManagementTaskItem)
                    .where(
                        col(ManagementTaskItem.tenant_id) == task.tenant_id,
                        col(ManagementTaskItem.task_id) == task.id,
                        col(ManagementTaskItem.execution_result) == "PENDING",
                        tuple_(account, group).in_(candidates),
                    )
                    .values(
                        execution_result="CONFLICT",
                        reason="roas_series_conflict"
                        if mutation.field == "roas"
                        else "name_duplicate_in_batch",
                    )
                )
                count = changed.rowcount
                counts["targets"] -= count
                counts["pending"] -= count
                counts["skipped"] = counts.get("skipped", 0) + count
                task.counts = counts
        task.status = "QUEUED" if counts.get("targets") else "SUCCEEDED"
    session.add(task)
    # 下一块出错时不能依赖失败连接再写重排：游标、冻结最终值与恢复消息
    # 在同一事务提交。最后一个检查点也覆盖“准备完成、执行尚未开始”的失联。
    enqueue_many_after_commit(
        session, context=context, entries=[preparation_watchdog_entry(task)]
    )
    session.flush()
    return done


def prepare_direct_task(engine, task_id: UUID) -> None:
    """只访问数据库；准备失败保留已冻结证据，禁止发送半份任务。"""
    from fastapi import HTTPException

    try:
        while True:
            with Session(engine) as session:
                done = prepare_direct_chunk(session, UUID(str(task_id)))
                session.commit()
            if done:
                return
    except (DomainError, HTTPException, ValueError) as exc:
        with Session(engine) as session:
            task = session.exec(
                select(ManagementTask)
                .where(col(ManagementTask.id) == task_id)
                .with_for_update()
            ).one_or_none()
            if task and task.status == "PREPARING":
                task.status = "FAILED"
                task.request = {
                    **task.request,
                    "preparation_error": getattr(
                        exc, "code", "management_preparation_failed"
                    ),
                    "preparation_error_message": "后台准备未完成：当前权限、连接或冻结参数已不可用，请检查后重试。",
                }
                session.add(task)
                session.commit()
