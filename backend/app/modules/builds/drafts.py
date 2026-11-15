"""Transactional preparation in bounded pages; callers commit local progress."""

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import and_, delete, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.capabilities import (
    get_capability_evidence,
    start_capability_refresh,
)
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.models import TenantBC
from app.modules.accounts.resolver import resolve_lines
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.accounts.schemas import InputLine
from app.modules.builds.image_composition_schemas import MediaKind
from app.modules.builds.models import (
    BuildDraft,
    DraftAccount,
    DraftDrama,
    DraftInput,
    DraftPreparation,
    DraftPreparationRequest,
)
from app.modules.builds.scene_job_models import (
    DraftCapabilityDependency,
    DraftSceneDependency,
    DraftScenePreparation,
    SceneJob,
)
from app.modules.builds.scene_jobs import ensure_scene_preparation
from app.modules.materials.content_identity import (
    content_key,
    material_content_key_expression,
)
from app.modules.materials.models import MaterialFile
from app.modules.materials.repository import material_visible
from app.modules.materials.service import match_materials
from app.modules.providers.capabilities import effective_link_config
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
    PromotionLink,
    ProviderConnection,
)
from app.modules.providers.repository import get_application, get_connection
from app.modules.providers.schemas import _validate_json
from app.modules.providers.service import get_link_results, prepare_links
from app.modules.strategies.models import Strategy
from app.modules.strategies.service import get_version_record
from app.modules.tenants.permissions import require_tenant

from .drama_details import material_match_text
from .image_composition_models import DraftCreativeUnitItem
from .manual_links import materialize, resolve_provider, validate_manual_links
from .video_units import add_video_unit, delete_video_units

# Engineering request bounds, independent of TikTok limits. Account processing
# remains paged even when the draft contains tens of thousands of pasted lines.
MAX_ACCOUNT_LINES = 100_000
MAX_INPUT_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 100


class _Unchanged(Enum):
    VALUE = "unchanged"


def collect_pages[T](fetch: Callable[[str | None], Page[T]]) -> Iterator[T]:
    cursor, seen = None, set()
    while True:
        page = fetch(cursor)
        yield from page.items
        cursor = page.next_cursor
        if cursor is None:
            return
        if cursor in seen:
            raise DomainError("repeated_cursor", "资源分页游标重复，请重新准备")
        seen.add(cursor)


def _authorize(session: Session, context: TenantContext, action: str = "build") -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )


def get_draft(
    session: Session, *, context: TenantContext, draft_id: UUID, lock: bool = False
) -> BuildDraft:
    _authorize(session, context, "build" if lock else "read")
    query = select(BuildDraft).where(
        col(BuildDraft.tenant_id) == context.tenant_id, col(BuildDraft.id) == draft_id
    )
    if lock:
        query = query.with_for_update()
    row = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("draft_not_found", "当前租户搭建草稿不存在")
    if lock:
        _authorize(session, context)
    return row


def _check_intent(
    session: Session,
    context: TenantContext,
    *,
    bc_id: str,
    strategy_version_id: UUID,
    provider_connection_id: UUID,
    application_id: str,
    drama_lines: list[str],
    account_lines: list[str],
    link_config: dict[str, Any],
    execution_connection_id: UUID | None = None,
) -> None:
    _authorize(session, context)
    if (
        not isinstance(bc_id, str)
        or not bc_id.strip()
        or len(bc_id) > 128
        or not isinstance(application_id, str)
        or not application_id.strip()
        or len(application_id) > 255
    ):
        raise DomainError("draft_input_invalid", "请选择当前 BC 和版权方应用")
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise DomainError("account_not_in_bc", "当前租户 BC 不可用")
    if execution_connection_id is not None:
        # 保存仅核实当前租户/BC绑定；原授权和契约版本在新准备时冻结。
        freeze_route(
            session, context=context, bc_id=bc_id, connection_id=execution_connection_id
        )
    version = get_version_record(
        session, context=context, version_id=strategy_version_id
    )
    strategy = session.get(Strategy, version.strategy_id, populate_existing=True)
    if strategy is None or not strategy.active:
        raise DomainError("strategy_not_found", "投放策略已停用或不存在")
    get_application(
        session,
        context=context,
        connection_id=provider_connection_id,
        application_id=application_id,
    )
    for values, bound in ((drama_lines, 1000), (account_lines, MAX_ACCOUNT_LINES)):
        if (
            not isinstance(values, list)
            or len(values) > bound
            or any(not isinstance(line, str) or len(line) > 1000 for line in values)
        ):
            raise DomainError(
                "draft_input_invalid",
                "剧名最多 1000 行、账户最多 100000 行，每行最多 1000 字符",
            )
    if (
        sum(len(line.encode()) for line in (*drama_lines, *account_lines))
        > MAX_INPUT_BYTES
    ):
        raise DomainError("draft_input_invalid", "粘贴输入总大小不能超过 16 MiB")
    try:
        if not isinstance(link_config, dict):
            raise ValueError
        _validate_json(link_config)
        if len(json.dumps(link_config, ensure_ascii=False).encode()) > 65536:
            raise ValueError
    except ValueError, TypeError, RecursionError:
        raise DomainError("draft_input_invalid", "推广链接配置无效") from None


def _store_inputs(
    session: Session,
    draft: BuildDraft,
    kind: str,
    lines: list[str],
    manual_links: dict[int, dict[str, Any]] | None = None,
) -> None:
    seen: dict[str, int] = {}
    for number, raw in enumerate(lines, 1):
        value = raw.strip()
        manual = (manual_links or {}).get(number, {})
        identity = (
            json.dumps(
                [
                    value,
                    {key: item for key, item in manual.items() if key != "line_no"},
                ],
                sort_keys=True,
            )
            if manual
            else value
        )
        duplicate = seen.get(identity) if value else None
        status = "empty" if not value else "duplicate" if duplicate else "pending"
        session.add(
            DraftInput(
                tenant_id=draft.tenant_id,
                draft_id=draft.id,
                kind=kind,
                line_no=number,
                raw_text=raw,
                manual_link=manual,
                status=status,
                duplicate_of=duplicate,
            )
        )
        if value:
            seen.setdefault(identity, number)
        if number % 500 == 0:
            session.flush()


def create_draft(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    strategy_version_id: UUID,
    provider_connection_id: UUID | None,
    application_id: str | None,
    drama_lines: list[str],
    account_lines: list[str],
    link_config: dict[str, Any],
    custom_provider_name: str | None = None,
    manual_links: list[dict[str, Any]] | None = None,
    request_id: UUID | None = None,
    execution_connection_id: UUID | None = None,
    media_kind: MediaKind = "VIDEO",
) -> UUID:
    if media_kind not in {"VIDEO", "CAROUSEL"}:
        raise DomainError("draft_media_kind_invalid", "草稿素材类型无效")
    provider_connection_id, application_id = resolve_provider(
        session, context, provider_connection_id, application_id, custom_provider_name
    )
    provider = get_connection(
        session, context=context, connection_id=provider_connection_id
    )
    manual = validate_manual_links(manual_links or [], drama_lines, provider.kind)
    intent: dict[str, Any] = {
        "bc_id": bc_id,
        "strategy_version_id": strategy_version_id,
        "provider_connection_id": provider_connection_id,
        "application_id": application_id,
        "drama_lines": drama_lines,
        "account_lines": account_lines,
        "link_config": link_config,
    }
    _check_intent(session, context, **intent)
    if manual:
        intent["manual_links"] = list(manual.values())
    # 未指定连接的既有请求摘要保持原文，便于原 request_id 幂等回读。
    if execution_connection_id is not None:
        intent["execution_connection_id"] = execution_connection_id
    # VIDEO 与历史省略值语义一致；新 CAROUSEL 明确进入幂等摘要。
    if media_kind != "VIDEO":
        intent["media_kind"] = media_kind
    request_id = request_id or uuid4()
    digest = hashlib.sha256(
        json.dumps(
            intent,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"draft:{context.tenant_id}:{request_id}"},
    )
    _authorize(session, context)
    prior = session.exec(
        select(BuildDraft).where(
            col(BuildDraft.tenant_id) == context.tenant_id,
            col(BuildDraft.request_id) == request_id,
        )
    ).one_or_none()
    if prior:
        if prior.request_digest != digest:
            raise DomainError("idempotency_conflict", "同一请求标识已用于不同草稿输入")
        return prior.id
    if execution_connection_id is not None:
        freeze_route(
            session, context=context, bc_id=bc_id, connection_id=execution_connection_id
        )
    draft = BuildDraft(
        media_kind=media_kind,
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        strategy_version_id=strategy_version_id,
        provider_connection_id=provider_connection_id,
        execution_connection_id=execution_connection_id,
        application_id=application_id,
        link_config=link_config,
        created_by=context.actor_id,
        request_id=request_id,
        request_digest=digest,
    )
    session.add(draft)
    session.flush()
    _store_inputs(session, draft, "drama", drama_lines, manual)
    _store_inputs(session, draft, "account", account_lines)
    session.flush()
    return draft.id


def prepare_draft(
    session: Session, *, context: TenantContext, draft_id: UUID, request_id: UUID
) -> UUID:
    _authorize(session, context)
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"draft-prepare:{context.tenant_id}:{request_id}"},
    )
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)

    prior = session.get(DraftPreparationRequest, (context.tenant_id, request_id))
    if prior:
        if prior.draft_id != draft.id:
            raise DomainError("idempotency_conflict", "请求标识已用于其他草稿")
        return prior.preparation_id
    existing = session.exec(
        select(DraftPreparation).where(
            col(DraftPreparation.tenant_id) == context.tenant_id,
            col(DraftPreparation.draft_id) == draft.id,
            col(DraftPreparation.draft_revision) == draft.revision,
        )
    ).one_or_none()
    if existing and existing.status == "PENDING":
        session.add(
            DraftPreparationRequest(
                tenant_id=context.tenant_id,
                request_id=request_id,
                draft_id=draft.id,
                preparation_id=existing.id,
            )
        )
        session.flush()
        return existing.id
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    if existing or draft.status != "DRAFT":
        _bump_revision(session, draft, draft.revision)
    # Every new local preparation starts account resolution afresh. Manual
    # groups survive while automatic groups are rebuilt in stable order.
    SASession.execute(
        session,
        delete(DraftAccount).where(
            col(DraftAccount.tenant_id) == context.tenant_id,
            col(DraftAccount.draft_id) == draft.id,
        ),
    )
    dramas = session.exec(
        select(DraftDrama).where(
            col(DraftDrama.tenant_id) == context.tenant_id,
            col(DraftDrama.draft_id) == draft.id,
        )
    ).all()
    for drama in dramas:
        if drama.material_state == "manual":
            continue
        delete_video_units(session, draft, drama_id=drama.drama_id)
        drama.material_state, drama.material_cursor, drama.matched_count = (
            "pending",
            None,
            0,
        )
        session.add(drama)
    input_rows = session.exec(
        select(DraftInput)
        .where(
            col(DraftInput.tenant_id) == context.tenant_id,
            col(DraftInput.draft_id) == draft.id,
            col(DraftInput.kind) == "drama",
        )
        .order_by(col(DraftInput.line_no))
    ).all()
    if not any(row.raw_text.strip() for row in input_rows):
        raise DomainError("draft_inputs_empty", "至少输入一部剧目后再解析准备")
    provider = session.get(ProviderConnection, draft.provider_connection_id)
    assert provider
    lines = []
    for row in input_rows:
        if row.status in {"empty", "duplicate"}:
            lines.append("")
        elif row.manual_link.get("url") or provider.kind == "other":
            materialize(session, context, draft, row)
            lines.append("")
        else:
            lines.append(row.raw_text)
    session.flush()
    prep = DraftPreparation(
        tenant_id=context.tenant_id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        actor_id=context.actor_id,
        request_id=request_id,
    )
    from app.modules.providers.link_steps import _digest

    # 与 create_preparation_request 使用同一合并规则，保证复用判断比较的是生效参数；
    # 手动版权方不自动取链，没有取链参数契约。
    link_config = (
        draft.link_config
        if provider.kind == "other"
        else effective_link_config(
            provider.kind,
            get_application(
                session,
                context=context,
                connection_id=typing_cast(UUID, draft.provider_connection_id),
                application_id=typing_cast(str, draft.application_id),
            ),
            draft.link_config,
        )
    )
    provider_digest = _digest(
        [
            str(draft.provider_connection_id),
            draft.application_id,
            list(lines),
            link_config,
        ]
    )
    previous_provider = session.exec(
        select(col(LinkPreparation.id))
        .join(
            DraftPreparation,
            and_(
                col(DraftPreparation.tenant_id) == col(LinkPreparation.tenant_id),
                col(DraftPreparation.provider_task_id) == col(LinkPreparation.id),
            ),
        )
        .where(
            col(DraftPreparation.tenant_id) == context.tenant_id,
            col(DraftPreparation.draft_id) == draft.id,
            col(LinkPreparation.connection_id) == draft.provider_connection_id,
            col(LinkPreparation.application_id) == draft.application_id,
            col(LinkPreparation.request_digest) == provider_digest,
        )
        .order_by(col(DraftPreparation.draft_revision).desc())
        .limit(1)
    ).first()
    if previous_provider is None and any(line.strip() for line in lines):
        # 单行补链可继续使用原任务，保留其他剧的已知或未知结果，不重新发送取链。
        candidate = session.exec(
            select(LinkPreparation)
            .join(
                DraftPreparation,
                (col(DraftPreparation.tenant_id) == col(LinkPreparation.tenant_id))
                & (col(DraftPreparation.provider_task_id) == col(LinkPreparation.id)),
            )
            .where(
                col(DraftPreparation.tenant_id) == context.tenant_id,
                col(DraftPreparation.draft_id) == draft.id,
                col(LinkPreparation.connection_id) == draft.provider_connection_id,
                col(LinkPreparation.application_id) == draft.application_id,
                col(LinkPreparation.config) == link_config,
            )
            .order_by(col(DraftPreparation.draft_revision).desc())
            .limit(1)
        ).first()
        if candidate:
            old_rows = session.exec(
                select(LinkPreparationItem).where(
                    col(LinkPreparationItem.tenant_id) == context.tenant_id,
                    col(LinkPreparationItem.preparation_id) == candidate.id,
                )
            ).all()
            old_lines = {row.line_no: row.raw_input for row in old_rows}
            current_lines = {row.line_no: row.raw_text.strip() for row in input_rows}
            if all(
                current_lines.get(number) == title
                for number, title in old_lines.items()
            ) and all(
                not value.strip() or old_lines.get(number) == value.strip()
                for number, value in enumerate(lines, 1)
            ):
                previous_provider = candidate.id
    if (
        previous_provider is not None
        and session.exec(
            select(col(LinkPreparationItem.id))
            .where(
                col(LinkPreparationItem.tenant_id) == context.tenant_id,
                col(LinkPreparationItem.preparation_id) == previous_provider,
                (col(LinkPreparationItem.status) == "failed")
                | (
                    (col(LinkPreparationItem.status) == "needs_resolution")
                    & (
                        col(LinkPreparationItem.resolved)["error_code"].astext
                        == "drama_not_found"
                    )
                ),
            )
            .limit(1)
        ).first()
    ):
        # 明确失败或查无剧目时，用户重新准备应重新查询，避免修复接入问题后仍复用旧失败。
        # 多候选消歧与未知写结果仍复用原任务；建链前先查已有链接，不重复建链。
        previous_provider = None
    prep.provider_task_id = (
        (
            previous_provider
            if previous_provider is not None
            else prepare_links(
                session,
                context=context,
                connection_id=typing_cast(UUID, draft.provider_connection_id),
                application_id=typing_cast(str, draft.application_id),
                lines=list(lines),
                config=link_config,
                request_id=uuid5(prep.id, "provider-links"),
            )
        )
        if any(line.strip() for line in lines)
        else None
    )
    return _enqueue_draft_preparation(session, context, draft, prep, route, request_id)


def prepare_selected_mini(
    session: Session, *, context: TenantContext, draft_id: UUID, request_id: UUID
) -> UUID:
    """小程序选择只重建场景依赖，保留已解析账户、链接和自动/手动素材分组。"""
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    if draft.status != "DRAFT":
        raise DomainError("draft_not_ready", "草稿状态已变化，请刷新后重新选择")
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    previous = session.exec(
        select(DraftPreparation)
        .where(
            col(DraftPreparation.tenant_id) == context.tenant_id,
            col(DraftPreparation.draft_id) == draft.id,
        )
        .order_by(col(DraftPreparation.draft_revision).desc())
        .limit(1)
    ).first()
    prep = DraftPreparation(
        tenant_id=context.tenant_id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        actor_id=context.actor_id,
        request_id=request_id,
        # 选择小程序或身份后只重建同媒体类型的场景依赖，保留已有图片组合。
        phase="scenes",
        provider_task_id=previous.provider_task_id if previous else None,
    )
    return _enqueue_draft_preparation(session, context, draft, prep, route, request_id)


def _enqueue_draft_preparation(
    session: Session,
    context: TenantContext,
    draft: BuildDraft,
    prep: DraftPreparation,
    route: FrozenTikTokRoute,
    request_id: UUID,
) -> UUID:
    from .draft_tasks import queue_preparation

    session.add(prep)
    draft.status = "PREPARING"
    session.add(draft)
    session.flush()
    # 草稿启动事务已锁住当前revision；在排队前固定唯一连接，所有分页继承。
    session.add(
        DraftScenePreparation(
            tenant_id=draft.tenant_id,
            draft_id=draft.id,
            preparation_id=prep.id,
            frozen_route=route.model_dump(mode="json"),
        )
    )
    session.add(
        DraftPreparationRequest(
            tenant_id=context.tenant_id,
            request_id=request_id,
            draft_id=draft.id,
            preparation_id=prep.id,
        )
    )
    session.flush()
    queue_preparation(session, prep, delay=0)
    return prep.id


def _scene_prep(
    session: Session, draft: BuildDraft, prep: DraftPreparation
) -> DraftScenePreparation:
    row = session.get(DraftScenePreparation, (draft.tenant_id, prep.id))
    if row is None or row.frozen_route is None:
        raise DomainError("scene_route_missing", "草稿准备缺少冻结连接，请重新准备")
    return row


def _preparation_route(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> FrozenTikTokRoute:
    row = _scene_prep(session, draft, prep)
    try:
        route = FrozenTikTokRoute.model_validate(row.frozen_route)
    except ValidationError:
        raise DomainError("scene_route_missing", "草稿准备缺少有效冻结连接") from None
    if (route.tenant_id, route.bc_id) != (draft.tenant_id, draft.bc_id):
        raise DomainError("scene_route_missing", "草稿冻结连接归属不一致")
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    return route


def _capabilities_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> bool:
    state = _scene_prep(session, draft, prep)
    route = _preparation_route(session, context, draft, prep)
    if state.capabilities_complete:
        return True
    if not state.capabilities_queued:
        # 场景目标沿用启动时冻结的连接；其他连接的能力不能替换它。
        connections = [route.connection_id]
        for identity in connections:
            job_id = start_capability_refresh(
                session,
                context=context,
                bc_id=draft.bc_id,
                connection_id=identity,
                request_id=uuid5(prep.id, f"capability:{identity}"),
            )
            session.add(
                DraftCapabilityDependency(
                    tenant_id=context.tenant_id,
                    draft_id=draft.id,
                    preparation_id=prep.id,
                    bc_id=draft.bc_id,
                    connection_id=identity,
                    job_id=job_id,
                )
            )
        if connections:
            state.connection_after = connections[-1]
        state.capabilities_queued = True
        session.flush()
    finished = session.exec(
        select(DraftCapabilityDependency, CapabilityJob)
        .join(
            CapabilityJob,
            (col(CapabilityJob.tenant_id) == col(DraftCapabilityDependency.tenant_id))
            & (col(CapabilityJob.id) == col(DraftCapabilityDependency.job_id)),
        )
        .where(
            col(DraftCapabilityDependency.tenant_id) == context.tenant_id,
            col(DraftCapabilityDependency.preparation_id) == prep.id,
            col(DraftCapabilityDependency.status) == "PENDING",
            col(CapabilityJob.status) != "PENDING",
        )
        .order_by(col(DraftCapabilityDependency.connection_id))
        .limit(PAGE_SIZE)
    ).all()
    for dependency, job in finished:
        if job.status == "STALE":
            dependency.job_id = start_capability_refresh(
                session,
                context=context,
                bc_id=draft.bc_id,
                connection_id=dependency.connection_id,
                request_id=uuid4(),
            )
        elif job.status == "COMPLETE" and job.scope_known and job.scope_build:
            dependency.status = "COMPLETE"
        else:
            dependency.status, dependency.error_code = (
                "BLOCKED",
                (
                    job.error_code
                    or (
                        "account_scope_unverified"
                        if not job.scope_known
                        else "account_build_unverified"
                    )
                ),
            )
    session.flush()
    pending = session.exec(
        select(col(DraftCapabilityDependency.connection_id))
        .where(
            col(DraftCapabilityDependency.tenant_id) == context.tenant_id,
            col(DraftCapabilityDependency.preparation_id) == prep.id,
            col(DraftCapabilityDependency.status) == "PENDING",
        )
        .limit(1)
    ).first()
    state.capabilities_complete = state.capabilities_queued and pending is None
    return state.capabilities_complete


def _scenes_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    from .identity_selection import selected_identity

    state = _scene_prep(session, draft, prep)
    route = _preparation_route(session, context, draft, prep)
    identity = selected_identity(draft)
    # 先以首个有素材的链接准备各账户目录。Mini 未确定时停在名称选择；
    # 用户选择覆盖本批次链接，预览仍逐剧检查各链接真实目标，不能从应用推断。
    link_id = session.exec(
        select(col(DraftDrama.link_id))
        .join(
            PromotionLink,
            (col(PromotionLink.tenant_id) == col(DraftDrama.tenant_id))
            & (col(PromotionLink.id) == col(DraftDrama.link_id)),
        )
        .where(
            col(DraftDrama.tenant_id) == context.tenant_id,
            col(DraftDrama.draft_id) == draft.id,
            col(DraftDrama.matched_count) > 0,
            col(PromotionLink.connection_id) == draft.provider_connection_id,
            col(PromotionLink.application_id) == draft.application_id,
            col(PromotionLink.status) == "ready",
            col(PromotionLink.verified_at).is_not(None),
        )
        .order_by(col(DraftDrama.first_line), col(DraftDrama.drama_id))
        .limit(1)
    ).first()
    if link_id is None:
        prep.status, prep.phase, draft.status = "READY", "done", "READY"
        return
    if not state.scenes_queued:
        query = select(col(DraftAccount.advertiser_id)).where(
            col(DraftAccount.tenant_id) == context.tenant_id,
            col(DraftAccount.draft_id) == draft.id,
        )
        if state.scene_after is not None:
            query = query.where(col(DraftAccount.advertiser_id) > state.scene_after)
        accounts = session.exec(
            query.order_by(col(DraftAccount.advertiser_id)).limit(PAGE_SIZE)
        ).all()
        for advertiser_id in accounts:
            result = ensure_scene_preparation(
                session,
                context=context,
                bc_id=draft.bc_id,
                advertiser_id=advertiser_id,
                link_id=link_id,
                route=route,
                selected_identity=identity,
                media_kind=draft.media_kind,
            )
            session.add(
                DraftSceneDependency(
                    tenant_id=context.tenant_id,
                    draft_id=draft.id,
                    preparation_id=prep.id,
                    bc_id=draft.bc_id,
                    advertiser_id=advertiser_id,
                    job_id=result.job_id,
                    status={
                        "ready": "COMPLETE",
                        "queued": "PENDING",
                        "blocked": "BLOCKED",
                    }[result.state],
                    error_code=result.reason_code,
                )
            )
        if accounts:
            state.scene_after = accounts[-1]
        state.scenes_queued = len(accounts) < PAGE_SIZE
        session.flush()
        if not state.scenes_queued:
            return
    finished = session.exec(
        select(DraftSceneDependency, SceneJob)
        .join(
            SceneJob,
            (col(SceneJob.tenant_id) == col(DraftSceneDependency.tenant_id))
            & (col(SceneJob.id) == col(DraftSceneDependency.job_id)),
        )
        .where(
            col(DraftSceneDependency.tenant_id) == context.tenant_id,
            col(DraftSceneDependency.preparation_id) == prep.id,
            col(DraftSceneDependency.status) == "PENDING",
            col(SceneJob.status) != "PENDING",
        )
        .order_by(col(DraftSceneDependency.advertiser_id))
        .limit(PAGE_SIZE)
    ).all()
    for dependency, job in finished:
        # 同一轮准备不能把已耗尽重试的只读任务再次续起；新一轮显式准备才恢复。
        if job.status == "FAILED":
            dependency.status, dependency.error_code = (
                "BLOCKED",
                job.error_code or "scene_refresh_failed",
            )
            continue
        # Once a job has completed, another account's queue delay must not turn
        # preparation into an endless refresh sweep. The pure preview will report
        # expired facts; execution ensure can renew the identical scope later.
        if (
            job.status == "COMPLETE"
            and job.expires_at
            and job.expires_at <= datetime.now(UTC)
        ):
            dependency.status, dependency.error_code = (
                "BLOCKED",
                "scene_evidence_expired",
            )
            continue
        result = ensure_scene_preparation(
            session,
            context=context,
            bc_id=draft.bc_id,
            advertiser_id=dependency.advertiser_id,
            link_id=link_id,
            route=route,
            selected_identity=identity,
            media_kind=draft.media_kind,
        )
        dependency.job_id = result.job_id
        dependency.status = {
            "ready": "COMPLETE",
            "queued": "PENDING",
            "blocked": "BLOCKED",
        }[result.state]
        dependency.error_code = result.reason_code
    session.flush()
    pending = session.exec(
        select(col(DraftSceneDependency.advertiser_id))
        .where(
            col(DraftSceneDependency.tenant_id) == context.tenant_id,
            col(DraftSceneDependency.preparation_id) == prep.id,
            col(DraftSceneDependency.status) == "PENDING",
        )
        .limit(1)
    ).first()
    if state.scenes_queued and pending is None:
        prep.status, prep.phase, draft.status = "READY", "done", "READY"


def _accounts_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    route = _preparation_route(session, context, draft, prep)
    rows = session.exec(
        select(DraftInput)
        .where(
            col(DraftInput.tenant_id) == context.tenant_id,
            col(DraftInput.draft_id) == draft.id,
            col(DraftInput.kind) == "account",
            col(DraftInput.line_no) > prep.account_after,
        )
        .order_by(col(DraftInput.line_no))
        .limit(PAGE_SIZE)
    ).all()
    if not rows:
        prep.phase = "links"
        return
    results = resolve_lines(
        session,
        context=context,
        bc_id=draft.bc_id,
        lines=[InputLine(line_no=row.line_no, raw=row.raw_text) for row in rows],
        connection_id=route.connection_id,
    )
    # Grant flags alone are not a complete role/scope proof. Before storing any
    # row of this page, wait for a renewed proof if its earlier completed job aged.
    for result in results:
        if (
            result.status == "BLOCKED"
            and result.reason == "account_access_denied"
            and result.advertiser_id
        ):
            # Diagnose only through a currently readable tenant/BC grant. The
            # strict resolver still controls admission and never upgrades UNKNOWN.
            try:
                readable = resolve_account_access(
                    session,
                    context=context,
                    bc_id=draft.bc_id,
                    advertiser_id=result.advertiser_id,
                    action="read",
                    connection_id=route.connection_id,
                )
            except DomainError:
                pass
            else:
                dependency = session.get(
                    DraftCapabilityDependency,
                    (context.tenant_id, prep.id, readable.connection_id),
                )
                if dependency and dependency.error_code:
                    result.reason = dependency.error_code
                else:
                    proof = get_capability_evidence(
                        session,
                        context=context,
                        bc_id=draft.bc_id,
                        advertiser_id=result.advertiser_id,
                        connection_id=readable.connection_id,
                    )
                    result.reason = (
                        "account_scope_unverified"
                        if proof is None or not proof.scope_verified
                        else "account_build_unverified"
                    )
        if result.status != "MATCHED" or not result.advertiser_id:
            continue
        access = resolve_account_access(
            session,
            context=context,
            bc_id=draft.bc_id,
            advertiser_id=result.advertiser_id,
            action="build",
            connection_id=route.connection_id,
        )
        proof = get_capability_evidence(
            session,
            context=context,
            bc_id=draft.bc_id,
            advertiser_id=result.advertiser_id,
            connection_id=access.connection_id,
        )
        if proof is not None and proof.can_build:
            continue
        dependency = session.get(
            DraftCapabilityDependency,
            (context.tenant_id, prep.id, access.connection_id),
        )
        if dependency is None or dependency.status == "BLOCKED":
            result.status, result.reason = (
                "BLOCKED",
                (
                    dependency.error_code
                    if dependency and dependency.error_code
                    else "account_scope_unverified"
                ),
            )
        elif proof is not None:
            result.status, result.reason = "BLOCKED", "account_build_unverified"
        else:
            dependency.job_id = start_capability_refresh(
                session,
                context=context,
                bc_id=draft.bc_id,
                connection_id=access.connection_id,
                request_id=uuid4(),
            )
            dependency.status = "PENDING"
            _scene_prep(session, draft, prep).capabilities_complete = False
            return
    for row, result in zip(rows, results, strict=True):
        row.status, row.reason_code = result.status.lower(), result.reason
        row.advertiser_id, row.duplicate_of = result.advertiser_id, result.duplicate_of
        row.candidates = [{"advertiser_id": value} for value in result.candidates]
        if result.status in {"MATCHED", "DUPLICATE"}:
            assert result.advertiser_id
            prior = session.get(
                DraftAccount, (context.tenant_id, draft.id, result.advertiser_id)
            )
            if prior:
                row.status, row.duplicate_of = "duplicate", prior.first_line
            elif result.status == "MATCHED":
                access = resolve_account_access(
                    session,
                    context=context,
                    bc_id=draft.bc_id,
                    advertiser_id=result.advertiser_id,
                    action="build",
                    connection_id=route.connection_id,
                )
                session.add(
                    DraftAccount(
                        tenant_id=context.tenant_id,
                        draft_id=draft.id,
                        bc_id=draft.bc_id,
                        advertiser_id=access.advertiser_id,
                        connection_id=access.connection_id,
                        currency=access.currency,
                        timezone=access.timezone,
                        first_line=row.line_no,
                    )
                )
        session.add(row)
    prep.account_after = rows[-1].line_no


def _links_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    if prep.provider_task_id is None:
        prep.phase = "materials"
        return
    page = get_link_results(
        session,
        context=context,
        task_id=prep.provider_task_id,
        cursor=prep.link_cursor,
        page_size=PAGE_SIZE,
    )
    from app.modules.providers.link_steps import has_runnable_link_items

    automatic_ids = []
    for result in page.items:
        row = session.exec(
            select(DraftInput).where(
                col(DraftInput.tenant_id) == context.tenant_id,
                col(DraftInput.draft_id) == draft.id,
                col(DraftInput.kind) == "drama",
                col(DraftInput.line_no) == result.line_no,
            )
        ).one()
        if row.manual_link.get("url"):
            continue  # 已补链输入不接受旧版权方结果覆盖。
        automatic_ids.append(result.input_id)
        row.status, row.reason_code, row.provider_input_id = (
            result.status,
            result.error_code,
            result.input_id,
        )
        row.drama_id = result.drama_id
        row.candidates = [
            candidate.model_dump(mode="json") for candidate in result.candidates
        ]
        if result.status == "ready":
            assert result.drama_id and result.link_id and result.title
            # DTOs do not weaken the persisted identity boundary.
            link = session.exec(
                select(PromotionLink).where(
                    col(PromotionLink.tenant_id) == context.tenant_id,
                    col(PromotionLink.id) == result.link_id,
                    col(PromotionLink.drama_id) == result.drama_id,
                    col(PromotionLink.connection_id) == draft.provider_connection_id,
                    col(PromotionLink.application_id) == draft.application_id,
                    col(PromotionLink.status) == "ready",
                )
            ).one_or_none()
            if link is None:
                raise DomainError("draft_link_invalid", "推广链接已变化，请重新准备")
            prior = session.get(
                DraftDrama, (context.tenant_id, draft.id, result.drama_id)
            )
            if prior is None:
                session.add(
                    DraftDrama(
                        tenant_id=context.tenant_id,
                        draft_id=draft.id,
                        bc_id=draft.bc_id,
                        drama_id=result.drama_id,
                        link_id=result.link_id,
                        title=result.title,
                        first_line=result.line_no,
                    )
                )
            elif draft.media_kind == "CAROUSEL":
                # 图片历史 generation 保留剧目引用；当前输入重新解析时更新现行展示位置。
                prior.first_line, prior.link_id, prior.title = (
                    result.line_no,
                    result.link_id,
                    result.title,
                )
            elif prior.first_line != result.line_no:
                row.status, row.duplicate_of = "duplicate", prior.first_line
        session.add(row)
    # 人工补链替代原结果；只等待仍参与自动取链的输入，跨页保留等待状态。
    prep.pending_links |= has_runnable_link_items(
        session,
        tenant_id=context.tenant_id,
        preparation_id=prep.provider_task_id,
        item_ids=automatic_ids,
    )
    if page.next_cursor is not None and page.next_cursor == prep.link_cursor:
        raise DomainError("repeated_cursor", "版权方分页游标重复")
    prep.link_cursor = page.next_cursor
    if page.next_cursor is None:
        prep.phase = "materials"


def _materials_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    if draft.media_kind != "VIDEO":
        raise DomainError(
            "draft_media_kind_mismatch", "图片草稿须使用图片候选与创意编排"
        )
    drama = session.exec(
        select(DraftDrama)
        .where(
            col(DraftDrama.tenant_id) == context.tenant_id,
            col(DraftDrama.draft_id) == draft.id,
            col(DraftDrama.material_state).in_(["pending", "matching"]),
        )
        .order_by(col(DraftDrama.first_line), col(DraftDrama.drama_id))
        .limit(1)
    ).first()
    if drama is None:
        if prep.pending_links:
            prep.pending_links = False
            prep.phase = "links"
        else:
            _scenes_page(session, context, draft, prep)
        return
    page = match_materials(
        session,
        context=context,
        bc_id=draft.bc_id,
        title=material_match_text(session, draft, drama),
        cursor=drama.material_cursor,
    )
    for item in page.items:
        # 库在分页期间可能新增同内容别名或切换可见代表；按内容核对已选项，
        # 保留最初冻结的文件 ID，不让同一视频重复进入素材包。分组由预览
        # 规划器根据冻结的素材包顺序决定，草稿阶段只保存一个稳定的源分组。
        exists = session.exec(
            select(col(DraftCreativeUnitItem.material_id))
            .join(
                MaterialFile,
                (col(MaterialFile.tenant_id) == col(DraftCreativeUnitItem.tenant_id))
                & (col(MaterialFile.id) == col(DraftCreativeUnitItem.material_id)),
            )
            .where(
                col(DraftCreativeUnitItem.tenant_id) == context.tenant_id,
                col(DraftCreativeUnitItem.draft_id) == draft.id,
                col(DraftCreativeUnitItem.drama_id) == drama.drama_id,
                material_content_key_expression() == item.content_key,
            )
        ).first()
        if exists is not None:
            continue
        index = drama.matched_count
        add_video_unit(
            session,
            draft,
            drama_id=drama.drama_id,
            material_id=item.material_id,
            position=index + 1,
        )
        drama.matched_count += 1
    if page.next_cursor is not None and page.next_cursor == drama.material_cursor:
        raise DomainError("repeated_cursor", "素材分页游标重复")
    drama.material_cursor = page.next_cursor
    drama.material_state = "matching" if page.next_cursor else "ready"
    session.add(drama)


def continue_draft(session: Session, *, context: TenantContext, task_id: UUID) -> bool:
    prep = session.exec(
        select(DraftPreparation)
        .where(
            col(DraftPreparation.tenant_id) == context.tenant_id,
            col(DraftPreparation.id) == task_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if prep is None:
        raise DomainError("draft_not_found", "草稿准备任务不存在")
    draft = get_draft(session, context=context, draft_id=prep.draft_id, lock=True)
    prep = session.exec(
        select(DraftPreparation)
        .where(col(DraftPreparation.id) == prep.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if prep.status != "PENDING":
        return True
    if prep.draft_revision != draft.revision:
        prep.status = "OBSOLETE"
        if draft.media_kind == "CAROUSEL":
            from .image_candidate_service import fail_initial_candidate_snapshot

            fail_initial_candidate_snapshot(session, prep, "draft_revision_conflict")
    elif prep.phase == "accounts":
        if _capabilities_page(session, context, draft, prep):
            _accounts_page(session, context, draft, prep)
    elif prep.phase == "links":
        _links_page(session, context, draft, prep)
    elif prep.phase == "materials":
        if draft.media_kind == "CAROUSEL":
            from .image_composition_service import prepare_image_candidates

            prepare_image_candidates(session, context, draft, prep)
        else:
            _materials_page(session, context, draft, prep)
    elif prep.phase == "scenes":
        _scenes_page(session, context, draft, prep)
    draft.updated_at = datetime.now(UTC)
    session.add_all([prep, draft])
    session.flush()
    return prep.status != "PENDING"


def _bump_revision(session: Session, draft: BuildDraft, expected_revision: int) -> int:
    from .image_composition_service import assert_no_image_run

    assert_no_image_run(session, draft)
    if type(expected_revision) is not int or draft.revision != expected_revision:
        raise DomainError(
            "draft_revision_conflict", "草稿已更新，请保留当前编辑并读取最新版本"
        )
    draft.revision += 1
    if draft.media_kind == "CAROUSEL":
        from .image_candidate_service import invalidate_candidate_snapshots

        invalidate_candidate_snapshots(session, draft)
    draft.updated_at = datetime.now(UTC)
    session.add(draft)
    return draft.revision


def edit_material_groups(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    drama_id: UUID,
    expected_revision: int,
    groups: list[list[UUID]],
) -> int:
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    if draft.media_kind != "VIDEO":
        raise DomainError("draft_media_kind_mismatch", "图片草稿须使用创意单元编辑")
    if draft.revision != expected_revision:
        raise DomainError(
            "draft_revision_conflict", "草稿已更新，请保留当前编辑并读取最新版本"
        )
    drama = session.get(
        DraftDrama, (context.tenant_id, draft.id, drama_id), populate_existing=True
    )
    if (
        draft.status != "READY"
        or drama is None
        or drama.material_state not in {"ready", "manual"}
    ):
        raise DomainError("draft_not_ready", "等待当前草稿准备完成后再调整素材")
    if (
        not isinstance(groups, list)
        or len(groups) > 100_000
        or any(not isinstance(group, list) or not group for group in groups)
    ):
        raise DomainError("draft_groups_invalid", "素材组不能为空组")
    identities = [identity for group in groups for identity in group]
    if (
        len(identities) > 100_000
        or any(not isinstance(identity, UUID) for identity in identities)
        or len(set(identities)) != len(identities)
    ):
        raise DomainError("draft_groups_invalid", "同一剧目不能重复使用同一素材")
    content_keys: set[str] = set()
    for offset in range(0, len(identities), PAGE_SIZE):
        chunk = identities[offset : offset + PAGE_SIZE]
        actual = session.exec(
            select(MaterialFile).where(
                col(MaterialFile.tenant_id) == context.tenant_id,
                col(MaterialFile.id).in_(chunk),
                col(MaterialFile.media_type) == "VIDEO",
                material_visible(tenant_id=context.tenant_id),
            )
        ).all()
        if {row.id for row in actual} != set(chunk):
            raise DomainError("material_not_found", "素材不在当前租户可用素材库")
        # 相同文件在多个 BC 的上传记录仍保留，但同一剧目不能重复选入相同内容。
        for row in actual:
            key = content_key(row)
            if key in content_keys:
                raise DomainError(
                    "draft_groups_invalid", "同一剧目不能重复使用相同内容的素材"
                )
            content_keys.add(key)
    revision = _bump_revision(session, draft, expected_revision)
    delete_video_units(session, draft, drama_id=drama_id)
    for position, identity in enumerate(identities, 1):
        add_video_unit(
            session, draft, drama_id=drama_id, material_id=identity, position=position
        )
    drama.matched_count, drama.material_state, drama.material_cursor = (
        len(identities),
        "manual",
        None,
    )
    session.add(drama)
    session.flush()
    return revision


def update_draft(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    expected_revision: int,
    strategy_version_id: UUID | None = None,
    provider_connection_id: UUID | None = None,
    application_id: str | None = None,
    drama_lines: list[str] | None = None,
    account_lines: list[str] | None = None,
    link_config: dict[str, Any] | None = None,
    manual_links: list[dict[str, Any]] | None = None,
    custom_provider_name: str | None = None,
    execution_connection_id: UUID | None | _Unchanged = _Unchanged.VALUE,
) -> int:
    """Input/strategy changes restart preparation; material-only edits use their
    separate endpoint to preserve every other drama's prepared grouping.
    """
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    if draft.revision != expected_revision:
        raise DomainError(
            "draft_revision_conflict", "草稿已更新，请保留当前编辑并读取最新版本"
        )
    if custom_provider_name is not None:
        provider_connection_id, application_id = resolve_provider(
            session, context, None, None, custom_provider_name
        )
    old_manual = [
        row.manual_link
        for row in session.exec(
            select(DraftInput).where(
                col(DraftInput.tenant_id) == context.tenant_id,
                col(DraftInput.draft_id) == draft.id,
                col(DraftInput.kind) == "drama",
            )
        ).all()
        if row.manual_link
    ]
    existing = session.exec(
        select(col(DraftInput.kind), col(DraftInput.raw_text))
        .where(
            col(DraftInput.tenant_id) == context.tenant_id,
            col(DraftInput.draft_id) == draft.id,
        )
        .order_by(col(DraftInput.kind), col(DraftInput.line_no))
    ).all()
    old_intent: dict[str, Any] = {
        "bc_id": draft.bc_id,
        "strategy_version_id": draft.strategy_version_id,
        "provider_connection_id": draft.provider_connection_id,
        "execution_connection_id": draft.execution_connection_id,
        "application_id": draft.application_id,
        "drama_lines": [raw for kind, raw in existing if kind == "drama"],
        "account_lines": [raw for kind, raw in existing if kind == "account"],
        "link_config": draft.link_config,
    }
    intent: dict[str, Any] = old_intent | {
        key: value
        for key, value in {
            "strategy_version_id": strategy_version_id,
            "provider_connection_id": provider_connection_id,
            "application_id": application_id,
            "drama_lines": drama_lines,
            "account_lines": account_lines,
            "link_config": link_config,
        }.items()
        if value is not None
    }
    if execution_connection_id is not _Unchanged.VALUE:
        intent["execution_connection_id"] = execution_connection_id
    if (
        old_manual
        and manual_links is None
        and intent["drama_lines"] != old_intent["drama_lines"]
    ):
        raise DomainError(
            "manual_link_mapping_required", "剧名发生变化，请同时提交对应的手动链接行号"
        )
    _check_intent(session, context, **intent)
    provider = session.get(ProviderConnection, intent["provider_connection_id"])
    assert provider
    manual = validate_manual_links(
        manual_links if manual_links is not None else old_manual,
        intent["drama_lines"],
        provider.kind,
    )
    if intent == old_intent and list(manual.values()) == old_manual:
        return draft.revision
    revision = _bump_revision(session, draft, expected_revision)
    if draft.media_kind == "CAROUSEL":
        from .image_composition_state import ImageCompositionState

        state = session.get(ImageCompositionState, (draft.tenant_id, draft.id))
        drama_changed = (
            any(
                intent[key] != old_intent[key]
                for key in (
                    "provider_connection_id",
                    "application_id",
                    "drama_lines",
                    "link_config",
                )
            )
            or list(manual.values()) != old_manual
        )
        if drama_changed:
            # 输入范围显式改变即开始新的图片准备；旧快照/单元/试算保留供审计，
            # 不删除被历史 FK 引用的剧目，也不清除用户显式排除。
            draft.active_image_generation = 0
            draft.applied_image_rule_revision = None
            if state:
                state.snapshot_id, state.drama_patches = None, {}
        reset_models = (DraftAccount, DraftInput)
    else:
        delete_video_units(session, draft)
        reset_models = (DraftDrama, DraftAccount, DraftInput)
    for model in reset_models:
        SASession.execute(
            session,
            delete(model).where(
                col(model.tenant_id) == context.tenant_id,
                col(model.draft_id) == draft.id,
            ),
        )
    draft.strategy_version_id = intent["strategy_version_id"]
    draft.provider_connection_id = intent["provider_connection_id"]
    draft.execution_connection_id = intent["execution_connection_id"]
    draft.application_id = intent["application_id"]
    draft.link_config = intent["link_config"]
    draft.status = "DRAFT"
    session.add(draft)
    session.flush()
    _store_inputs(session, draft, "drama", intent["drama_lines"], manual)
    _store_inputs(session, draft, "account", intent["account_lines"])
    session.flush()
    return revision
