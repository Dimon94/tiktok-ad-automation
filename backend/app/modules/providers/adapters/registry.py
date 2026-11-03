"""Provider kind metadata and the runtime adapter dispatch boundary.

The connection layer must validate credentials from one canonical table.  New
providers are intentionally allowed to be saved before their protocol adapter
is deployed; verification then fails closed instead of accidentally using the
Wangyan protocol.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.modules.providers.capabilities import capabilities_for_kind
from app.modules.providers.schemas import DramaCandidate

from .contract import (
    JsonDict,
    LegacyProviderClient,
    LinkLookupPage,
    LinkReceipt,
    SearchPage,
    VerifiedLink,
    failure,
    verify_receipt,
)
from .duiba import DuibaClient
from .gangganhao import GangganhaoClient
from .jiashu import JiashuClient
from .minglue import MinglueClient
from .rongliang import RongliangClient
from .wangyan import WangyanClient

_CREDENTIAL_FIELDS: Mapping[str, frozenset[str]] = {
    "wangyan": frozenset({"email", "password"}),
    "jiashu": frozenset({"username", "password"}),
    "duiba": frozenset({"account", "password"}),
    "gangganhao": frozenset({"portal_id", "username", "password"}),
    "minglue": frozenset({"account", "password"}),
    "rongliang": frozenset({"email", "password"}),
}


def credential_fields(kind: str) -> frozenset[str]:
    """Return the exact encrypted credential keys accepted for *kind*."""

    try:
        return _CREDENTIAL_FIELDS[kind]
    except KeyError:
        raise failure("provider_request_invalid") from None


def adapter_for_kind(
    kind: str,
    http: httpx.Client,
    credentials: dict[str, str],
    *,
    application_id: str = "",
    channel_prefix: str = "",
    channel_config: JsonDict | None = None,
) -> LegacyProviderAdapter | DuibaClient | GangganhaoClient | RongliangClient | MinglueClient:
    """Build a session adapter without falling back across provider kinds."""

    if kind == "jiashu":
        return LegacyProviderAdapter(
            kind,
            JiashuClient(
                http,
                session=credentials["session"],
                application_id=application_id,
                channel_prefix=channel_prefix,
            ),
        )
    if kind == "wangyan":
        return LegacyProviderAdapter(
            kind,
            WangyanClient(
                http, token=credentials["token"], application_id=application_id
            ),
        )
    if kind == "minglue":
        return MinglueClient(http, token=credentials["token"], application_id=application_id, channel_config=channel_config)
    if kind == "duiba":
        return DuibaClient(http, token=credentials["token"])
    if kind == "gangganhao":
        return GangganhaoClient(
            http, token=credentials["token"], application_id=application_id
        )
    if kind == "rongliang":
        if credentials.get("email") and credentials.get("password"):
            return RongliangClient.login(
                http,
                email=credentials["email"],
                password=credentials["password"],
                use_curl=True,
            )
        return RongliangClient(http, token=credentials["token"])
    raise failure("provider_unavailable", retryable=True)


@dataclass
class LegacyProviderAdapter:
    """Normalize the two pre-existing clients while their workflows migrate."""

    kind: str
    client: LegacyProviderClient

    def search(self, title: str, cursor: str | None) -> SearchPage:
        try:
            page = int(cursor or 1)
        except (TypeError, ValueError):
            raise failure("provider_request_invalid") from None
        raw = self.client.search(title, page)
        items = [DramaCandidate.model_validate(item) for item in raw["items"]]
        return SearchPage(
            items=items,
            next_cursor=raw.get("next_cursor"),
            complete=raw.get("complete") is True,
        )

    def lookup_link(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> LinkLookupPage:
        raw = self.client.find_existing(drama_id, config, cursor)
        return LinkLookupPage(
            items=[item for item in raw["items"] if isinstance(item, dict)],
            next_cursor=raw.get("next_cursor"),
            complete=raw.get("complete") is True,
        )

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt:
        raise failure("provider_unavailable", retryable=True)

    def read_link(self, remote_id: str) -> LinkReceipt:
        raw = self.client.read_link(remote_id)
        return LinkReceipt(
            remote_id=str(raw["remote_id"]),
            url=raw["url"],
            protected_base=raw.get("protected_base"),
            attribution=raw.get("attribution", {}),
            config=raw.get("config", {}),
        )

    def verify_link(
        self, receipt: LinkReceipt, drama: DramaCandidate, config: JsonDict
    ) -> VerifiedLink:
        return verify_receipt(receipt, drama, config)

    def capabilities(self, application: Any):
        return capabilities_for_kind(self.kind, application)
