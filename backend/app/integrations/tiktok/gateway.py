"""任务独占双通道会话；业务代码只取得固定 BC 的 typed gateway。"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import business_api_client as sdk
from redis import Redis
from sqlalchemy import Engine, or_
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.diagnostics import span
from app.core.errors import DomainError
from app.integrations.tiktok.adapters.ads_read import (
    ADS_OPERATIONS,
    FINANCE_OPERATION,
    FinanceReadState,
)
from app.integrations.tiktok.adapters.mcp_ads import McpAdsReadOperations
from app.integrations.tiktok.adapters.mcp_builds import McpBuildOperations
from app.integrations.tiktok.adapters.mcp_management import McpManagementOperations
from app.integrations.tiktok.adapters.mcp_materials import MCPMaterialOperations
from app.integrations.tiktok.adapters.sdk_ads import SdkAdsReadOperations
from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
from app.integrations.tiktok.adapters.sdk_management import SdkManagementOperations
from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
from app.integrations.tiktok.adapters.sdk_music import (
    MUSIC_OPERATION,
    SDKMusicOperations,
)
from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
from app.integrations.tiktok.admission import (
    PROTOCOL_OPERATIONS,
    admit_tiktok_call,
    quota_scope,
)
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session
from app.integrations.tiktok.contracts.accounts import (
    AccountsGateway,
    AuthorizationFacts,
    RuntimeReadContext,
)
from app.integrations.tiktok.contracts.ads import AdsReadOperations, EntityRef
from app.integrations.tiktok.contracts.builds import BuildOperations
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.management import (
    MANAGEMENT_OPERATIONS,
    ManagementOperations,
)
from app.integrations.tiktok.contracts.materials import MaterialOperations
from app.integrations.tiktok.contracts.music import (
    MusicOperations,
    UnsupportedMusicOperations,
)
from app.integrations.tiktok.contracts.report_reader import ReportReader
from app.integrations.tiktok.contracts.scenes import ScenesGateway
from app.integrations.tiktok.group_isolation import (
    FrozenGroupIsolation,
    group_isolation_contract_revision,
    isolation_contracts,
    require_isolation_target,
)
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.integrations.tiktok.mcp.accounts import McpAccountsGateway
from app.integrations.tiktok.mcp.authorization import (
    material_authorization as mcp_material_authorization,
)
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol, load_tool_contracts
from app.integrations.tiktok.mcp.scenes import McpScenesGateway
from app.integrations.tiktok.mcp.transport import open_bound_mcp_client
from app.integrations.tiktok.mcp_auth.refresh import ensure_mcp_credentials
from app.integrations.tiktok.object_coordination import MutationLease, claim_mutation
from app.integrations.tiktok.official.accounts import (
    OfficialAccountsGateway,
    OfficialReadRequests,
)
from app.integrations.tiktok.official.authorization import (
    material_authorization as api_material_authorization,
)
from app.integrations.tiktok.official.scenes import OfficialScenesGateway
from app.integrations.tiktok.request_diagnostics import trace_sdk_request
from app.integrations.tiktok.response_capture import (
    ResponseObserver,
    capture_sdk_response,
)
from app.integrations.tiktok.sdk import official_client
from app.jobs.admission import Workload, admission_policy
from app.modules.accounts.connection_models import (
    ConnectionAuthorization,
    ConnectionToolObservation,
)
from app.modules.accounts.models import AdvertiserAccount, TikTokConnection
from app.modules.accounts.routing import Capability, verify_read_routes, verify_route

# 按精确操作登记所需权限；不得按前缀把未来写操作默认为 read。
_OPERATION_CAPABILITIES: dict[str, Capability] = {
    **dict.fromkeys(PROTOCOL_OPERATIONS, "read"),
    **dict.fromkeys(ADS_OPERATIONS.values(), "read"),
    FINANCE_OPERATION: "read",
    MUSIC_OPERATION: "read",
    "accounts.authorization_facts": "read",
    "accounts.list_bcs": "read",
    "accounts.list_bc_assets": "read",
    "accounts.list_bc_members": "read",
    "accounts.list_asset_members": "read",
    "accounts.list_authorized_advertisers": "read",
    "accounts.get_advertisers": "read",
    "scene.list_identities": "read",
    "scene.get_identity_video": "read",
    "scene.list_minis": "read",
    "scene.recommend_ctas": "read",
    "scene.list_regions": "read",
    "scene.check_vbo": "read",
    "materials.get_videos": "read",
    "materials.search_videos": "read",
    "materials.get_suggested_covers": "read",
    "materials.get_images": "read",
    "materials.search_images": "read",
    "materials.upload_video_url": "upload",
    "materials.share_assets": "upload",
    "materials.upload_video_file": "upload",
    "materials.upload_image_url": "upload",
    "build.get_campaigns": "read",
    "build.get_adgroups": "read",
    "scene.list_budget_adgroups": "read",
    "build.get_ads": "read",
    "build.get_cta_portfolio": "read",
    "build.get_regular_adgroups": "read",
    "build.create_campaign": "build",
    "build.create_adgroup": "build",
    "build.create_ad": "build",
    "build.create_cta_portfolio": "build",
    "build.disable_adgroup": "build",
    "build.list_optimizer_rules": "read",
    # 管理写操作逐一登记；账户写权仍由 verify_route 按搭建权限核验。
    **dict.fromkeys(
        (f"management.{name}" for name in MANAGEMENT_OPERATIONS), "ads_manage"
    ),
    # 报表端点按独立操作登记，确保每个同步页、异步任务状态和下载请求
    # 都重新通过同一冻结路由与共享额度门禁。
    "reports.integrated": "read",
    "reports.bc": "read",
    "reports.material_breakdown": "read",
    "reports.material_overview": "read",
}
_DIRECTORY_OPERATIONS = frozenset(
    operation
    for operation in _OPERATION_CAPABILITIES
    if operation.startswith("accounts.")
)


@dataclass(frozen=True, repr=False)
class TikTokGateway:
    accounts: AccountsGateway
    scenes: ScenesGateway
    materials: MaterialOperations
    builds: BuildOperations
    ads: AdsReadOperations
    management: ManagementOperations
    report_reader: ReportReader | None = None
    music: MusicOperations = field(default_factory=UnsupportedMusicOperations)


def _capability(advertiser_id: str | None, operation: str) -> Capability:
    capability = _OPERATION_CAPABILITIES.get(operation)
    if capability is None:
        raise DomainError("gateway_operation_forbidden", "本次会话不支持该操作")
    if (
        advertiser_id is None
        and operation
        not in PROTOCOL_OPERATIONS
        | _DIRECTORY_OPERATIONS
        | {FINANCE_OPERATION, "reports.bc"}
    ):
        raise DomainError("account_required", "该操作必须指定广告账户")
    if (
        advertiser_id is not None
        and operation
        in PROTOCOL_OPERATIONS
        | _DIRECTORY_OPERATIONS
        | {FINANCE_OPERATION, "reports.bc"}
    ):
        raise DomainError("gateway_operation_forbidden", "目录操作归属参数无效")
    return capability


def _authorization(
    session: Session, route: FrozenTikTokRoute
) -> ConnectionAuthorization | None:
    return session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.tenant_id == route.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
    ).one_or_none()


def _facts(
    authorization: ConnectionAuthorization | None, route: FrozenTikTokRoute
) -> AuthorizationFacts:
    # UNKNOWN 的坐标只是固定服务地址，不是已验证 OAuth 声明；epoch 保证其不构成新鲜授权。
    if route.channel == "OFFICIAL_MCP":
        profile = load_mcp_protocol()
        issuer, resource = profile.issuer, profile.resource
    else:
        issuer = "https://business-api.tiktok.com"
        resource = "https://business-api.tiktok.com/open_api/v1.3"
    assert issuer is not None and resource is not None
    try:
        return AuthorizationFacts(
            subject_id=authorization.upstream_subject if authorization else None,
            grant_id=authorization.upstream_grant_id if authorization else None,
            issuer=authorization.issuer or issuer if authorization else issuer,
            resource=authorization.resource or resource if authorization else resource,
            scopes=tuple(authorization.scopes) if authorization else (),
            read_authorized=authorization.permission_summary.get("read_authorized")
            if authorization
            else None,
            upload_authorized=authorization.permission_summary.get("upload_authorized")
            if authorization
            else None,
            build_authorized=authorization.permission_summary.get("build_authorized")
            if authorization
            else None,
            management_operations=frozenset(
                authorization.permission_summary.get("management_operations", ())
            )
            if authorization
            else frozenset(),
            evidence_source=authorization.source if authorization else "UNKNOWN",
            observed_at=authorization.verified_at or datetime.fromtimestamp(0, UTC)
            if authorization
            else datetime.fromtimestamp(0, UTC),
        )
    except ValueError, TypeError:
        raise DomainError("gateway_authorization_invalid", "当前授权摘要无效") from None


@contextmanager
def open_tiktok_gateway(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    route: FrozenTikTokRoute,
    task_deadline: datetime,
    workload: Workload = "live",
    before_request: Callable[[], None] | None = None,
    response_observer: ResponseObserver | None = None,
    group_isolation: FrozenGroupIsolation | None = None,
    before_isolation_write: Callable[[], None] | None = None,
) -> Iterator[TikTokGateway]:
    if group_isolation is not None and (
        group_isolation.route != route
        or group_isolation.contract_revision != group_isolation_contract_revision()
    ):
        raise DomainError(
            "group_isolation_scope_mismatch", "冻结隔离路由或补充合同不匹配"
        )
    # API 不刷新凭据，直接在下方读取凭据的事务核验；MCP 刷新前后均核验原路由。
    if route.channel == "OFFICIAL_MCP":
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=None,
                capability="read",
            )
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=route.connection_id,
            task_deadline=task_deadline,
        )
    else:
        settings.require_tiktok_app()
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        verify_route(
            session, context=context, route=route, advertiser_id=None, capability="read"
        )
        connection = session.get(TikTokConnection, route.connection_id)
        assert connection is not None
        material = decrypt_credentials(
            tenant_id=context.tenant_id,
            ciphertext=connection.credential_ciphertext or "",
        )
        token = material.get("access_token")
        if not token or any(character in token for character in ("\r", "\n")):
            raise DomainError("credential_invalid", "凭据缺少访问令牌")
        credential_revision = connection.credential_revision
        authorization = _authorization(session, route)
        facts = _facts(authorization, route)
        # 重新观测只携带当前凭据的实际授权声明，不能替换尚未完整核实的持久事实。
        observation_facts = (
            mcp_material_authorization
            if route.channel == "OFFICIAL_MCP"
            else api_material_authorization
        )(material, observed_at=datetime.now(UTC))
        observed: dict[str, Any] = {}
        if route.channel == "OFFICIAL_MCP":
            profile = load_mcp_protocol()
            if route.adapter_contract_revision != profile.schema_manifest_sha256:
                raise DomainError("route_contract_changed", "当前适配契约已更新")
            if (material.get("issuer"), material.get("resource")) != (
                profile.issuer,
                profile.resource,
            ):
                raise DomainError("gateway_authorization_invalid", "MCP 授权服务不匹配")
            current_candidate = (
                authorization.mcp_authorization_attempt_id if authorization else None
            )
            observation = session.exec(
                select(ConnectionToolObservation)
                .where(
                    ConnectionToolObservation.tenant_id == route.tenant_id,
                    ConnectionToolObservation.connection_id == route.connection_id,
                    ConnectionToolObservation.expected_contract_revision
                    == route.adapter_contract_revision,
                    ConnectionToolObservation.pagination_complete == True,  # noqa: E712
                    or_(
                        col(ConnectionToolObservation.candidate_attempt_id).is_(None),
                        col(ConnectionToolObservation.candidate_attempt_id)
                        == current_candidate,
                    ),
                )
                .order_by(col(ConnectionToolObservation.observed_at).desc())
            ).first()
            if observation is None:
                raise DomainError(
                    "gateway_tool_observation_required",
                    "请先完成当前连接的工具合同核验",
                )
            observed = json.loads(json.dumps(observation.tool_schemas))

    finance = FinanceReadState()
    isolation_lease: MutationLease | None = None

    def check_account(advertiser_id: str) -> str:
        # 本地权限读独立事务；既用于未知财务证据，也用于完整分页后再次过滤权限。
        if group_isolation is not None:
            require_isolation_target(group_isolation, advertiser_id=advertiser_id)
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability="read",
            )
            account = session.get(AdvertiserAccount, (route.tenant_id, advertiser_id))
            assert account is not None
            return account.currency

    def check_accounts(advertiser_ids: tuple[str, ...]) -> dict[str, str]:
        # 同一返回页批量核验，不跨HTTP或事务缓存授权；隔离目标仍逐项限制。
        if group_isolation is not None:
            for advertiser_id in advertiser_ids:
                require_isolation_target(group_isolation, advertiser_id=advertiser_id)
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            return verify_read_routes(
                session, context=context, route=route, advertiser_ids=advertiser_ids
            )

    # 此后不保留 Session；portal 回调各自拥有独立、限时的数据库事务。
    def authorize(
        advertiser_id: str | None,
        operation: str,
        entity_kind: str | None = None,
        *,
        check_workflow: bool = True,
    ) -> None:
        if check_workflow and before_request is not None:
            before_request()
        capability = _capability(advertiser_id, operation)
        management_operation = (
            operation.removeprefix("management.")
            if operation.startswith("management.")
            else None
        )
        if operation in {"build.disable_adgroup", "build.list_optimizer_rules"}:
            assert advertiser_id is not None
            require_isolation_target(group_isolation, advertiser_id=advertiser_id)
        if group_isolation is not None:
            if advertiser_id is not None:
                require_isolation_target(group_isolation, advertiser_id=advertiser_id)
            # 专用隔离会话不借机创建或修改其他对象；纠正创建使用独立冻结意图。
            if capability != "read" and operation != "build.disable_adgroup":
                raise DomainError(
                    "group_isolation_operation_forbidden", "隔离会话只允许停用原组"
                )
        if route.channel == "OFFICIAL_MCP":
            with bounded_session(
                database_engine, task_deadline=task_deadline
            ) as session:
                verify_route(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=advertiser_id,
                    capability=capability,
                    operation=management_operation,
                    entity_kind=entity_kind,
                )
            ensure_mcp_credentials(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                connection_id=route.connection_id,
                task_deadline=task_deadline,
            )
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability=capability,
                operation=management_operation,
                entity_kind=entity_kind,
            )
            if operation == FINANCE_OPERATION:
                current = _authorization(session, route)
                subject = current.upstream_subject if current else None
                target = finance.require(subject)
                verify_route(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=target,
                    capability="read",
                )
            row = session.get(TikTokConnection, route.connection_id)
            if row is None or row.credential_revision != credential_revision:
                # 当前会话内存 header 已冻结；发送前退出后可按同一 route 新建会话，不重放已发调用。
                raise DomainError(
                    "gateway_credentials_changed",
                    "访问凭据已更新，请重新打开调用会话",
                    retryable=True,
                )
        if operation == "build.disable_adgroup":
            if before_isolation_write is None:
                raise DomainError(
                    "group_isolation_authority_required", "停用前必须核查持久纠正账本"
                )
            # 与授权和准入一样在每个物理发送前重复执行；调用方必须幂等核对
            # ledger_id、冻结 body/digest/route、独占 claim 和截止时间，不得仅传空回调。
            before_isolation_write()
            isolation_guard()

    @contextmanager
    def admit(
        advertiser_id: str | None, operation: str, physical_endpoint: str | None = None
    ) -> Iterator[None]:
        _capability(advertiser_id, operation)
        policy_operation = (
            "reports.integrated"
            if route.channel == "OFFICIAL_API" and operation == "reports.bc"
            else operation
        )
        if route.channel == "OFFICIAL_API" and physical_endpoint is None:
            from app.integrations.tiktok.platform_quotas import sdk_endpoint

            try:
                physical_endpoint = sdk_endpoint(operation)
            except ValueError:
                # 未登记SDK路径不可套用其他接口的额度；旧配置仅用于尚未迁移的部署。
                if "sdk_level" in settings.TIKTOK_CALL_POLICIES:
                    raise DomainError(
                        "admission_policy_invalid", "SDK物理接口额度尚未登记"
                    ) from None
        policy = admission_policy(
            policy_operation, channel=route.channel, physical_endpoint=physical_endpoint
        )
        remaining_ms = (task_deadline - datetime.now(UTC)).total_seconds() * 1000
        if remaining_ms <= 0 or policy.lease_ms <= remaining_ms + 1000:
            raise DomainError(
                "admission_policy_invalid", "调用租约必须覆盖本次任务期限"
            )
        scope = quota_scope(
            channel=route.channel,
            app_id=settings.TIKTOK_APP_ID if route.channel == "OFFICIAL_API" else None,
            verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
        )
        with bounded_redis(redis_client, task_deadline=task_deadline) as bounded:
            with admit_tiktok_call(
                bounded,
                route=route,
                advertiser_id=advertiser_id,
                operation=operation,
                scope=scope,
                policy=policy,
                workload=workload,
                physical_endpoint=physical_endpoint,
            ):
                yield

    # share_arguments逐账户核验当前授权；完整业务围栏由物理请求入口在准入前后
    # 各执行一次，不随共享目标数反复扫描整个批次，也不跨请求缓存授权。
    @contextmanager
    def request_scope(
        advertiser_id: str | None, operation: str, deadline: datetime
    ) -> Iterator[None]:
        if deadline != task_deadline:
            raise DomainError("read_deadline_invalid", "调用期限与任务不一致")
        # 普通目录/素材子页与报表同样发送真实HTTP，不能因缺埋点误归为纯本地耗时。
        diagnostic = operation.startswith(("materials.", "build.", "ads.", "reports."))
        fields = {
            "tenant_id": str(context.tenant_id),
            "bc_id": route.bc_id,
            "advertiser_id": advertiser_id,
            "operation": operation,
        }
        with span("request_scope", **fields) if diagnostic else nullcontext():
            authorize(advertiser_id, operation)
            with admit(advertiser_id, operation):
                authorize(advertiser_id, operation)
                from app.integrations.tiktok.attempt_observer import (
                    current_attempt_observer,
                    physical_attempt_scope,
                )
                from app.integrations.tiktok.platform_quotas import sdk_endpoint

                with (
                    physical_attempt_scope(official, sdk_endpoint(operation))
                    if current_attempt_observer() is not None
                    else nullcontext(),
                    trace_sdk_request(official, **fields)
                    if diagnostic
                    else nullcontext(),
                    capture_sdk_response(
                        official,
                        operation=operation,
                        advertiser_id=advertiser_id,
                        observer=response_observer,
                    ),
                ):
                    yield

    def check_report_request(operation: str, arguments: dict[str, Any]) -> None:
        # 合并账户读取必须逐一核验；BC 报表只能读取本次冻结绑定的 BC。
        if operation == "reports.bc":
            if arguments.get("bc_id") != route.bc_id:
                raise DomainError("report_scope_forbidden", "报表 BC 与冻结路由不一致")
        for advertiser in arguments.get("advertiser_ids", []):
            authorize(advertiser, operation)

    @contextmanager
    def management_scope(
        advertiser_id: str,
        operation: str,
        entity_kind: str,
        deadline: datetime,
        physical_endpoint: str,
    ) -> Iterator[None]:
        if deadline != task_deadline:
            raise DomainError("read_deadline_invalid", "调用期限与任务不一致")
        full_operation = f"management.{operation}"
        # SDK adapter在实际HTTP发送前调用before_send完整围栏；此处只重验授权，
        # 不能在一次物理请求里重复扫描整批五次、耗尽连接池与请求期限。
        authorize(advertiser_id, full_operation, entity_kind, check_workflow=False)
        with admit(advertiser_id, full_operation, physical_endpoint):
            authorize(advertiser_id, full_operation, entity_kind, check_workflow=False)
            yield

    def isolation_guard() -> None:
        """Fence the exact instant before the legacy disable request."""
        nonlocal isolation_lease
        if group_isolation is None or before_isolation_write is None:
            raise DomainError(
                "group_isolation_authority_required", "停用前必须核查持久纠正账本"
            )
        # Legacy group isolation and C4 management use the same remote object
        # domain. Claim lazily because SDK adapters invoke this hook immediately
        # before entering their request scope.
        if isolation_lease is None:
            ref = EntityRef(
                route.tenant_id,
                group_isolation.advertiser_id,
                "adgroup",
                group_isolation.adgroup_id,
            )
            with bounded_session(
                database_engine, task_deadline=task_deadline
            ) as lock_session:
                isolation_lease = claim_mutation(
                    lock_session,
                    (ref,),
                    group_isolation.ledger_id,
                    redis_client=redis_client,
                )
        if isolation_lease is None:
            raise DomainError(
                "group_isolation_authority_required", "停用前未领取远端对象租约"
            )
        isolation_lease.fence(redis_client)

    read_context = RuntimeReadContext(route.bc_id)
    try:
        if route.channel == "OFFICIAL_MCP":
            contracts = {
                contract.operation: contract
                for contract in load_tool_contracts()
                if contract.operation in _OPERATION_CAPABILITIES
            }
            if group_isolation is not None:
                contracts.update(
                    {contract.operation: contract for contract in isolation_contracts()}
                )
            with open_bound_mcp_client(
                token=token,
                task_deadline=task_deadline,
                authorize=authorize,
                admit=admit,
                contracts=contracts,
                observed_tools=observed,
                response_observer=response_observer,
                group_isolation=group_isolation,
            ) as client:
                yield TikTokGateway(
                    ads=McpAdsReadOperations(
                        client,
                        route=route,
                        check_account=check_account,
                        check_accounts=check_accounts,
                        subject_id=facts.subject_id,
                        finance=finance,
                    ),
                    accounts=McpAccountsGateway(
                        client,
                        context=read_context,
                        authorization=facts,
                        observation_authorization=observation_facts,
                    ),
                    scenes=McpScenesGateway(client, context=read_context),
                    builds=McpBuildOperations(
                        client,
                        isolation=group_isolation,
                        before_disable=isolation_guard,
                    ),
                    management=McpManagementOperations(
                        client,
                        capability_check=lambda advertiser, operation, kind: authorize(
                            advertiser, f"management.{operation}", kind
                        ),
                    ),
                    music=UnsupportedMusicOperations(),
                    materials=MCPMaterialOperations(
                        client,
                        share_authorize=lambda account: authorize(
                            account, "materials.share_assets", check_workflow=False
                        ),
                        preview_allowed_hosts=settings.MATERIAL_REMOTE_MEDIA_HOSTS,
                        upload_policy=material_upload_policy(
                            channel=route.channel,
                            adapter_contract_revision=route.adapter_contract_revision,
                        ),
                    ),
                )
        else:
            with official_client(access_token=token) as official:
                yield TikTokGateway(
                    ads=SdkAdsReadOperations(
                        official,
                        route=route,
                        check_account=check_account,
                        check_accounts=check_accounts,
                        subject_id=facts.subject_id,
                        finance=finance,
                        request_scope=request_scope,
                        deadline=task_deadline,
                    ),
                    report_reader=SdkReportReader(
                        send=_sdk_report_send(
                            official,
                            request_scope,
                            task_deadline,
                            request_check=check_report_request,
                        )
                    ),
                    accounts=OfficialAccountsGateway(
                        official,
                        context=read_context,
                        authorization=facts,
                        observation_authorization=observation_facts,
                        app_id=settings.TIKTOK_APP_ID,
                        secret=settings.TIKTOK_APP_SECRET,
                        request_scope=request_scope,
                        deadline=task_deadline,
                    ),
                    builds=ApiBuildOperations(
                        official,
                        request_scope=request_scope,
                        deadline=task_deadline,
                        isolation=group_isolation,
                        before_disable=isolation_guard,
                    ),
                    management=SdkManagementOperations(
                        official,
                        request_scope=request_scope,
                        capability_check=lambda advertiser, operation, kind: authorize(
                            advertiser,
                            f"management.{operation}",
                            kind,
                            check_workflow=False,
                        ),
                        management_scope=management_scope,
                        before_send=before_request,
                        deadline=task_deadline,
                    ),
                    music=SDKMusicOperations(
                        official,
                        route=route,
                        request_scope=request_scope,
                        deadline=task_deadline,
                    ),
                    materials=SDKMaterialOperations(
                        official,
                        share_authorize=lambda account: authorize(
                            account, "materials.share_assets", check_workflow=False
                        ),
                        request_scope=request_scope,
                        deadline=task_deadline,
                        preview_allowed_hosts=settings.MATERIAL_REMOTE_MEDIA_HOSTS,
                        # 数字 scope 只由官方授权边界解释；封面适配器保留各操作原有叶权限。
                        api_scope_ids=frozenset(
                            int(scope) for scope in observation_facts.scopes
                        ),
                    ),
                    scenes=OfficialScenesGateway(
                        official,
                        context=read_context,
                        request_scope=request_scope,
                        deadline=task_deadline,
                    ),
                )
    finally:
        if isolation_lease is not None:
            isolation_lease.release(redis_client)
        material.clear()
        token = None


def _sdk_report_send(client, request_scope, deadline, *, request_check=None):
    """每个物理请求独立准入；直接使用已核对支持批量参数的官方生成 SDK。"""
    requests = OfficialReadRequests(
        client, request_scope=request_scope, deadline=deadline
    )
    api = sdk.ReportingApi(client)
    token = client.default_headers["Access-Token"]

    def send(operation, advertiser_id, arguments):
        if operation not in {
            "reports.integrated",
            "reports.bc",
            "reports.material_breakdown",
            "reports.material_overview",
            "materials.search_videos",
            "materials.get_videos",
            "scene.get_identity_video",
        }:
            raise DomainError("report_operation_invalid", "报表操作未登记")

        def call(timeout):
            # 额度准入后再查全体账户，避免等待期间撤权仍发出合并请求。
            if request_check is not None:
                request_check(operation, arguments)
            # 仅在准入与冻结授权核验通过后计数；平台返回限流仍是一次已发送请求。
            sender.request_count += 1
            args = dict(arguments)
            if operation == "materials.get_videos":
                return sdk.FileApi(client).ad_video_info(
                    args.pop("advertiser_id"),
                    args.pop("video_ids"),
                    token,
                    **args,
                    _request_timeout=timeout,
                )
            if operation == "scene.get_identity_video":
                return sdk.IdentityApi(client).identity_video_info(
                    access_token=token,
                    **args,
                    _request_timeout=timeout,
                )
            if operation == "materials.search_videos":
                return sdk.FileApi(client).ad_video_search(
                    args.pop("advertiser_id"), token, **args, _request_timeout=timeout
                )
            if operation in {"reports.integrated", "reports.bc"}:
                return api.report_integrated_get(
                    args.pop("report_type"), token, **args, _request_timeout=timeout
                )
            if operation == "reports.material_overview":
                # 概览只用于“主素材 → 广告”对照，不取日期参数（全量历史）。
                return api.smart_plus_material_report_overview(
                    args.pop("advertiser_id"),
                    args.pop("dimensions"),
                    token,
                    **args,
                    _request_timeout=timeout,
                )
            return api.smart_plus_material_report_breakdown(
                args.pop("advertiser_id"),
                args.pop("dimensions"),
                args.pop("start_date"),
                args.pop("end_date"),
                token,
                **args,
                _request_timeout=timeout,
            )

        return requests.invoke(
            operation,
            advertiser_id,
            call,
            exact_numbers=operation.startswith("reports."),
        ).data

    class Sender:
        request_count = 0

        def __call__(self, operation, advertiser_id, arguments):
            return send(operation, advertiser_id, arguments)

    sender = Sender()
    return sender
