"""准备块的短生命周期目录缓存；权限和目录各按集合加载，绝不跨事务复用。"""

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import tuple_
from sqlmodel import col, select

from app.modules.accounts.access import usable_grants
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.models import BCAccountAccess
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection


@contextmanager
def expansion_cache(session, context, bc_id, route, record, mutation):
    from app.modules.ad_management.expansion import _ref_key
    from app.modules.reporting.selection import selection_public

    selection = selection_public(record)
    grants = {
        g.advertiser_id: g
        for g in session.exec(
            usable_grants(
                tenant_id=context.tenant_id, bc_id=bc_id, action="read"
            ).where(
                col(BCAccountAccess.connection_id) == route.connection_id,
                col(BCAccountAccess.permission_state) == "VERIFIED",
                col(BCAccountAccess.checked_at) <= datetime.now(UTC),
            )
        ).all()
    }
    authorization = session.exec(
        select(ConnectionAuthorization).where(
            col(ConnectionAuthorization.tenant_id) == context.tenant_id,
            col(ConnectionAuthorization.connection_id) == route.connection_id,
            col(ConnectionAuthorization.authorization_revision)
            == route.authorization_revision,
        )
    ).one_or_none()
    objects = {}
    refs = (
        *selection.refs,
        *(u.ad_ref for u in selection.material_uses),
        *mutation.include_parents,
    )
    keys = {
        _ref_key(r)
        for r in refs
        if r.tenant_id == context.tenant_id and r.advertiser_id in grants
    }
    # 广告→广告组→系列最多两级；关系身份也按集合读取。
    for _ in range(3):
        if not keys:
            break
        rows = session.exec(
            select(AdObject).where(
                tuple_(
                    col(AdObject.tenant_id),
                    col(AdObject.advertiser_id),
                    col(AdObject.kind),
                    col(AdObject.remote_id),
                ).in_(keys)
            )
        ).all()
        for row in rows:
            objects[_ref_key(row.ref)] = row
        keys = {
            _ref_key(r.parent_ref)
            for r in rows
            if r.parent_ref and _ref_key(r.parent_ref) not in objects
        }
    children = {}
    campaigns = [
        (r.advertiser_id, r.remote_id) for r in objects.values() if r.kind == "campaign"
    ]
    if campaigns and mutation.field in {"budget", "roas"}:
        rows = session.exec(
            select(AdObject).where(
                col(AdObject.tenant_id) == context.tenant_id,
                col(AdObject.kind) == "adgroup",
                col(AdObject.parent_kind) == "campaign",
                tuple_(col(AdObject.advertiser_id), col(AdObject.parent_remote_id)).in_(
                    campaigns
                ),
                col(AdObject.source_connection_id) == route.connection_id,
                col(AdObject.source_channel) == route.channel,
                col(AdObject.observed_at) <= record.created_at,
            )
        ).all()
        for row in rows:
            objects[_ref_key(row.ref)] = row
            children.setdefault((row.advertiser_id, row.parent_remote_id), []).append(
                row
            )
    projections = {}
    if campaigns:
        rows = session.exec(
            select(CampaignNameProjection)
            .where(
                col(CampaignNameProjection.tenant_id) == context.tenant_id,
                tuple_(
                    col(CampaignNameProjection.advertiser_id),
                    col(CampaignNameProjection.campaign_remote_id),
                ).in_(campaigns),
            )
            .order_by(col(CampaignNameProjection.name_revision).desc())
        ).all()
        for row in rows:
            projections.setdefault((row.advertiser_id, row.campaign_remote_id), row)
    materials = {}
    ads = [
        (u.ad_ref.advertiser_id, u.ad_ref.remote_id) for u in selection.material_uses
    ]
    if ads:
        for row in session.exec(
            select(AdMaterialReference).where(
                col(AdMaterialReference.tenant_id) == context.tenant_id,
                tuple_(
                    col(AdMaterialReference.advertiser_id),
                    col(AdMaterialReference.ad_remote_id),
                ).in_(ads),
            )
        ).all():
            materials[
                (
                    row.tenant_id,
                    row.advertiser_id,
                    "ad",
                    row.ad_remote_id,
                    row.platform_material_id,
                    row.ad_material_id,
                    row.material_type,
                )
            ] = row
    session.info["management_expansion_cache"] = {
        "objects": objects,
        "children": children,
        "projections": projections,
        "materials": materials,
        "grants": grants,
        "authorization": authorization,
    }
    try:
        yield
    finally:
        session.info.pop("management_expansion_cache", None)
