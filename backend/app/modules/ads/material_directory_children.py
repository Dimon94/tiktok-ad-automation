"""Smart+ 素材补读复用目录运行/分页；私有冻结范围不进入通用 DirectoryQuery。"""

import json
from datetime import datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
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
from app.modules.ads.directory_services import (
    IDENTITY_SERVICE,
    SERVICE_KEY,
    STATUS_SERVICE,
    directory_service,
)
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
    *,
    include_materials: bool = True,
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
            # 领取也不能急切解码整个冻结桥：十万条素材的JSON/值对象构造会
            # 使持锁事务连续空闲超过10秒。先读窄元数据，再以500行游标逐批
            # 取回并校验完整素材；读取保留值对象，发布仅校验，不放宽事务预算。
            row = (
                session.connection()
                .execute(
                    text("""
                    SELECT tenant_id,advertiser_id,coverage,complete,next_page,published_version,
                        items <> '[]'::jsonb AS items,materials <> '[]'::jsonb AS materials,
                        evidence #- '{material_child_v1,materials}' AS evidence,
                        jsonb_typeof(evidence->'material_child_v1'->'materials')='array' AS valid_materials
                    FROM ad_directory_page WHERE run_id=:run AND page=:page
                """),
                    {"run": run.id, "page": MATERIAL_CONTEXT_PAGE},
                )
                .mappings()
                .one_or_none()
            )
            context = SimpleNamespace(**row) if row is not None else None
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
        if not context.valid_materials:
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
        collected = []
        result = session.connection().execute(
            text("""
                SELECT value FROM ad_directory_page p CROSS JOIN LATERAL
                    jsonb_array_elements(p.evidence->'material_child_v1'->'materials') x(value)
                WHERE p.run_id=:run AND p.page=:page
            """).execution_options(yield_per=500),
            {"run": run.id, "page": MATERIAL_CONTEXT_PAGE},
        )
        try:
            for row in result:
                material = _usage(row[0])
                if material.use_ref.ad_ref not in refs:
                    raise _invalid()
                if include_materials:
                    collected.append(material)
        finally:
            result.close()
        materials = tuple(collected)
        if (
            not ads
            or len(refs) != len(ads)
            or len({ad.parent_ref.remote_id for ad in ads if ad.parent_ref})
            > _MAX_GROUPS
            or set(run.query.get("parent_ids", ())) != {ref.remote_id for ref in refs}
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
    material_child_scope(session, run, include_materials=False)
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
    # 状态轮询的素材字段不完整，并不代表产生了新的素材详情需求。
    # 保存主目录与现有桥接即可；真正缺关系的报表由request_link_directory以identity用途补读。
    # 避免每次状态刷新都把同一广告重新展开为整批视频/图片身份查询。
    if directory_service(source) == STATUS_SERVICE:
        return 0
    # source_run_id在JSON里没有外键；与归档DELETE共享父行锁防止悬空引用。
    if (
        session.exec(
            select(col(AdDirectoryRun.id))
            .where(
                col(AdDirectoryRun.id) == source.id,
                col(AdDirectoryRun.tenant_id) == source.tenant_id,
            )
            .with_for_update(read=True, key_share=True)
        ).first()
        is None
    ):
        raise _invalid()
    from app.modules.ads.sync import _complete_chain, _directory_payload_batches

    if any(page.run_id != source.id for page in pages):
        raise _invalid()
    _complete_chain(pages)
    # 校验流与SQL聚合共用持锁的完整源页；宽桥不再随着全部页堆在Python字典中。
    for batch in _directory_payload_batches(session, source, "items"):
        for item in batch:
            if item["ref"]["kind"] != "ad":
                continue
            ref = _ref(item["ref"])
            parent = _ref(item["parent_ref"]) if item.get("parent_ref") else None
            if (
                ref.tenant_id != source.tenant_id
                or ref.advertiser_id != source.advertiser_id
            ):
                raise _invalid()
            if parent is not None and (
                parent.kind != "adgroup"
                or parent.tenant_id != ref.tenant_id
                or parent.advertiser_id != ref.advertiser_id
            ):
                raise _invalid()
    for batch in _directory_payload_batches(session, source, "materials"):
        for data in batch:
            if data.get("creative_ids"):
                use = _usage(data).use_ref.ad_ref
                if (
                    use.tenant_id != source.tenant_id
                    or use.advertiser_id != source.advertiser_id
                    or use.kind != "ad"
                ):
                    raise _invalid()
    owners_table = f"child_owners_{uuid4().hex}"
    bridges_table = f"child_bridges_{uuid4().hex}"
    connection = session.connection()
    connection.execute(
        text(f"""
        CREATE TEMP TABLE {owners_table} ON COMMIT DROP AS
        SELECT x.value->'ref'->>'remote_id' AS ad,
            x.value->'parent_ref'->>'remote_id' AS parent,
            x.value->>'observed_at' AS observed, p.page, x.position,
            p.coverage IN ('PARTIAL_MATERIALS','MATERIALS_INCOMPLETE') AS missing
        FROM ad_directory_page p
        CROSS JOIN LATERAL jsonb_array_elements(p.items) WITH ORDINALITY x(value,position)
        WHERE p.run_id=:run AND x.value->'ref'->>'kind'='ad'
            AND jsonb_typeof(x.value->'parent_ref')='object'
    """),
        {"run": source.id},
    )
    if connection.execute(
        text(f"""
        SELECT 1 FROM {owners_table} GROUP BY ad HAVING count(DISTINCT parent)>1 LIMIT 1
    """)
    ).first():
        raise _invalid()
    connection.execute(text(f"CREATE INDEX ON {owners_table} (parent,ad)"))
    connection.execute(
        text(f"""
        CREATE TEMP TABLE {bridges_table} ON COMMIT DROP AS
        WITH raw AS (
            SELECT x.value AS payload,p.page,x.position,
                jsonb_build_array(x.value->'use_ref'->'ad_ref',
                    x.value->'use_ref'->'platform_material_id',
                    x.value->'use_ref'->'ad_material_id',x.value->'use_ref'->'material_type') AS identity
            FROM ad_directory_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.materials) WITH ORDINALITY x(value,position)
            WHERE p.run_id=:run AND jsonb_array_length(COALESCE(x.value->'creative_ids','[]'::jsonb))>0
        ), first_payload AS (
            SELECT DISTINCT ON (identity) identity,payload FROM raw ORDER BY identity,page,position
        ), ids AS (
            SELECT identity,jsonb_agg(DISTINCT cid.value ORDER BY cid.value) AS creative_ids
            FROM raw CROSS JOIN LATERAL jsonb_array_elements(payload->'creative_ids') cid(value)
            GROUP BY identity
        )
        SELECT f.payload->'use_ref'->'ad_ref'->>'remote_id' AS ad,
            jsonb_set(f.payload,'{{creative_ids}}',ids.creative_ids) AS payload
        FROM first_payload f JOIN ids USING(identity)
    """),
        {"run": source.id},
    )
    connection.execute(text(f"CREATE INDEX ON {bridges_table} (ad)"))
    created = 0
    # 这里只保留窄组身份，context中的全部素材由PG按子范围直接形成持久JSON。
    groups = (
        connection.execute(
            text(
                f"SELECT DISTINCT parent FROM {owners_table} WHERE missing ORDER BY parent"
            )
        )
        .scalars()
        .all()
    )
    for offset in range(0, len(groups), _MAX_GROUPS):
        selected_groups = set(groups[offset : offset + _MAX_GROUPS])
        selected = dict(
            tuple(row)
            for row in connection.execute(
                text(f"""
            SELECT DISTINCT ON (ad) ad,jsonb_build_array(parent,observed)
            FROM {owners_table} WHERE parent=ANY(:groups) ORDER BY ad,page DESC,position DESC
        """),
                {"groups": sorted(selected_groups)},
            ).all()
        )
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
            # 归档只需观察时间上界；精确owner身份已在query，不重复保存宽素材桥。
            material_owner_observed_through=max(
                datetime.fromisoformat(value[1]) for value in selected.values()
            ),
            # 补读是原请求的未完成部分，沿用排队年龄，避免每次拆分重新落到队尾。
            created_at=source.created_at,
        )
        session.add(child)
        session.flush()
        from app.modules.sync_control.directory_demand import register_directory_run

        register_directory_run(session, child, new_execution=True)
        connection.execute(
            text(f"""
            INSERT INTO ad_directory_page
                (run_id,page,tenant_id,advertiser_id,claim_generation,complete,coverage,
                 items,materials,evidence,observed_at)
            SELECT :child,:page,:tenant,:advertiser,1,false,'MATERIAL_CONTEXT','[]'::jsonb,'[]'::jsonb,
                jsonb_build_object(CAST(:key AS text),jsonb_build_object(
                    'child_run_id',CAST(:child AS text),'source_run_id',CAST(:source AS text),
                    'scope_signature',CAST(:signature AS text),
                    'owners',CAST(:owners AS jsonb),
                    'materials',COALESCE((SELECT jsonb_agg(payload) FROM {bridges_table} WHERE ad=ANY(:ads)),'[]'::jsonb)
                )),clock_timestamp()
        """),
            {
                "child": child.id,
                "page": MATERIAL_CONTEXT_PAGE,
                "tenant": child.tenant_id,
                "advertiser": child.advertiser_id,
                "key": MATERIAL_CHILD_KEY,
                "source": source.id,
                "signature": _scope_signature(source),
                "owners": json.dumps([[ad, *selected[ad]] for ad in sorted(selected)]),
                "ads": sorted(selected),
            },
        )
        created += 1
    return created


def filter_current_child_materials(
    session: Session,
    *,
    run: AdDirectoryRun,
    materials: list[dict],
    version: int,
    published_objects: list[AdObject],
    owner_cache: dict | None = None,
) -> tuple[list[dict], bool]:
    """旧主桥及旧子页不能以晚发布版本覆盖新观察，缺口仍留在运行审计。"""
    if owner_cache is None:
        owner_cache = {}
    if "current_owners" not in owner_cache:
        scope = material_child_scope(session, run, include_materials=False)
        if scope is None:
            owner_cache["current_owners"] = None
        else:
            ads, _bridges = scope
            expected = {ad.ref.remote_id: ad.observed_at for ad in ads}
            observed = _owner_observations(session, run, list(expected))
            owner_cache["current_owners"] = {
                remote_id
                for remote_id, value in observed.items()
                if value <= expected[remote_id]
            }
    current_owners = owner_cache["current_owners"]
    if current_owners is None:
        return materials, False
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
                select(col(AdObject.remote_id), col(AdObject.observed_at)).where(
                    col(AdObject.tenant_id) == run.tenant_id,
                    col(AdObject.advertiser_id) == run.advertiser_id,
                    col(AdObject.kind) == "ad",
                    col(AdObject.remote_id).in_(owner_ids[offset : offset + 500]),
                )
            ).all()
        )
    return observed


def material_child_superseded(
    session: Session,
    run: AdDirectoryRun,
    *,
    frozen_owners: tuple[AdEntity, ...] | None = None,
) -> bool:
    """全体冻结主观察确已被替换时提前让出；缺失事实不能视为较新证据。"""
    if frozen_owners is None:
        scope = material_child_scope(session, run, include_materials=False)
        if scope is None:
            return False
        frozen_owners, _bridges = scope
    # 调用方只在同次量子、相同冻结输入下提供已校验owner；当前事实必须重新查。
    ads = frozen_owners
    observed = _owner_observations(session, run, [ad.ref.remote_id for ad in ads])
    return all(
        ad.ref.remote_id in observed and observed[ad.ref.remote_id] > ad.observed_at
        for ad in ads
    )
