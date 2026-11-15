"""用真实搭建回执和冻结剧目识别自定义名称，避免按字段位置猜测归属。"""

from typing import cast as typing_cast

from sqlalchemy import select as sa_select
from sqlalchemy import tuple_
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col

from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ads.naming import CampaignIdentity
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit, PreviewDrama
from app.modules.strategies.naming import naming_parts


def built_campaign_identities(
    session: Session, campaigns: tuple[tuple[EntityRef, str], ...]
) -> dict[EntityRef, CampaignIdentity]:
    names = dict(campaigns)
    if not names:
        return {}
    rows = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            # 目录每批会重验晚到回执，仅取身份列，避免加载完整执行输入和预览正文。
            sa_select(
                col(ExecutionStep.tenant_id),
                col(BuildUnit.advertiser_id),
                col(ExecutionStep.remote_id),
                col(BuildUnit.campaign_name),
                col(PreviewDrama.protected_base),
                col(PreviewDrama.title),
                col(PreviewDrama.provider_pinyin),
            )
            .join(
                BuildUnit,
                (col(BuildUnit.tenant_id) == col(ExecutionStep.tenant_id))
                & (col(BuildUnit.id) == col(ExecutionStep.unit_id))
                & (col(BuildUnit.preview_id) == col(ExecutionStep.preview_id))
                & (col(BuildUnit.bc_id) == col(ExecutionStep.bc_id)),
            )
            .join(
                PreviewDrama,
                (col(PreviewDrama.tenant_id) == col(BuildUnit.tenant_id))
                & (col(PreviewDrama.preview_id) == col(BuildUnit.preview_id))
                & (col(PreviewDrama.drama_id) == col(BuildUnit.drama_id)),
            )
            .where(
                col(ExecutionStep.kind) == "CAMPAIGN",
                col(ExecutionStep.remote_id).is_not(None),
                tuple_(
                    col(ExecutionStep.tenant_id),
                    col(BuildUnit.advertiser_id),
                    col(ExecutionStep.remote_id),
                ).in_(
                    [(ref.tenant_id, ref.advertiser_id, ref.remote_id) for ref in names]
                ),
            )
        )
        .all()
    )
    result: dict[EntityRef, CampaignIdentity] = {}
    for tenant, advertiser, remote_id, frozen_name, base, title, provider in rows:
        assert remote_id is not None
        ref = EntityRef(tenant, advertiser, "campaign", remote_id)
        # 平台另行改名后不沿用原冻结身份；外部广告继续走既有名称解析规则。
        if names[ref] != frozen_name:
            continue
        label, title = naming_parts(base, title, provider)
        if label.strip() and title.strip():
            result[ref] = CampaignIdentity(label, title, "VALID")
    return result
