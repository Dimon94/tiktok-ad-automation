"""官方 SDK 音乐读取：冻结范围、一次物理请求一次准入、严格原始回执。"""

import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from business_api_client.rest import ApiException
from urllib3.exceptions import HTTPError, TimeoutError

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.materials import MaterialPage, RemoteCallBudget
from app.integrations.tiktok.contracts.music import MusicItem, MusicPage
from app.integrations.tiktok.official.accounts import (
    RequestScope,
    _provider_retry_after_ms,
)
from app.integrations.tiktok.sdk import AccountAdmissionDeferred

MUSIC_OPERATION = "music.get"
MUSIC_ENDPOINT = "/open_api/v1.3/file/music/get/"
_PAGE_SIZE = 100
_MAX_BODY = 2 * 1024 * 1024


def _invalid_schema(evidence: CallEvidence = CallEvidence()) -> RemoteCallError:
    return RemoteCallError(
        "unsupported_music_schema", effect="UNKNOWN", evidence=evidence
    )


def _text(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or len(value) > 1000:
        raise ValueError("invalid text")
    value = value.strip()
    if not value and not optional:
        raise ValueError("missing text")
    return value or None


def _url(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > 16384
        or any(ord(c) <= 32 for c in value)
    ):
        raise ValueError("invalid URL")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("invalid URL")
    return value


def _advertiser(value: object) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
    ):
        raise DomainError("music_request_invalid", "音乐查询参数无效")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid number")


def _item(row: object) -> MusicItem:
    if type(row) is not dict:
        raise ValueError("invalid item")
    row = cast(dict[str, Any], row)
    identity = _text(row.get("music_id"))
    assert identity is not None
    if len(identity) > 128 or identity != row["music_id"]:
        raise ValueError("invalid identity")
    name = _text(row.get("name"), optional=True)
    file_name = _text(row.get("file_name"), optional=True)
    author = _text(row.get("author"), optional=True)
    duration = row.get("duration")
    if duration is not None and (
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("invalid duration")
    return MusicItem(
        identity, name or file_name or identity, author, duration, _url(row.get("url"))
    )


def _distinct_music(rows: tuple[MusicItem, ...]) -> tuple[MusicItem, ...]:
    # 平台可按不同素材记录重复返回同一曲目；展示/选曲使用 music_id，
    # 去重在原始分页验证之后进行，不能用去重数量改写平台总数和游标。
    unique: dict[str, MusicItem] = {}
    for item in rows:
        unique.setdefault(item.music_id, item)
    return tuple(unique.values())


@contextmanager
def _bounded_error_responses(client: Any) -> Iterator[None]:
    """在 SDK 创建 ApiException 前丢弃非成功正文，避免其 .data 无界读取。"""
    # 客户端属于本次冻结 gateway；只包装它已有的 PoolManager，不另建 HTTP
    # 客户端、不改变 SDK 的 URL/请求/重试。包装持续到 SDK 线程完成后恢复。
    manager = client.rest_client.pool_manager
    original = manager.request
    missing = object()
    previous = manager.__dict__.get("request", missing)

    def request(*args: Any, **kwargs: Any) -> Any:
        response = original(*args, **kwargs)
        if 200 <= response.status <= 299:
            return response
        # HTTP 错误正文不能提供成功音乐事实，读取零字节即可；仅保留退避所需头。
        error = ApiException(status=response.status)
        retry_after = response.headers.get("Retry-After")
        if isinstance(retry_after, str) and len(retry_after) <= 128:
            error.headers = {"Retry-After": retry_after}
        try:
            response.close()
        finally:
            response.release_conn()
        raise error

    manager.request = request
    try:
        yield
    finally:
        if previous is missing:
            del manager.request
        else:
            manager.request = previous


class SDKMusicOperations:
    def __init__(
        self,
        client: Any,
        *,
        route: FrozenTikTokRoute,
        request_scope: RequestScope,
        deadline: datetime,
    ):
        if route.channel != "OFFICIAL_API":
            raise DomainError("music_channel_unsupported", "当前连接不支持音乐读取")
        self._client = client
        self._route = route
        self._scope = request_scope
        self._deadline = deadline

    def _signature(self, value: str) -> str:
        # 复用部署密钥；游标不包含凭据、查询原文或临时 URL，不另建持久化状态。
        return hmac.new(
            settings.SECRET_KEY.encode(),
            ("tiktok-music-v1:" + value).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _context(self, advertiser: str, keyword: str) -> str:
        return self._signature(
            json.dumps(
                {
                    "route": self._route.model_dump(mode="json"),
                    "advertiser_id": advertiser,
                    "keyword": keyword,
                    "page_size": _PAGE_SIZE,
                    "music_scene": "CAROUSEL_ADS",
                    "search_type": "SEARCH_BY_KEYWORD",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _cursor(self, context: str, page: int) -> str:
        body = f"1.{page}.{context}"
        return f"{body}.{self._signature(body)}"

    def _page(self, cursor: str | None, context: str) -> int:
        if cursor is None:
            return 1
        if type(cursor) is str and re.fullmatch(
            r"1\.[0-9]{1,4}\.[0-9a-f]{64}\.[0-9a-f]{64}", cursor
        ):
            body, signature = cursor.rsplit(".", 1)
            _, page, bound = body.split(".")
            if (
                hmac.compare_digest(self._signature(body), signature)
                and hmac.compare_digest(context, bound)
                and 2 <= int(page) <= 1000
            ):
                return int(page)
        raise DomainError("music_cursor_invalid", "音乐分页范围已变化，请重新搜索")

    def search(
        self,
        *,
        advertiser_id: str,
        keyword: str,
        cursor: str | None,
        budget: RemoteCallBudget,
    ) -> MusicPage:
        if type(keyword) is not str or not keyword.strip() or len(keyword) > 1000:
            raise DomainError("music_request_invalid", "音乐查询参数无效")
        _advertiser(advertiser_id)
        context = self._context(advertiser_id, keyword)
        page = self._page(cursor, context)
        result = self._read(
            advertiser_id, "SEARCH_BY_KEYWORD", {"keyword": keyword}, page, budget
        )
        return MusicPage(
            _distinct_music(result.rows),
            self._cursor(context, page + 1) if page < result.total_pages else None,
            result.evidence,
        )

    def recommend(
        self,
        *,
        advertiser_id: str,
        image_urls: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> MusicPage:
        try:
            if (
                type(image_urls) is not tuple
                or not 2 <= len(image_urls) <= 35
                or any(_url(url) is None for url in image_urls)
            ):
                raise ValueError("invalid images")
        except ValueError, TypeError:
            raise DomainError(
                "music_request_invalid", "音乐推荐需要 2–35 张图片的有效预览地址"
            ) from None
        # URL 必须来自调用方同一冻结路线下的新鲜 image preview；本层不缓存、不下载。
        result = self._read(
            advertiser_id,
            "SEARCH_BY_RECOMMEND",
            {"image_urls": list(image_urls)},
            1,
            budget,
        )
        return MusicPage(
            _distinct_music(result.rows),
            None,
            result.evidence,
            truncated=result.total_pages > 1,
        )

    def read(
        self,
        *,
        advertiser_id: str,
        music_ids: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> MusicPage:
        if (
            type(music_ids) is not tuple
            or not 1 <= len(music_ids) <= _PAGE_SIZE
            or any(
                type(identity) is not str
                or not identity.strip()
                or identity != identity.strip()
                or len(identity) > 128
                for identity in music_ids
            )
            or len(set(music_ids)) != len(music_ids)
        ):
            raise DomainError("music_request_invalid", "音乐查询参数无效")
        result = self._read(
            advertiser_id,
            "SEARCH_BY_MUSIC_ID",
            {"music_ids": list(music_ids)},
            1,
            budget,
        )
        if any(item.music_id not in music_ids for item in result.rows):
            raise _invalid_schema(result.evidence)
        if result.total_pages > 1:
            raise RemoteCallError(
                "music_read_incomplete", effect="UNKNOWN", evidence=result.evidence
            )
        # 缺失 ID 保持缺失；调用方不能用曲名或历史 URL 补出平台未返回的音乐。
        return MusicPage(_distinct_music(result.rows), None, result.evidence)

    def _read(
        self,
        advertiser: str,
        search_type: str,
        filtering: dict[str, Any],
        page: int,
        budget: RemoteCallBudget,
    ) -> MaterialPage[MusicItem]:
        _advertiser(advertiser)
        if budget.deadline != self._deadline:
            raise DomainError("material_deadline", "素材预算与本次会话期限不一致")
        budget.timeout(upload=False)
        query = {
            "advertiser_id": advertiser,
            "music_scene": "CAROUSEL_ADS",
            "search_type": search_type,
            "filtering": filtering,
            "page": page,
            "page_size": _PAGE_SIZE,
        }
        with self._scope(advertiser, MUSIC_OPERATION, budget.deadline):
            try:
                # 固定 SDK 无生成音乐方法；官方 ApiClient 支持原始回执模式。
                # 端点来自官方目录，详细字段来自历史接受回执；不是本轮实时联调证明。
                with _bounded_error_responses(self._client):
                    response = self._client.call_api(
                        MUSIC_ENDPOINT,
                        "GET",
                        {},
                        [
                            (
                                key,
                                json.dumps(value) if isinstance(value, dict) else value,
                            )
                            for key, value in query.items()
                        ],
                        {
                            "Access-Token": self._client.default_headers[
                                "Access-Token"
                            ],
                            "Accept": "application/json",
                        },
                        auth_settings=[],
                        async_req=True,
                        _return_http_data_only=True,
                        _preload_content=False,
                        _request_timeout=budget.timeout(upload=False),
                    ).get()
                try:
                    raw = response.read(_MAX_BODY + 1)
                    headers = response.headers
                finally:
                    try:
                        response.close()
                    finally:
                        response.release_conn()
            except ApiException as error:
                if error.status == 429:
                    raise AccountAdmissionDeferred(
                        _provider_retry_after_ms(error.headers) or 5000,
                        source="provider",
                        reason="http_429",
                    ) from None
                raise RemoteCallError(
                    "music_request_failed", effect="UNKNOWN", evidence=CallEvidence()
                ) from None
            except TimeoutError:
                raise RemoteCallError(
                    "music_timeout", effect="UNKNOWN", evidence=CallEvidence()
                ) from None
            except HTTPError:
                raise RemoteCallError(
                    "music_request_failed", effect="UNKNOWN", evidence=CallEvidence()
                ) from None
            budget.timeout(upload=False)
            return self._parse(
                raw,
                page=page,
                headers=headers,
                recommendation=search_type == "SEARCH_BY_RECOMMEND",
            )

    def _parse(
        self, raw: bytes, *, page: int, headers: Any, recommendation: bool = False
    ) -> MaterialPage[MusicItem]:
        evidence = CallEvidence()
        try:
            if len(raw) > _MAX_BODY:
                raise ValueError("oversized response")
            payload = json.loads(
                raw, object_pairs_hook=_unique, parse_constant=_reject_constant
            )
            if type(payload) is not dict or type(payload.get("code")) is not int:
                raise ValueError("invalid envelope")
            request_id = payload.get("request_id")
            evidence = CallEvidence(
                request_id=request_id
                if type(request_id) is str
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)
                else None,
                remote_code=payload["code"] or None,
            )
            if payload["code"] == 40100:
                raise AccountAdmissionDeferred(
                    max(300000, _provider_retry_after_ms(headers) or 0),
                    source="provider",
                    reason="40100",
                )
            if payload["code"] != 0:
                raise RemoteCallError(
                    "music_request_failed",
                    effect="REJECTED_NO_EFFECT",
                    evidence=evidence,
                )
            data = payload["data"]
            if (
                type(data) is not dict
                or type(data.get("musics")) is not list
                or type(data.get("page_info")) is not dict
            ):
                raise ValueError("invalid data")
            info = data["page_info"]
            if (
                info["page"] != page
                or type(info["page"]) is not int
                or type(info["page_size"]) is not int
                # 推荐接口可将请求的 100 收窄为实际候选数（真实回执为 98）；
                # 搜索/按ID读取仍校验请求页大小，推荐保留实际分页事实。
                or not 1 <= info["page_size"] <= _PAGE_SIZE
                or not recommendation
                and info["page_size"] != _PAGE_SIZE
                or type(info["total_number"]) is not int
            ):
                raise ValueError("unexpected page")
            rows = tuple(_item(row) for row in data["musics"])
            return MaterialPage(
                rows,
                page,
                info["page_size"],
                info["total_page"],
                info["total_number"],
                evidence,
            )
        except ValueError, TypeError, KeyError, OverflowError, RecursionError:
            raise _invalid_schema(evidence) from None
