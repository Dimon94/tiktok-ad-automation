"""只从明确 BC 默认冻结连接；后续任务只核验原连接，不重新选路。"""

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.local_read_batch import reuse_local_read
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.management_rules import (
    MANAGEMENT_ENTITY_KINDS,
    management_allowed,
)
from app.modules.accounts.models import (
    OPERABLE_REMOTE_STATUSES,
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

Capability = Literal["read", "upload", "build", "ads_manage"]


def _bc(session: Session, context: TenantContext, bc_id: str) -> TenantBC:
    row = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if row is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    if row.ownership_conflict:
        raise DomainError("account_ownership_conflict", "BC 归属冲突")
    return row


def _connection(
    session: Session, context: TenantContext, bc_id: str, connection_id: UUID
) -> TikTokConnection:
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if connection is None or connection.tenant_id != context.tenant_id:
        raise DomainError("connection_not_found", "当前租户连接不存在")
    binding = session.get(
        BCConnectionBinding,
        (context.tenant_id, bc_id, connection_id),
        populate_existing=True,
    )
    if binding is None or binding.kind != connection.kind or binding.status != "ACTIVE":
        raise DomainError("connection_bc_mismatch", "连接未绑定当前 BC")
    if connection.status != "ACTIVE":
        raise DomainError("connection_unavailable", "连接当前不可用")
    return connection


@reuse_local_read
def freeze_route(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID | None = None,
) -> FrozenTikTokRoute:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    _bc(session, context, bc_id)
    if connection_id is None:
        default = session.get(
            BCDefaultRoute, (context.tenant_id, bc_id), populate_existing=True
        )
        if default is None:
            raise DomainError(
                "bc_default_connection_required", "请先为该 BC 选择默认执行连接"
            )
        connection_id = default.connection_id
    connection = _connection(session, context, bc_id, connection_id)
    binding = session.get(
        BCConnectionBinding, (context.tenant_id, bc_id, connection_id)
    )
    assert binding is not None
    return FrozenTikTokRoute(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        connection_id=connection.id,
        channel=connection.kind,
        authorization_revision=connection.authorization_revision,
        adapter_contract_revision=connection.adapter_contract_revision,
        binding_revision=binding.revision,
    )


def observed_authorization(observed_at: datetime | None, now: datetime) -> bool:
    """已确认授权持续有效；时间仅用于识别缺失或无效记录，不设置使用期限。"""
    if observed_at is None or observed_at.tzinfo is None:
        return False
    return observed_at <= now


@reuse_local_read
def verify_route(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_id: str | None,
    capability: Capability,
    operation: str | None = None,
    entity_kind: str | None = None,
) -> None:
    if capability not in {"read", "upload", "build", "ads_manage"}:
        raise DomainError("invalid_account_action", "账户动作无效")
    if route.tenant_id != context.tenant_id:
        raise DomainError("connection_tenant_mismatch", "连接不属于当前租户")
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action=capability,
    )
    if advertiser_id is None and capability != "read":
        raise DomainError("account_required", "该操作必须指定广告账户")
    if advertiser_id is not None and (
        type(advertiser_id) is not str or not advertiser_id.strip()
    ):
        raise DomainError("account_required", "该操作必须指定广告账户")
    _bc(session, context, route.bc_id)
    connection = _connection(session, context, route.bc_id, route.connection_id)
    binding = session.get(
        BCConnectionBinding, (context.tenant_id, route.bc_id, route.connection_id)
    )
    if binding is None or binding.revision != route.binding_revision:
        raise DomainError("route_binding_changed", "原任务的 BC 绑定已失效，请重新准备")
    if connection.kind != route.channel:
        raise DomainError("connection_channel_mismatch", "连接通道已改变")
    if connection.authorization_revision != route.authorization_revision:
        raise DomainError("route_authorization_changed", "原任务的授权范围已改变")
    if connection.adapter_contract_revision != route.adapter_contract_revision:
        raise DomainError("route_contract_changed", "原任务的接口契约已改变")
    # BC 目录/角色读取正用于更新账户证据；不能先要求待刷新的账户证据新鲜。
    # gateway 操作白名单负责将 None 限定为这些读取及协议请求。
    if advertiser_id is None:
        return
    account = session.get(
        AdvertiserAccount,
        (context.tenant_id, advertiser_id),
        populate_existing=True,
    )
    grant = session.get(
        BCAccountAccess,
        (context.tenant_id, route.bc_id, advertiser_id, route.connection_id),
        populate_existing=True,
    )
    authorization = session.exec(
        select(ConnectionAuthorization)
        .where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    _verify_account_evidence(
        account,
        grant,
        authorization,
        capability=capability,
        operation=operation,
        entity_kind=entity_kind,
    )


def _verify_account_evidence(
    account: AdvertiserAccount | None,
    grant: BCAccountAccess | None,
    authorization: ConnectionAuthorization | None,
    *,
    capability: Capability,
    operation: str | None = None,
    entity_kind: str | None = None,
) -> None:
    """单账户及批量读取共用同一证据判定，避免优化绕过授权规则。"""
    if account is None or grant is None:
        raise DomainError("account_not_in_bc", "账户不在该连接的 BC 目录")
    if account.ownership_conflict:
        raise DomainError("account_ownership_conflict", "账户归属冲突")
    if not account.currency.strip() or not account.timezone.strip():
        raise DomainError("account_metadata_incomplete", "账户信息尚未完整")
    if not (grant.in_bc and grant.authorized and grant.active):
        raise DomainError("account_access_denied", "当前连接未授权该账户")

    now = datetime.now(UTC)
    if (
        authorization is None
        or not observed_authorization(authorization.verified_at, now)
        or not observed_authorization(grant.checked_at, now)
    ):
        raise DomainError("account_access_denied", "缺少已确认的账户授权")
    if capability == "ads_manage":
        if (
            account.remote_status not in OPERABLE_REMOTE_STATUSES
            or grant.permission_state != "VERIFIED"
        ):
            raise DomainError("account_access_denied", "账户当前状态不支持管理操作")
        if operation is None or operation not in MANAGEMENT_ENTITY_KINDS:
            raise DomainError(
                "management_permission_unverified",
                "management_permission_unverified: 必须指定管理操作",
            )
        if (
            entity_kind is not None
            and entity_kind not in MANAGEMENT_ENTITY_KINDS[operation]
        ):
            raise DomainError(
                "management_permission_unverified",
                "management_permission_unverified: 该对象层级不支持此管理操作",
            )
        if not management_allowed(grant, authorization.scopes, operation):
            raise DomainError(
                "management_permission_unverified",
                "management_permission_unverified: 当前连接对该账户没有广告管理权限",
            )
        return
    if (
        not authorization.source
        or authorization.source == "UNKNOWN"
        or authorization.permission_summary.get(f"{capability}_authorized") is not True
    ):
        raise DomainError("account_access_denied", "缺少该操作的明确授权证据")
    if capability != "read" and (
        not authorization.scopes
        or grant.permission_state != "VERIFIED"
        or account.remote_status not in OPERABLE_REMOTE_STATUSES
        or (capability == "upload" and not grant.can_upload)
        or (capability == "build" and not grant.can_build)
    ):
        raise DomainError("account_access_denied", "账户当前角色或范围不支持该操作")


def verify_read_routes(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_ids: Collection[str],
) -> dict[str, str]:
    """一个事务阶段批量读取账户证据；HTTP前后必须各调用一次，不跨阶段缓存。"""
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    if any(type(value) is not str or not value.strip() for value in advertiser_ids):
        raise DomainError("account_required", "该操作必须指定广告账户")
    advertisers = list(dict.fromkeys(advertiser_ids))
    authorization = session.exec(
        select(ConnectionAuthorization)
        .where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    currencies: dict[str, str] = {}
    # 只读取当前租户、BC、冻结连接的账户证据；每500项一页限制SQL参数和内存。
    for offset in range(0, len(advertisers), 500):
        page = advertisers[offset : offset + 500]
        accounts = {
            row.advertiser_id: row
            for row in session.exec(
                select(AdvertiserAccount)
                .where(
                    col(AdvertiserAccount.tenant_id) == context.tenant_id,
                    col(AdvertiserAccount.advertiser_id).in_(page),
                )
                .execution_options(populate_existing=True)
            ).all()
        }
        grants = {
            row.advertiser_id: row
            for row in session.exec(
                select(BCAccountAccess)
                .where(
                    col(BCAccountAccess.tenant_id) == context.tenant_id,
                    col(BCAccountAccess.bc_id) == route.bc_id,
                    col(BCAccountAccess.connection_id) == route.connection_id,
                    col(BCAccountAccess.advertiser_id).in_(page),
                )
                .execution_options(populate_existing=True)
            ).all()
        }
        for advertiser in page:
            _verify_account_evidence(
                accounts.get(advertiser),
                grants.get(advertiser),
                authorization,
                capability="read",
            )
            currencies[advertiser] = accounts[advertiser].currency

    return currencies


def set_default_route(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID,
) -> FrozenTikTokRoute:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    # BC 行是设置默认的互斥点；一笔事务中验证并写入同一条明确绑定。
    row = session.exec(
        select(TenantBC)
        .where(TenantBC.tenant_id == context.tenant_id, TenantBC.bc_id == bc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    route = freeze_route(
        session, context=context, bc_id=bc_id, connection_id=connection_id
    )
    default = session.get(BCDefaultRoute, (context.tenant_id, bc_id))
    if default is None:
        default = BCDefaultRoute(
            tenant_id=context.tenant_id, bc_id=bc_id, connection_id=connection_id
        )
    else:
        default.connection_id = connection_id
    session.add(default)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.bc.default_connection",
            target_id=bc_id,
            details={"connection_id": str(connection_id), "channel": route.channel},
        )
    )
    session.flush()
    return route
