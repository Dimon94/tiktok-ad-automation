"""固定 MCP 合同目录读取；没有普通/Smart+ 或跨通道隐式兜底。"""

from collections.abc import Callable
from typing import Any

from app.integrations.tiktok.adapters.ads_read import AdsReadAdapter, FinanceReadState
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.mcp.transport import BoundMCPClient


class McpAdsReadOperations(AdsReadAdapter):
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        route: FrozenTikTokRoute,
        check_account: Callable[[str], str],
        check_accounts: Callable[[tuple[str, ...]], dict[str, str]],
        subject_id: str | None,
        finance: FinanceReadState,
    ):
        super().__init__(
            route=route,
            check_account=check_account,
            check_accounts=check_accounts,
            subject_id=subject_id,
            finance=finance,
        )
        self._client = client

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        return self._client.call(
            operation=operation,
            advertiser_id=arguments.get("advertiser_id"),
            arguments=arguments,
        )
