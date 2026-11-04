"""只展开已界定的账户集合；每次读取重新校验当前成员与 BC 授权。"""

from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import tuple_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import (
    ENTITY_KINDS,
    EntityRef,
    MaterialUseRef,
)
from app.integrations.tiktok.contracts.carousel import (
    CarouselDirectory,
    decode_carousels,
)
from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.ads.naming import parse_campaign_name
from app.modules.tenants.permissions import require_tenant


def _authorize(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_ids: tuple[str, ...],
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if not advertiser_ids or len(set(advertiser_ids)) != len(advertiser_ids):
        raise DomainError("invalid_directory_scope", "请先界定要读取的账户集合")
    grants = session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
        .where(col(BCAccountAccess.advertiser_id).in_(advertiser_ids))
        .execution_options(populate_existing=True)
    ).all()
    if {grant.advertiser_id for grant in grants} != set(advertiser_ids):
        raise DomainError("directory_forbidden", "当前 BC 没有这些账户的有效读取授权")


def locate(
    session: Session, *, context: TenantContext, bc_id: str, ref: EntityRef
) -> AdObject:
    if ref.tenant_id != context.tenant_id:
        raise DomainError("directory_forbidden", "无法读取其他租户的投放对象")
    _authorize(
        session, context=context, bc_id=bc_id, advertiser_ids=(ref.advertiser_id,)
    )
    row = session.get(
        AdObject,
        (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id),
        populate_existing=True,
    )
    if row is None:
        raise DomainError("ad_object_not_found", "投放对象尚未同步")
    return row


def list_objects(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_ids: tuple[str, ...],
    kind: str,
    parent: EntityRef | None = None,
) -> tuple[AdObject, ...]:
    if kind not in ENTITY_KINDS or (
        parent is not None
        and (
            parent.tenant_id != context.tenant_id
            or parent.advertiser_id not in advertiser_ids
        )
    ):
        raise DomainError("invalid_directory_scope", "对象类型或父对象范围无效")
    _authorize(session, context=context, bc_id=bc_id, advertiser_ids=advertiser_ids)
    statement = select(AdObject).where(
        AdObject.tenant_id == context.tenant_id,
        col(AdObject.advertiser_id).in_(advertiser_ids),
        AdObject.kind == kind,
    )
    if parent is not None:
        statement = statement.where(
            AdObject.advertiser_id == parent.advertiser_id,
            AdObject.parent_kind == parent.kind,
            AdObject.parent_remote_id == parent.remote_id,
        )
    return tuple(
        session.exec(
            statement.order_by(
                AdObject.advertiser_id, AdObject.remote_id
            ).execution_options(populate_existing=True)
        ).all()
    )


def append_campaign_name_projection(
    session: Session, *, campaign_ref: EntityRef, raw_name: str, parser_revision: int
) -> CampaignNameProjection:
    """单对象调用与同步批量发布共用同一名称审计规则。"""
    return append_campaign_name_projections(
        session, campaigns=((campaign_ref, raw_name),), parser_revision=parser_revision
    )[0]


def append_campaign_name_projections(
    session: Session,
    *,
    campaigns: tuple[tuple[EntityRef, str], ...],
    parser_revision: int,
) -> tuple[CampaignNameProjection, ...]:
    """按批锁定对象、读取最新审计并追加；备注变化只增加名称版本。"""
    if (
        type(parser_revision) is not int
        or parser_revision < 1
        or any(ref.kind != "campaign" for ref, _name in campaigns)
    ):
        raise DomainError("invalid_campaign_projection", "系列名称投影参数无效")
    identities = {
        (ref.tenant_id, ref.advertiser_id, ref.remote_id): name
        for ref, name in campaigns
    }
    result = []
    keys = sorted(identities)
    for start in range(0, len(keys), 500):
        batch = keys[start : start + 500]
        # 与独立调用者共用稳定对象行锁；按身份排序避免多个批次逆序拿锁。
        objects = session.exec(
            select(AdObject)
            .where(
                AdObject.kind == "campaign",
                tuple_(
                    col(AdObject.tenant_id),
                    col(AdObject.advertiser_id),
                    col(AdObject.remote_id),
                ).in_(batch),
            )
            .order_by(
                col(AdObject.tenant_id),
                col(AdObject.advertiser_id),
                col(AdObject.remote_id),
            )
            .with_for_update()
        ).all()
        if len(objects) != len(batch):
            raise DomainError("ad_object_not_found", "系列对象尚未同步")
        previous_rows = session.exec(
            select(CampaignNameProjection)
            .where(
                tuple_(
                    col(CampaignNameProjection.tenant_id),
                    col(CampaignNameProjection.advertiser_id),
                    col(CampaignNameProjection.campaign_remote_id),
                ).in_(batch),
            )
            .distinct(
                col(CampaignNameProjection.tenant_id),
                col(CampaignNameProjection.advertiser_id),
                col(CampaignNameProjection.campaign_remote_id),
            )
            .order_by(
                col(CampaignNameProjection.tenant_id),
                col(CampaignNameProjection.advertiser_id),
                col(CampaignNameProjection.campaign_remote_id),
                col(CampaignNameProjection.name_revision).desc(),
            )
        ).all()
        previous_by_key = {
            (p.tenant_id, p.advertiser_id, p.campaign_remote_id): p
            for p in previous_rows
        }
        # 自由命名无法靠前两段识别；本工具搭建的系列按远端回执读取冻结身份。
        from app.modules.builds.naming_identity import built_campaign_identities

        built_identities = built_campaign_identities(
            session,
            tuple(
                (EntityRef(t, a, "campaign", r), identities[t, a, r])
                for t, a, r in batch
                if (previous := previous_by_key.get((t, a, r))) is None
                or (previous.raw_name, previous.parser_revision)
                != (identities[t, a, r], parser_revision)
            ),
        )
        for tenant_id, advertiser_id, remote_id in batch:
            raw_name = identities[tenant_id, advertiser_id, remote_id]
            identity = built_identities.get(
                EntityRef(tenant_id, advertiser_id, "campaign", remote_id)
            ) or parse_campaign_name(raw_name)
            previous = previous_by_key.get((tenant_id, advertiser_id, remote_id))
            if previous and (previous.raw_name, previous.parser_revision) == (
                raw_name,
                parser_revision,
            ):
                result.append(previous)
                continue
            changed = previous is not None and (
                previous.provider_label,
                previous.drama_name,
                previous.status,
            ) != (identity.provider_label, identity.drama_name, identity.status)
            projection = CampaignNameProjection(
                tenant_id=tenant_id,
                advertiser_id=advertiser_id,
                campaign_remote_id=remote_id,
                raw_name=raw_name,
                provider_label=identity.provider_label,
                drama_name=identity.drama_name,
                status=identity.status,
                parser_revision=parser_revision,
                name_revision=previous.name_revision + 1 if previous else 1,
                grouping_revision=previous.grouping_revision + int(changed)
                if previous
                else 1,
            )
            session.add(projection)
            result.append(projection)
        session.flush()
    # 单条接口要求原身份对应的返回值；批量输入由调用方按对象身份去重。
    by_key = {(p.tenant_id, p.advertiser_id, p.campaign_remote_id): p for p in result}
    return tuple(
        by_key[ref.tenant_id, ref.advertiser_id, ref.remote_id] for ref, _ in campaigns
    )


def locate_material_use(
    session: Session, use: MaterialUseRef
) -> AdMaterialReference | None:
    """按完整业务身份读取素材，供预览和执行复用；表主键是独立 UUID。"""
    statement = select(AdMaterialReference).where(
        AdMaterialReference.tenant_id == use.ad_ref.tenant_id,
        AdMaterialReference.advertiser_id == use.ad_ref.advertiser_id,
        AdMaterialReference.ad_remote_id == use.ad_ref.remote_id,
        AdMaterialReference.platform_material_id == use.platform_material_id,
        AdMaterialReference.material_type == use.material_type,
    )
    if use.ad_material_id is None:
        statement = statement.where(col(AdMaterialReference.ad_material_id).is_(None))
    else:
        statement = statement.where(
            AdMaterialReference.ad_material_id == use.ad_material_id
        )
    return session.exec(statement.execution_options(populate_existing=True)).first()


def current_ad_materials(session: Session, ref: EntityRef) -> list[AdMaterialReference]:
    """每个视频一条：历史身份留库核算消耗，展示和操作使用最近完整观察的广告内身份。"""
    rows = session.exec(
        select(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == ref.tenant_id,
            AdMaterialReference.advertiser_id == ref.advertiser_id,
            AdMaterialReference.ad_remote_id == ref.remote_id,
            col(AdMaterialReference.complete).is_(True),
        )
        .execution_options(populate_existing=True)
    ).all()
    return latest_ad_materials(rows)


def latest_ad_materials(
    rows: Sequence[AdMaterialReference],
) -> list[AdMaterialReference]:
    """在同一广告内选各视频的当前唯一身份；规则批量加载与广告详情共用。"""
    grouped: dict[tuple[str, str], list[AdMaterialReference]] = defaultdict(list)
    for row in rows:
        grouped[row.platform_material_id, row.material_type].append(row)
    result = []
    for uses in grouped.values():
        # 同步通道还会返回无广告内 ID 的视频行，不能覆盖可单独启停的精确身份。
        candidates = [use for use in uses if use.ad_material_id] or uses
        version = max(use.published_version for use in candidates)
        latest = [use for use in candidates if use.published_version == version]
        current = latest[0]
        if len(latest) > 1:
            # 同次观察仍有多个身份时无法确定唯一操作对象，不能按 ID 大小猜测。
            # 只改返回副本，不污染数据库中的历史状态；前端对无 ID 的行禁用选择。
            current = current.model_copy(
                update={"ad_material_id": None, "operation_status": None}
            )
        result.append(current)
    return sorted(
        result, key=lambda row: (row.name, row.platform_material_id, row.material_type)
    )


def directory_carousels(entity: AdObject) -> CarouselDirectory:
    if entity.kind not in {"ad", "creative"}:
        return CarouselDirectory()
    try:
        # 空值只交给严格解析器拒绝轮播身份；不能把无父ID的视频误标成轮播。
        ad_id = (
            entity.remote_id if entity.kind == "ad" else (entity.parent_remote_id or "")
        )
        return decode_carousels(
            entity.configuration or {},
            advertiser_id=entity.advertiser_id,
            ad_id=ad_id,
            smart=entity.ad_type == "SMART_PLUS" and entity.kind == "ad",
            creative_id=entity.remote_id if entity.kind == "creative" else None,
        )
    except ValueError:
        # 历史已发布的坏结构不影响广告金额；说明关系明确不可用。
        return CarouselDirectory(is_carousel=True, complete=False)
