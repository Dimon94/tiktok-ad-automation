"""仅解析已知预览链接；绝不将用户网页当作可抓取的视频 URL。"""

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import DomainError

PREVIEW_HOST = "ad-creative-studio-platform.tiktok-row.net"
VID_PATTERN = re.compile(r"v[a-z0-9]{20,80}")


class ParsedLink(BaseModel):
    line_no: int
    video_id: str
    url: str
    duplicate_of: int | None = None


def parse_links(text: str) -> list[ParsedLink]:
    lines = [
        (i, raw.strip()) for i, raw in enumerate(text.splitlines(), 1) if raw.strip()
    ]
    if (
        not 1 <= len(lines) <= 500
        or len(text) > 250000
        or any(i > 10000 for i, _ in lines)
    ):
        raise DomainError("link_input_invalid", "请填写 1–500 行素材预览链接")
    seen: dict[str, int] = {}
    result = []
    for i, raw in lines:
        try:
            url = urlsplit(raw)
            params = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
            if (
                url.scheme != "https"
                or url.netloc != PREVIEW_HOST
                or url.path != "/preview"
                or url.fragment
                or len(params) != 1
                or params[0][0] != "vid"
                or not VID_PATTERN.fullmatch(params[0][1])
                or any(c.isspace() or ord(c) < 32 for c in raw)
            ):
                raise ValueError
            vid = params[0][1]
        except ValueError:
            raise DomainError(
                "link_input_invalid", f"第 {i} 行不是支持的 TikTok 素材预览链接"
            ) from None
        result.append(
            ParsedLink(
                line_no=i,
                video_id=vid,
                url=f"https://{PREVIEW_HOST}/preview?vid={vid}",
                duplicate_of=seen.get(vid),
            )
        )
        seen.setdefault(vid, i)
    return result


class LinkBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    bc_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=250000)
    advertiser_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("advertiser_ids")
    @classmethod
    def valid_accounts(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[0-9]{1,30}", v) for v in values
        ):
            raise ValueError("账户 ID 必须为不重复的数字")
        return values


class LinkItemPublic(BaseModel):
    id: UUID
    batch_id: UUID
    bc_id: str
    duplicate_line_no: int | None = None
    line_no: int
    source_url: str
    source_video_id: str
    advertiser_id: str
    requested_file_name: str
    actual_file_name: str | None
    status: str
    video_id: str | None
    mid: str | None
    material_id: UUID | None
    duplicate_of: UUID | None
    error_code: str | None
    error_message: str | None
    request_id: str | None
    can_retry: bool
    can_recheck: bool
    updated_at: datetime


class LinkBatchPublic(BaseModel):
    id: UUID
    bc_id: str
    naming_date: str
    input_count: int
    unique_count: int
    duplicate_count: int
    account_count: int
    total: int
    counts: dict[str, int]
    created_at: datetime
