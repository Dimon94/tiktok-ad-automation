"""固定官方 SDK 目录端点；每次发送复用工厂的期限、授权与额度门禁。"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import business_api_client as sdk

from app.integrations.tiktok.adapters.ads_read import (
    ADS_OPERATIONS,
    FINANCE_OPERATION,
    AdsReadAdapter,
    FinanceReadState,
)
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.official.accounts import OfficialReadRequests, RequestScope


class SdkAdsReadOperations(AdsReadAdapter):
    def __init__(
        self,
        client: Any,
        *,
        route: FrozenTikTokRoute,
        check_account: Callable[[str], str],
        check_accounts: Callable[[tuple[str, ...]], dict[str, str]],
        subject_id: str | None,
        finance: FinanceReadState,
        request_scope: RequestScope,
        deadline: datetime,
    ):
        super().__init__(
            route=route,
            check_account=check_account,
            check_accounts=check_accounts,
            subject_id=subject_id,
            finance=finance,
        )
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        client = self._requests.client
        token = client.default_headers["Access-Token"]
        methods: dict[str, tuple[Any, str]] = {
            ADS_OPERATIONS[kind, smart]: (
                api,
                f"{'smart_plus_' if smart else ''}{kind}_get",
            )
            for kind, api in (
                ("campaign", sdk.CampaignCreationApi),
                ("adgroup", sdk.AdgroupApi),
                ("ad", sdk.AdApi),
            )
            for smart in (False, True)
        }
        methods["accounts.list_bc_members"] = (sdk.BCApi, "bc_member_get")
        methods[FINANCE_OPERATION] = (sdk.BCApi, "advertiser_balance_get")
        api, method = methods[operation]
        if operation == FINANCE_OPERATION:
            # 固定 SDK 缺少 fields；复用其 ApiClient 发送文档指定的只读字段扩展。
            def send_balance(timeout: tuple[float, float]) -> object:
                query = [
                    (
                        key,
                        json.dumps(value) if isinstance(value, (dict, list)) else value,
                    )
                    for key, value in arguments.items()
                ]
                return client.call_api(
                    "/open_api/v1.3/advertiser/balance/get/",
                    "GET",
                    {},
                    query,
                    {"Access-Token": token},
                    response_type="InlineResponse200",
                    auth_settings=[],
                    _return_http_data_only=True,
                    _request_timeout=timeout,
                )

            return self._requests.invoke(
                operation, None, send_balance, exact_numbers=True
            )
        return self._requests.invoke(
            operation,
            arguments.get("advertiser_id"),
            lambda timeout: getattr(api(client), method)(
                access_token=token, **arguments, _request_timeout=timeout
            ),
            exact_numbers=True,
        )
