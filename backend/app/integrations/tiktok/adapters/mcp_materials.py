"""MCP 素材操作：官方代码客户端拥有准入；视频写入保留独立能力门禁。"""

from collections.abc import Callable
from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts import materials as contracts
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient
from app.modules.materials import cover_sdk
from app.modules.materials.channel_policy import (
    MaterialUploadPolicy,
    require_url_upload,
)
from app.modules.materials.sdk_assets import (
    MaterialReadAdapter,
    validate_video_upload,
    video_upload_receipt,
)
from app.modules.materials.sharing import share_arguments, share_receipt


class MCPMaterialOperations(MaterialReadAdapter):
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        preview_allowed_hosts: frozenset[str] = frozenset(),
        share_authorize: Callable[[str], None] | None = None,
        upload_policy: MaterialUploadPolicy | None = None,
    ):
        super().__init__(preview_allowed_hosts=preview_allowed_hosts)
        self._client = client
        self._share_authorize = share_authorize
        self._upload_policy = upload_policy or MaterialUploadPolicy(None)

    def _call(
        self,
        operation: str,
        advertiser_id: str,
        arguments: dict[str, Any],
        budget: contracts.RemoteCallBudget,
    ) -> McpBusinessResponse:
        return self._client.call(
            operation=operation,
            advertiser_id=advertiser_id,
            arguments=arguments,
            deadline=budget.deadline,
        )

    def share_assets(
        self, request: contracts.AssetShare, *, budget: contracts.RemoteCallBudget
    ) -> contracts.AssetShareReceipt:
        try:
            arguments = share_arguments(request, authorize=self._share_authorize)
            budget.timeout(upload=True)
        except DomainError as error:
            raise RemoteCallError(
                error.code, effect="NOT_SENT", evidence=CallEvidence()
            ) from None
        response = self._client.call(
            operation="materials.share_assets",
            advertiser_id=request.advertiser_id,
            arguments=arguments,
            deadline=budget.deadline,
        )
        return share_receipt(response, request)

    def upload_video_id(
        self, request: contracts.VIDVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )

    def upload_video_url(
        self, request: contracts.URLVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        try:
            require_url_upload(self._upload_policy, byte_size=request.byte_size)
            validate_video_upload(request)
            budget.timeout(upload=True)
        except DomainError as error:
            raise RemoteCallError(
                error.code, effect="NOT_SENT", evidence=CallEvidence()
            ) from None
        # MCP 合同不接收 video_signature；本地摘要用于原件身份及未知结果恢复。
        # 正常业务成功回执中的实际 VID 直接确认上传，不强制再查询一次。
        response = self._client.call(
            operation="materials.upload_video_url",
            advertiser_id=request.advertiser_id,
            arguments={
                "advertiser_id": request.advertiser_id,
                "file_name": request.file_name,
                "upload_type": "UPLOAD_BY_URL",
                "video_url": request.url,
                "auto_fix_enabled": False,
                "auto_bind_enabled": False,
            },
            deadline=budget.deadline,
        )
        return video_upload_receipt(
            response, advertiser_id=request.advertiser_id, channel="OFFICIAL_MCP"
        )

    def upload_video_file(
        self, request: contracts.FileVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )

    def read_image_preview(
        self, *, advertiser_id: str, image_id: str, budget: contracts.RemoteCallBudget
    ) -> contracts.ImagePreview:
        # 退役通道不新增图片预览能力，也不发送任何替代请求。
        raise DomainError("material_channel_unverified", "当前通道不支持图片预览")

    def upload_image_url(
        self, request: contracts.URLImageUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.ImageReceipt:
        try:
            cover_sdk.validate_image_upload(request)
            budget.timeout(upload=True)
        except DomainError as error:
            raise RemoteCallError(
                error.code, effect="NOT_SENT", evidence=CallEvidence()
            ) from None
        response = self._client.call(
            operation="materials.upload_image_url",
            advertiser_id=request.advertiser_id,
            arguments={
                "advertiser_id": request.advertiser_id,
                "upload_type": "UPLOAD_BY_URL",
                "image_url": request.url,
                "file_name": request.file_name,
            },
            deadline=budget.deadline,
        )
        return cover_sdk.image_receipt(response, advertiser_id=request.advertiser_id)
