"""一分钟缓存：真实PG/Redis验证金额、撤权、去重、失租与新一代刷新。"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from redis import Redis
from sqlalchemy import text

from app.core.context import TenantContext
from app.models import User
from app.modules.reporting import page_cache as cache
from app.modules.reporting.page_cache_tasks import plan_due
from app.modules.reporting.query_reuse import QueryCancellation
from app.modules.reporting.schemas import ReportingFilter
from app.modules.tenants.models import TenantMembership
from tests.modules.reporting.query_helpers import finish_projections
from tests.modules.reporting.test_material_query import D, material_row, publish, use


@pytest.fixture
def cached_case(committed_sync_seed, monkeypatch):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(cache, "PREFIX", "test:report-page:" + uuid4().hex)
    url = os.environ["TEST_REDIS_URL"]
    monkeypatch.setattr(cache, "client", lambda: Redis.from_url(url))
    redis = Redis.from_url(url)
    with factory() as session:
        publish(session, seed, "acc-1", [material_row("p1", "10.123456")])
        use(
            session, seed, adv="acc-1", ad="a1", video="v1", ad_material="m1", post="p1"
        )
        finish_projections(session)
        session.commit()
    filters = ReportingFilter(
        dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    with factory() as session:
        key = cache.scope_key(
            session, context=seed.context, bc_id="bc-a", filters=filters, limit=50
        )
    case = SimpleNamespace(
        seed=seed, factory=factory, redis=redis, filters=filters, key=key
    )

    def warm():
        cache.watch(
            redis,
            key,
            context=seed.context,
            bc_id="bc-a",
            filters=filters,
            limit=50,
            force=True,
        )
        token = uuid4().hex
        assert redis.set(cache.lease_key(key), token, nx=True, ex=cache.LEASE_SECONDS)
        assert cache.refresh_page(redis, key, token)
        page = cache.cached(redis, key)
        assert page is not None
        return page

    case.warm = warm
    try:
        yield case
    finally:
        keys = list(redis.scan_iter(cache.PREFIX + "*"))
        if keys:
            redis.delete(*keys)
        redis.close()


def request(case, **kwargs):
    with case.factory() as session:
        return cache.query_cached_ads(
            session,
            context=case.seed.context,
            bc_id="bc-a",
            filters=case.filters,
            wait_seconds=0.02,
            **kwargs,
        )


def test_cache_hit_preserves_complete_financial_response_without_rebuilding(
    cached_case, monkeypatch
):
    case = cached_case
    original = case.warm()
    monkeypatch.setattr(
        cache,
        "_build_snapshot",
        lambda *_args, **_kwargs: pytest.fail("cache hit rebuilt"),
    )
    page = request(case)
    assert page.cache_hit
    assert page.summary == original.summary
    assert page.items == original.items
    assert page.snapshot == original.snapshot
    assert 0 < case.redis.ttl(case.key) <= 60
    assert (
        timedelta(0)
        < page.cache_expires_at - page.cache_generated_at
        <= timedelta(seconds=60)
    )


def test_fact_update_is_cached_but_force_refresh_waits_for_new_generation(
    cached_case, monkeypatch
):
    case = cached_case
    original = case.warm()
    with case.factory() as session:
        publish(session, case.seed, "acc-1", [material_row("p1", "20.654321")])
        finish_projections(session)
        session.commit()
    assert request(case).summary == original.summary
    registered = Event()
    old_watch = cache.watch

    def watch(*args, **kwargs):
        old_watch(*args, **kwargs)
        registered.set()

    monkeypatch.setattr(cache, "watch", watch)

    def refresh_request():
        with case.factory() as session:
            return cache.query_cached_ads(
                session,
                context=case.seed.context,
                bc_id="bc-a",
                filters=case.filters,
                force_refresh=True,
                wait_seconds=5,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(refresh_request)
        assert registered.wait(3)
        assert (
            plan_due(
                case.redis,
                lambda key, token: cache.refresh_page(case.redis, key, token),
            )
            == 1
        )
        fresh = future.result(timeout=5)
    assert fresh.snapshot.snapshot_id != original.snapshot.snapshot_id
    assert fresh.summary != original.summary
    assert not fresh.cache_hit


def test_force_refresh_never_silently_returns_same_cached_generation(cached_case):
    case = cached_case
    original = case.warm()
    with pytest.raises(HTTPException) as error:
        request(case, force_refresh=True)
    assert error.value.status_code == 503
    assert cache.cached(case.redis, case.key).snapshot == original.snapshot


def test_revoked_account_cannot_read_still_live_cache(cached_case):
    case = cached_case
    case.warm()
    with case.factory() as session:
        session.execute(
            text(
                "UPDATE bc_account_access SET authorized=false WHERE tenant_id=:t AND advertiser_id='acc-1'"
            ),
            {"t": case.seed.tenant_id},
        )
        session.commit()
    with pytest.raises(HTTPException) as error:
        request(case)
    assert error.value.status_code == 403


def test_user_bc_and_page_size_are_isolated(cached_case):
    case = cached_case
    original = case.warm()
    actor = uuid4()
    try:
        with case.factory() as session:
            session.add(
                User(
                    id=actor,
                    username="cache-test-" + actor.hex[:12],
                    hashed_password="x",
                    is_active=True,
                )
            )
            session.flush()
            session.add(
                TenantMembership(
                    tenant_id=case.seed.tenant_id,
                    user_id=actor,
                    role="viewer",
                    active=True,
                )
            )
            session.commit()
        with case.factory() as session:
            other = cache.scope_key(
                session,
                context=TenantContext(case.seed.tenant_id, actor, "viewer"),
                bc_id="bc-a",
                filters=case.filters,
                limit=50,
            )
            larger = cache.scope_key(
                session,
                context=case.seed.context,
                bc_id="bc-a",
                filters=case.filters,
                limit=100,
            )
            other_bc = cache.scope_key(
                session,
                context=case.seed.context,
                bc_id="bc-b",
                filters=case.filters.model_copy(update={"advertiser_ids": ()}),
                limit=50,
            )
        assert len({case.key, other, larger, other_bc}) == 4
        # 即使缓存内容误放到另一操作者的键，原快照所有权也会阻止读取。
        case.redis.set(other, cache.serialize_page(original), ex=60)
        with case.factory() as session, pytest.raises(HTTPException) as error:
            cache.query_cached_ads(
                session,
                context=TenantContext(case.seed.tenant_id, actor, "viewer"),
                bc_id="bc-a",
                filters=case.filters,
                wait_seconds=0.01,
            )
        assert error.value.status_code == 404
    finally:
        with case.factory() as session:
            session.execute(
                text("DELETE FROM tenant_membership WHERE tenant_id=:t AND user_id=:u"),
                {"t": case.seed.tenant_id, "u": actor},
            )
            session.execute(text('DELETE FROM "user" WHERE id=:u'), {"u": actor})
            session.commit()


def test_expired_result_is_never_served_or_renewed_by_redis_ttl(cached_case):
    case = cached_case
    page = case.warm().model_copy(
        update={"cache_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    case.redis.set(case.key, cache.serialize_page(page), ex=60)
    assert cache.cached(case.redis, case.key) is None
    with pytest.raises(HTTPException) as error:
        request(case)
    assert error.value.status_code == 503


def test_build_time_counts_toward_maximum_age(cached_case, monkeypatch):
    case = cached_case
    monkeypatch.setattr(cache, "CACHE_SECONDS", 0)
    cache.watch(
        case.redis,
        case.key,
        context=case.seed.context,
        bc_id="bc-a",
        filters=case.filters,
        limit=50,
    )
    token = uuid4().hex
    case.redis.set(cache.lease_key(case.key), token, ex=120)
    assert not cache.refresh_page(case.redis, case.key, token)
    assert case.redis.get(case.key) is None


def test_lost_refresh_lease_cannot_publish_or_delete_winner(cached_case, monkeypatch):
    case = cached_case
    original = case.warm()
    old_read = cache._read_page

    def read(*args, **kwargs):
        page = old_read(*args, **kwargs)
        case.redis.set(cache.lease_key(case.key), "winner", ex=120)
        return page

    monkeypatch.setattr(cache, "_read_page", read)
    token = uuid4().hex
    case.redis.set(cache.lease_key(case.key), token, ex=120)
    assert not cache.refresh_page(case.redis, case.key, token)
    assert cache.cached(case.redis, case.key).snapshot == original.snapshot
    assert case.redis.get(cache.lease_key(case.key)) == b"winner"


def test_global_refresh_admission_has_only_two_slots(cached_case):
    case = cached_case
    with ThreadPoolExecutor(max_workers=8) as pool:
        slots = list(
            pool.map(lambda _: cache.acquire_slot(case.redis, uuid4().hex), range(8))
        )
    assert len([slot for slot in slots if slot]) == 2


def test_parallel_planners_only_publish_once(cached_case):
    case = cached_case
    cache.watch(
        case.redis,
        case.key,
        context=case.seed.context,
        bc_id="bc-a",
        filters=case.filters,
        limit=50,
    )
    sent = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(
            pool.map(
                lambda _: plan_due(
                    case.redis, lambda key, token: sent.append((key, token))
                ),
                range(4),
            )
        )
    assert sum(counts) == 1
    assert len(sent) == 1


def test_cancellation_does_not_destroy_other_readers_cache(cached_case):
    case = cached_case
    page = case.warm()
    cancellation = QueryCancellation()
    cancellation.event.set()
    with pytest.raises(HTTPException) as error:
        request(case, cancellation=cancellation)
    assert error.value.status_code == 499
    assert cache.cached(case.redis, case.key).snapshot == page.snapshot


def test_failed_refresh_does_not_renew_existing_result(cached_case, monkeypatch):
    case = cached_case
    original = case.warm()
    expires = case.redis.pttl(case.key)
    token = uuid4().hex
    case.redis.set(cache.lease_key(case.key), token, ex=120)

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated transport boundary failure")

    monkeypatch.setattr(cache, "_build_snapshot", fail)
    assert not cache.refresh_page(case.redis, case.key, token)
    assert cache.cached(case.redis, case.key).snapshot == original.snapshot
    assert case.redis.pttl(case.key) <= expires


def test_expired_compact_snapshots_are_drained_in_large_bounded_batches(cached_case):
    from app.modules.reporting import job_tasks
    from app.modules.reporting.query_models import QuerySnapshot

    case = cached_case
    live = case.warm()
    now = datetime.now(UTC)
    with case.factory() as session:
        expired = QuerySnapshot(
            # 只扫描本夹具的末尾窗口；随机UUID之后可能还有其他租户的到期行，
            # 会共同消耗全局5000行预算，不能据此推断本快照恰好剩2行。
            id=UUID(int=(1 << 128) - 1),
            tenant_id=case.seed.tenant_id,
            actor_id=case.seed.actor_id,
            bc_id="bc-a",
            advertiser_ids=["acc-1"],
            filters=case.filters.model_dump(mode="json"),
            filter_digest="x" * 64,
            created_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        session.add(expired)
        session.flush()
        count = job_tasks.CLEANUP_SNAPSHOT_BATCH_ROWS + 2
        session.execute(
            text("""INSERT INTO query_snapshot_row
            (snapshot_id,row_key,stable_sequence,dimension,display,refs,material_uses,metric_buckets,capabilities,directory_versions,membership_digest,member_keys,member_count)
            SELECT :s,jsonb_build_array('post','acc-1','cleanup-probe-'||n)::text,n,'material','{}','[]','[]','[]','{}','{}','','[]',0
            FROM generate_series(0,:last) n"""),
            {"s": expired.id, "last": count - 1},
        )
        # 主键游标定位本测试窗口，避免共享测试库的历史快照占满扫描预算。
        cursors = {"snapshots": [str(UUID(int=expired.id.int - 1))]}
        job_tasks.cleanup_store(
            session, now=now, categories=("snapshots",), cursors=cursors
        )
        remaining = session.execute(
            text("SELECT count(*) FROM query_snapshot_row WHERE snapshot_id=:s"),
            {"s": expired.id},
        ).scalar_one()
        assert remaining == 2
        job_tasks.cleanup_store(
            session, now=now, categories=("snapshots",), cursors=cursors
        )
        assert session.get(QuerySnapshot, expired.id, populate_existing=True) is None
        assert (
            session.get(
                QuerySnapshot, live.snapshot.snapshot_id, populate_existing=True
            )
            is not None
        )
        session.rollback()
