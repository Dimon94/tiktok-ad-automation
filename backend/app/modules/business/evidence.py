"""赔付依据读取与记录；空依据与已核实无赔付分开。"""

from datetime import date

from fastapi import HTTPException
from sqlmodel import Session

from app.core.context import TenantContext
from app.modules.business.configuration import scope_accounts
from app.modules.business.models import BusinessEvidence


def read_evidence(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    campaign_id: str,
    stat_date: date,
) -> BusinessEvidence | None:
    if advertiser_id not in scope_accounts(session, context, bc_id):
        raise HTTPException(403, "当前BC未授权账户")
    return session.get(
        BusinessEvidence,
        (context.tenant_id, bc_id, advertiser_id, campaign_id, stat_date),
    )
