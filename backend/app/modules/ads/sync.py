"""广告目录分页暂存与发布。

采集和发布严格分成两个事务：采集器只写冻结运行的页，发布器在确认页链完整、
当前 claim 仍然有效并重新核验冻结路由后，才把远端观察投影到目录表。这样网络
请求期间连接被撤销时，旧 worker 不能继续制造可见版本。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import inspect, text, tuple_
from sqlalchemy.orm import QueryableAttribute, load_only
from sqlalchemy.orm.util import identity_key
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import DirectoryPage, EntityRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory import append_campaign_name_projections
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.directory_services import (
    STATUS_SERVICE,
    directory_service,
    promote_status_requests,
)
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.tenants.models import Tenant


def _ref_json(ref: EntityRef | None) -> dict[str, str] | None:
    if ref is None:
        return None
    return {
        "tenant_id": str(ref.tenant_id),
        "advertiser_id": ref.advertiser_id,
        "kind": ref.kind,
        "remote_id": ref.remote_id,
    }


def _json_value(value: Any) -> Any:
    """递归规范化配置中的 UUID、时间和定点数，避免把 psycopg 当序列化器。"""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _entity_json(entity: Any) -> dict[str, Any]:
    """将 A3 frozen DTO 转为 JSONB；UUID 和 datetime 不能直接交给 psycopg。"""
    return {
        "ref": _ref_json(entity.ref),
        "parent_ref": _ref_json(entity.parent_ref),
        "ad_type": entity.ad_type,
        "name": entity.name,
        "configuration": _json_value(entity.configuration),
        "operation_status": entity.operation_status,
        "review_status": entity.review_status,
        "delivery_status": entity.delivery_status,
        "observed_at": entity.observed_at.isoformat(),
    }


def _material_json(material: Any) -> dict[str, Any]:
    use = material.use_ref
    return {
        "use_ref": {
            "ad_ref": _ref_json(use.ad_ref),
            "platform_material_id": use.platform_material_id,
            "ad_material_id": use.ad_material_id,
            "material_type": use.material_type,
        },
        "local_material_id": (
            str(material.local_material_id) if material.local_material_id else None
        ),
        "operation_status": material.operation_status,
        "complete": material.complete,
        "name": material.name,
        "creative_ids": list(material.creative_ids),
    }


def _evidence_json(page: DirectoryPage) -> dict[str, Any]:
    return asdict(page.evidence)


def _domain(code: str, message: str) -> DomainError:
    return DomainError(code, message)


def _page_payload(
    page: DirectoryPage,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [_entity_json(item) for item in page.items],
        [_material_json(item) for item in page.materials],
    )


def _same_staged_page(
    row: AdDirectoryPage,
    *,
    page: DirectoryPage,
    claim_generation: int,
    require_claim: bool = True,
) -> bool:
    items, materials = _page_payload(page)
    coverage = _coverage(page)
    return (
        (not require_claim or row.claim_generation == claim_generation)
        and row.next_page == page.next_page
        and row.complete == page.complete
        and row.items == items
        and row.materials == materials
        and row.evidence == _evidence_json(page)
        and row.coverage == coverage
        and row.missing_reason == page.material_missing_reason
    )


def _coverage(page: DirectoryPage) -> str:
    if not page.complete:
        return "PARTIAL_MATERIALS" if not page.materials_complete else "PARTIAL"
    return "COMPLETE_MATERIALS" if page.materials_complete else "MATERIALS_INCOMPLETE"


def stage_directory_page(
    session: Session,
    *,
    run_id: UUID,
    page: DirectoryPage,
    claim_generation: int,
) -> None:
    """暂存一页；相同页重试幂等，冲突内容永远不能覆盖证据。"""
    if type(claim_generation) is not int or claim_generation < 1:
        raise _domain("directory_claim_invalid", "目录 claim 代数无效")
    # 先锁运行行，再检查代数并写页；claim 替换无法插入旧代数的页。
    run = session.exec(
        select(AdDirectoryRun)
        .where(AdDirectoryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None:
        raise _domain("directory_run_not_found", "目录同步运行不存在")
    if run.published_version is not None or run.status in {
        "COMPLETE",
        "CANCELLED",
        "STALE",
    }:
        raise _domain("directory_run_closed", "目录同步运行已经结束")
    if page.page < 1:
        raise _domain("directory_page_invalid", "目录页码无效")
    from app.modules.ads.material_directory_children import (
        MATERIAL_CONTEXT_PAGE,
        is_material_child,
    )

    if is_material_child(run) and (
        page.page >= MATERIAL_CONTEXT_PAGE
        or page.next_page is not None
        and page.next_page >= MATERIAL_CONTEXT_PAGE
    ):
        raise _domain("directory_page_invalid", "素材子页不能使用上下文保留页码")
    if claim_generation > run.claim_generation:
        raise _domain("directory_claim_lost", "目录同步 claim 已被替换")
    if claim_generation < run.claim_generation:
        existing = session.get(
            AdDirectoryPage, (run_id, page.page), populate_existing=True
        )
        if existing is None or not _same_staged_page(
            existing, page=page, claim_generation=claim_generation
        ):
            raise _domain("directory_claim_lost", "旧目录 worker 不能追加暂存页")
        return

    for item in page.items:
        if (
            item.ref.tenant_id != run.tenant_id
            or item.ref.advertiser_id != run.advertiser_id
        ):
            raise _domain("directory_scope_mismatch", "目录对象不属于冻结账户")
        if item.ad_type != run.ad_type:
            # 自动化类型决定物理读取端点；错误类型不能混入冻结分区。
            raise _domain(
                "directory_type_mismatch", "目录对象自动化类型与冻结分区不一致"
            )
        if item.ref.kind != run.kind and not (
            run.kind == "ad"
            and run.ad_type == "SMART_PLUS"
            and item.ref.kind == "creative"
            and item.parent_ref is not None
            and item.parent_ref.kind == "ad"
        ):
            raise _domain("directory_kind_mismatch", "目录对象类型不属于本次分区")
    for usage in page.materials:
        if (
            usage.use_ref.ad_ref.tenant_id != run.tenant_id
            or usage.use_ref.ad_ref.advertiser_id != run.advertiser_id
        ):
            raise _domain("directory_scope_mismatch", "素材使用不属于冻结账户")

    items, materials = _page_payload(page)
    existing = session.get(AdDirectoryPage, (run_id, page.page), populate_existing=True)
    if existing is not None:
        # 页证据是不可变事实；新的 claim 可以继续使用旧代数已经接受的相同页。
        if _same_staged_page(
            existing, page=page, claim_generation=claim_generation, require_claim=False
        ):
            return
        raise _domain("directory_page_conflict", "同一目录页已有不同暂存证据")
    terminal = session.exec(
        select(AdDirectoryPage)
        .where(
            col(AdDirectoryPage.run_id) == run_id,
            col(AdDirectoryPage.complete).is_(True),
        )
        .with_for_update()
    ).first()
    if terminal is not None and (
        page.page > terminal.page or (page.complete and page.page != terminal.page)
    ):
        raise _domain("directory_run_terminal", "目录运行已暂存末页")

    session.add(
        AdDirectoryPage(
            run_id=run.id,
            page=page.page,
            tenant_id=run.tenant_id,
            advertiser_id=run.advertiser_id,
            claim_generation=claim_generation,
            next_page=page.next_page,
            complete=page.complete,
            evidence=_evidence_json(page),
            coverage=_coverage(page),
            missing_reason=page.material_missing_reason,
            items=items,
            materials=materials,
        )
    )
    session.flush()


def _parse_ref(value: Any) -> tuple[UUID, str, str, str] | None:
    if not isinstance(value, dict):
        return None
    try:
        tenant_id = UUID(str(value["tenant_id"]))
        advertiser_id = str(value["advertiser_id"])
        kind = str(value["kind"])
        remote_id = str(value["remote_id"])
    except KeyError, TypeError, ValueError:
        return None
    if (
        not advertiser_id
        or not remote_id
        or kind not in {"campaign", "adgroup", "ad", "creative"}
    ):
        return None
    return tenant_id, advertiser_id, kind, remote_id


def _observed(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return fallback


def _route_for_run(run: AdDirectoryRun) -> FrozenTikTokRoute:
    try:
        route = FrozenTikTokRoute.model_validate(run.frozen_route)
    except (TypeError, ValueError) as exc:
        raise _domain("frozen_route_changed", "目录运行的冻结路由无效") from exc
    if (
        route.tenant_id != run.tenant_id
        or route.bc_id != run.bc_id
        or route.connection_id != run.connection_id
        or route.channel != run.channel
    ):
        raise _domain("frozen_route_scope_mismatch", "目录运行与冻结路由范围不一致")
    return route


def _ensure_route_authority(
    session: Session, run: AdDirectoryRun, route: FrozenTikTokRoute
) -> None:
    # 后台发布没有 HTTP context，仍以运行中冻结的 actor 重建当前租户权限。
    verify_route(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        route=route,
        advertiser_id=run.advertiser_id,
        capability="read",
    )


def _complete_chain(pages: list[AdDirectoryPage]) -> list[AdDirectoryPage]:
    by_number = {row.page: row for row in pages}
    if not by_number or 1 not in by_number:
        raise _domain("directory_incomplete", "目录暂存缺少第一页")
    ordered: list[AdDirectoryPage] = []
    page_no = 1
    seen: set[int] = set()
    while True:
        row = by_number.get(page_no)
        if row is None or row.page in seen:
            raise _domain("directory_incomplete", "目录分页存在缺页")
        seen.add(row.page)
        ordered.append(row)
        if row.complete:
            if row.next_page is not None:
                raise _domain("directory_incomplete", "完整页不能继续分页")
            break
        if row.next_page is None:
            raise _domain("directory_incomplete", "目录中间页没有后续页")
        page_no = row.next_page
    if len(seen) != len(by_number):
        raise _domain("directory_incomplete", "目录暂存包含断开的页")
    return ordered


# 每条语句限制参数量和 JSONB 负载，避免把大目录一次塞进单条 INSERT。
_PUBLISH_BATCH_SIZE = 500


def _batches[T](items: list[T]):
    for start in range(0, len(items), _PUBLISH_BATCH_SIZE):
        yield items[start : start + _PUBLISH_BATCH_SIZE]


def _entity_values(
    data: dict[str, Any], *, run: AdDirectoryRun, version: int, fallback: datetime
) -> dict[str, Any]:
    ref = _parse_ref(data.get("ref"))
    if ref is None:
        raise _domain("directory_page_invalid", "暂存对象身份无效")
    tenant_id, advertiser_id, kind, remote_id = ref
    if tenant_id != run.tenant_id or advertiser_id != run.advertiser_id:
        raise _domain("directory_scope_mismatch", "暂存对象超出冻结账户")
    parent = _parse_ref(data.get("parent_ref"))
    if parent is not None and parent[:2] != ref[:2]:
        raise _domain("directory_scope_mismatch", "暂存父对象超出冻结账户")
    return {
        "tenant_id": tenant_id,
        "advertiser_id": advertiser_id,
        "kind": kind,
        "remote_id": remote_id,
        "parent_kind": parent[2] if parent else None,
        "parent_remote_id": parent[3] if parent else None,
        "ad_type": str(data.get("ad_type") or run.ad_type),
        "name": str(data.get("name") or ""),
        "configuration": data.get("configuration")
        if isinstance(data.get("configuration"), dict)
        else {},
        "operation_status": data.get("operation_status"),
        "review_status": data.get("review_status"),
        "delivery_status": data.get("delivery_status"),
        "observed_at": _observed(data.get("observed_at"), fallback),
        "published_version": version,
        "source_connection_id": run.connection_id,
        "source_channel": run.channel,
    }


def _publish_entities(
    session: Session,
    entities: list[dict[str, Any]],
    *,
    run: AdDirectoryRun,
    version: int,
    fallback: datetime,
) -> list[AdObject]:
    # 跨页重复对象先合并；旧观察不能覆盖新观察，同时间沿用最后一条。
    values: dict[tuple[str, str], dict[str, Any]] = {}
    for data in entities:
        value = _entity_values(data, run=run, version=version, fallback=fallback)
        key = (value["kind"], value["remote_id"])
        previous = values.get(key)
        if previous is None or previous["observed_at"] <= value["observed_at"]:
            values[key] = value
    result: list[AdObject] = []
    for batch in _batches(list(values.values())):
        # 对象配置可能很大；固定SQL避免每500条重新编译8000个参数，发布仍原子。
        session.connection().execute(
            text("""
            INSERT INTO ad_object (
              tenant_id,advertiser_id,kind,remote_id,parent_kind,parent_remote_id,
              ad_type,name,configuration,operation_status,review_status,delivery_status,
              observed_at,published_version,source_connection_id,source_channel
            )
            SELECT tenant_id,advertiser_id,kind,remote_id,parent_kind,parent_remote_id,
              ad_type,name,configuration,operation_status,review_status,delivery_status,
              observed_at,published_version,source_connection_id,source_channel
            FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS incoming (
              tenant_id uuid,advertiser_id text,kind text,remote_id text,
              parent_kind text,parent_remote_id text,ad_type text,name text,
              configuration jsonb,operation_status text,review_status text,
              delivery_status text,observed_at timestamptz,published_version integer,
              source_connection_id uuid,source_channel text
            )
            ON CONFLICT (tenant_id,advertiser_id,kind,remote_id) DO UPDATE SET
              parent_kind=EXCLUDED.parent_kind,parent_remote_id=EXCLUDED.parent_remote_id,
              ad_type=EXCLUDED.ad_type,name=EXCLUDED.name,
              configuration=EXCLUDED.configuration,operation_status=EXCLUDED.operation_status,
              review_status=EXCLUDED.review_status,delivery_status=EXCLUDED.delivery_status,
              observed_at=EXCLUDED.observed_at,published_version=EXCLUDED.published_version,
              source_connection_id=EXCLUDED.source_connection_id,
              source_channel=EXCLUDED.source_channel
            WHERE ad_object.observed_at<=EXCLUDED.observed_at
            """),
            {"rows": json.dumps(batch, default=str, ensure_ascii=False)},
        )
        for item in batch:
            loaded = session.identity_map.get(
                identity_key(
                    AdObject,
                    (
                        item["tenant_id"],
                        item["advertiser_id"],
                        item["kind"],
                        item["remote_id"],
                    ),
                )
            )
            if loaded is not None:
                # 原已加载配置也必须失效；窄回读不应留下同事务中的旧完整对象。
                session.expire(loaded)
        # 被较新事实挡住的对象仍参与父目录及名称审计，保持原有发布语义。
        result.extend(
            session.exec(
                select(AdObject)
                .where(
                    AdObject.tenant_id == run.tenant_id,
                    AdObject.advertiser_id == run.advertiser_id,
                    tuple_(col(AdObject.kind), col(AdObject.remote_id)).in_(
                        [(item["kind"], item["remote_id"]) for item in batch]
                    ),
                )
                .options(
                    load_only(
                        cast(QueryableAttribute[Any], AdObject.parent_kind),
                        cast(QueryableAttribute[Any], AdObject.parent_remote_id),
                        cast(QueryableAttribute[Any], AdObject.name),
                        cast(QueryableAttribute[Any], AdObject.published_version),
                    )
                )
                .execution_options(populate_existing=True)
            ).all()
        )
    return result


def _material_values(
    data: dict[str, Any], *, run: AdDirectoryRun, version: int
) -> dict[str, Any]:
    use = data.get("use_ref")
    ad_ref = _parse_ref(use.get("ad_ref")) if isinstance(use, dict) else None
    if (
        not isinstance(use, dict)
        or ad_ref is None
        or ad_ref[0] != run.tenant_id
        or ad_ref[1] != run.advertiser_id
        or ad_ref[2] != "ad"
    ):
        raise _domain("directory_scope_mismatch", "暂存素材使用身份无效")
    try:
        local_material_id = (
            UUID(data["local_material_id"]) if data.get("local_material_id") else None
        )
    except (TypeError, ValueError) as exc:
        raise _domain("directory_page_invalid", "本地素材身份无效") from exc
    return {
        "id": uuid4(),
        "tenant_id": run.tenant_id,
        "advertiser_id": run.advertiser_id,
        "ad_remote_id": ad_ref[3],
        "platform_material_id": str(use.get("platform_material_id") or ""),
        "ad_material_id": use.get("ad_material_id"),
        "material_type": str(use.get("material_type") or ""),
        "name": str(data.get("name") or ""),
        "creative_ids": list(data.get("creative_ids") or []),
        "local_material_id": local_material_id,
        "operation_status": data.get("operation_status"),
        "complete": bool(data.get("complete")),
        "published_version": version,
    }


def _publish_materials(
    session: Session,
    materials: list[dict[str, Any]],
    *,
    run: AdDirectoryRun,
    version: int,
) -> None:
    identity = (
        "tenant_id",
        "advertiser_id",
        "ad_remote_id",
        "platform_material_id",
        "ad_material_id",
        "material_type",
    )
    values: dict[tuple, dict[str, Any]] = {}
    for data in materials:
        value = _material_values(data, run=run, version=version)
        key = tuple(value[field] for field in identity)
        # 不完整响应既不能覆盖数据库已有记录，也不能清空本批更完整的观察。
        if key not in values or value["complete"]:
            values[key] = value
    for batch in _batches(list(values.values())):
        # 素材无需返回ORM对象。固定SQL+单个JSON参数避免每500行重新编译6500个
        # 绑定参数；仍在目录原子事务内写入，完整性与版本冲突规则保持一致。
        session.connection().execute(
            text("""
            INSERT INTO ad_material_reference (
              id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
              ad_material_id,material_type,name,creative_ids,local_material_id,
              operation_status,complete,published_version
            )
            SELECT id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
              ad_material_id,material_type,name,creative_ids,local_material_id,
              operation_status,complete,published_version
            FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS incoming (
              id uuid,tenant_id uuid,advertiser_id text,ad_remote_id text,
              platform_material_id text,ad_material_id text,material_type text,
              name text,creative_ids jsonb,local_material_id uuid,
              operation_status text,complete boolean,published_version integer
            )
            ON CONFLICT ON CONSTRAINT uq_ad_material_usage DO UPDATE SET
              name=EXCLUDED.name,creative_ids=EXCLUDED.creative_ids,
              local_material_id=EXCLUDED.local_material_id,
              operation_status=EXCLUDED.operation_status,
              complete=EXCLUDED.complete,published_version=EXCLUDED.published_version
            WHERE EXCLUDED.complete
            """),
            {"rows": json.dumps(batch, default=str, ensure_ascii=False)},
        )
    # Core批量写不自动刷新identity map；只失效本批已加载的素材，避免后续同事务
    # 查询读到旧完整性/版本。身份列未加载时保守失效，不能为识别身份逐行查库。
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AdMaterialReference):
            mapped = inspect(obj)
            assert mapped is not None
            state = mapped.dict
            if (
                not all(field in state for field in identity)
                or tuple(state[field] for field in identity) in values
            ):
                session.expire(obj)


def _queue_missing_parents(
    session: Session, *, run: AdDirectoryRun, objects: list[AdObject]
) -> None:
    parents = sorted(
        {
            (obj.parent_kind, obj.parent_remote_id)
            for obj in objects
            if obj.parent_kind is not None and obj.parent_remote_id is not None
        }
    )
    missing = set(parents)
    for batch in _batches(parents):
        missing.difference_update(
            session.exec(
                select(AdObject.kind, AdObject.remote_id).where(
                    AdObject.tenant_id == run.tenant_id,
                    AdObject.advertiser_id == run.advertiser_id,
                    tuple_(col(AdObject.kind), col(AdObject.remote_id)).in_(batch),
                )
            ).all()
        )
    if missing:
        # 同账户多个子对象并发发现缺父级时，只保留一份相同冻结授权的补读。
        # 锁仅覆盖本地发布事务，不持有网络请求；不能跨路由复用别人的读取证据。
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {
                "scope": f"directory-parent:{run.tenant_id}:{run.bc_id}:{run.advertiser_id}"
            },
        )
        include_deleted = bool(run.query.get("include_deleted"))
        pending = session.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == run.tenant_id,
                AdDirectoryRun.bc_id == run.bc_id,
                AdDirectoryRun.advertiser_id == run.advertiser_id,
                AdDirectoryRun.actor_id == run.actor_id,
                AdDirectoryRun.frozen_route == run.frozen_route,
                AdDirectoryRun.ad_type == run.ad_type,
                col(AdDirectoryRun.status).in_(
                    ["QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE"]
                ),
            )
        ).all()
        status_requests = []
        for pending_run in pending:
            if (run.workload != "background" and pending_run.workload != "live") or (
                include_deleted and not pending_run.query.get("include_deleted")
            ):
                continue
            covered = missing.intersection(
                (pending_run.kind, remote_id)
                for remote_id in pending_run.query.get("ids", ())
            )
            if covered and directory_service(run) == STATUS_SERVICE:
                status_requests.append(pending_run)
            missing.difference_update(covered)
        promote_status_requests(session, status_requests)
        if not missing:
            return
        # 父级与子级属于同一广告类型；只补父级，不能顺带再扫子级或其它两种类型。
        # 历史子对象的父级也可能已经删除，须继承原查询的可见范围。
        request_directory(
            session,
            tenant_id=run.tenant_id,
            bc_id=run.bc_id,
            frozen_route=run.frozen_route,
            actor_id=run.actor_id,
            advertiser_ids=[run.advertiser_id],
            scope="targeted",
            include_deleted=include_deleted,
            workload=run.workload,
            service=directory_service(run),
            ad_types={run.advertiser_id: {run.ad_type: {kind for kind, _ in missing}}},
            refs=[
                EntityRef(run.tenant_id, run.advertiser_id, cast(Any, kind), remote_id)
                for kind, remote_id in sorted(missing)
            ],
        )


def _next_directory_version(session: Session) -> int:
    # 序列从迁移时的最大历史版本之后开始；回滚允许跳号，不重复分配版本。
    return (
        session.connection()
        .execute(text("SELECT nextval('ad_directory_published_version_seq')"))
        .scalar_one()
    )


def directory_run_superseded(session: Session, run: AdDirectoryRun) -> bool:
    """沿用发布顺序判定；已被更新发布取代的运行不再占读取与恢复窗口。"""
    from app.modules.ads.material_directory_children import (
        is_material_child,
        material_child_superseded,
    )

    # 子运行只覆盖冻结广告组的部分素材，不是全账户创意快照；不同子范围不能互相淘汰。
    if is_material_child(run):
        return material_child_superseded(session, run)
    if run.request_sequence is None:
        return False
    from app.modules.ads.directory_supersession import superseding_directory_predicate

    return (
        session.exec(
            select(AdDirectoryRun.id)
            .where(superseding_directory_predicate(run, AdDirectoryRun))
            .limit(1)
        ).first()
        is not None
    )


def publish_directory(session: Session, *, run_id: UUID, claim_generation: int) -> int:
    """验证完整分页后原子发布；返回本运行使用的单一 published_version。"""
    if type(claim_generation) is not int or claim_generation < 1:
        raise _domain("directory_claim_invalid", "目录 claim 代数无效")
    with session.begin_nested():
        run = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == run_id).with_for_update()
        ).one_or_none()
        if run is None:
            raise _domain("directory_run_not_found", "目录同步运行不存在")
        if run.published_version is not None:
            if run.claim_generation != claim_generation:
                raise _domain("directory_claim_lost", "目录同步 claim 已被替换")
            return run.published_version
        if run.claim_generation != claim_generation:
            raise _domain("directory_claim_lost", "目录同步 claim 已被替换")
        route = _route_for_run(run)
        _ensure_route_authority(session, run, route)
        pages = session.exec(
            select(AdDirectoryPage)
            .where(AdDirectoryPage.run_id == run.id)
            .order_by(col(AdDirectoryPage.page))
            .with_for_update()
        ).all()
        from app.modules.ads.material_directory_children import (
            material_child_provider_pages,
        )

        ordered = _complete_chain(
            material_child_provider_pages(session, run, list(pages))
        )
        # 版本来自数据库序列；同账户的请求排序用账户锁保护。整租户行锁会让
        # 一个大历史目录挡住其他账户的实时发布，不能作为多账户的执行容量。
        tenant = session.exec(
            select(Tenant).where(Tenant.id == run.tenant_id)
        ).one_or_none()
        if tenant is None:
            raise _domain("tenant_forbidden", "目录运行的租户不存在")
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"directory-publish:{run.tenant_id}:{run.advertiser_id}"},
        )
        # 必须在租户版本锁之后重查，避免旧运行检查完毕后等待锁期间被新运行发布。
        if directory_run_superseded(session, run):
            raise _domain("directory_run_stale", "较早目录运行不能晚于新运行发布")
        version = _next_directory_version(session)
        fallback = datetime.now(UTC)
        entities: list[dict[str, Any]] = []
        materials: list[dict[str, Any]] = []
        for page in ordered:
            entities.extend(page.items)
            materials.extend(page.materials)
        published_objects = _publish_entities(
            session, entities, run=run, version=version, fallback=fallback
        )
        from app.modules.ads.material_directory_children import (
            filter_current_child_materials,
        )

        materials, stale_materials = filter_current_child_materials(
            session,
            run=run,
            materials=materials,
            version=version,
            published_objects=published_objects,
        )
        stale_owner_materials = False
        if run.kind in {"ad", "creative"}:
            # 部分重叠查询可以晚发布未覆盖的广告；但输给新观察的广告素材不能
            # 借本次更大发布版本覆盖新事实。复用已回读版本，不增加逐广告查询。
            winning_ads = {
                obj.remote_id
                for obj in published_objects
                if obj.kind == "ad" and obj.published_version == version
            }
            # 普通 creative 也携带素材；只接受本轮真实获胜且归属相符的创意。
            # child 已先经过冻结广告观察保护，此处同样不能放宽其创意身份。
            winning_creatives = {
                obj.remote_id: obj.parent_remote_id
                for obj in published_objects
                if obj.kind == "creative"
                and obj.parent_kind == "ad"
                and obj.published_version == version
            }
            accepted = []
            for material in materials:
                use = material.get("use_ref")
                ref = _parse_ref(use.get("ad_ref")) if isinstance(use, dict) else None
                creative_ids = material.get("creative_ids")
                current = ref is not None and (
                    ref[3] in winning_ads
                    if run.kind == "ad"
                    else isinstance(creative_ids, list)
                    and bool(creative_ids)
                    and all(
                        winning_creatives.get(creative_id) == ref[3]
                        for creative_id in creative_ids
                    )
                )
                if current:
                    accepted.append(material)
                else:
                    # 失效观察也不能掩盖原本应报错的非法暂存身份。
                    _material_values(material, run=run, version=version)
                    stale_owner_materials = True
            materials = accepted
            stale_materials = stale_materials or stale_owner_materials
        _publish_materials(session, materials, run=run, version=version)
        _queue_missing_parents(session, run=run, objects=published_objects)
        append_campaign_name_projections(
            session,
            campaigns=tuple(
                (obj.ref, obj.name)
                for obj in published_objects
                if obj.kind == "campaign"
            ),
            parser_revision=1,
        )
        material_incomplete = stale_materials or any(
            page.coverage in {"PARTIAL_MATERIALS", "MATERIALS_INCOMPLETE"}
            for page in ordered
        )
        run.status = "COMPLETE"
        # 分页已经完整不代表素材子查询完整；运行摘要保留这两个覆盖维度。
        run.coverage = "MATERIALS_INCOMPLETE" if material_incomplete else "COMPLETE"
        run.observed_at = fallback
        run.published_version = version
        run.completed_at = fallback
        run.missing_reason = next(
            (
                page.missing_reason
                for page in ordered
                if page.missing_reason is not None
            ),
            None,
        )
        if stale_materials:
            run.missing_reason = (
                "material_owner_observation_stale"
                if stale_owner_materials
                else "material_child_observation_stale"
            )
        session.add(run)
        session.flush()
        for page in ordered:
            page.published_version = version
            session.add(page)
        session.flush()
        # 子运行与主目录同事务提交；执行入口在提交后直接补位，避免累积 Control 唤醒。
        from app.modules.ads.material_directory_children import (
            ensure_material_directory_children,
        )

        ensure_material_directory_children(session, source=run, pages=ordered)
    return version
