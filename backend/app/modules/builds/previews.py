import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.local_read_batch import local_read_batch
from app.core.pagination import Page, count_rows
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.builds.batch_numbers import (
    insert_preview_with_number,
    is_current_batch_number,
)
from app.modules.builds.drafts import get_draft
from app.modules.builds.execution_models import Submission
from app.modules.builds.models import (
    BuildDraft,
    DraftAccount,
    DraftDrama,
    DraftInput,
)
from app.modules.builds.preview_materials import (
    SKIPPABLE_MATERIAL_REASONS,
    material_limit_exceeded,
    material_not_skipped,
)
from app.modules.builds.preview_models import (
    BuildPreview,
    BuildUnit,
    PlannedAd,
    PlannedGroup,
    PreviewCopy,
    PreviewDrama,
    PreviewDramaGroup,
    PreviewInput,
    PreviewSkippedMaterial,
)
from app.modules.builds.preview_schemas import (
    ExcludedDramaPublic,
    FrozenAd,
    FrozenGroup,
    FrozenUnit,
    PreviewGenerationProgress,
    PreviewInputPublic,
    PreviewSummary,
    PreviewUnit,
    Readiness,
    SkippedMaterialPublic,
    bid_strategy_label,
    budget_strategy_label,
    budget_unit_label,
    build_structure_summary,
    frozen_bid_strategy,
    generation_mode_label,
)
from app.modules.builds.preview_validation import (
    final_ad_count_exceeded,
    measured,
    name_reasons,
    scene_reasons,
)
from app.modules.builds.route_views import execution_route_view
from app.modules.builds.routes import load_preview_route, save_preview_route
from app.modules.builds.scene import read_scene_context
from app.modules.builds.scene_schemas import SceneContext
from app.modules.builds.targeting import apply_targeting
from app.modules.builds.targeting_directory import draft_directory
from app.modules.builds.targeting_service import effective_targeting
from app.modules.materials.models import MaterialFile
from app.modules.materials.readiness import get_material_readiness_batch
from app.modules.providers.models import (
    PromotionLink,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.strategies.naming import local_display_drama_id, render_names
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import get_copies, get_version
from app.modules.strategies.structure import estimate_batch_structure, plan_structure
from app.modules.tenants.permissions import require_tenant

from .creative_units import (
    preview_contract,
    require_freeze_ready,
    snapshot_creative_units,
)
from .image_composition_models import DraftCreativeUnit
from .preview_material_views import AdMaterialView as PreviewAdMaterial
from .preview_material_views import GroupMaterialView as PreviewGroupMaterial
from .preview_models import (
    PreviewAdCreativeUnit,
    PreviewContract,
    PreviewCreativeUnit,
    PreviewGroupCreativeUnit,
    PreviewImageExclusion,
)
from .structure import effective_structure_config

"""Preview planning is local and never dispatches target assets or ads."""


def iter_pairs[D, A](
    dramas: Iterable[D], accounts: Callable[[], Iterable[A]]
) -> Iterator[tuple[D, A]]:
    for drama in dramas:
        for account in accounts():
            yield drama, account


def unit_readiness(
    currency: str,
    account_currency: str,
    material_states: Iterable[str],
    scene_reasons: Iterable[str],
) -> Literal["READY", "PREPARING", "BLOCKED"]:
    states = tuple(material_states)
    if (
        currency != account_currency
        or tuple(scene_reasons)
        or not states
        or any(state not in {"ready", "preparable"} for state in states)
    ):
        return "BLOCKED"
    return "PREPARING" if "preparable" in states else "READY"


# Local engineering transaction/page sizes, never TikTok account limits.
PAGE_SIZE = 100
STEP_LIMIT = 200


def _authorize(
    session: Session, context: TenantContext, *, write: bool = False
) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="build" if write else "read",
    )


def _preview(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> BuildPreview:
    _authorize(session, context, write=lock)
    statement = select(BuildPreview).where(
        BuildPreview.tenant_id == context.tenant_id, BuildPreview.id == identity
    )
    if lock:
        statement = statement.with_for_update()
    row = session.exec(
        statement.execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("preview_not_found", "预览不存在")
    return row


def _hash(previous: str, value: Any) -> str:
    return hashlib.sha256(
        (
            previous
            + json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        ).encode()
    ).hexdigest()


def _record(preview: BuildPreview, value: Any) -> None:
    preview.progress["digest"] = _hash(preview.progress.get("digest", ""), value)


def _bump_preview_retry_revision(
    session: Session, draft: BuildDraft, expected_revision: int
) -> None:
    from .creative_units import has_live_music_intent
    from .drafts import _bump_revision

    if draft.media_kind == "CAROUSEL" and has_live_music_intent(session, draft):
        # 重试不能借版本提升取消当前批量配乐意图；旧订阅不阻塞新尝试。
        raise DomainError(
            "music_preparation_pending", "配乐准备尚未结束，请稍后重试预览"
        )
    was_applied = draft.applied_image_rule_revision == draft.revision
    _bump_revision(session, draft, expected_revision)
    if was_applied:
        draft.applied_image_rule_revision = draft.revision


def generate_preview(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    expected_revision: int,
    refresh_continuation: bool = False,
) -> UUID:
    from .preview_capture_limits import freeze_capture_limits

    with freeze_capture_limits(session):
        return _generate_preview(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=expected_revision,
            refresh_continuation=refresh_continuation,
        )


def _generate_preview(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    expected_revision: int,
    refresh_continuation: bool = False,
) -> UUID:
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    existing = session.exec(
        select(BuildPreview).where(
            BuildPreview.tenant_id == context.tenant_id,
            BuildPreview.draft_id == draft_id,
            BuildPreview.draft_revision == expected_revision,
        )
    ).one_or_none()
    legacy = bool(
        existing
        and preview_contract(
            session, tenant_id=context.tenant_id, preview_id=existing.id
        ).version
        == 1
    )
    refresh = bool(
        refresh_continuation
        and existing
        and session.exec(
            select(Submission.id)
            .where(
                Submission.tenant_id == context.tenant_id,
                Submission.draft_id == draft.id,
                Submission.preview_id != existing.id,
            )
            .limit(1)
        ).first()
    )
    if draft.revision != expected_revision:
        # 响应丢失/重复点击仅返回失败重试或 V1 升级直接产生的下一版本。
        if (
            existing
            and (existing.status == "FAILED" or legacy or refresh)
            and draft.revision == expected_revision + 1
        ):
            retried = session.exec(
                select(BuildPreview).where(
                    BuildPreview.tenant_id == context.tenant_id,
                    BuildPreview.draft_id == draft_id,
                    BuildPreview.draft_revision == draft.revision,
                )
            ).one_or_none()
            if retried and retried.progress.get("retry_of") == str(existing.id):
                return retried.id
        raise DomainError("draft_revision_conflict", "草稿已变更")
    if existing:
        existing = _preview(session, context, existing.id, lock=True)
        if existing.status != "FAILED" and not legacy and not refresh:
            return existing.id
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "草稿尚未准备完成")
        if (
            session.exec(
                select(Submission.id).where(
                    Submission.tenant_id == context.tenant_id,
                    Submission.preview_id == existing.id,
                )
            ).first()
            is not None
        ):
            raise DomainError("preview_already_submitted", "已提交的预览不能重试生成")
        # FAILED/V1 历史正文受冻结保护；仅提升草稿版本，复用输入创建 V2。
        _bump_preview_retry_revision(session, draft, expected_revision)
        session.flush()
        identity = generate_preview(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=draft.revision,
        )
        retried = _preview(session, context, identity, lock=True)
        retried.progress = {**retried.progress, "retry_of": str(existing.id)}
        session.add(retried)
        session.flush()
        return identity
    if draft.status != "READY":
        raise DomainError("draft_not_ready", "草稿尚未准备完成")
    from app.modules.builds.identity_selection import require_preview_identity
    from app.modules.builds.mini_selection import require_preview_mini

    # 必填目标在排队前检查，避免展开全部剧目×账户后才发现整批无法搭建。
    require_preview_mini(session, context=context, draft=draft)
    chosen_identity = require_preview_identity(session, context=context, draft=draft)
    config = effective_structure_config(
        get_version(session, context=context, version_id=draft.strategy_version_id),
        draft.structure_override,
    )
    # 在账户广告展开前按当前有效单元计数，避免整批乘法绕过单策略上限。
    unit_counts = session.exec(
        select(func.count(col(DraftCreativeUnit.id)))
        .where(
            DraftCreativeUnit.tenant_id == context.tenant_id,
            DraftCreativeUnit.draft_id == draft.id,
            DraftCreativeUnit.generation
            == (1 if draft.media_kind == "VIDEO" else draft.active_image_generation),
            select(DraftInput.id)
            .where(
                DraftInput.tenant_id == context.tenant_id,
                DraftInput.draft_id == draft.id,
                DraftInput.kind == "drama",
                DraftInput.drama_id == DraftCreativeUnit.drama_id,
                DraftInput.status == "ready",
            )
            .exists(),
        )
        .group_by(col(DraftCreativeUnit.drama_id))
    ).all()
    account_count = session.exec(
        select(func.count())
        .select_from(DraftAccount)
        .where(
            DraftAccount.tenant_id == context.tenant_id,
            DraftAccount.draft_id == draft.id,
        )
    ).one()
    estimate_batch_structure(unit_counts, account_count=account_count, config=config)
    targeting = effective_targeting(session, context, draft)
    directory = draft_directory(session, context=context, draft_id=draft.id)
    if directory.state != "READY":
        raise DomainError(
            "targeting_regions_unverified",
            "共同可投国家尚未核实或没有交集，请重新准备或调整账户",
        )
    if directory.unavailable_region_codes:
        raise DomainError(
            "targeting_regions_unavailable", "所选国家不在本批共同可投范围，请调整定向"
        )
    delivery = draft.delivery_config or {}
    mode = delivery.get("schedule_mode", "IMMEDIATE")
    schedule_type = "SCHEDULE_START_END" if mode == "START_END" else "SCHEDULE_FROM_NOW"
    config = config.model_copy(
        update={
            "targeting": targeting,
            "schedule_type": schedule_type,
            "schedule_start_time": (
                delivery.get("schedule_start_time") if mode != "IMMEDIATE" else None
            ),
            "schedule_end_time": (
                delivery.get("schedule_end_time") if mode == "START_END" else None
            ),
        }
    )
    row = BuildPreview(
        tenant_id=context.tenant_id,
        bc_id=draft.bc_id,
        draft_id=draft_id,
        draft_revision=draft.revision,
        strategy_version_id=draft.strategy_version_id,
        actor_id=context.actor_id,
        batch_short_id="",
        local_date=datetime.now(UTC).strftime("%Y%m%d"),
        config=config.model_dump(mode="json"),
        budget=config.budget,
        target_roas=config.target_roas,
    )
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    require_freeze_ready(session, draft, context=context, route=route)
    insert_preview_with_number(session, row)
    contract = PreviewContract(**_scope(row), version=2, media_kind=draft.media_kind)
    session.add(contract)
    session.flush()
    creative_records = snapshot_creative_units(session, preview=row, draft=draft)
    save_preview_route(session, context=context, preview_id=row.id, route=route)
    row.progress = {
        "phase": "inputs",
        "selected_identity": chosen_identity,
        "targeting_region_codes": directory.region_codes,
        # 在父草稿锁下固定分母；不使用后续可能已修改的草稿推算历史进度。
        "total_units": session.exec(
            select(func.count())
            .select_from(DraftDrama)
            .where(
                DraftDrama.tenant_id == context.tenant_id,
                DraftDrama.draft_id == draft_id,
                select(DraftInput.id)
                .where(
                    DraftInput.tenant_id == context.tenant_id,
                    DraftInput.draft_id == draft_id,
                    DraftInput.kind == "drama",
                    DraftInput.status == "ready",
                    DraftInput.drama_id == DraftDrama.drama_id,
                )
                .exists(),
            )
        ).one()
        * session.exec(
            select(func.count())
            .select_from(DraftAccount)
            .where(
                DraftAccount.tenant_id == context.tenant_id,
                DraftAccount.draft_id == draft_id,
            )
        ).one(),
        "updated_at": datetime.now(UTC).isoformat(),
        # 版本标记避免旧的未完成预览在续跑时混用长技术 ID 与展示编号。
        "naming_drama_id": "display",
        "kind": "drama",
        "after": 0,
        "digest": _hash(
            "",
            {
                "preview_id": row.id,
                "route": route.model_dump(mode="json"),
                "draft_revision": draft.revision,
                "config": row.config,
                "date": row.local_date,
                "batch": row.batch_short_id,
            },
        ),
    }
    _record(row, contract.model_dump(mode="json"))
    _record(row, creative_records)
    session.add(row)
    session.flush()
    from app.modules.builds.preview_tasks import queue_preview

    queue_preview(session, row)
    session.flush()
    return row.id


def _scope(preview: BuildPreview) -> dict[str, Any]:
    return {
        "tenant_id": preview.tenant_id,
        "preview_id": preview.id,
        "bc_id": preview.bc_id,
    }


def _snapshot_inputs(session: Session, preview: BuildPreview) -> None:
    p = preview.progress
    rows = session.exec(
        select(DraftInput)
        .where(
            DraftInput.tenant_id == preview.tenant_id,
            DraftInput.draft_id == preview.draft_id,
            DraftInput.kind == p["kind"],
            DraftInput.line_no > p["after"],
        )
        .order_by(col(DraftInput.line_no))
        .limit(PAGE_SIZE)
    ).all()
    for row in rows:
        value = PreviewInput(
            **_scope(preview),
            kind=row.kind,
            line_no=row.line_no,
            raw_text=row.raw_text,
            status=row.status,
            reason_code=row.reason_code,
            duplicate_of=row.duplicate_of,
        )
        session.add(value)
        _record(preview, value.model_dump(mode="json"))
    if rows:
        p["after"] = rows[-1].line_no
    else:
        p["after"] = 0
        if p["kind"] == "drama":
            p["kind"] = "account"
        else:
            p.update(
                phase="dramas",
                drama_after=None,
                current_drama=None,
                group_after=0,
                position=0,
            )


def _snapshot_drama(
    session: Session,
    context: TenantContext,
    preview: BuildPreview,
    config: StrategyConfig,
) -> None:
    p = preview.progress
    if not p["current_drama"]:
        query = select(DraftDrama).where(
            DraftDrama.tenant_id == preview.tenant_id,
            DraftDrama.draft_id == preview.draft_id,
            select(DraftInput.id)
            .where(
                DraftInput.tenant_id == preview.tenant_id,
                DraftInput.draft_id == preview.draft_id,
                DraftInput.kind == "drama",
                DraftInput.status == "ready",
                DraftInput.drama_id == DraftDrama.drama_id,
            )
            .exists(),
        )
        if p["drama_after"]:
            query = query.where(DraftDrama.drama_id > UUID(p["drama_after"]))
        drama = session.exec(query.order_by(col(DraftDrama.drama_id)).limit(1)).first()
        if drama is None:
            p.update(
                phase="units",
                drama_after=None,
                current_drama=None,
                account_after="",
                unit_id=None,
                group_after=0,
            )
            return
        link = session.exec(
            select(PromotionLink).where(
                PromotionLink.tenant_id == preview.tenant_id,
                PromotionLink.id == drama.link_id,
            )
        ).one()
        provider_drama, connection = session.exec(
            select(ProviderDrama, ProviderConnection)
            .join(
                ProviderConnection,
                (col(ProviderConnection.tenant_id) == col(ProviderDrama.tenant_id))
                & (col(ProviderConnection.id) == col(ProviderDrama.connection_id)),
            )
            .where(
                ProviderDrama.tenant_id == preview.tenant_id,
                ProviderDrama.id == drama.drama_id,
                ProviderDrama.connection_id == link.connection_id,
                ProviderDrama.application_id == link.application_id,
            )
        ).one()
        # 手动录入且未填写版权方剧目 ID 时，用系统编号保证广告名称完整。
        display_drama_id = (
            provider_drama.display_drama_id or ""
        ).strip() or local_display_drama_id(provider_drama.id)
        frozen = PreviewDrama(
            **_scope(preview),
            drama_id=drama.drama_id,
            link_id=drama.link_id,
            title=drama.title,
            provider_pinyin=connection.display_name
            if connection.kind == "other"
            else connection.kind,
            external_drama_id=provider_drama.external_drama_id,
            display_drama_id=display_drama_id,
            url=link.url or "",
            protected_base=link.protected_base or "",
            reason_codes=[]
            if link.status == "ready" and link.url
            else ["link_unavailable"],
        )
        session.add(frozen)
        session.flush()
        _record(preview, frozen.model_dump(mode="json"))
        p.update(
            current_drama=str(drama.drama_id),
            group_after=0,
            position=0,
            current_group=None,
        )
        return
    drama_id = UUID(p["current_drama"])
    if p["current_group"] is None:
        # 草稿素材只是有序素材包来源；最终组数和基础广告数由冻结策略规划器决定。
        if p.get("planned"):
            p.update(drama_after=p["current_drama"], current_drama=None, planned=False)
            return
        source_units = session.exec(
            select(PreviewCreativeUnit)
            .where(
                PreviewCreativeUnit.tenant_id == preview.tenant_id,
                PreviewCreativeUnit.preview_id == preview.id,
                PreviewCreativeUnit.drama_id == drama_id,
            )
            .order_by(col(PreviewCreativeUnit.position))
        ).all()
        if not source_units:
            p.update(drama_after=p["current_drama"], current_drama=None)
            return
        pool = get_copies(session, context=context, version_id=config.copy_pool_version)
        seed = int(_hash("", [preview.id, drama_id]), 16)
        plans = plan_structure(source_units, config=config, pool=tuple(pool), seed=seed)
        for group_plan in plans:
            group = PreviewDramaGroup(
                **_scope(preview), drama_id=drama_id, group_no=group_plan.group_no
            )
            session.add(group)
            session.flush()
            _record(preview, group.model_dump(mode="json"))
            for position, unit_id in enumerate(group_plan.unit_ids, 1):
                material_row = PreviewGroupCreativeUnit(
                    **_scope(preview),
                    drama_id=drama_id,
                    group_no=group_plan.group_no,
                    position=position,
                    creative_unit_id=unit_id,
                )
                session.add(material_row)
                _record(preview, material_row.model_dump(mode="json"))
            for ad_plan in group_plan.ads:
                for position, unit_id in enumerate(ad_plan.unit_ids, 1):
                    mapping = PreviewAdCreativeUnit(
                        **_scope(preview),
                        drama_id=drama_id,
                        group_no=group_plan.group_no,
                        base_ad_no=ad_plan.base_ad_no,
                        position=position,
                        creative_unit_id=unit_id,
                    )
                    session.add(mapping)
                    _record(preview, mapping.model_dump(mode="json"))
                for creative_no, copy in enumerate(ad_plan.copies, 1):
                    value = PreviewCopy(
                        **_scope(preview),
                        drama_id=drama_id,
                        group_no=group_plan.group_no,
                        base_ad_no=ad_plan.base_ad_no,
                        creative_no=creative_no,
                        copy_id=copy.copy_id,
                        text=copy.text,
                    )
                    session.add(value)
                    _record(preview, value.model_dump(mode="json"))
        p.update(planned=True, current_group=None, position=0)
        return


def _names(
    preview: BuildPreview,
    drama: PreviewDrama,
    config: StrategyConfig,
    group: int,
    creative: int,
    base_ad_no: int = 1,
) -> tuple[str, str, str]:
    # The engineering ceiling prevents malformed local text allocation; official
    # per-level measurements are checked separately, including CJK weighting.
    return render_names(
        protected_base=drama.protected_base,
        title=drama.title,
        date_text=preview.local_date,
        batch_short_id=preview.batch_short_id,
        group_no=group,
        # 使用连续最终广告序号，使同组不同基础广告的复制名称始终唯一。
        creative_no=(base_ad_no - 1) * config.creative_count + creative,
        max_length=10000,
        provider_pinyin=drama.provider_pinyin,
        display_drama_id=drama.display_drama_id,
        template=config.campaign_name_template,
    )


def _block(unit: BuildUnit, reasons: Iterable[str]) -> None:
    unit.reason_codes = list(dict.fromkeys([*unit.reason_codes, *reasons]))
    if unit.reason_codes:
        unit.readiness = "BLOCKED"


def _expand_unit(
    session: Session,
    context: TenantContext,
    preview: BuildPreview,
    config: StrategyConfig,
    route: FrozenTikTokRoute,
) -> None:
    p = preview.progress
    if not p["current_drama"]:
        query = select(PreviewDrama).where(
            PreviewDrama.tenant_id == preview.tenant_id,
            PreviewDrama.preview_id == preview.id,
        )
        if p["drama_after"]:
            query = query.where(PreviewDrama.drama_id > UUID(p["drama_after"]))
        drama = session.exec(
            query.order_by(col(PreviewDrama.drama_id)).limit(1)
        ).first()
        if drama is None:
            p.update(phase="digest", after_id=None)
            return
        p.update(
            current_drama=str(drama.drama_id),
            account_after="",
            unit_id=None,
            group_after=0,
        )
        return
    drama = session.get(
        PreviewDrama, (preview.tenant_id, preview.id, UUID(p["current_drama"]))
    )
    assert drama
    if not p["unit_id"]:
        account = session.exec(
            select(DraftAccount)
            .where(
                DraftAccount.tenant_id == preview.tenant_id,
                DraftAccount.draft_id == preview.draft_id,
                DraftAccount.advertiser_id > p["account_after"],
            )
            .order_by(col(DraftAccount.advertiser_id))
            .limit(1)
        ).first()
        if account is None:
            p.update(drama_after=p["current_drama"], current_drama=None)
            return
        # 没有原连接的账户关系时不能丢弃输入或借旧连接插入组合；整个预览失败。
        if (
            session.get(
                BCAccountAccess,
                (
                    context.tenant_id,
                    route.bc_id,
                    account.advertiser_id,
                    route.connection_id,
                ),
                populate_existing=True,
            )
            is None
        ):
            raise DomainError(
                "account_not_in_bc", "所选冻结连接不包含该账户，请重新准备草稿"
            )
        media_kind = preview_contract(
            session, tenant_id=preview.tenant_id, preview_id=preview.id
        ).media_kind
        try:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=account.advertiser_id,
                capability="read",
            )
            current_access = resolve_account_access(
                session,
                context=context,
                bc_id=preview.bc_id,
                advertiser_id=account.advertiser_id,
                action="read",
                connection_id=route.connection_id,
            )
            if (
                current_access.currency != account.currency
                or current_access.timezone != account.timezone
            ):
                raise DomainError(
                    "account_metadata_changed", "账户信息已改变，请重新准备草稿"
                )
            scene = read_scene_context(
                session,
                context=context,
                bc_id=preview.bc_id,
                advertiser_id=account.advertiser_id,
                link_id=drama.link_id,
                route=route,
                selected_identity=p.get("selected_identity"),
                media_kind=media_kind,
            )
        except DomainError as error:
            if error.code not in {
                "account_not_in_bc",
                "account_ownership_conflict",
                "account_metadata_incomplete",
                "account_access_denied",
                "account_authorization_changed",
                "account_metadata_changed",
                "scene_link_unavailable",
                "resource_not_found",
            }:
                raise
            scene = SceneContext(
                supported=False,
                reason_codes=(error.code,),
                capability_revision="unavailable",
                media_kind=media_kind,
            )
        scene = apply_targeting(
            scene, config.targeting, p.get("targeting_region_codes", [])
        )
        # 预算/竞价属于本次预览的业务意图，和账户场景事实一起落入冻结快照。
        # 执行阶段只读取这两个值，避免策略版本后续编辑改变已提交请求。
        scene_snapshot = scene.to_snapshot()
        scene_snapshot.update(
            budget_strategy=config.budget_strategy,
            bid_strategy=config.bid_strategy,
        )
        name = _names(preview, drama, config, 1, 1)[0]
        unit = BuildUnit(
            **_scope(preview),
            drama_id=drama.drama_id,
            advertiser_id=account.advertiser_id,
            connection_id=route.connection_id,
            currency=account.currency,
            timezone=account.timezone,
            campaign_name=name,
            campaign_digest=hashlib.sha256(name.encode()).hexdigest(),
            scene_snapshot=scene_snapshot,
        )
        _block(
            unit,
            [
                *drama.reason_codes,
                # 校验使用冻结预览的素材类型；图片场景不能落入默认 VIDEO 比较。
                *scene_reasons(config, scene, account.currency, media_kind=media_kind),
                *name_reasons(name, "campaign", unit.scene_snapshot),
            ],
        )
        # 完整名称包含版权方展示剧目 ID；仍校验账户内碰撞，防止不同字段拼接出同名。
        # 保留所有组合，并明确阻断冲突双方。
        collisions = session.exec(
            select(BuildUnit).where(
                BuildUnit.tenant_id == preview.tenant_id,
                BuildUnit.preview_id == preview.id,
                BuildUnit.advertiser_id == account.advertiser_id,
                BuildUnit.campaign_digest == unit.campaign_digest,
                BuildUnit.campaign_name == name,
            )
        ).all()
        if collisions:
            _block(unit, ["duplicate_campaign_name"])
            for old in collisions:
                _block(old, ["duplicate_campaign_name"])
                session.add(old)
        session.add(unit)
        session.flush()
        p.update(
            unit_id=str(unit.id), group_after=0, account_after=account.advertiser_id
        )
        return
    resumed_unit = session.get(BuildUnit, UUID(p["unit_id"]))
    assert resumed_unit
    unit = resumed_unit
    group = session.exec(
        select(PreviewDramaGroup)
        .where(
            PreviewDramaGroup.tenant_id == preview.tenant_id,
            PreviewDramaGroup.preview_id == preview.id,
            PreviewDramaGroup.drama_id == drama.drama_id,
            PreviewDramaGroup.group_no > p["group_after"],
        )
        .order_by(col(PreviewDramaGroup.group_no))
        .limit(1)
    ).first()
    if group is None:
        if unit.group_count == 0:
            _block(unit, ["materials_missing"])
        from .ad_readiness import included_group_sql

        if not SQLAlchemySession.execute(
            session,
            text(
                "SELECT EXISTS(SELECT 1 FROM planned_group g WHERE g.tenant_id=:tenant AND g.unit_id=:unit AND "
                + included_group_sql()
                + ")"
            ),
            {"tenant": preview.tenant_id, "unit": unit.id},
        ).scalar_one():
            _block(
                unit,
                ["carousel_ads_unavailable"]
                if preview_contract(
                    session, tenant_id=preview.tenant_id, preview_id=preview.id
                ).media_kind
                == "CAROUSEL"
                else ["materials_missing"],
            )
        unit.complete = True
        session.add(unit)
        p.update(unit_id=None, group_after=0)
        return
    contract = preview_contract(
        session, tenant_id=preview.tenant_id, preview_id=preview.id
    )
    carousel = contract.media_kind == "CAROUSEL"
    ad_failures = {}
    maximum = unit.scene_snapshot["creative_limit"]
    # 广告级素材集合才是平台单广告上限；共享组展示行可以大于 50，不能
    # 用组级总数错误阻断每个基础广告均在上限内的策略。
    if not maximum:
        _block(
            unit,
            ["field_limits_unverified"],
        )
    else:
        # 本地读取仍限 50 条；平台数量限制按排除不可用素材后的实际组校验。
        materials = session.exec(
            select(PreviewGroupMaterial.material_id)
            .where(
                PreviewGroupMaterial.tenant_id == preview.tenant_id,
                PreviewGroupMaterial.preview_id == preview.id,
                PreviewGroupMaterial.drama_id == drama.drama_id,
                PreviewGroupMaterial.group_no == group.group_no,
            )
            .order_by(col(PreviewGroupMaterial.position))
        ).all()
        from .carousel_readiness import image_preview_readiness

        readiness = (
            image_preview_readiness(
                session,
                context=context,
                route=route,
                advertiser_id=unit.advertiser_id,
                material_ids=list(materials),
            )
            if carousel and materials
            else (
                get_material_readiness_batch(
                    session,
                    context=context,
                    bc_id=preview.bc_id,
                    material_ids=list(materials),
                    advertiser_id=unit.advertiser_id,
                    route=route,
                )
                if materials
                else {}
            )
        )
        skipped_ids = [
            identity
            for identity, result in readiness.items()
            if not carousel
            and result.state == "blocked"
            and result.reason_code in SKIPPABLE_MATERIAL_REASONS
        ]
        # 文件名及原因随预览冻结，分页展示；不会因为以后补传成功而改变本次内容。
        if skipped_ids:
            material_names = dict(
                session.exec(
                    select(MaterialFile.id, MaterialFile.file_name).where(
                        MaterialFile.tenant_id == preview.tenant_id,
                        col(MaterialFile.id).in_(skipped_ids),
                    )
                ).all()
            )
            for identity in skipped_ids:
                if (
                    session.get(
                        PreviewSkippedMaterial,
                        (preview.tenant_id, unit.id, identity),
                    )
                    is None
                ):
                    skipped = PreviewSkippedMaterial(
                        tenant_id=preview.tenant_id,
                        unit_id=unit.id,
                        material_id=identity,
                        preview_id=preview.id,
                        bc_id=preview.bc_id,
                        file_name=material_names[identity],
                        reason_code=readiness[identity].reason_code
                        or "material_unavailable",
                    )
                    session.add(skipped)
                    _record(preview, skipped.model_dump(mode="json"))
        for identity, result in readiness.items():
            if result.state == "blocked":
                if identity not in skipped_ids and not carousel:
                    _block(unit, [result.reason_code or "material_unavailable"])
            elif result.state == "preparable" and unit.readiness != "BLOCKED":
                unit.readiness = "PREPARING"
        if materials and len(skipped_ids) == len(materials):
            # 空组不创建 Ad Group/Ad；其他组继续，整部无有效组才在收尾阻断。
            session.add(unit)
            p["group_after"] = group.group_no
            return
        if not materials:
            _block(unit, ["materials_missing"])
            session.add(unit)
            p["group_after"] = group.group_no
            return
        ad_material_counts = {}
        mappings = session.exec(
            select(PreviewAdCreativeUnit).where(
                PreviewAdCreativeUnit.tenant_id == preview.tenant_id,
                PreviewAdCreativeUnit.preview_id == preview.id,
                PreviewAdCreativeUnit.drama_id == drama.drama_id,
                PreviewAdCreativeUnit.group_no == group.group_no,
            )
        ).all()
        from .preview_models import PreviewCreativeUnitItem

        # 一次读取整组映射成员，不能按映射逐个查库（共享组可含数千单元）。
        member_rows = session.exec(
            select(
                PreviewCreativeUnitItem.creative_unit_id,
                PreviewCreativeUnitItem.material_id,
            ).where(
                PreviewCreativeUnitItem.tenant_id == preview.tenant_id,
                PreviewCreativeUnitItem.preview_id == preview.id,
                col(PreviewCreativeUnitItem.creative_unit_id).in_(
                    [m.creative_unit_id for m in mappings]
                ),
            )
        ).all()
        members_by_unit = {}
        for identity, member in member_rows:
            members_by_unit.setdefault(identity, []).append(member)
        skipped_set = set(skipped_ids)
        for mapping in mappings:
            members = members_by_unit.get(mapping.creative_unit_id, [])
            if members and not all(member in skipped_set for member in members):
                ad_material_counts[mapping.base_ad_no] = (
                    ad_material_counts.get(mapping.base_ad_no, 0) + 1
                )
            if carousel:
                reasons = ad_failures.setdefault(mapping.base_ad_no, set())
                if not members:
                    reasons.add("materials_missing")
                for member in members:
                    result = readiness.get(member)
                    if result is None or result.state == "blocked":
                        reasons.add(
                            result.reason_code
                            if result and result.reason_code
                            else "image_source_unavailable"
                        )
        if material_limit_exceeded(list(ad_material_counts.values()), maximum):
            _block(unit, ["material_group_limit_exceeded"])
    names = _names(preview, drama, config, group.group_no, 1)
    planned = PlannedGroup(
        **_scope(preview),
        unit_id=unit.id,
        drama_id=drama.drama_id,
        group_no=group.group_no,
        name=names[1],
    )
    _block(unit, name_reasons(planned.name, "adgroup", unit.scene_snapshot))
    session.add(planned)
    session.flush()
    _record(preview, planned.model_dump(mode="json"))
    copies = session.exec(
        select(PreviewCopy)
        .where(
            PreviewCopy.tenant_id == preview.tenant_id,
            PreviewCopy.preview_id == preview.id,
            PreviewCopy.drama_id == drama.drama_id,
            PreviewCopy.group_no == group.group_no,
        )
        .order_by(col(PreviewCopy.base_ad_no), col(PreviewCopy.creative_no))
    ).all()
    maximum_ads = unit.scene_snapshot["field_constraints"].get("max_ads_per_adgroup")
    base_ad_count = len({copy.base_ad_no for copy in copies})
    if final_ad_count_exceeded(
        base_ad_count=base_ad_count,
        creative_count=config.creative_count,
        maximum=maximum_ads,
    ):
        _block(unit, ["creative_count_exceeded"])
    readiness_facts = []
    for copy in copies:
        ad_name = _names(
            preview, drama, config, group.group_no, copy.creative_no, copy.base_ad_no
        )[2]
        _block(unit, name_reasons(ad_name, "ad", unit.scene_snapshot))
        method = unit.scene_snapshot["field_constraints"].get("copy_measurement")
        copy_limit = unit.scene_snapshot["copy_length_limit"]
        if not copy.text.strip():
            _block(unit, ["copy_empty"])
        elif method not in {"characters", "cjk_weighted"} or not copy_limit:
            _block(unit, ["field_limits_unverified"])
        elif measured(copy.text, method) > copy_limit:
            _block(unit, ["copy_too_long"])
        options = list(config.cta_option_ids) or list(
            unit.scene_snapshot["cta_fields"].get("asset_ids", [])
        )
        ad = PlannedAd(
            **_scope(preview),
            group_id=planned.id,
            base_ad_no=copy.base_ad_no,
            creative_no=copy.creative_no,
            name=ad_name,
            copy_id=copy.copy_id,
            text=copy.text,
            cta_option_ids=options,
        )
        session.add(ad)
        if carousel:
            from .preview_models import PreviewAdReadiness

            reasons = sorted(ad_failures.get(copy.base_ad_no, set()))
            fact = PreviewAdReadiness(
                **_scope(preview),
                ad_id=ad.id,
                included=not reasons,
                reason_codes=reasons,
            )
            readiness_facts.append(fact)
            _record(preview, fact.model_dump(mode="json"))
        _record(preview, ad.model_dump(mode="json"))
    if readiness_facts:
        # 一组一次批量刷广告，再批量写事实；避免每条文案复制都独占一次 SQL 往返。
        session.flush()
        session.add_all(readiness_facts)
        if unit.readiness != "BLOCKED" and any(f.included for f in readiness_facts):
            # 目标曲目只在实际 attempt 的事务外核验；本地图就绪不能声称配乐已验证。
            unit.readiness = "PREPARING"
            unit.reason_codes = sorted(
                set(unit.reason_codes) | {"carousel_music_unverified"}
            )
    unit.group_count += 1
    unit.ad_count += len(copies)
    session.add(unit)
    p["group_after"] = group.group_no


def _digest_units(
    session: Session, preview: BuildPreview, context: TenantContext
) -> bool:
    p = preview.progress
    query = select(BuildUnit).where(
        BuildUnit.tenant_id == preview.tenant_id, BuildUnit.preview_id == preview.id
    )
    if p["after_id"]:
        query = query.where(BuildUnit.id > UUID(p["after_id"]))
    rows = session.exec(query.order_by(col(BuildUnit.id)).limit(PAGE_SIZE)).all()
    from .continuation import freeze_continuation

    for row in rows:
        freeze_continuation(session, context=context, preview=preview, unit=row)
        _record(preview, row.model_dump(mode="json"))
    if rows:
        p["after_id"] = str(rows[-1].id)
        return False
    preview.content_digest = p["digest"]
    preview.status = "FROZEN"
    return True


def continue_preview(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    step_limit: int = STEP_LIMIT,
) -> bool:
    if type(step_limit) is not int or not 1 <= step_limit <= STEP_LIMIT:
        raise ValueError("Invalid preview step count")
    initial = _preview(session, context, preview_id)
    # Same parent-first order as editing and the revision invalidation trigger.
    draft = get_draft(session, context=context, draft_id=initial.draft_id, lock=True)
    preview = _preview(session, context, preview_id, lock=True)
    if preview.status != "BUILDING":
        return True
    if (
        preview_contract(
            session, tenant_id=context.tenant_id, preview_id=preview.id
        ).version
        != 2
    ):
        preview.status = "FAILED"
        preview.error_code = "preview_contract_expired"
        session.add(preview)
        session.flush()
        return True
    if (
        not is_current_batch_number(preview.batch_short_id)
        or "campaign_suffix" in preview.config
        or not preview.config.get("campaign_name_template")
        or preview.progress.get("naming_drama_id") != "display"
    ):
        # 旧版未冻结预览不可混用新规则；冻结及已提交名称继续读取原记录。
        preview.status = "FAILED"
        preview.error_code = "preview_naming_outdated"
        session.add(preview)
        session.flush()
        return True
    if draft.revision != preview.draft_revision:
        preview.status = "OBSOLETE"
        session.add(preview)
        session.flush()
        return True
    if (
        "targeting" not in preview.config
        or "targeting_region_codes" not in preview.progress
    ):
        # 未完成的旧预览必须重新生成，不能混合新旧定向规则；历史冻结任务不改写。
        preview.status = "FAILED"
        preview.error_code = "preview_targeting_outdated"
        session.add(preview)
        session.flush()
        return True
    route = load_preview_route(session, context=context, preview_id=preview.id)
    config = StrategyConfig.model_validate(
        preview.config, context={"allow_legacy_fixed_counts": True}
    )
    # SQLAlchemy JSON mutations are tracked by replacing the container once.
    preview.progress = dict(preview.progress)
    # 短批次及时提交真实进度；仅本地计算复用授权，退出时全量重验后才保存。
    deadline = monotonic() + 3
    with local_read_batch(session):
        verify_route(
            session, context=context, route=route, advertiser_id=None, capability="read"
        )
        for _ in range(step_limit):
            phase = preview.progress["phase"]
            if phase == "inputs":
                _snapshot_inputs(session, preview)
            elif phase == "dramas":
                _snapshot_drama(session, context, preview, config)
            elif phase == "units":
                _expand_unit(session, context, preview, config, route)
            elif _digest_units(session, preview, context):
                break
            session.flush()
            if monotonic() >= deadline:
                break
    preview.progress["updated_at"] = datetime.now(UTC).isoformat()
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(preview, "progress")
    session.add(preview)
    session.flush()
    return preview.status != "BUILDING"


def _excluded_dramas(
    session: Session, context: TenantContext, preview_id: UUID
) -> list[ExcludedDramaPublic]:
    """整部剧在全部账户都被阻断时单独列出，避免只显示“排除 N 个组合”而漏看整剧未搭建。"""
    blocked = (
        select(BuildUnit.drama_id)
        .where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
            col(BuildUnit.complete).is_(True),
        )
        .group_by(col(BuildUnit.drama_id))
        .having(func.bool_and(BuildUnit.readiness == "BLOCKED"))
    )
    dramas = session.exec(
        select(PreviewDrama.drama_id, PreviewDrama.title)
        .where(
            PreviewDrama.tenant_id == context.tenant_id,
            PreviewDrama.preview_id == preview_id,
            col(PreviewDrama.drama_id).in_(blocked),
        )
        .order_by(col(PreviewDrama.title))
        .limit(100)
    ).all()
    if not dramas:
        return []
    reasons: dict[UUID, set[str]] = {drama_id: set() for drama_id, _ in dramas}
    for drama_id, codes in session.exec(
        select(BuildUnit.drama_id, BuildUnit.reason_codes).where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
            col(BuildUnit.drama_id).in_(reasons),
        )
    ).all():
        reasons[drama_id].update(codes or [])
    return [
        ExcludedDramaPublic(
            drama_id=drama_id, title=title, reason_codes=sorted(reasons[drama_id])
        )
        for drama_id, title in dramas
    ]


def get_preview_summary(
    session: Session, *, context: TenantContext, preview_id: UUID
) -> PreviewSummary:
    preview = _preview(session, context, preview_id)
    rows = session.exec(
        select(
            BuildUnit.readiness,
            func.count(),
            func.sum(BuildUnit.group_count),
            func.sum(BuildUnit.ad_count),
        )
        .where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
            col(BuildUnit.complete).is_(True),
        )
        .group_by(col(BuildUnit.readiness))
    ).all()  # noqa: E712
    counts = {state: (int(n), int(g or 0), int(a or 0)) for state, n, g, a in rows}
    eligible = [counts.get(state, (0, 0, 0)) for state in ("READY", "PREPARING")]
    n, g, a = (sum(x[i] for x in eligible) for i in range(3))
    from .ad_readiness import included_ad_sql

    n, g, a = SQLAlchemySession.execute(
        session,
        text(
            """SELECT count(DISTINCT u.id),count(DISTINCT g.id),count(a.id)
      FROM build_unit u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.unit_id=u.id
      JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.group_id=g.id
      WHERE u.tenant_id=:tenant AND u.preview_id=:preview AND u.complete AND u.readiness IN ('READY','PREPARING') AND """
            + included_ad_sql()
        ),
        {"tenant": context.tenant_id, "preview": preview_id},
    ).one()
    unique_material_count = session.exec(
        select(func.count(func.distinct(PreviewGroupMaterial.material_id))).where(
            PreviewGroupMaterial.tenant_id == context.tenant_id,
            PreviewGroupMaterial.preview_id == preview_id,
        )
    ).one()
    ad_material_rows = session.exec(
        select(func.count(col(PreviewAdMaterial.material_id))).where(
            PreviewAdMaterial.tenant_id == context.tenant_id,
            PreviewAdMaterial.preview_id == preview_id,
        )
    ).one()
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    # 仅显式 GROUP 历史布局按组素材还原展示；V2 缺行不回退。
    # 这里只读既有冻结行，不重新规划广告或素材。
    material_allocation_count = (
        ad_material_rows
        if descriptor.version == 2 or descriptor.legacy_material_layout != "GROUP"
        else session.exec(
            select(func.count(col(PreviewGroupMaterial.material_id)))
            .select_from(PlannedAd)
            .join(
                PlannedGroup,
                (col(PlannedGroup.tenant_id) == col(PlannedAd.tenant_id))
                & (PlannedGroup.preview_id == PlannedAd.preview_id)
                & (PlannedGroup.id == PlannedAd.group_id),
            )
            .join(
                PreviewGroupMaterial,
                (col(PreviewGroupMaterial.tenant_id) == col(PlannedGroup.tenant_id))
                & (PreviewGroupMaterial.preview_id == PlannedGroup.preview_id)
                & (PreviewGroupMaterial.drama_id == PlannedGroup.drama_id)
                & (PreviewGroupMaterial.group_no == PlannedGroup.group_no),
            )
            .where(
                PlannedAd.tenant_id == context.tenant_id,
                PlannedAd.preview_id == preview_id,
            )
        ).one()
    )
    config = preview.config
    budget_strategy = config.get("budget_strategy", "SERIES")
    bid_strategy = frozen_bid_strategy(
        scene_snapshot={}, preview_config=config, target_roas=preview.target_roas
    )
    group_generation_mode = config.get("group_generation_mode", "FIXED")
    ad_generation_mode = config.get("ad_generation_mode", "BY_MATERIAL")
    creative_count = int(config.get("creative_count", 1))
    structure_summary = (
        build_structure_summary(
            campaign_count=n,
            group_count=g,
            ad_count=a,
            creative_count=creative_count,
            group_generation_mode=group_generation_mode,
            ad_generation_mode=ad_generation_mode,
            material_allocation_count=int(material_allocation_count),
            unique_material_count=int(unique_material_count),
        )
        + f"；预算策略 {budget_strategy_label(budget_strategy)}；竞价策略 {bid_strategy_label(bid_strategy)}"
    )
    issue_count = session.exec(
        select(func.count())
        .select_from(PreviewInput)
        .where(
            PreviewInput.tenant_id == context.tenant_id,
            PreviewInput.preview_id == preview_id,
            col(PreviewInput.status).not_in(["matched", "ready"]),
        )
    ).one()
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    from .continuation import preview_counts

    continuation = preview_counts(
        session, tenant_id=context.tenant_id, preview_id=preview_id
    )
    prior_ids = session.exec(
        select(Submission.id)
        .where(
            Submission.tenant_id == context.tenant_id,
            Submission.draft_id == preview.draft_id,
            Submission.preview_id != preview.id,
        )
        .order_by(col(Submission.ordinal).desc())
        .limit(20)
    ).all()
    return PreviewSummary(
        **continuation,
        can_refresh_continuation=bool(prior_ids),
        continuation_source_ids=list(prior_ids),
        new_campaign_count=n - continuation["reused_campaign_count"],
        new_adgroup_count=g - continuation["reused_adgroup_count"],
        new_ad_count=a - continuation["reused_ad_count"],
        contract_version=descriptor.version,
        media_kind=descriptor.media_kind,
        excluded_image_count=session.exec(
            select(func.count())
            .select_from(PreviewImageExclusion)
            .where(
                PreviewImageExclusion.tenant_id == context.tenant_id,
                PreviewImageExclusion.preview_id == preview.id,
            )
        ).one(),
        excluded_images=[
            {
                "drama_id": str(item.drama_id),
                "material_id": str(item.material_id),
                "file_name": item.file_name,
                "reason": item.reason,
            }
            for item in session.exec(
                select(PreviewImageExclusion)
                .where(
                    PreviewImageExclusion.tenant_id == context.tenant_id,
                    PreviewImageExclusion.preview_id == preview.id,
                )
                .order_by(
                    col(PreviewImageExclusion.drama_id),
                    col(PreviewImageExclusion.material_id),
                )
                .limit(100)
            ).all()
        ],
        excluded_dramas=_excluded_dramas(session, context, preview_id),
        targeting=preview.config.get("targeting"),
        targeting_region_codes=preview.progress.get("targeting_region_codes", []),
        skipped_material_count=session.exec(
            select(func.count(func.distinct(PreviewSkippedMaterial.material_id)))
            .join(
                BuildUnit,
                (col(BuildUnit.tenant_id) == PreviewSkippedMaterial.tenant_id)
                & (col(BuildUnit.id) == PreviewSkippedMaterial.unit_id),
            )
            .where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == preview_id,
            )
        ).one(),
        generation_progress=PreviewGenerationProgress(
            phase="complete"
            if preview.status == "FROZEN"
            else preview.progress["phase"],
            completed_units=sum(x[0] for x in counts.values()),
            total_units=preview.progress.get(
                "total_units",
                sum(x[0] for x in counts.values())
                if preview.status == "FROZEN"
                else None,
            ),
            updated_at=preview.progress.get("updated_at", preview.created_at),
        ),
        # 历史配置的只读状态来自持久化任务，跨浏览器也不重新展示创建入口。
        submission_id=session.exec(
            select(Submission.id).where(
                Submission.tenant_id == context.tenant_id,
                Submission.bc_id == preview.bc_id,
                Submission.preview_id == preview.id,
            )
        ).one_or_none(),
        execution_route=execution_route_view(
            session, context=context, preview_id=preview.id
        ),
        preview_id=preview.id,
        draft_id=preview.draft_id,
        draft_revision=preview.draft_revision,
        bc_id=preview.bc_id,
        status=cast(
            Literal["BUILDING", "FROZEN", "OBSOLETE", "FAILED"], preview.status
        ),
        currency=preview.config["currency"],
        campaign_count=n,
        adgroup_count=g,
        ad_count=a,
        blocked_count=counts.get("BLOCKED", (0, 0, 0))[0],
        preparing_count=counts.get("PREPARING", (0, 0, 0))[0],
        input_issue_count=issue_count,
        total_unit_count=sum(x[0] for x in counts.values()),
        daily_budget_sum=preview.budget * (g if budget_strategy == "ADGROUP" else n),
        daily_budget_label=budget_strategy_label(budget_strategy),
        budget_strategy=budget_strategy,
        bid_strategy=bid_strategy,
        creation_status=preview.config.get("creation_status", "ENABLE"),
        schedule_type=preview.config.get("schedule_type", "SCHEDULE_FROM_NOW"),
        schedule_start_time=preview.config.get("schedule_start_time"),
        schedule_end_time=preview.config.get("schedule_end_time"),
        group_generation_mode=group_generation_mode,
        ad_generation_mode=ad_generation_mode,
        creative_count=creative_count,
        unique_material_count=int(unique_material_count),
        material_allocation_count=int(material_allocation_count),
        structure_summary=structure_summary,
        content_digest=preview.content_digest,
        error_code=preview.error_code,
        created_at=preview.created_at,
    )


def _page_scope(
    context: TenantContext,
    identity: UUID,
    kind: str,
    limit: int,
    cursor: str | None,
    **extra: Any,
) -> tuple[dict[str, Any], str | None]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("configuration_invalid", "分页大小无效")
    scope = {
        "kind": kind,
        "tenant_id": str(context.tenant_id),
        "id": str(identity),
        **extra,
    }
    return scope, decode_cursor(cursor, scope=scope)


def get_preview_units(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
    readiness: str | None = None,
    drama_id: UUID | None = None,
) -> Page[PreviewUnit]:
    preview = _preview(session, context, preview_id)
    scope, after = _page_scope(
        context,
        preview_id,
        "preview_units",
        limit,
        cursor,
        readiness=readiness,
        drama_id=str(drama_id) if drama_id else None,
    )
    query = (
        select(BuildUnit, PreviewDrama)
        .join(
            PreviewDrama,
            and_(
                col(PreviewDrama.tenant_id) == BuildUnit.tenant_id,
                col(PreviewDrama.preview_id) == BuildUnit.preview_id,
                col(PreviewDrama.drama_id) == BuildUnit.drama_id,
            ),
        )
        .where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
            col(BuildUnit.complete).is_(True),
        )
    )  # noqa: E712
    if readiness:
        query = query.where(BuildUnit.readiness == readiness)
    if drama_id:
        query = query.where(BuildUnit.drama_id == drama_id)
    total = count_rows(session, query)
    if after:
        query = query.where(BuildUnit.id > UUID(after))
    rows = session.exec(query.order_by(col(BuildUnit.id)).limit(limit + 1)).all()
    skipped_counts = dict(
        session.exec(
            select(PreviewSkippedMaterial.unit_id, func.count())
            .where(
                PreviewSkippedMaterial.tenant_id == context.tenant_id,
                col(PreviewSkippedMaterial.unit_id).in_(
                    [u.id for u, _ in rows[:limit]]
                ),
            )
            .group_by(col(PreviewSkippedMaterial.unit_id))
        ).all()
    )
    unit_ids = [u.id for u, _ in rows[:limit]]
    material_rows = (
        session.exec(
            select(
                BuildUnit.id,
                func.count(col(PreviewGroupMaterial.material_id)),
                func.count(func.distinct(PreviewGroupMaterial.material_id)),
            )
            .join(
                PreviewGroupMaterial,
                (col(PreviewGroupMaterial.tenant_id) == col(BuildUnit.tenant_id))
                & (PreviewGroupMaterial.preview_id == BuildUnit.preview_id)
                & (PreviewGroupMaterial.drama_id == BuildUnit.drama_id),
            )
            .where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == preview_id,
                col(BuildUnit.id).in_(unit_ids),
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=col(BuildUnit.id),
                    material_id=col(PreviewGroupMaterial.material_id),
                ),
            )
            .group_by(col(BuildUnit.id))
        ).all()
        if unit_ids
        else []
    )
    ad_material_rows = (
        session.exec(
            select(col(BuildUnit.id), func.count(col(PreviewAdMaterial.material_id)))
            .join(
                PreviewAdMaterial,
                (col(PreviewAdMaterial.tenant_id) == col(BuildUnit.tenant_id))
                & (PreviewAdMaterial.preview_id == BuildUnit.preview_id)
                & (PreviewAdMaterial.drama_id == BuildUnit.drama_id),
            )
            .where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == preview_id,
                col(BuildUnit.id).in_(unit_ids),
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=col(BuildUnit.id),
                    material_id=col(PreviewAdMaterial.material_id),
                ),
            )
            .group_by(col(BuildUnit.id))
        ).all()
        if unit_ids
        else []
    )
    ad_material_counts = {row[0]: int(row[1] or 0) for row in ad_material_rows}
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    if (
        descriptor.version == 1
        and descriptor.legacy_material_layout == "GROUP"
        and unit_ids
    ):
        legacy_ad_material_rows = session.exec(
            select(col(BuildUnit.id), func.count(col(PreviewGroupMaterial.material_id)))
            .join(
                PlannedGroup,
                (col(PlannedGroup.tenant_id) == col(BuildUnit.tenant_id))
                & (PlannedGroup.preview_id == BuildUnit.preview_id)
                & (PlannedGroup.unit_id == BuildUnit.id),
            )
            .join(
                PlannedAd,
                (col(PlannedAd.tenant_id) == col(PlannedGroup.tenant_id))
                & (PlannedAd.preview_id == PlannedGroup.preview_id)
                & (PlannedAd.group_id == PlannedGroup.id),
            )
            .join(
                PreviewGroupMaterial,
                (col(PreviewGroupMaterial.tenant_id) == col(PlannedGroup.tenant_id))
                & (PreviewGroupMaterial.preview_id == PlannedGroup.preview_id)
                & (PreviewGroupMaterial.drama_id == PlannedGroup.drama_id)
                & (PreviewGroupMaterial.group_no == PlannedGroup.group_no),
            )
            .where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == preview_id,
                col(BuildUnit.id).in_(unit_ids),
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=col(BuildUnit.id),
                    material_id=col(PreviewGroupMaterial.material_id),
                ),
            )
            .group_by(col(BuildUnit.id))
        ).all()
        ad_material_counts = {
            row[0]: int(row[1] or 0) for row in legacy_ad_material_rows
        }
    material_counts = {
        row[0]: (int(row[1] or 0), int(row[2] or 0), ad_material_counts.get(row[0], 0))
        for row in material_rows
    }
    config = preview.config
    budget_strategy = config.get("budget_strategy", "SERIES")
    bid_strategy = frozen_bid_strategy(
        scene_snapshot={}, preview_config=config, target_roas=preview.target_roas
    )
    group_generation_mode = config.get("group_generation_mode", "FIXED")
    ad_generation_mode = config.get("ad_generation_mode", "BY_MATERIAL")
    creative_count = int(config.get("creative_count", 1))
    return Page(
        items=[
            PreviewUnit(
                skipped_material_count=skipped_counts.get(u.id, 0),
                unit_id=u.id,
                drama_id=u.drama_id,
                title=d.title,
                advertiser_id=u.advertiser_id,
                currency=u.currency,
                budget=preview.budget,
                campaign_name=u.campaign_name,
                readiness=cast(Readiness, u.readiness),
                reason_codes=u.reason_codes,
                group_count=u.group_count,
                ad_count=u.ad_count,
                material_count=material_counts.get(u.id, (0, 0, 0))[0],
                unique_material_count=material_counts.get(u.id, (0, 0, 0))[1],
                material_allocation_count=material_counts.get(u.id, (0, 0, 0))[2],
                group_summary=f"{u.group_count} 个广告组（广告组{generation_mode_label(group_generation_mode)}）",
                ad_summary=f"{u.ad_count} 个广告（广告{generation_mode_label(ad_generation_mode)}，创意数量 {creative_count}）",
                material_summary=(
                    f"去重素材 {material_counts.get(u.id, (0, 0, 0))[1]} 个，"
                    f"广告素材分配 {material_counts.get(u.id, (0, 0, 0))[2]} 次"
                ),
                structure_summary=build_structure_summary(
                    campaign_count=1,
                    group_count=u.group_count,
                    ad_count=u.ad_count,
                    creative_count=creative_count,
                    group_generation_mode=group_generation_mode,
                    ad_generation_mode=ad_generation_mode,
                    material_allocation_count=material_counts.get(u.id, (0, 0, 0))[2],
                    unique_material_count=material_counts.get(u.id, (0, 0, 0))[1],
                )
                + f"；{budget_unit_label(budget_strategy)}；竞价策略 {bid_strategy_label(bid_strategy)}",
            )
            for u, d in rows[:limit]
        ],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1][0].id))
        if len(rows) > limit
        else None,
        total=total,
    )


def _unit(
    session: Session, context: TenantContext, unit_id: UUID
) -> tuple[BuildPreview, BuildUnit, PreviewDrama]:
    _authorize(session, context)
    unit = session.exec(
        select(BuildUnit).where(
            BuildUnit.tenant_id == context.tenant_id, BuildUnit.id == unit_id
        )
    ).one_or_none()
    if unit is None:
        raise DomainError("preview_not_found", "预览单元不存在")
    preview = _preview(session, context, unit.preview_id)
    if preview.content_digest is None or not unit.complete:
        raise DomainError("preview_not_frozen", "预览尚未完成")
    drama = session.get(PreviewDrama, (context.tenant_id, preview.id, unit.drama_id))
    assert drama
    return preview, unit, drama


def load_frozen_unit(
    session: Session, *, context: TenantContext, unit_id: UUID
) -> FrozenUnit:
    preview, unit, drama = _unit(session, context, unit_id)
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    return _unit_snapshot(preview, unit, drama, descriptor)


def planned_unit_snapshot(
    session: Session, *, context: TenantContext, preview: BuildPreview, unit: BuildUnit
) -> FrozenUnit:
    """仅供冻结收尾编译计划；公开读取仍必须通过 _unit 的冻结状态检查。"""
    if (unit.tenant_id, unit.preview_id) != (
        context.tenant_id,
        preview.id,
    ) or not unit.complete:
        raise DomainError("preview_scope_mismatch", "冻结组合尚未完成")
    drama = session.get(PreviewDrama, (context.tenant_id, preview.id, unit.drama_id))
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    return _unit_snapshot(preview, unit, drama, descriptor)


def _unit_snapshot(preview, unit, drama, descriptor) -> FrozenUnit:
    return FrozenUnit(
        contract_version=descriptor.version,
        media_kind=descriptor.media_kind,
        unit_id=unit.id,
        preview_id=preview.id,
        tenant_id=unit.tenant_id,
        drama_id=drama.drama_id,
        link_id=drama.link_id,
        strategy_version_id=preview.strategy_version_id,
        bc_id=preview.bc_id,
        advertiser_id=unit.advertiser_id,
        connection_id=unit.connection_id,
        currency=unit.currency,
        timezone=unit.timezone,
        campaign_name=unit.campaign_name,
        protected_base=drama.protected_base,
        url=drama.url,
        budget=preview.budget,
        target_roas=preview.target_roas,
        budget_strategy=unit.scene_snapshot.get(
            "budget_strategy", preview.config.get("budget_strategy", "SERIES")
        ),
        bid_strategy=frozen_bid_strategy(
            scene_snapshot=unit.scene_snapshot,
            preview_config=preview.config,
            target_roas=preview.target_roas,
        ),
        creation_status=preview.config.get("creation_status", "ENABLE"),
        schedule_type=preview.config.get("schedule_type", "SCHEDULE_FROM_NOW"),
        schedule_start_time=preview.config.get("schedule_start_time"),
        schedule_end_time=preview.config.get("schedule_end_time"),
        readiness=cast(Readiness, unit.readiness),
        reason_codes=tuple(unit.reason_codes),
        scene_snapshot=unit.scene_snapshot,
        targeting=unit.scene_snapshot.get("field_constraints", {}).get(
            "audience_targeting"
        ),
        targeting_region_codes=unit.scene_snapshot.get("field_constraints", {}).get(
            "selected_region_codes", []
        ),
    )


def get_frozen_groups(
    session: Session,
    *,
    context: TenantContext,
    unit_id: UUID,
    cursor: str | None = None,
    limit: int = 20,
) -> Page[FrozenGroup]:
    preview, unit, _ = _unit(session, context, unit_id)
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    if descriptor.version == 2:
        from .preview_details import group_summaries

        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("configuration_invalid", "分页大小无效")
        return group_summaries(
            session,
            context=context,
            preview=preview,
            unit=unit,
            cursor=cursor,
            limit=limit,
        )
    scope, after = _page_scope(context, unit_id, "frozen_groups", limit, cursor)
    query = select(PlannedGroup).where(
        PlannedGroup.tenant_id == context.tenant_id, PlannedGroup.unit_id == unit_id
    )
    total = count_rows(session, query)
    if after:
        query = query.where(PlannedGroup.group_no > int(after))
    rows = session.exec(
        query.order_by(col(PlannedGroup.group_no)).limit(limit + 1)
    ).all()
    page_groups = rows[:limit]
    group_nos = [group.group_no for group in page_groups]
    group_ids = [group.id for group in page_groups]

    contract = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    member_limit = 100
    # 先以窗口函数按组做有界读取；计数和行读取都在数据库完成，避免恶意
    # 数据在 Python 中聚合后才发现超过 100 条。排除证据使用租户/任务范围
    # 的 EXISTS，保持旧预览的组级回退语义和输出顺序。
    group_material_rows = []
    if group_nos:
        group_material_counts = dict(
            session.exec(
                select(
                    PreviewGroupMaterial.group_no,
                    func.count().label("material_count"),
                )
                .where(
                    PreviewGroupMaterial.tenant_id == context.tenant_id,
                    PreviewGroupMaterial.preview_id == preview.id,
                    PreviewGroupMaterial.drama_id == unit.drama_id,
                    col(PreviewGroupMaterial.group_no).in_(group_nos),
                    material_not_skipped(
                        tenant_id=context.tenant_id,
                        unit_id=unit_id,
                        material_id=col(PreviewGroupMaterial.material_id),
                    ),
                )
                .group_by(col(PreviewGroupMaterial.group_no))
            ).all()
        )
        if any(count > member_limit for count in group_material_counts.values()):
            raise DomainError(
                "preview_group_too_large", "素材分组超过可用上限，请先调整草稿分组"
            )
        material_rank = (
            func.row_number()
            .over(
                partition_by=col(PreviewGroupMaterial.group_no),
                order_by=col(PreviewGroupMaterial.position),
            )
            .label("row_no")
        )
        material_rows = (
            select(
                PreviewGroupMaterial.group_no,
                PreviewGroupMaterial.position,
                PreviewGroupMaterial.material_id,
                material_rank,
            )
            .where(
                PreviewGroupMaterial.tenant_id == context.tenant_id,
                PreviewGroupMaterial.preview_id == preview.id,
                PreviewGroupMaterial.drama_id == unit.drama_id,
                col(PreviewGroupMaterial.group_no).in_(group_nos),
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=unit_id,
                    material_id=col(PreviewGroupMaterial.material_id),
                ),
            )
            .subquery()
        )
        group_material_rows = session.exec(
            select(
                material_rows.c.group_no,
                material_rows.c.position,
                material_rows.c.material_id,
            )
            .where(
                material_rows.c.row_no <= member_limit,
            )
            .order_by(
                material_rows.c.group_no,
                material_rows.c.position,
            )
        ).all()
    group_material_map: dict[int, list[UUID]] = {}
    for row in group_material_rows:
        group_material_map.setdefault(row[0], []).append(row[2])
    ads_by_group: dict[UUID, list[PlannedAd]] = {}
    if group_ids:
        ad_counts = dict(
            session.exec(
                select(PlannedAd.group_id, func.count().label("ad_count"))
                .where(
                    PlannedAd.tenant_id == context.tenant_id,
                    col(PlannedAd.group_id).in_(group_ids),
                )
                .group_by(col(PlannedAd.group_id))
            ).all()
        )
        if any(count > 100 for count in ad_counts.values()):
            raise DomainError("preview_group_too_large", "创意数量超过可用上限")
        ad_rank = (
            func.row_number()
            .over(
                partition_by=col(PlannedAd.group_id),
                order_by=(col(PlannedAd.base_ad_no), col(PlannedAd.creative_no)),
            )
            .label("row_no")
        )
        ad_rows = (
            select(PlannedAd, ad_rank)
            .where(
                PlannedAd.tenant_id == context.tenant_id,
                col(PlannedAd.group_id).in_(group_ids),
            )
            .subquery()
        )
        ad_alias = aliased(PlannedAd, ad_rows)
        planned_ads = session.exec(
            select(ad_alias)
            .where(ad_rows.c.row_no <= 100)
            .order_by(
                col(ad_alias.group_id),
                col(ad_alias.base_ad_no),
                col(ad_alias.creative_no),
            )
        ).all()
        for ad in planned_ads:
            ads_by_group.setdefault(ad.group_id, []).append(ad)

    contract = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    has_ad_material_rows = (
        contract.version == 2 or contract.legacy_material_layout != "GROUP"
    )
    ad_material_map: dict[tuple[int, int], list[UUID]] = {}
    if has_ad_material_rows and group_nos:
        ad_material_counts = session.exec(
            select(
                PreviewAdMaterial.group_no,
                PreviewAdMaterial.base_ad_no,
                func.count().label("material_count"),
            )
            .where(
                PreviewAdMaterial.tenant_id == context.tenant_id,
                PreviewAdMaterial.preview_id == preview.id,
                PreviewAdMaterial.drama_id == unit.drama_id,
                col(PreviewAdMaterial.group_no).in_(group_nos),
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=unit_id,
                    material_id=col(PreviewAdMaterial.material_id),
                ),
            )
            .group_by(
                col(PreviewAdMaterial.group_no),
                col(PreviewAdMaterial.base_ad_no),
            )
        ).all()
        if any(row[2] > 100 for row in ad_material_counts):
            raise DomainError("preview_group_too_large", "广告素材数量超过可用上限")
        ad_material_rank = (
            func.row_number()
            .over(
                partition_by=(
                    col(PreviewAdMaterial.group_no),
                    col(PreviewAdMaterial.base_ad_no),
                ),
                order_by=col(PreviewAdMaterial.position),
            )
            .label("row_no")
        )
        ad_material_rows_query = (
            select(
                PreviewAdMaterial.group_no,
                PreviewAdMaterial.base_ad_no,
                PreviewAdMaterial.position,
                col(PreviewAdMaterial.material_id),
            )
            .where(
                PreviewAdMaterial.tenant_id == context.tenant_id,
                PreviewAdMaterial.preview_id == preview.id,
                PreviewAdMaterial.drama_id == unit.drama_id,
                col(PreviewAdMaterial.group_no).in_(group_nos),
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=unit_id,
                    material_id=col(PreviewAdMaterial.material_id),
                ),
            )
            .add_columns(ad_material_rank)
            .subquery()
        )
        ad_material_rows = session.exec(
            select(
                ad_material_rows_query.c.group_no,
                ad_material_rows_query.c.base_ad_no,
                ad_material_rows_query.c.position,
                ad_material_rows_query.c.material_id,
            )
            .where(
                ad_material_rows_query.c.row_no <= 100,
            )
            .order_by(
                ad_material_rows_query.c.group_no,
                ad_material_rows_query.c.base_ad_no,
                ad_material_rows_query.c.position,
            )
        ).all()
        for row in ad_material_rows:
            ad_material_map.setdefault((row[0], row[1]), []).append(row[3])
    filtered_group_materials = {
        group_no: list(material_ids)
        for group_no, material_ids in group_material_map.items()
    }
    for group_no in group_nos:
        materials = filtered_group_materials.get(group_no, [])
        if len(materials) > member_limit:
            raise DomainError(
                "preview_group_too_large", "素材分组超过可用上限，请先调整草稿分组"
            )
    items = []
    for group in page_groups:
        materials = filtered_group_materials.get(group.group_no, [])
        ads = ads_by_group.get(group.id, [])
        items.append(
            FrozenGroup(
                group_id=group.id,
                group_no=group.group_no,
                name=group.name,
                material_ids=tuple(materials),
                ads=tuple(
                    FrozenAd(
                        ad_id=a.id,
                        base_ad_no=a.base_ad_no,
                        creative_no=a.creative_no,
                        name=a.name,
                        copy_id=a.copy_id,
                        text=a.text,
                        cta_option_ids=tuple(a.cta_option_ids),
                        material_ids=tuple(
                            ad_material_map.get(
                                (group.group_no, a.base_ad_no),
                                []
                                if has_ad_material_rows
                                else filtered_group_materials.get(group.group_no, []),
                            )
                        ),
                    )
                    for a in ads
                ),
            )
        )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].group_no))
        if len(rows) > limit
        else None,
        total=total,
    )


def get_skipped_materials(
    session: Session,
    *,
    context: TenantContext,
    unit_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[SkippedMaterialPublic]:
    _unit(session, context, unit_id)
    scope, after = _page_scope(context, unit_id, "skipped_materials", limit, cursor)
    query = select(PreviewSkippedMaterial).where(
        PreviewSkippedMaterial.tenant_id == context.tenant_id,
        PreviewSkippedMaterial.unit_id == unit_id,
    )
    total = count_rows(session, query)
    if after:
        query = query.where(PreviewSkippedMaterial.material_id > UUID(after))
    rows = session.exec(
        query.order_by(col(PreviewSkippedMaterial.material_id)).limit(limit + 1)
    ).all()
    return Page(
        items=[SkippedMaterialPublic(**r.model_dump()) for r in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].material_id))
        if len(rows) > limit
        else None,
        total=total,
    )


def get_preview_inputs(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    kind: str,
    cursor: str | None = None,
    limit: int = 50,
    issues_only: bool = False,
    status: str | None = None,
) -> Page[PreviewInputPublic]:
    _preview(session, context, preview_id)
    if kind not in {"drama", "account"}:
        raise DomainError("draft_input_invalid", "输入类别无效")
    scope, after = _page_scope(
        context,
        preview_id,
        "preview_inputs",
        limit,
        cursor,
        input_kind=kind,
        issues_only=str(issues_only),
        status=status,
    )
    query = select(PreviewInput).where(
        PreviewInput.tenant_id == context.tenant_id,
        PreviewInput.preview_id == preview_id,
        PreviewInput.kind == kind,
    )
    if issues_only:
        query = query.where(col(PreviewInput.status).not_in(["matched", "ready"]))
    if status:
        query = query.where(PreviewInput.status == status)
    total = count_rows(session, query)
    if after:
        query = query.where(PreviewInput.line_no > int(after))
    rows = session.exec(
        query.order_by(col(PreviewInput.line_no)).limit(limit + 1)
    ).all()
    return Page(
        items=[PreviewInputPublic(**r.model_dump()) for r in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].line_no))
        if len(rows) > limit
        else None,
        total=total,
    )


def get_frozen_ads(session, *, context, unit_id, group_id, cursor=None, limit=2):
    preview, unit, _ = _unit(session, context, unit_id)
    descriptor = preview_contract(
        session, tenant_id=context.tenant_id, preview_id=preview.id
    )
    if descriptor.version != 2:
        raise DomainError("preview_contract_expired", "历史预览请使用原分组详情")
    from .preview_details import ad_page

    return ad_page(
        session,
        context=context,
        preview=preview,
        unit=unit,
        group_id=group_id,
        cursor=cursor,
        limit=limit,
    )
