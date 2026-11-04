"""历史系列用明确版权方与剧编号认领，单独的相似剧名不能成为财务归属。"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.ads.naming import parse_campaign_name
from app.modules.providers.models import ProviderConnection, ProviderDrama

LABELS = {
    "jiashu": "嘉书",
    "wangyan": "网眼",
    "duiba": "兑吧",
    "gangganhao": "刚刚好",
    "rongliang": "容量",
    "minglue": "明略",
}


def catalog_snapshot(session: Session, tenant_id: UUID) -> dict[str, Any]:
    providers = session.exec(
        select(ProviderConnection)
        .where(col(ProviderConnection.tenant_id) == tenant_id)
        .order_by(col(ProviderConnection.id))
    ).all()
    dramas = session.exec(
        select(ProviderDrama)
        .where(col(ProviderDrama.tenant_id) == tenant_id)
        .order_by(col(ProviderDrama.id))
    ).all()
    return {
        "providers": [
            {"id": str(p.id), "kind": p.kind, "name": p.display_name} for p in providers
        ],
        "dramas": [
            {
                "id": str(d.id),
                "provider_id": str(d.connection_id),
                "external_id": d.external_drama_id,
                "display_id": d.display_drama_id,
                "title": d.title,
            }
            for d in dramas
        ],
    }


EXTRA_LABELS = {"gangganhao": {"ggh", "gangganghao"}}


def _normal(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _parts(
    name: str, catalog: dict[str, Any]
) -> tuple[dict[str, Any], str, str | None] | None:
    # 两种实际历史命名：版权方-剧名-编号-MMDD-备注；{b编号/s…/c…}-剧名-编号-MMDD-备注-版权方。
    name = unicodedata.normalize("NFC", name.strip())
    braced = re.match(r"^\{b(\d+)/s\d+/c\d+\}-(.+)$", name)
    body = braced[2] if braced else name
    candidates = []
    for provider in catalog["providers"]:
        aliases = {
            provider["name"],
            provider["kind"],
            LABELS.get(provider["kind"], ""),
            *EXTRA_LABELS.get(provider["kind"], set()),
        } - {"", "other"}
        for alias in sorted(aliases, key=len, reverse=True):
            alias = unicodedata.normalize("NFC", alias.strip())
            if braced and _normal(body).endswith("-" + _normal(alias)):
                payload = body[: -(len(alias) + 1)]
            elif not braced and _normal(body).startswith(_normal(alias) + "-"):
                payload = body[len(alias) + 1 :]
            else:
                continue
            identifiers = []
            for match in re.finditer(r"(?<=-)(\d+)-(\d{8}|\d{4})(?=-)", payload):
                if braced and match[1] != braced[1]:
                    continue
                try:
                    datetime.strptime(
                        match[2] if len(match[2]) == 8 else "2000" + match[2], "%Y%m%d"
                    )
                except ValueError:
                    continue
                if match.start() > 1:
                    identifiers.append((payload[: match.start() - 1], match[1]))
            # 名称或备注中有多个看似编号的位置时，不任取一个认领财务归属。
            if len(identifiers) == 1:
                title, number = identifiers[0]
                candidates.append((provider, title, number))
            elif len(identifiers) > 1:
                # 明确区分无编号和编号冲突，不能再按同名目录认领后者。
                candidates.append((provider, identifiers[0][0], None))
            break
    return candidates[0] if len(candidates) == 1 else None


def _provider_for_label(label: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    matches = [
        p
        for p in catalog["providers"]
        if _normal(label)
        in {
            _normal(p["name"]),
            _normal(p["kind"]) if p["kind"] != "other" else "",
            _normal(LABELS.get(p["kind"], "")),
            *(_normal(label) for label in EXTRA_LABELS.get(p["kind"], set())),
        }
    ]
    return matches[0] if len(matches) == 1 else None


def identify_campaign(name: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    """复用广告报表的版权方/剧名解析；唯一版权方连接足以核算分成，不要求先选剧建链。"""
    parts = _parts(name, catalog)
    provider: dict[str, Any] | None
    number: str | None
    parsed = parse_campaign_name(name)
    if parts:
        provider, title, number = parts
        label = provider["name"]
    elif parsed.status == "VALID":
        label, title, number = (
            parsed.provider_label or "",
            parsed.drama_name or "",
            None,
        )
        provider = _provider_for_label(label, catalog)
    else:
        return None
    matches = [
        d
        for d in catalog["dramas"]
        if provider
        and d["provider_id"] == provider["id"]
        and (
            number in {d["external_id"], d["display_id"]}
            if number
            else parts is None and _normal(d["title"]) == _normal(title)
        )
    ]
    if len(matches) == 1 and provider is not None:
        drama = matches[0]
        return {
            "drama_id": UUID(drama["id"]),
            "drama_key": drama["id"],
            "drama_name": drama["title"],
            "provider_id": UUID(provider["id"]),
            "provider_name": provider["name"],
        }
    # 有明确唯一版权方+剧编号可形成稳定历史身份；目录尚未收录也不丢失平台或经营金额。
    if number and provider and not matches:
        key = f"external:{provider['id']}:{number}"
    else:
        # 与广告报表按版权方标签/剧名归组的口径一致。没有编号的分组不据此认定补贴门槛。
        key = (
            "named:"
            + hashlib.sha256(
                json.dumps(
                    [provider["id"] if provider else label, title], ensure_ascii=False
                ).encode()
            ).hexdigest()
        )
    return {
        "drama_id": None,
        "drama_key": key,
        "drama_name": title,
        "provider_id": UUID(provider["id"]) if provider else None,
        "provider_name": provider["name"] if provider else label,
    }


def unresolved_name(name: str, catalog: dict[str, Any]) -> str:
    """可读的名称不等于已核实归属；保留独立系列键及空经营金额。"""
    parts = _parts(name, catalog)
    if parts:
        return parts[1]
    parsed = parse_campaign_name(name)
    return parsed.drama_name or name or "待归属"
