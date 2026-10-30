"""素材子运行的窄归档证明；冻结owner身份在query，时间上界独立保留。"""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select as sa_select
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.ads.material_directory_children import (
    MATERIAL_CHILD_KEY,
    MATERIAL_CONTEXT_PAGE,
    _scope_signature,
    is_material_child,
)
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun

MAX_OWNER_IDS = 100_000
OWNER_BATCH = 500


def _source_and_owners(
    session: Session, run: AdDirectoryRun, *, lock: bool
) -> tuple[AdDirectoryRun, list[str]] | None:
    """缺失或不匹配的源不能凭UUID/当前全局对象猜授权范围。"""
    marker = run.query.get(MATERIAL_CHILD_KEY)
    owners = run.query.get("parent_ids")
    if (
        run.kind != "creative"
        or run.ad_type != "SMART_PLUS"
        or not isinstance(marker, dict)
        or set(marker) != {"source_run_id", "context_page"}
        or marker.get("context_page") != MATERIAL_CONTEXT_PAGE
        or run.query.get("advertiser_id") != run.advertiser_id
        or run.query.get("kind") != run.kind
        or run.query.get("ad_type") != run.ad_type
        or run.query.get("ids")
        or not isinstance(owners, list)
        or not 0 < len(owners) <= MAX_OWNER_IDS
        or any(not isinstance(owner, str) or not owner for owner in owners)
        or len(set(owners)) != len(owners)
    ):
        return None
    try:
        source_id = UUID(marker["source_run_id"])
    except KeyError, ValueError, TypeError, AttributeError:
        return None
    from app.modules.sync_control.archive import _stream_rows

    predicate = (col(AdDirectoryRun.id) == source_id) & (
        col(AdDirectoryRun.tenant_id) == run.tenant_id
    )
    if lock:
        session.exec(
            select(AdDirectoryRun.id)
            .where(predicate)
            .with_for_update(read=True, key_share=True)
        ).first()
    source = next(_stream_rows(session, AdDirectoryRun, predicate), None)
    if (
        source is None
        or source.kind != "ad"
        or source.ad_type != "SMART_PLUS"
        or source.published_version is None
        or is_material_child(source)
        or _scope_signature(source) != _scope_signature(run)
    ):
        return None
    return source, owners


def _current_owner_times(
    session: Session, run: AdDirectoryRun, owners: list[str], *, lock: bool = False
) -> Iterator[datetime]:
    """每批用目录复合主键读取；没有对象/不同连接不能算较新观察。"""
    for offset in range(0, len(owners), OWNER_BATCH):
        batch = owners[offset : offset + OWNER_BATCH]
        query = sa_select(
            col(AdObject.remote_id),
            col(AdObject.observed_at),
            col(AdObject.source_connection_id),
            col(AdObject.source_channel),
            col(AdObject.ad_type),
        ).where(
            col(AdObject.tenant_id) == run.tenant_id,
            col(AdObject.advertiser_id) == run.advertiser_id,
            col(AdObject.kind) == "ad",
            col(AdObject.remote_id).in_(batch),
        )
        if lock:
            query = query.with_for_update(read=True)
        rows = session.connection().execute(query).all()
        if len(rows) != len(batch):
            raise ValueError("material_child_owner_missing")
        for _identity, observed, connection, channel, ad_type in rows:
            if (
                connection != run.connection_id
                or channel != run.channel
                or ad_type != "SMART_PLUS"
            ):
                raise ValueError("material_child_owner_source_mismatch")
            yield observed


def _context_owner_ceiling(
    session: Session, run: AdDirectoryRun, owners: list[str]
) -> datetime | None:
    """上下文仍在时只流式读取owner三元组，绝不解码宽materials桥。"""
    parameters = {"run": run.id, "page": MATERIAL_CONTEXT_PAGE}
    meta = (
        session.connection()
        .execute(
            text("""
        SELECT tenant_id,advertiser_id,coverage,complete,next_page,published_version,
            items='[]'::jsonb AND materials='[]'::jsonb AS empty_payload,
            evidence->'material_child_v1'->>'child_run_id' AS child_id,
            evidence->'material_child_v1'->>'source_run_id' AS source_id,
            evidence->'material_child_v1'->>'scope_signature' AS signature,
            jsonb_typeof(evidence->'material_child_v1'->'owners') AS owner_type
        FROM ad_directory_page WHERE run_id=:run AND page=:page
    """),
            parameters,
        )
        .mappings()
        .first()
    )
    if meta is None:
        return None
    if (
        meta["tenant_id"] != run.tenant_id
        or meta["advertiser_id"] != run.advertiser_id
        or meta["coverage"] != "MATERIAL_CONTEXT"
        or meta["complete"]
        or meta["next_page"] is not None
        or meta["published_version"] is not None
        or not meta["empty_payload"]
        or meta["child_id"] != str(run.id)
        or meta["source_id"] != run.query[MATERIAL_CHILD_KEY]["source_run_id"]
        or meta["signature"] != _scope_signature(run)
        or meta["owner_type"] != "array"
    ):
        raise ValueError("material_child_context_invalid")
    expected, seen, ceiling = set(owners), set(), None
    result = session.connection().execute(
        text("""
        SELECT owner.value->>0, owner.value->>1, owner.value->>2
        FROM ad_directory_page p CROSS JOIN LATERAL
            jsonb_array_elements(p.evidence->'material_child_v1'->'owners') owner(value)
        WHERE p.run_id=:run AND p.page=:page LIMIT :limit
    """).execution_options(yield_per=OWNER_BATCH),
        {**parameters, "limit": len(owners) + 1},
    )
    try:
        for owner, parent, observed in result:
            if owner not in expected or owner in seen or not parent or not observed:
                raise ValueError("material_child_context_invalid")
            at = datetime.fromisoformat(observed)
            if at.tzinfo is None:
                raise ValueError("material_child_context_invalid")
            seen.add(owner)
            ceiling = at if ceiling is None else max(ceiling, at)
    finally:
        result.close()
    if seen != expected:
        raise ValueError("material_child_context_invalid")
    return ceiling


def register_legacy_owner_ceiling(
    session_factory: Callable[[], Session],
    *,
    tenant_id: UUID,
    run_id: UUID,
    now: datetime,
) -> str | None:
    """仅显式apply登记缺失上界；即便已有较新数据，本轮也不删除历史child。"""
    from app.modules.sync_control.archive import _stream_rows

    with session_factory() as session:
        rows = list(
            _stream_rows(
                session,
                AdDirectoryRun,
                (col(AdDirectoryRun.id) == run_id)
                & (col(AdDirectoryRun.tenant_id) == tenant_id),
                lock=True,
            )
        )
        if not rows:
            return None
        run = rows[0]
        if (
            not is_material_child(run)
            or run.material_owner_observed_through is not None
            or run.published_version is None
            or run.status != "COMPLETE"
            or run.completed_at is None
            or run.completed_at >= now - timedelta(days=90)
            or run.claim_token is not None
            or (run.claimed_until is not None and run.claimed_until > now)
        ):
            return None
        scope = _source_and_owners(session, run, lock=True)
        if scope is None:
            return "material_child_source_unproven"
        _source, owners = scope
        try:
            ceiling = _context_owner_ceiling(session, run, owners)
            if ceiling is None:
                # 原冻结观察丢失时，单调AdObject的当前值是保守上界；必须等下一次更高观察。
                ceiling = max(_current_owner_times(session, run, owners))
        except ValueError as error:
            return str(error)
        run.material_owner_observed_through = ceiling
        session.add(run)
        session.commit()
        return "material_child_owner_baseline_registered"


def material_child_archive_covered(
    session: Session, run: AdDirectoryRun, *, lock: bool
) -> str | None:
    """比运行中逐owner比较更强：所有当前owner必须越过冻结max且有同授权完整新快照。"""
    from app.modules.sync_control.archive import _directory_cover_exists

    if run.material_owner_observed_through is None:
        return "material_child_owner_baseline_missing"
    scope = _source_and_owners(session, run, lock=lock)
    if scope is None:
        return "material_child_source_unproven"
    source, owners = scope
    if not _directory_cover_exists(session, source, lock=lock, full_only=True):
        return "material_child_source_coverage_proof"
    try:
        if not all(
            observed > run.material_owner_observed_through
            for observed in _current_owner_times(session, run, owners, lock=lock)
        ):
            return "material_child_owner_observation_current"
    except ValueError as error:
        return str(error)
    return None
