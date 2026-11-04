"""用真实搭建回执和冻结剧目识别自定义名称，避免按字段位置猜测归属。"""

from sqlalchemy import tuple_
from sqlmodel import Session, col, select

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
    rows = session.exec(
        select(ExecutionStep, BuildUnit, PreviewDrama)
        .join(
            BuildUnit,
            (col(BuildUnit.tenant_id) == ExecutionStep.tenant_id)
            & (col(BuildUnit.id) == ExecutionStep.unit_id)
            & (col(BuildUnit.preview_id) == ExecutionStep.preview_id)
            & (col(BuildUnit.bc_id) == ExecutionStep.bc_id),
        )
        .join(
            PreviewDrama,
            (col(PreviewDrama.tenant_id) == BuildUnit.tenant_id)
            & (col(PreviewDrama.preview_id) == BuildUnit.preview_id)
            & (col(PreviewDrama.drama_id) == BuildUnit.drama_id),
        )
        .where(
            ExecutionStep.kind == "CAMPAIGN",
            col(ExecutionStep.remote_id).is_not(None),
            tuple_(
                col(ExecutionStep.tenant_id),
                col(BuildUnit.advertiser_id),
                col(ExecutionStep.remote_id),
            ).in_([(ref.tenant_id, ref.advertiser_id, ref.remote_id) for ref in names]),
        )
    ).all()
    result: dict[EntityRef, CampaignIdentity] = {}
    for step, unit, drama in rows:
        assert step.remote_id is not None
        ref = EntityRef(step.tenant_id, unit.advertiser_id, "campaign", step.remote_id)
        # 平台另行改名后不沿用原冻结身份；外部广告继续走既有名称解析规则。
        if names[ref] != unit.campaign_name:
            continue
        label, title = naming_parts(
            drama.protected_base, drama.title, drama.provider_pinyin
        )
        if label.strip() and title.strip():
            result[ref] = CampaignIdentity(label, title, "VALID")
    return result
