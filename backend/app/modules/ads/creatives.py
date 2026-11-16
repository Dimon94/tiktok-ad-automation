"""创意身份发布和本地回填；目录缺页与缺项均不构成删除证据。"""

import json
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, text
from sqlmodel import Session

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.ads_read import material_usages
from app.integrations.tiktok.contracts.ads import AdMaterialUsage, EntityRef
from app.modules.ads.creative_models import AdCreative

_BATCH_SIZE = 500
_COLUMNS = "tenant_id,advertiser_id,creative_id,ad_remote_id,ad_material_id,material_type,platform_material_id,assets,name,operation_status,deleted,identity_status,first_observed_at,observed_at,published_version,evidence"
# MID 缺失只表示观察较弱；父广告和有序资产相同才允许补全，两个已知 MID
# 不一致仍是真冲突。历史引用可能同时包含有 MID 与无 MID 的重复记录。
_SAME_IDENTITY = """(ad_creative.ad_remote_id=EXCLUDED.ad_remote_id
    AND ad_creative.assets=EXCLUDED.assets
    AND (ad_creative.ad_material_id IS NULL OR EXCLUDED.ad_material_id IS NULL
        OR ad_creative.ad_material_id=EXCLUDED.ad_material_id))"""
_MID_ENRICHMENT = f"""({_SAME_IDENTITY} AND ad_creative.ad_material_id IS NULL
    AND EXCLUDED.ad_material_id IS NOT NULL)"""
# 旧版本可能已经把空 MID 记为冲突。只在全部原始冲突证据都与当前强身份
# 相容时恢复分类，保留原证据并追加恢复回执；任何真正的归属/资产/MID 变化不恢复。
_WEAK_CONFLICT = """(ad_creative.identity_status='CONFLICT'
    AND COALESCE(ad_creative.ad_material_id,EXCLUDED.ad_material_id) IS NOT NULL
    AND jsonb_array_length(ad_creative.evidence->'conflicts')>0
    AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(ad_creative.evidence->'conflicts') c
        WHERE c->'identity'->>'ad_remote_id' IS DISTINCT FROM ad_creative.ad_remote_id
           OR c->'identity'->'assets' IS DISTINCT FROM ad_creative.assets
           OR (c->'identity'->>'ad_material_id' IS NOT NULL
               AND c->'identity'->>'ad_material_id' IS DISTINCT FROM
                   COALESCE(ad_creative.ad_material_id,EXCLUDED.ad_material_id))))"""
_ENRICHMENT = """ad_creative.identity_status='UNRESOLVED' AND ad_creative.assets='[]'::jsonb
    AND ad_creative.ad_remote_id=EXCLUDED.ad_remote_id
    AND (ad_creative.ad_material_id IS NULL OR ad_creative.ad_material_id=EXCLUDED.ad_material_id)
    AND EXCLUDED.identity_status IN ('RESOLVED','COMPOSITE')"""


def _value(
    row: dict[str, Any],
    *,
    tenant_id: UUID,
    advertiser_id: str,
    observed_at: datetime,
    published_version: int,
) -> dict[str, Any]:
    creative_id, parent = row["creative_id"], row["ad_remote_id"]
    if (
        not isinstance(creative_id, str)
        or not creative_id
        or not isinstance(parent, str)
        or not parent
    ):
        raise ValueError("creative and parent identities must be nonempty strings")
    assets = row["assets"]
    if not isinstance(assets, list):
        raise ValueError("creative assets must be an ordered list")
    normalized = []
    for asset in assets:
        if not isinstance(asset, dict) or any(
            not isinstance(asset.get(key), str) or not asset[key]
            for key in ("platform_material_id", "material_type")
        ):
            raise ValueError("creative asset identity must be nonempty strings")
        normalized.append(
            {key: asset[key] for key in ("platform_material_id", "material_type")}
        )
    kind, platform, status = None, None, "UNRESOLVED"
    if len(normalized) == 1 and normalized[0]["material_type"] in {
        "VIDEO",
        "IMAGE",
        "TIKTOK_POST",
    }:
        kind, platform, status = (
            normalized[0]["material_type"],
            normalized[0]["platform_material_id"],
            "RESOLVED",
        )
    elif len(normalized) > 1 and all(
        asset["material_type"] == "IMAGE" for asset in normalized
    ):
        kind, status = "CAROUSEL", "COMPOSITE"
    identity = {
        "ad_remote_id": parent,
        "ad_material_id": row.get("ad_material_id"),
        "assets": normalized,
    }
    return {
        "tenant_id": tenant_id,
        "advertiser_id": advertiser_id,
        "creative_id": creative_id,
        **identity,
        "material_type": kind,
        "platform_material_id": platform,
        "name": row.get("name") or "",
        "operation_status": row.get("operation_status"),
        "deleted": bool(row.get("deleted", False)),
        "identity_status": status,
        "first_observed_at": observed_at,
        "observed_at": observed_at,
        "published_version": published_version,
        "evidence": {
            "original_identity": identity,
            "conflicts": [],
            "asset_order_source": row.get("asset_order_source", "OBSERVED"),
        },
    }


def upsert_creatives(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    rows: list[dict[str, Any]],
    observed_at: datetime,
    published_version: int,
) -> int:
    """按固定键序原子写入；旧观察无效，冲突只追加证据，不改写原身份。"""
    if published_version < 1 or observed_at.tzinfo is None:
        raise ValueError(
            "creative publication requires a positive version and aware timestamp"
        )
    if not rows:
        return 0
    values = [
        _value(
            row,
            tenant_id=tenant_id,
            advertiser_id=advertiser_id,
            observed_at=observed_at,
            published_version=published_version,
        )
        for row in rows
    ]
    return _upsert_values(
        session, tenant_id=tenant_id, advertiser_id=advertiser_id, values=values
    )


def _upsert_values(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    values: list[dict[str, Any]],
) -> int:
    if not values:
        return 0
    # 同账户发布与回填共享锁，覆盖尚不存在的身份；不能只锁现有行留下首次写竞态。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"directory-publish:{tenant_id}:{advertiser_id}"},
    )
    total = 0
    # 同一批有冲突身份时分轮提交给UPSERT，避免一条SQL多次影响同一个目标行。
    pending = sorted(values, key=lambda value: value["creative_id"])
    while pending:
        unique: dict[str, dict[str, Any]] = {}
        remaining = []
        for value in pending:
            if value["creative_id"] in unique:
                remaining.append(value)
            else:
                unique[value["creative_id"]] = value
        ordered = list(unique.values())
        for start in range(0, len(ordered), _BATCH_SIZE):
            result = session.connection().execute(
                text(f"""
                INSERT INTO ad_creative ({_COLUMNS})
                SELECT {_COLUMNS} FROM jsonb_populate_recordset(NULL::ad_creative,CAST(:rows AS jsonb))
                ORDER BY tenant_id,advertiser_id,creative_id
                ON CONFLICT (tenant_id,advertiser_id,creative_id) DO UPDATE SET
                  name=CASE WHEN {_SAME_IDENTITY} OR {_ENRICHMENT} THEN EXCLUDED.name ELSE ad_creative.name END,
                  operation_status=CASE WHEN {_SAME_IDENTITY} OR {_ENRICHMENT} THEN EXCLUDED.operation_status ELSE ad_creative.operation_status END,
                  deleted=CASE WHEN {_SAME_IDENTITY} OR {_ENRICHMENT} THEN EXCLUDED.deleted ELSE ad_creative.deleted END,
                  observed_at=EXCLUDED.observed_at,
                  published_version=EXCLUDED.published_version,
                  ad_material_id=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.ad_material_id
                    WHEN {_SAME_IDENTITY} THEN COALESCE(ad_creative.ad_material_id,EXCLUDED.ad_material_id)
                    ELSE ad_creative.ad_material_id END,
                  assets=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.assets ELSE ad_creative.assets END,
                  material_type=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.material_type ELSE ad_creative.material_type END,
                  platform_material_id=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.platform_material_id ELSE ad_creative.platform_material_id END,
                  identity_status=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.identity_status
                    WHEN {_SAME_IDENTITY} THEN CASE WHEN {_WEAK_CONFLICT} THEN EXCLUDED.identity_status
                        ELSE ad_creative.identity_status END ELSE 'CONFLICT' END,
                  evidence=(CASE WHEN {_ENRICHMENT} THEN
                    jsonb_set(ad_creative.evidence,'{{enrichments}}',
                      COALESCE(ad_creative.evidence->'enrichments','[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                        'identity',EXCLUDED.evidence->'original_identity',
                        'observed_at',EXCLUDED.observed_at,'published_version',EXCLUDED.published_version)))
                    WHEN {_SAME_IDENTITY} THEN ad_creative.evidence ELSE
                    jsonb_set(ad_creative.evidence,'{{conflicts}}',
                      (ad_creative.evidence->'conflicts') || jsonb_build_array(jsonb_build_object(
                        'identity',EXCLUDED.evidence->'original_identity',
                        'observed_at',EXCLUDED.observed_at,'published_version',EXCLUDED.published_version))) END
                    || CASE WHEN {_MID_ENRICHMENT} THEN jsonb_build_object('enrichments',
                        COALESCE(ad_creative.evidence->'enrichments','[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                            'identity',EXCLUDED.evidence->'original_identity',
                            'observed_at',EXCLUDED.observed_at,'published_version',EXCLUDED.published_version)))
                        ELSE '{{}}'::jsonb END
                    || CASE WHEN {_SAME_IDENTITY} AND {_WEAK_CONFLICT} THEN
                        jsonb_build_object('conflict_recoveries',COALESCE(ad_creative.evidence->'conflict_recoveries','[]'::jsonb)
                            || jsonb_build_array(jsonb_build_object('reason','missing_ad_material_id',
                                'observed_at',EXCLUDED.observed_at,'published_version',EXCLUDED.published_version)))
                        ELSE '{{}}'::jsonb END)
                WHERE ad_creative.published_version<=EXCLUDED.published_version
                  AND ad_creative.observed_at<=EXCLUDED.observed_at
                RETURNING creative_id
            """),
                {
                    "rows": json.dumps(
                        ordered[start : start + _BATCH_SIZE],
                        default=str,
                        ensure_ascii=False,
                    )
                },
            )
            total += len(result.fetchall())
        pending = remaining
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AdCreative):
            state = inspect(obj)
            assert state is not None
            identity = state.identity
            if identity and identity[:2] == (tenant_id, advertiser_id):
                session.expire(obj)
    return total


def _group_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for ref in references:
        # Smart+没有native creative id时保持未知，不用ad_material_id或父广告伪造。
        creative_ids = ref["creative_ids"] or (
            [ref["ad_remote_id"]]
            if ref["ad_type"] is not None and ref["ad_type"] != "SMART_PLUS"
            else []
        )
        for creative_id in creative_ids:
            key = (creative_id, ref["ad_remote_id"], ref["ad_material_id"])
            if key not in groups:
                groups[key] = {
                    "creative_id": creative_id,
                    "ad_remote_id": ref["ad_remote_id"],
                    "ad_material_id": ref["ad_material_id"],
                    "assets": [],
                    "name": ref["name"],
                    "operation_status": ref["operation_status"],
                    "deleted": ref["parent_status"] in {"DELETE", "DELETED"},
                    "observed_at": ref["observed_at"],
                    "published_version": ref["published_version"],
                }
            row = groups[key]
            asset = {
                "platform_material_id": ref["platform_material_id"],
                "material_type": ref["material_type"],
            }
            if asset not in row["assets"]:
                row["assets"].append(asset)
            row["published_version"] = min(
                row["published_version"], ref["published_version"]
            )
    return list(groups.values())


def _write_groups(
    session: Session, *, tenant_id: UUID, advertiser_id: str, rows: list[dict[str, Any]]
) -> int:
    values = [
        _value(
            row,
            tenant_id=tenant_id,
            advertiser_id=advertiser_id,
            observed_at=row["observed_at"],
            published_version=row["published_version"],
        )
        for row in rows
    ]
    return _upsert_values(
        session, tenant_id=tenant_id, advertiser_id=advertiser_id, values=values
    )


def _cached_rows(
    entity: Mapping[str, Any],
    tenant: UUID,
    account: str,
    requested: frozenset[str] | None,
) -> list[dict[str, Any]]:
    """复用目录解析器，只使用缓存中明确的创意、父广告和资产字段。"""
    configuration = entity["configuration"]
    configuration = configuration if isinstance(configuration, dict) else {}
    smart = entity["kind"] == "ad" and entity["ad_type"] == "SMART_PLUS"
    parent = (
        entity["parent_remote_id"]
        if entity["kind"] == "creative"
        else entity["remote_id"]
    )
    if not parent or (entity["kind"] == "creative" and parent == entity["remote_id"]):
        return []
    header = "smart_plus_ad_id" if smart else "ad_id"
    valid = all(
        configuration.get(k) in (None, v)
        for k, v in (
            ("advertiser_id", account),
            (header, entity["remote_id"]),
        )
    )
    if entity["kind"] == "creative":
        valid &= configuration.get("smart_plus_ad_id") in (None, parent)
    entries = configuration.get("creative_list", []) if smart else [configuration]
    if not isinstance(entries, list) or (smart and not valid):
        return []
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        creative = entry.get("smart_plus_creative_id") if smart else entity["remote_id"]
        if not isinstance(creative, str) or not creative or len(creative) > 128:
            continue
        if (smart and creative == parent) or (
            requested is not None and creative not in requested
        ):
            continue
        usages: list[AdMaterialUsage] = []
        if valid:
            try:
                usages, _complete = material_usages(
                    {**configuration, "creative_list": [entry]} if smart else entry,
                    EntityRef(tenant, account, "ad", parent),
                    smart=smart,
                    creative_id=None if smart or entity["kind"] == "ad" else creative,
                )
            except DomainError as error:
                if error.code != "ads_response_invalid":
                    raise
        assets = []
        for usage in usages:
            asset = {
                "platform_material_id": usage.use_ref.platform_material_id,
                "material_type": usage.use_ref.material_type,
            }
            if len(asset["platform_material_id"]) <= 128 and asset not in assets:
                assets.append(asset)
        partial_assets = []
        images = [u for u in usages if u.use_ref.material_type == "IMAGE"]
        if images:
            info = entry.get("creative_info", {}) if smart else entry
            declared = info.get("image_info" if smart else "image_ids")
            # 解析完整性还包含音乐；财务身份只要求图片成员完整，不能把
            # 缺一个成员的轮播缩成单图，也不能因缺音乐丢弃完整图片身份。
            if (
                not isinstance(declared, list)
                or len(images) != len(declared)
                or any(len(u.use_ref.platform_material_id) > 128 for u in images)
            ):
                partial_assets, assets = assets, []
        # 没有资产内容不等于没有MID：保留显式、合法、同身份的正向MID证据。
        explicit_mid = (
            entry.get("ad_material_id")
            if valid and (smart or entity["kind"] == "creative")
            else None
        )
        material = (
            explicit_mid
            if isinstance(explicit_mid, str)
            and explicit_mid.strip()
            and len(explicit_mid) <= 128
            else None
        )
        status = (
            entry.get("material_operation_status")
            if smart
            else entity["operation_status"]
        )
        if status is not None and not isinstance(status, str):
            status = None
        if material is not None and len(material) > 128:
            assets, material = [], None
        rows.append(
            {
                "creative_id": creative,
                "ad_remote_id": parent,
                "ad_material_id": material,
                "assets": assets,
                "partial_assets": partial_assets,
                "name": entity["name"],
                "operation_status": status,
                "deleted": status in {"DELETE", "DELETED"}
                or entity["operation_status"] in {"DELETE", "DELETED"}
                or entity.get("parent_status") in {"DELETE", "DELETED"},
                "observed_at": entity["observed_at"],
                "published_version": entity["published_version"],
                "asset_order_source": "OBSERVED_CACHED_DIRECTORY",
            }
        )
    return rows


def _cached_directory_rows(
    session: Session,
    tenant: UUID,
    account: str,
    requested: frozenset[str] | None,
    *,
    parent_ids: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    params: dict[str, Any] = {"tenant": tenant, "account": account, "limit": 50}
    direct = (
        " AND o.remote_id=ANY(CAST(:ids AS text[]))" if requested is not None else ""
    )
    if requested is not None:
        params["ids"] = sorted(requested)
    after = ("", "")
    while True:
        page = (
            session.connection()
            .execute(
                text(f"""
            SELECT o.*,p.operation_status AS parent_status FROM ad_object o
            LEFT JOIN ad_object p ON p.tenant_id=o.tenant_id AND p.advertiser_id=o.advertiser_id
                AND o.kind='creative' AND p.kind='ad' AND p.remote_id=o.parent_remote_id
            WHERE o.tenant_id=:tenant AND o.advertiser_id=:account
              AND ((o.kind='creative' AND o.parent_kind='ad') OR (o.kind='ad' AND o.ad_type<>'SMART_PLUS'))
              AND (o.kind,o.remote_id)>(:kind,:after) {direct}
            ORDER BY o.kind,o.remote_id LIMIT :limit
        """),
                params | {"kind": after[0], "after": after[1]},
            )
            .mappings()
            .all()
        )
        if not page:
            break
        parents = {e["parent_remote_id"] for e in page if e["kind"] == "creative"}
        if parents:
            parent_rows = (
                session.connection()
                .execute(
                    text("""
                SELECT * FROM ad_object WHERE tenant_id=:tenant AND advertiser_id=:account
                  AND kind='ad' AND ad_type='SMART_PLUS' AND remote_id=ANY(CAST(:parents AS text[]))
                ORDER BY remote_id
            """),
                    params | {"parents": sorted(parents)},
                )
                .mappings()
                .all()
            )
            page_ids = {e["remote_id"] for e in page if e["kind"] == "creative"}
            # 同一目录观察先消费父配置的MID/完整内容，再消费子对象的状态。
            # 只解析当前页创意，不能为每页反复写父广告下的全部成员。
            for parent_row in parent_rows:
                yield from _cached_rows(
                    dict(parent_row), tenant, account, frozenset(page_ids)
                )
        for entity in page:
            yield from _cached_rows(dict(entity), tenant, account, requested)
        after = (page[-1]["kind"], page[-1]["remote_id"])
    # 创意子记录不存在时，仍可从父广告的明确 creative_list 恢复官方关系。
    # 已有完整身份无需重扫父配置；范围为一个账户，原始 JSON 每页最多50行。
    needed = requested
    if requested is not None:
        known = set(
            session.connection()
            .execute(
                text("""
            SELECT creative_id FROM ad_creative WHERE tenant_id=:tenant AND advertiser_id=:account
              AND creative_id=ANY(CAST(:ids AS text[])) AND identity_status IN ('RESOLVED','COMPOSITE','CONFLICT')
        """),
                params,
            )
            .scalars()
        )
        needed = requested - known
        if not needed:
            return
        params["ids"] = sorted(needed)
    match = (
        """ AND EXISTS (SELECT 1 FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(o.configuration->'creative_list')='array'
        THEN o.configuration->'creative_list' ELSE '[]'::jsonb END) e
        WHERE e->>'smart_plus_creative_id'=ANY(CAST(:ids AS text[])))"""
        if needed is not None
        else ""
    )
    if parent_ids is not None:
        # 一次性物理退休按父广告分页，不能为每页缺失 CID 重扫整个账户 JSON。
        params["parent_ids"] = parent_ids
        match += " AND o.remote_id=ANY(CAST(:parent_ids AS text[]))"
    after_id = ""
    while True:
        page = (
            session.connection()
            .execute(
                text(f"""
            SELECT o.* FROM ad_object o WHERE o.tenant_id=:tenant AND o.advertiser_id=:account
              AND o.kind='ad' AND o.ad_type='SMART_PLUS' AND o.remote_id>:after {match}
            ORDER BY o.remote_id LIMIT :limit
        """),
                params | {"after": after_id},
            )
            .mappings()
            .all()
        )
        if not page:
            break
        for entity in page:
            yield from _cached_rows(dict(entity), tenant, account, needed)
        after_id = page[-1]["remote_id"]


def _backfill_cached(
    session: Session,
    tenant: UUID,
    account: str,
    requested: frozenset[str] | None,
    *,
    primary: bool,
    parent_ids: list[str] | None = None,
) -> int:
    count = 0
    batch = []

    def flush_unique(rows: list[dict[str, Any]]) -> int:
        originals = {
            r["creative_id"]: r
            for r in session.connection()
            .execute(
                text("""
            SELECT * FROM ad_creative WHERE tenant_id=:tenant AND advertiser_id=:account
              AND creative_id=ANY(CAST(:ids AS text[]))
        """),
                {
                    "tenant": tenant,
                    "account": account,
                    "ids": sorted({r["creative_id"] for r in rows}),
                },
            )
            .mappings()
        }
        accepted = []
        for row in rows:
            original = originals.get(row["creative_id"])
            if original:
                same_parent = original["ad_remote_id"] == row["ad_remote_id"]
                mid_compatible = (
                    original["ad_material_id"] is None
                    or row["ad_material_id"] is None
                    or original["ad_material_id"] == row["ad_material_id"]
                )
                observed = row.get("partial_assets", [])
                partial_conflict = bool(original["assets"] and observed) and not {
                    (a["material_type"], a["platform_material_id"]) for a in observed
                } <= {
                    (a["material_type"], a["platform_material_id"])
                    for a in original["assets"]
                }
                if partial_conflict:
                    row["assets"] = observed
                # 空内容/帖子引用不撤销已知主资产；保留旧的正向证据及完整轮播。
                # 但不同的已知MID或新增图片成员是真矛盾，必须保留原始冲突证据。
                if (
                    same_parent
                    and mid_compatible
                    and not partial_conflict
                    and not primary
                    and original["assets"]
                ):
                    row["assets"] = original["assets"]
                    if row["ad_material_id"] is None:
                        row["ad_material_id"] = original["ad_material_id"]
                if row["published_version"] <= original["published_version"]:
                    enrich = (
                        row["published_version"] == original["published_version"]
                        and original["identity_status"] == "UNRESOLVED"
                        and not original["assets"]
                        and bool(row["assets"])
                        and same_parent
                    )
                    conflict = row["published_version"] == original[
                        "published_version"
                    ] and (
                        not same_parent
                        or not mid_compatible
                        or bool(
                            original["assets"]
                            and row["assets"]
                            and original["assets"] != row["assets"]
                        )
                    )
                    if not enrich and not conflict:
                        continue
                    # 同版本只补正向资产，不借缓存回放覆盖live元数据。
                    row["name"], row["operation_status"] = (
                        original["name"],
                        original["operation_status"],
                    )
                    row["deleted"] |= original["deleted"]
            accepted.append(row)
        return _write_groups(
            session, tenant_id=tenant, advertiser_id=account, rows=accepted
        )

    def flush(rows: list[dict[str, Any]]) -> int:
        written = 0
        # 同CID的后续观察必须读取前一轮UPSERT结果；批边界不能改变空内容保护。
        while rows:
            unique, remaining = {}, []
            for row in rows:
                if row["creative_id"] in unique:
                    remaining.append(row)
                else:
                    unique[row["creative_id"]] = row
            written += flush_unique(list(unique.values()))
            rows = remaining
        return written

    for row in _cached_directory_rows(
        session, tenant, account, requested, parent_ids=parent_ids
    ):
        assets = row["assets"]
        is_primary = bool(assets) and all(
            a["material_type"] in {"VIDEO", "IMAGE"} for a in assets
        )
        if is_primary == primary:
            batch.append(row)
        if len(batch) == _BATCH_SIZE:
            count += flush(batch)
            batch = []
    if batch:
        count += flush(batch)
    return count


def backfill_creatives(
    session: Session,
    *,
    tenant_id: UUID | None = None,
    advertiser_id: str | None = None,
    creative_ids: list[str] | None = None,
) -> int:
    """仅从官方目录快照补身份；当前创意事实保留历史完整资产、版本和冲突证据。

    筛选仅缩小创意范围，轮播始终按整个创意读取，不能按素材引用重新推断身份。
    """
    requested = frozenset(creative_ids) if creative_ids is not None else None
    if requested is not None and not requested:
        return 0
    filters, params = [], {}
    if tenant_id is not None:
        filters.append("tenant_id=:tenant")
        params["tenant"] = tenant_id
    if advertiser_id is not None:
        filters.append("advertiser_id=:advertiser")
        params["advertiser"] = advertiser_id
    where = " AND " + " AND ".join(filters) if filters else ""
    accounts = (
        [(tenant_id, advertiser_id)]
        if tenant_id is not None and advertiser_id is not None
        else session.connection()
        .execute(
            text(
                "SELECT DISTINCT tenant_id,advertiser_id FROM ad_object "
                "WHERE kind IN ('ad','creative')"
                + where
                + " ORDER BY tenant_id,advertiser_id"
            ),
            params,
        )
        .all()
    )
    count = 0
    for tenant, account in accounts:
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"directory-publish:{tenant}:{account}"},
        )
        count += _backfill_cached(session, tenant, account, requested, primary=True)
        count += _backfill_cached(session, tenant, account, requested, primary=False)
    return count


def publish_staged_creatives(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    materials_table: str,
    entities_table: str,
    published_version: int,
    observed_at: datetime,
) -> int:
    """只接收目录发布器已胜出且完整的素材；同创意跨页图片合并后一次发布。"""
    # 临时表名只由sync内部生成；完整观察流保留跨页共享素材的每个创意成员，
    # 按观察顺序分别组装各创意轮播，不使用canonical去重后的行或物理ctid。
    references = (
        session.connection()
        .execute(
            text(f"""
        SELECT m.*,p.ad_type,p.operation_status AS parent_status,COALESCE(p.observed_at,:observed) AS observed_at
        FROM {materials_table} m LEFT JOIN ad_object p
          ON p.tenant_id=m.tenant_id AND p.advertiser_id=m.advertiser_id
            AND p.kind='ad' AND p.remote_id=m.ad_remote_id
        WHERE m.complete ORDER BY m.creative_asset_ordinal
    """),
            {"observed": observed_at},
        )
        .mappings()
        .all()
    )
    rows = _group_references([dict(ref) for ref in references])
    # entity-only观察只能更新已有映射，或建立明确普通广告/创意的未知身份。
    # 不要求configuration.creative_list存在，普通creative子读取也可补齐身份。
    entities = (
        session.connection()
        .execute(
            text(f"""
        SELECT o.kind,o.remote_id,o.parent_kind,o.parent_remote_id,o.ad_type,
               o.name,o.operation_status,o.observed_at,o.configuration,o.published_version,
               p.operation_status AS parent_status
        FROM {entities_table} s JOIN ad_object o
          ON o.tenant_id=:tenant AND o.advertiser_id=:account AND o.kind=s.kind AND o.remote_id=s.remote_id
        LEFT JOIN ad_object p ON p.tenant_id=o.tenant_id AND p.advertiser_id=o.advertiser_id
          AND p.kind='ad' AND p.remote_id=o.parent_remote_id AND o.parent_kind='ad'
        WHERE o.published_version=:version AND o.kind IN ('ad','creative')
        ORDER BY o.kind,o.remote_id
    """),
            {
                "tenant": tenant_id,
                "account": advertiser_id,
                "version": published_version,
            },
        )
        .mappings()
        .all()
    )
    entities_by_id = {
        entity["remote_id"]: entity
        for entity in entities
        if entity["kind"] == "creative"
    }
    for row in rows:
        child = entities_by_id.get(row["creative_id"])
        if (
            child
            and child["parent_kind"] == "ad"
            and child["parent_remote_id"] == row["ad_remote_id"]
        ):
            row["observed_at"] = child["observed_at"]
            row["operation_status"] = child["operation_status"]
            row["deleted"] = row["deleted"] or child["operation_status"] in {
                "DELETE",
                "DELETED",
            }
    mapped = {row["creative_id"] for row in rows}
    candidates = [
        entity
        for entity in entities
        if entity["remote_id"] not in mapped
        and (
            entity["kind"] == "creative"
            and entity["parent_kind"] == "ad"
            or entity["kind"] == "ad"
            and entity["ad_type"] != "SMART_PLUS"
        )
    ]
    deleted_parents = {
        entity["remote_id"]: entity
        for entity in entities
        if entity["kind"] == "ad"
        and entity["operation_status"] in {"DELETE", "DELETED"}
    }
    if candidates or deleted_parents:
        known = (
            session.connection()
            .execute(
                text("""
            SELECT * FROM ad_creative WHERE tenant_id=:tenant AND advertiser_id=:account AND (creative_id=ANY(:ids) OR ad_remote_id=ANY(:parents))
        """),
                {
                    "tenant": tenant_id,
                    "account": advertiser_id,
                    "ids": [entity["remote_id"] for entity in candidates],
                    "parents": list(deleted_parents),
                },
            )
            .mappings()
            .all()
        )
        existing = {row["creative_id"]: row for row in known}
        for entity in candidates:
            original = existing.get(entity["remote_id"])
            # 仅解析缺少本页素材引用的子对象，复用账户/父广告校验和轮播完整性校验。
            parsed = (
                next(
                    (
                        row
                        for row in _cached_rows(entity, tenant_id, advertiser_id, None)
                        if row["assets"]
                    ),
                    None,
                )
                if entity["kind"] == "creative"
                else None
            )
            # 完整子目录按当前胜出版本发布；空内容继续保全既有正向身份。
            # 与既有资产不一致仍由统一UPSERT记录真实冲突，不能静默覆盖。
            # 空资产只代表未知；后续同父广告完整证据可显式补齐并留下enrichment证据。
            rows.append(
                {
                    "creative_id": entity["remote_id"],
                    "ad_remote_id": entity["parent_remote_id"]
                    if entity["kind"] == "creative"
                    else entity["remote_id"],
                    "ad_material_id": (parsed.get("ad_material_id") if parsed else None)
                    or (original["ad_material_id"] if original else None),
                    "assets": parsed["assets"]
                    if parsed
                    else (original["assets"] if original else []),
                    "name": entity["name"],
                    "operation_status": entity["operation_status"],
                    # 子对象ENABLE不能推翻父广告的明确删除；父状态在账户发布锁下
                    # 从canonical读取，不要求父广告也出现在本轮部分子目录中。
                    "deleted": entity["operation_status"] in {"DELETE", "DELETED"}
                    or entity["parent_status"] in {"DELETE", "DELETED"},
                    "observed_at": entity["observed_at"],
                    "published_version": published_version,
                }
            )
        observed_ids = {row["creative_id"] for row in rows}
        for original in known:
            parent = deleted_parents.get(original["ad_remote_id"])
            if parent and original["creative_id"] not in observed_ids:
                rows.append(
                    {
                        **dict(original),
                        "deleted": True,
                        "operation_status": "DELETE",
                        "observed_at": parent["observed_at"],
                        "published_version": published_version,
                    }
                )
    return _write_groups(
        session, tenant_id=tenant_id, advertiser_id=advertiser_id, rows=rows
    )
