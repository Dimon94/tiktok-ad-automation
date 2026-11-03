"""Offline-reviewed transport contract. No global session or implicit retry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.core.errors import DomainError
from app.modules.providers.schemas import DramaCandidate

type JsonDict = dict[str, Any]


@dataclass(frozen=True)
class SearchPage:
    """A provider search page whose cursor proves whether the scan is complete."""

    items: list[DramaCandidate]
    next_cursor: str | None
    complete: bool

    def __post_init__(self) -> None:
        if self.complete and self.next_cursor is not None:
            raise ValueError("complete search pages cannot carry a cursor")
        if not self.complete and not self.next_cursor:
            raise ValueError("incomplete search pages require a cursor")


@dataclass(frozen=True)
class LinkLookupPage:
    """Existing-link results with the same completeness guarantee as searches."""

    items: list[JsonDict]
    next_cursor: str | None
    complete: bool

    def __post_init__(self) -> None:
        if self.complete and self.next_cursor is not None:
            raise ValueError("complete link pages cannot carry a cursor")
        if not self.complete and not self.next_cursor:
            raise ValueError("incomplete link pages require a cursor")


@dataclass(frozen=True)
class LinkReceipt:
    """Provider link identity and attribution after protocol normalization."""

    remote_id: str
    url: str
    protected_base: str | None
    attribution: JsonDict
    config: JsonDict

    def __post_init__(self) -> None:
        if not self.remote_id or not isinstance(self.remote_id, str):
            raise ValueError("link receipt remote_id must be non-empty")
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("link receipt URL must be non-empty")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("link receipt URL must be HTTPS")
        # None means the adapter failed to prove an attribution base. Empty
        # string is an explicit provider result and is therefore valid.
        if self.protected_base is None:
            raise ValueError("link receipt protected_base is missing")
        if not isinstance(self.protected_base, str):
            raise ValueError("link receipt protected_base must be a string")
        if not isinstance(self.attribution, dict) or not isinstance(self.config, dict):
            raise ValueError("link receipt metadata must be objects")


@dataclass(frozen=True)
class VerifiedLink:
    remote_id: str
    external_drama_id: str
    title: str
    url: str
    protected_base: str
    attribution: JsonDict
    config: JsonDict


class ProviderClient(Protocol):
    """持久任务共用步骤发送/回读；搜索分页由各版权方独立合同约束。"""

    def create_step(self, step: str, payload: JsonDict) -> JsonDict: ...
    def read_link(self, remote_id: str) -> JsonDict | LinkReceipt: ...


class LegacyProviderClient(ProviderClient, Protocol):
    def search(self, title: str, page: int) -> JsonDict: ...
    def find_existing(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> JsonDict: ...
    def create_step(self, step: str, payload: JsonDict) -> JsonDict: ...
    def read_link(self, remote_id: str) -> JsonDict: ...


class ProviderAdapter(Protocol):
    def search(self, title: str, cursor: str | None) -> SearchPage: ...

    def lookup_link(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> LinkLookupPage: ...

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt: ...

    def read_link(self, remote_id: str) -> LinkReceipt: ...

    def verify_link(
        self, receipt: LinkReceipt, drama: DramaCandidate, config: JsonDict
    ) -> VerifiedLink: ...

    def capabilities(self, application: Any) -> Any: ...


def verify_receipt(
    receipt: LinkReceipt, drama: DramaCandidate, config: JsonDict
) -> VerifiedLink:
    """Verify the normalized identity/config before persisting a ready link."""

    if receipt.config != config:
        raise ValueError("link receipt configuration does not match request")
    if not drama.external_drama_id.strip() or not drama.title.strip():
        raise ValueError("drama identity must be non-empty")
    return VerifiedLink(
        remote_id=receipt.remote_id,
        external_drama_id=drama.external_drama_id,
        title=drama.title,
        url=receipt.url,
        protected_base=receipt.protected_base or "",
        attribution=receipt.attribution,
        config=receipt.config,
    )


@dataclass(frozen=True)
class ProviderSession:
    connection_id: UUID
    application_id: str
    http: httpx.Client = field(repr=False)
    client: ProviderClient = field(repr=False)


def failure(code: str, *, retryable: bool = False) -> DomainError:
    return DomainError(code, "版权方操作未完成，请根据错误状态处理", retryable)


def request_json(
    http: httpx.Client, method: str, url: str, *, write: bool = False, timeout: float = 30, **kwargs: Any
) -> tuple[JsonDict, httpx.Response]:
    # httpcore DEBUG response-header traces can include login Set-Cookie.
    for name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logging.getLogger(name).disabled = True
    try:
        response = http.request(
            method, url, timeout=timeout, follow_redirects=False, **kwargs
        )
        if response.status_code in {401, 403}:
            raise failure(
                "provider_session_expired"
                if response.status_code == 401
                else "provider_application_forbidden"
            )
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise failure(
                "provider_result_unknown" if write else "provider_unavailable",
                retryable=not write,
            )
        if not 200 <= response.status_code < 300:
            raise failure("provider_rejected")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError
        return body, response
    except httpx.HTTPError:
        raise failure(
            "provider_result_unknown" if write else "provider_unavailable",
            retryable=not write,
        ) from None
    except ValueError:
        raise failure(
            "provider_result_unknown" if write else "provider_schema_unsupported"
        ) from None


def positive(value: object) -> int:
    if isinstance(value, bool):
        raise failure("provider_request_invalid")
    try:
        number = int(str(value))
    except ValueError:
        raise failure("provider_request_invalid") from None
    if number < 1 or str(number) != str(value):
        raise failure("provider_request_invalid")
    return number


def count_or_none(value: object, *, minimum: int) -> int | None:
    """剧单数值字段缺失或格式异常时返回 None，由取链流程按 CLI 规则回退。"""

    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value))
    except ValueError:
        return None
    return number if number >= minimum else None


def external_id(value: object) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not str(value)
        or len(str(value)) > 255
    ):
        raise failure("provider_schema_unsupported")
    return str(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise failure("provider_schema_unsupported")
    return value


def counted_page(
    data: object, page: int, page_size: int = 20
) -> tuple[list[JsonDict], str | None]:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise failure("provider_schema_unsupported")
    rows, count = data["data"], data.get("count")
    if (
        type(count) is not int
        or count < 0
        or any(not isinstance(row, dict) for row in rows)
        or len(rows) > page_size
    ):
        raise failure("provider_schema_unsupported")
    offset = (page - 1) * page_size
    if count < offset + len(rows) or (
        count > offset + len(rows) and len(rows) != page_size
    ):
        raise failure("provider_schema_unsupported")
    return rows, str(page + 1) if count > page * page_size else None
