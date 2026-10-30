"""Smart+ 素材补读复用目录运行/分页；私有冻结范围不进入通用 DirectoryQuery。"""

import json
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid5

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import (
    AdEntity,
    AdMaterialUsage,
    EntityRef,
    MaterialUseRef,
)
from app.modules.ads.directory_requests import _partition_key
from app.modules.ads.directory_services import IDENTITY_SERVICE, SERVICE_KEY
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun

MATERIAL_CHILD_KEY = "material_child_v1"
MATERIAL_CONTEXT_PAGE = 2_147_483_647
_MAX_GROUPS = 100


def is_material_child(run: AdDirectoryRun) -> bool:
    return MATERIAL_CHILD_KEY in run.query


def _scope_signature(run: AdDirectoryRun) -> str:
    return sha256(
        json.dumps(
            [
                str(run.tenant_id),
                run.advertiser_id,
                run.bc_id,
                str(run.actor_id),
                str(run.connection_id),
                run.channel,
                run.frozen_route,
                run.workload,
                run.query.get("include_deleted"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _invalid() -> DomainError:
    return DomainError("directory_scope_mismatch", "素材子运行冻结范围无效")


def _ref(value: dict) -> EntityRef:
    return EntityRef(
        UUID(value["tenant_id"]),
        value["advertiser_id"],
        value["kind"],
        value["remote_id"],
    )


def _usage(value: dict) -> AdMaterialUsage:
    use = value["use_ref"]
    return AdMaterialUsage(
        MaterialUseRef(
            _ref(use["ad_ref"]),
            use["platform_material_id"],
            use.get("ad_material_id"),
            use["material_type"],
        ),
        UUID(value["local_material_id"]) if value.get("local_material_id") else None,
        value.get("operation_status"),
        bool(value["complete"]),
        value.get("name", ""),
        tuple(value.get("creative_ids", ())),
    )


def material_child_scope(
    session: Session,
    run: AdDirectoryRun,
) -> tuple[tuple[AdEntity, ...], tuple[AdMaterialUsage, ...]] | None:
    """从子运行自身还原冻结父链，源页按原保留期清理后仍可续读。"""
    if not is_material_child(run):
        return None
    try:
        marker = run.query[MATERIAL_CHILD_KEY]
        if (
            set(marker) != {"source_run_id", "context_page"}
            or type(marker["context_page"]) is not int
            or marker["context_page"] != MATERIAL_CONTEXT_PAGE
        ):
            raise _invalid()
        # 校验范围之前不把调用方尚未核验的运行变更刷入数据库。
        with session.no_autoflush:
            context = session.get(AdDirectoryPage, (run.id, MATERIAL_CONTEXT_PAGE))
        if (
            context is None
            or context.tenant_id != run.tenant_id
            or context.advertiser_id != run.advertiser_id
            or context.coverage != "MATERIAL_CONTEXT"
            or context.complete
            or context.next_page is not None
            or context.published_version is not None
            or context.items
            or context.materials
        ):
            raise _invalid()
        state = context.evidence[MATERIAL_CHILD_KEY]
        if (
            run.kind != "creative"
            or run.ad_type != "SMART_PLUS"
            or state["child_run_id"] != str(run.id)
            or state["source_run_id"] != marker["source_run_id"]
            or state["scope_signature"] != _scope_signature(run)
            or run.query.get("advertiser_id") != run.advertiser_id
            or run.query.get("kind") != run.kind
            or run.query.get("ad_type") != run.ad_type
            or run.query.get("ids")
        ):
            raise _invalid()
        UUID(state["source_run_id"])
        ads = tuple(
            AdEntity(
                EntityRef(run.tenant_id, run.advertiser_id, "ad", ad),
                EntityRef(run.tenant_id, run.advertiser_id, "adgroup", group),
                "SMART_PLUS",
                "",
                {},
                None,
                None,
                None,
                datetime.fromisoformat(observed),
            )
            for ad, group, observed in state["owners"]
        )
        refs = {ad.ref for ad in ads}
        materials = tuple(_usage(value) for value in state["materials"])
        if (
            not ads
            or len(refs) != len(ads)
            or len({ad.parent_ref.remote_id for ad in ads if ad.parent_ref})
            > _MAX_GROUPS
            or set(run.query.get("parent_ids", ())) != {ref.remote_id for ref in refs}
            or any(material.use_ref.ad_ref not in refs for material in materials)
        ):
            raise _invalid()
        return ads, materials
    except KeyError, TypeError, ValueError, AttributeError:
        raise _invalid() from None


def material_child_provider_pages(
    session: Session, run: AdDirectoryRun, pages: list[AdDirectoryPage]
) -> list[AdDirectoryPage]:
    """只有核验过自身上下文的子运行可剥离保留页，普通分页仍须完整连通。"""
    if not is_material_child(run):
        return pages
    material_child_scope(session, run)
    if not any(page.page == MATERIAL_CONTEXT_PAGE for page in pages):
        raise _invalid()
    return [page for page in pages if page.page != MATERIAL_CONTEXT_PAGE]


def ensure_material_directory_children(
    session: Session, *, source: AdDirectoryRun, pages: list[AdDirectoryPage]
) -> int:
    """主发布同事务创建去重子运行；只冻结已证实父链，不再读取主端点。"""
    if (
        not settings.ADS_SYNC_ENABLED
        or source.channel != "OFFICIAL_API"
        or source.kind != "ad"
        or source.ad_type != "SMART_PLUS"
    ):
        return 0
    owners: dict[str, tuple[str, str]] = {}
    missing_groups: set[str] = set()
    bridges: dict[tuple, dict] = {}
    for page in pages:
        for item in page.items:
            if item["ref"]["kind"] != "ad":
                continue
            ref = _ref(item["ref"])
            parent = _ref(item["parent_ref"]) if item.get("parent_ref") else None
            if (
                ref.tenant_id != source.tenant_id
                or ref.advertiser_id != source.advertiser_id
            ):
                raise _invalid()
            if parent is None:
                continue  # 没有真实父组时保留缺口，不能猜测筛选身份。
            if (
                parent.kind != "adgroup"
                or parent.tenant_id != ref.tenant_id
                or parent.advertiser_id != ref.advertiser_id
            ):
                raise _invalid()
            value = (parent.remote_id, item["observed_at"])
            if ref.remote_id in owners and owners[ref.remote_id][0] != value[0]:
                raise _invalid()
            owners[ref.remote_id] = value
            if page.coverage in {"PARTIAL_MATERIALS", "MATERIALS_INCOMPLETE"}:
                missing_groups.add(parent.remote_id)
        for data in page.materials:
            if not data.get("creative_ids"):
                continue
            material = _usage(data)
            use = material.use_ref
            key = (
                use.ad_ref,
                use.platform_material_id,
                use.ad_material_id,
                use.material_type,
            )
            previous = bridges.get(key)
            if previous is None:
                bridges[key] = dict(data)
            else:
                previous["creative_ids"] = sorted(
                    set(previous["creative_ids"]) | set(data["creative_ids"])
                )
    created = []
    groups = sorted(missing_groups)
    for offset in range(0, len(groups), _MAX_GROUPS):
        selected_groups = set(groups[offset : offset + _MAX_GROUPS])
        selected = {
            ad: value for ad, value in owners.items() if value[0] in selected_groups
        }
        # 每个组只属于一个子运行；request/id确定性绑定源运行，事务重试不会重复展开。
        child_id = uuid5(source.id, json.dumps(sorted(selected_groups)))
        if session.get(AdDirectoryRun, child_id) is not None:
            continue
        query = {
            "advertiser_id": source.advertiser_id,
            "kind": "creative",
            "ad_type": "SMART_PLUS",
            "page": 1,
            "page_size": 1000,
            "ids": [],
            "parent_ids": sorted(selected),
            "include_deleted": source.query.get("include_deleted", False),
            SERVICE_KEY: IDENTITY_SERVICE,
            MATERIAL_CHILD_KEY: {
                "source_run_id": str(source.id),
                "context_page": MATERIAL_CONTEXT_PAGE,
            },
        }
        child = AdDirectoryRun(
            id=child_id,
            request_id=child_id,
            tenant_id=source.tenant_id,
            advertiser_id=source.advertiser_id,
            bc_id=source.bc_id,
            actor_id=source.actor_id,
            connection_id=source.connection_id,
            channel=source.channel,
            frozen_route=dict(source.frozen_route),
            workload=source.workload,
            kind="creative",
            ad_type="SMART_PLUS",
            query=query,
            partition_key=_partition_key(query),
            claim_generation=1,
            # 补读是原请求的未完成部分，沿用排队年龄，避免每次拆分重新落到队尾。
            created_at=source.created_at,
        )
        session.add(child)
        session.flush()
        # 大冻结桥只在执行/发布时加载；热调度查询不应反复解压整份素材上下文。
        session.add(
            AdDirectoryPage(
                run_id=child.id,
                page=MATERIAL_CONTEXT_PAGE,
                tenant_id=child.tenant_id,
                advertiser_id=child.advertiser_id,
                claim_generation=1,
                complete=False,
                coverage="MATERIAL_CONTEXT",
                items=[],
                materials=[],
                evidence={
                    MATERIAL_CHILD_KEY: {
                        "child_run_id": str(child.id),
                        "source_run_id": str(source.id),
                        "scope_signature": _scope_signature(source),
                        "owners": [[ad, *selected[ad]] for ad in sorted(selected)],
                        "materials": [
                            data
                            for key, data in bridges.items()
                            if key[0].remote_id in selected
                        ],
                    }
                },
            )
        )
        session.flush()
        material_child_scope(session, child)
        created.append(child)
    return len(created)


def filter_current_child_materials(
    session: Session,
    *,
    run: AdDirectoryRun,
    materials: list[dict],
    version: int,
    published_objects: list[AdObject],
) -> tuple[list[dict], bool]:
    """旧主桥及旧子页不能以晚发布版本覆盖新观察，缺口仍留在运行审计。"""
    scope = material_child_scope(session, run)
    if scope is None:
        return materials, False
    ads, _bridges = scope
    expected = {ad.ref.remote_id: ad.observed_at for ad in ads}
    observed = _owner_observations(session, run, list(expected))
    current_owners = {
        remote_id
        for remote_id, value in observed.items()
        if value <= expected[remote_id]
    }
    # 发布已窄回读实际胜出的对象及版本；复用结果，避免每500创意重复触盘。
    # published_version必须在发布回读中显式加载，不能改成逐对象懒加载。
    current_creatives = {
        obj.remote_id: obj.parent_remote_id
        for obj in published_objects
        if obj.tenant_id == run.tenant_id
        and obj.advertiser_id == run.advertiser_id
        and obj.kind == "creative"
        and obj.published_version == version
    }
    accepted = []
    for row in materials:
        owner = row["use_ref"]["ad_ref"]["remote_id"]
        ids = row.get("creative_ids", [])
        if (
            owner in current_owners
            and ids
            and all(current_creatives.get(cid) == owner for cid in ids)
        ):
            accepted.append(row)
    return accepted, len(accepted) != len(materials)


def _owner_observations(
    session: Session, run: AdDirectoryRun, owner_ids: list[str]
) -> dict[str, datetime]:
    observed = {}
    for offset in range(0, len(owner_ids), 500):
        observed.update(
            session.exec(
                select(AdObject.remote_id, AdObject.observed_at).where(
                    AdObject.tenant_id == run.tenant_id,
                    AdObject.advertiser_id == run.advertiser_id,
                    AdObject.kind == "ad",
                    col(AdObject.remote_id).in_(owner_ids[offset : offset + 500]),
                )
            ).all()
        )
    return observed


def material_child_superseded(session: Session, run: AdDirectoryRun) -> bool:
    """全体冻结主观察确已被替换时提前让出；缺失事实不能视为较新证据。"""
    scope = material_child_scope(session, run)
    if scope is None:
        return False
    ads, _bridges = scope
    observed = _owner_observations(session, run, [ad.ref.remote_id for ad in ads])
    return all(
        ad.ref.remote_id in observed and observed[ad.ref.remote_id] > ad.observed_at
        for ad in ads
    )
