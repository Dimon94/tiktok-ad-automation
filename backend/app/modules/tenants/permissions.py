from typing import Literal
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.local_read_batch import reuse_local_read
from app.models import User
from app.modules.tenants.models import Tenant, TenantMembership

Action = Literal[
    "read",
    "manage",
    "build",
    "upload",
    "strategy_write",
    "provider_write",
    "reporting_write",
    "ads_manage",
]
Role = Literal["platform_admin", "tenant_admin", "operator", "viewer"]
ALL_ACTIONS = frozenset(
    {
        "read",
        "manage",
        "build",
        "upload",
        "strategy_write",
        "provider_write",
        "reporting_write",
        "ads_manage",
    }
)
PERMISSIONS = {
    "platform_admin": ALL_ACTIONS,
    "tenant_admin": ALL_ACTIONS,
    "operator": ALL_ACTIONS - {"manage"},
    "viewer": frozenset({"read"}),
}


@reuse_local_read
def require_tenant(
    session: Session, *, actor_id: UUID, tenant_id: UUID, action: str
) -> TenantContext:
    """Rebuild current authority from rows, including on background execution."""
    if action not in ALL_ACTIONS:
        raise DomainError("unknown_action", "未定义的业务动作")
    user = session.get(User, actor_id, populate_existing=True)
    tenant = session.get(Tenant, tenant_id, populate_existing=True)
    if not user or not user.is_active or not tenant or not tenant.active:
        raise DomainError("tenant_forbidden", "无法进入该租户")
    member = session.get(
        TenantMembership, (tenant_id, actor_id), populate_existing=True
    )
    if user.is_superuser:
        role = "platform_admin"
    elif member and member.active:
        role = member.role
    else:
        raise DomainError("tenant_forbidden", "无法进入该租户")
    if action not in PERMISSIONS.get(role, frozenset()):
        raise DomainError("action_forbidden", "当前角色不能执行此操作")
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, role=role)


def build_authorization_stamp(
    session: Session, *, actor_id: UUID, tenant_id: UUID
) -> dict:
    """批准与检查共用事实行；锁顺序与原生物理发送 User→Tenant→Member 一致。"""
    user = session.get(
        User, actor_id, populate_existing=True, with_for_update={"read": True}
    )
    tenant = session.get(
        Tenant, tenant_id, populate_existing=True, with_for_update={"read": True}
    )
    member = session.get(
        TenantMembership,
        (tenant_id, actor_id),
        populate_existing=True,
        with_for_update={"read": True},
    )
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="build")
    # 同事务共享锁内的授权检查已保证用户、租户和所需成员存在。
    assert user is not None and tenant is not None
    member_generation = None
    if not user.is_superuser:
        assert member is not None
        member_generation = member.authorization_generation
    return {
        "actor_id": str(actor_id),
        "tenant_id": str(tenant_id),
        "user": user.authorization_generation,
        "tenant": tenant.authorization_generation,
        # 平台管理员不依赖成员角色；无关 membership 变更不撤销平台批准。
        "member": member_generation,
    }


def require_build_authorization_stamp(session: Session, *, submission) -> None:
    current = build_authorization_stamp(
        session, actor_id=submission.actor_id, tenant_id=submission.tenant_id
    )
    if submission.authorization_stamp != current:
        raise DomainError(
            "actor_authorization_changed",
            "原批准的操作者授权已失效，请新建预览并明确提交",
        )
