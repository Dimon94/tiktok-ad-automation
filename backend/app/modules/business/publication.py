"""季度可独立构建，但同次商业配置变更必须整体切换查询清单。"""

from sqlmodel import Session, col, select

from app.modules.business.models import BusinessPublication, BusinessRun, BusinessScope


def publish_ready_manifest(session: Session, scope: BusinessScope) -> bool:
    """调用方持有BC与scope行锁；旧清单持续服务，直到已知季度全部就绪。"""
    session.flush()
    latest = session.exec(
        select(BusinessRun)
        .join(
            BusinessPublication, col(BusinessPublication.run_id) == col(BusinessRun.id)
        )
        .where(
            col(BusinessPublication.tenant_id) == scope.tenant_id,
            col(BusinessPublication.bc_id) == scope.bc_id,
            col(BusinessRun.status) == "COMPLETE",
        )
    ).all()
    candidates = {r.quarter.isoformat(): r for r in latest}
    required = set(scope.published_runs) | set(candidates)
    if not required or any(
        q not in candidates or candidates[q].scope_revision != scope.revision
        for q in required
    ):
        return False
    if len({candidates[q].inputs.get("calculation_revision") for q in required}) != 1:
        return False
    scope.published_runs = {q: str(candidates[q].id) for q in sorted(required)}
    scope.published_revision = scope.revision
    session.add(scope)
    return True
