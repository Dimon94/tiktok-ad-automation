"""原生目录的官方响应事实：Series 游标与身份页码互不混用。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.core.errors import DomainError
from app.modules.builds.selection_schemas import SelectionIdentity


class NativeAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")
    native_series_id: str = Field(min_length=1)
    native_series_name: str | None = None
    native_series_cover_url: str | None = None
    native_series_total_episode: StrictInt | None = None
    native_series_total_duration: StrictInt | None = None


class NativeIdentity(SelectionIdentity):
    model_config = ConfigDict(extra="ignore")
    display_name: str | None = None
    username: str | None = None
    available_status: str | None = None
    can_use_series: StrictBool | None = None
    can_push_video: StrictBool | None = None
    is_gpppa: StrictBool | None = None


class NativeDirectoryFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[NativeAsset | NativeIdentity, ...]
    cursor: int = 0
    has_more: bool
    total_number: int | None = None
    total_page: int | None = None
    item_id_hashes: tuple[str, ...]


def native_page(
    data: dict[str, Any] | list[dict[str, Any]],
    *,
    resource: str,
    page: int,
    cursor: int,
    identity_type: str | None = None,
) -> NativeDirectoryFacts:
    import json
    from hashlib import sha256

    from pydantic import ValidationError

    try:
        if not isinstance(data, dict):
            raise TypeError()
        if resource == "native_series":
            rows = data["list"]
            next_cursor, more = data["cursor"], data["has_more"]
            if (
                type(next_cursor) is not int
                or type(more) is not bool
                or next_cursor < 0
                or (more and next_cursor <= cursor)
                or not isinstance(rows, list)
                or len(rows) > 20
            ):
                raise ValueError()
            items = tuple(NativeAsset.model_validate(row) for row in rows)
            total = pages = None
        else:
            rows, info = data["identity_list"], data["page_info"]
            if not isinstance(rows, list) or any(
                type(info.get(k)) is not int
                for k in ("page", "page_size", "total_number", "total_page")
            ):
                raise ValueError()
            total, pages = info["total_number"], info["total_page"]
            if (
                info["page"] != page
                or info["page_size"] != 50
                or total < 0
                or pages != (total + 49) // 50
                or page > max(1, pages)
                or len(rows) != min(50, max(0, total - (page - 1) * 50))
            ):
                raise ValueError()
            items = tuple(NativeIdentity.model_validate(row) for row in rows)
            if any(item.identity_type != identity_type for item in items):
                raise ValueError()
            next_cursor, more = 0, page < pages
        keys = [
            item.native_series_id
            if isinstance(item, NativeAsset)
            else json.dumps(
                [item.identity_type, item.identity_id, item.identity_authorized_bc_id]
            )
            for item in items
        ]
        if len(keys) != len(set(keys)):
            raise ValueError()
        return NativeDirectoryFacts(
            items=items,
            cursor=next_cursor,
            has_more=more,
            total_number=total,
            total_page=pages,
            item_id_hashes=tuple(sha256(key.encode()).hexdigest() for key in keys),
        )
    except KeyError, TypeError, ValueError, ValidationError:
        raise DomainError(
            "scene_response_unverified", "原生目录缺少完整分页证据"
        ) from None
