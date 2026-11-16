"""双通道素材唯一 DTO/Protocol；本地预算不承诺撤销已发送的远端请求。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence


@dataclass(frozen=True)
class RemoteCallBudget:
    """Actual worker deadline and admitted endpoint policy, supplied by caller.

    Socket timeouts cannot enforce a whole-task deadline. The caller must run
    in a process with this hard limit and acquire the corresponding Redis lease.
    """

    deadline: datetime
    hard_limit_seconds: int
    lease_ms: int

    def timeout(self, *, upload: bool) -> tuple[float, float]:
        if (
            type(self.hard_limit_seconds) is not int
            or self.hard_limit_seconds <= 5
            or type(self.lease_ms) is not int
            or self.lease_ms <= (self.hard_limit_seconds + 5) * 1000
            or not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise DomainError("admission_policy_invalid", "素材调用执行期限或租约无效")
        available = (
            min(
                (self.deadline - datetime.now(UTC)).total_seconds(),
                self.hard_limit_seconds,
            )
            - 5
        )
        if available <= 0:
            raise DomainError("material_deadline", "素材处理已到达本次期限")
        connect = min(10 if upload else 5, available / 2)
        return connect, min(300 if upload else 30, available - connect)


@dataclass(frozen=True)
class SourcePreview:
    """Fresh account-scoped evidence; URL stays only in the current call's memory."""

    advertiser_id: str
    video_id: str
    mid: str | None
    md5: str = field(repr=False)
    url: str = field(repr=False)
    width: int
    height: int
    size: int
    duration: float
    format: str
    displayable: bool = True
    evidence: CallEvidence = field(default_factory=CallEvidence)


@dataclass(frozen=True)
class ImagePreview:
    """Fresh account-scoped image_url; never persist in mappings or identity logs."""

    advertiser_id: str
    image_id: str
    image_url: str = field(repr=False)
    width: int
    height: int
    evidence: CallEvidence = field(default_factory=CallEvidence)


@dataclass(frozen=True)
class VideoCover:
    url: str | None = field(repr=False)
    width: int
    height: int
    evidence: CallEvidence = field(default_factory=CallEvidence)


@dataclass(frozen=True)
class ImageReceipt:
    image_id: str
    signature: str | None = field(repr=False)
    evidence: CallEvidence = field(default_factory=CallEvidence)
    # 完整上传回执可直接核实封面；尾部字段保持既有位置参数合同。
    width: int | None = None
    height: int | None = None
    displayable: bool | None = None
    # 源图片原生共享只使用平台实际返回的素材 MID；缺失时保留已知图片 ID 回查。
    mid: str | None = None


@dataclass(frozen=True)
class VideoRecord:
    advertiser_id: str
    video_id: str
    mid: str | None
    md5: str | None = field(repr=False)
    file_name: str | None
    width: int | None
    height: int | None
    size: int | None
    duration: float | None
    format: str | None
    displayable: bool | None
    evidence: CallEvidence


@dataclass(frozen=True)
class ImageRecord:
    advertiser_id: str
    image_id: str
    signature: str | None = field(repr=False)
    file_name: str | None
    width: int | None
    height: int | None
    displayable: bool | None
    evidence: CallEvidence
    mid: str | None = None
    # 主图资格只接受平台回读事实；视频封面/displayable 不能推导轮播可用。
    is_carousel_usable: bool | None = None


@dataclass(frozen=True)
class MaterialPage[T]:
    rows: tuple[T, ...]
    page: int
    page_size: int
    total_pages: int
    total_number: int | None
    evidence: CallEvidence

    def __post_init__(self) -> None:
        if (
            type(self.rows) is not tuple
            or type(self.page) is not int
            or type(self.page_size) is not int
            or not 1 <= self.page_size <= 100
            or type(self.total_pages) is not int
            or not 0 <= self.total_pages <= 1000
            or not 1 <= self.page <= max(1, self.total_pages)
            or len(self.rows) > self.page_size
            or (self.total_pages == 0 and self.rows)
            or (self.page < self.total_pages and not self.rows)
        ):
            raise ValueError("invalid material page")
        if self.total_number is not None and (
            type(self.total_number) is not int
            or self.total_number < 0
            or self.total_number < len(self.rows)
            or max(1, (self.total_number + self.page_size - 1) // self.page_size)
            != max(1, self.total_pages)
            or len(self.rows)
            != min(
                self.page_size,
                max(0, self.total_number - (self.page - 1) * self.page_size),
            )
        ):
            raise ValueError("inconsistent material count")


@dataclass(frozen=True)
class VideoReceipt:
    video_id: str
    mid: str | None
    evidence: CallEvidence


@dataclass(frozen=True)
class VIDVideoUpload:
    advertiser_id: str
    video_id: str
    file_name: str


@dataclass(frozen=True)
class URLVideoUpload:
    advertiser_id: str
    url: str = field(repr=False)
    file_name: str
    expected_md5: str = field(repr=False)
    byte_size: int


@dataclass(frozen=True)
class FileVideoUpload:
    advertiser_id: str
    local_path: str = field(repr=False)
    file_name: str
    expected_md5: str = field(repr=False)
    byte_size: int


@dataclass(frozen=True)
class URLImageUpload:
    advertiser_id: str
    url: str = field(repr=False)
    file_name: str


@dataclass(frozen=True)
class AssetShare:
    advertiser_id: str
    material_ids: tuple[str, ...]
    shared_advertiser_ids: tuple[str, ...]
    asset_type: str = "VIDEO"


@dataclass(frozen=True)
class AssetShareReceipt:
    evidence: CallEvidence
    failed_infos: dict[str, tuple[str, ...]]


class MaterialOperations(Protocol):
    def read_image_preview(
        self, *, advertiser_id: str, image_id: str, budget: RemoteCallBudget
    ) -> ImagePreview: ...

    def read_videos(
        self,
        *,
        advertiser_id: str,
        video_ids: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> tuple[VideoRecord, ...]: ...

    def read_images(
        self,
        *,
        advertiser_id: str,
        image_ids: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> tuple[ImageRecord, ...]: ...

    def share_assets(
        self, request: AssetShare, *, budget: RemoteCallBudget
    ) -> AssetShareReceipt: ...

    def read_video(
        self, *, advertiser_id: str, video_id: str, budget: RemoteCallBudget
    ) -> VideoRecord | None: ...
    def read_source_preview(
        self, *, advertiser_id: str, video_id: str, budget: RemoteCallBudget
    ) -> SourcePreview: ...
    def search_videos(
        self,
        *,
        advertiser_id: str,
        page: int,
        material_ids: tuple[str, ...],
        budget: RemoteCallBudget,
        video_name: str | None = None,
    ) -> MaterialPage[VideoRecord]: ...
    def upload_video_id(
        self, request: VIDVideoUpload, *, budget: RemoteCallBudget
    ) -> VideoReceipt: ...
    def upload_video_url(
        self, request: URLVideoUpload, *, budget: RemoteCallBudget
    ) -> VideoReceipt: ...
    def upload_video_file(
        self, request: FileVideoUpload, *, budget: RemoteCallBudget
    ) -> VideoReceipt: ...
    def read_video_cover(
        self, *, advertiser_id: str, video_id: str, md5: str, budget: RemoteCallBudget
    ) -> VideoCover: ...
    def suggest_cover(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        width: int,
        height: int,
        budget: RemoteCallBudget,
    ) -> VideoCover | None: ...
    def upload_image_url(
        self, request: URLImageUpload, *, budget: RemoteCallBudget
    ) -> ImageReceipt: ...
    def read_image(
        self, *, advertiser_id: str, image_id: str, budget: RemoteCallBudget
    ) -> ImageRecord | None: ...
    def search_images(
        self,
        *,
        advertiser_id: str,
        page: int,
        budget: RemoteCallBudget,
        material_ids: tuple[str, ...] = (),
        image_ids: tuple[str, ...] = (),
        page_size: int = 100,
    ) -> MaterialPage[ImageRecord]: ...
