"""目录提交后直接推进有界派发，不再等待 Control 的补位消息。"""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.config import settings
from app.core.db import engine
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch, tasks
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync import stage_directory_page
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from tests.modules.ads.test_material_directory_children import _source_page
from tests.modules.reporting.test_jobs import _route


def directory(session, seed, *, terminal=False):
    ref = uuid4().hex
    run = AdDirectoryRun(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        actor_id=seed.actor_id,
        connection_id=seed.connection.id,
        frozen_route=_route(seed),
        kind="campaign",
        ad_type="REGULAR",
        workload="live",
        partition_key=ref * 2,
        claim_generation=1,
        query={
            "advertiser_id": "acc-1",
            "kind": "campaign",
            "ad_type": "REGULAR",
            "page": 1,
            "page_size": 100,
            "ids": [ref],
            "parent_ids": [],
            "include_deleted": False,
        },
    )
    session.add(run)
    session.flush()
    if terminal:
        stage_directory_page(
            session, run_id=run.id, page=page(complete=True), claim_generation=1
        )
    return run


def page(*, complete):
    return DirectoryPage(
        items=(),
        materials=(),
        next_page=None if complete else 2,
        complete=complete,
        evidence=CallEvidence(request_id="test-request"),
    )


@pytest.fixture(autouse=True)
def enabled(monkeypatch, redis_client):
    del redis_client
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])


def register_pending(db, seed):
    for run in db.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
    ).all():
        register_directory_run(db, run)


def claim_current(factory, seed, redis_client, identity, monkeypatch):
    with factory() as db, db.begin():
        register_pending(db, seed)
        assert (
            dispatch_directories_due(
                db, redis_client, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
            )
            == 1
        )
        run = db.get(AdDirectoryRun, identity)
        generation = run.claim_generation
        assert generation > 1
    # 通过真实outbox发送入口消费首条；仅broker transport替身，不能伪造领取。
    with monkeypatch.context() as patch:
        patch.setattr(outbox.celery_app, "send_task", lambda *_a, **_kw: None)
        outbox.flush_dispatch()
    return generation


@pytest.mark.parametrize("broker_fails", [False, True])
def test_completed_directory_claims_and_sends_next_without_control(
    committed_sync_seed, monkeypatch, redis_client, broker_fails
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        finished = directory(db, seed, terminal=True).id
        waiting = directory(db, seed).id
        register_pending(db, seed)
    sent = []

    def publish(name, **kwargs):
        # 真实第二连接证实发送发生在发布/领取提交后，且不持有窗口锁。
        with factory() as observer:
            row = observer.exec(
                select(AdDirectoryRun)
                .where(AdDirectoryRun.id == finished)
                .with_for_update(nowait=True)
            ).one()
            assert row.status == "COMPLETE" and row.published_version is not None
            assert observer.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:directory-dispatch-window',0))"
                )
            ).scalar_one()
        sent.append((name, kwargs))
        if broker_fails:
            raise ConnectionError("offline broker")

    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    args = {
        "database_engine": engine,
        "redis_client": redis_client,
        "context": seed.context,
        "run_id": finished,
        "claim_generation": 1,
        "publish_only": True,
    }
    assert tasks.run_directory_step(**args) == "READY"
    with factory() as db:
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 1 and rows[0].task_name == "ads.targeted_step"
        assert rows[0].payload["run_id"] == str(waiting)
        assert (rows[0].published_at is None) is broker_fails
        assert rows[0].attempts == 1
    assert len(sent) == 1
    assert tasks.run_directory_step(**args) == "READY"
    assert len(sent) == 1  # 重复终态不能再补位或触发发送扫描。


@pytest.mark.parametrize("background_facts", [False, True])
def test_nonterminal_page_refills_through_canonical_dispatcher(
    committed_sync_seed, monkeypatch, redis_client, background_facts
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        current = directory(db, seed)
        identity = current.id
        directory(db, seed)
        if background_facts:
            from datetime import UTC, datetime

            from tests.modules.reporting.test_background_execution_share import pending

            pending(db, seed, "material_day", 0, datetime.now(UTC))

        register_pending(db, seed)
    generation = claim_current(factory, seed, redis_client, identity, monkeypatch)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(
            ads=SimpleNamespace(read_page=lambda _: page(complete=False))
        )

    scans = []
    original = job_tasks._dispatch_directories

    def track(*args, **kwargs):
        scans.append(True)
        return original(*args, **kwargs)

    sent = []
    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(job_tasks, "_dispatch_directories", track)
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append((name, kw))
    )
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=generation,
        )
        == "CONTINUE"
    )
    assert scans and len(scans) <= 8
    directory_sent = [item for item in sent if item[0] == "ads.targeted_step"]
    assert len(directory_sent) == 1
    assert sum(name == "reporting.backfill_step" for name, _ in sent) == int(
        background_facts
    )
    with factory() as db:
        row = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key.endswith(":page:2"),
            )
        ).one()
        assert row.payload["claim_generation"] > generation
        assert row.task_key.endswith(":page:2") and row.published_at is not None


def test_periodic_marker_stays_local_when_reading_and_continuing(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        request_directory(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            advertiser_ids=["acc-1"],
            scope="directory",
            ad_types={"acc-1": {"REGULAR": ("campaign",)}},
            periodic_intervals={"acc-1": timedelta(days=1)},
        )
        identity = db.exec(
            select(AdDirectoryRun.id).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).one()
        register_pending(db, seed)
    generation = claim_current(factory, seed, redis_client, identity, monkeypatch)

    queries = []

    def read(query):
        queries.append(query)
        assert query.ids == () and query.page == 1 and query.include_deleted
        return page(complete=False)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    sent = []
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append(name)
    )
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=generation,
        )
        == "CONTINUE"
    )
    assert len(queries) == 1 and sent == ["ads.sync_step"]


def test_full_directory_publication_immediately_sends_targeted_material_child(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        source = directory(db, seed)
        source.kind, source.ad_type = "ad", "SMART_PLUS"
        source.query = dict(source.query, kind="ad", ad_type="SMART_PLUS", ids=[])
        assert not directory_dispatch.is_live_directory(source)
        page_seed = SimpleNamespace(
            context=seed.context, ref=SimpleNamespace(advertiser_id="acc-1")
        )
        stage_directory_page(
            db,
            run_id=source.id,
            claim_generation=1,
            page=_source_page(page_seed, 1, [("ad-a", "group-a")], terminal=True),
        )
        identity = source.id
        register_pending(db, seed)
    sent = []
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append((name, kw))
    )
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    with factory() as db:
        child = db.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == seed.tenant_id,
                AdDirectoryRun.kind == "creative",
            )
        ).one()
        rows = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
            )
        ).all()
        row = next(row for row in rows if row.payload.get("run_id") == str(child.id))
        assert directory_dispatch.is_live_directory(child)
        assert row.task_name == "ads.targeted_step"
        assert row.published_at is not None
        parent = db.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == seed.tenant_id,
                AdDirectoryRun.kind == "adgroup",
            )
        ).one()
        assert any(
            row.payload.get("run_id") == str(parent.id) and row.published_at is not None
            for row in rows
        )
    assert sum(name == "ads.targeted_step" for name, _ in sent) == 2


def test_admission_wait_keeps_delayed_continuation_without_refilling(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed).id
        directory(db, seed)

        register_pending(db, seed)
    generation = claim_current(factory, seed, redis_client, identity, monkeypatch)

    @contextmanager
    def deferred(**_kwargs):
        raise AccountAdmissionDeferred(30000)
        yield

    sent = []
    monkeypatch.setattr(tasks, "open_tiktok_gateway", deferred)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: sent.append(a))
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=generation,
        )
        == "WAIT"
    )
    assert sent == [("reporting.plan_due",)]
    with factory() as db:
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 2
        assert len([row for row in rows if row.task_name == "reporting.plan_due"]) == 1
        assert (
            len([row for row in rows if row.payload.get("run_id") == str(identity)])
            == 1
        )
        run = db.get(AdDirectoryRun, identity)
        assert (
            run.error_code == "admission_deferred"
            and run.next_attempt_at > datetime.now(UTC)
        )
        from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

        head = db.exec(
            select(SyncSchedule)
            .join(
                SyncExecutionScope, SyncExecutionScope.scope_id == SyncSchedule.scope_id
            )
            .where(SyncExecutionScope.directory_run_id == identity)
        ).one()
        assert (
            head.next_eligible_at <= run.next_attempt_at
        )  # 另一个未延期执行仍可竞争同头。
        assert head.lease_token is None


def test_failed_publication_commit_does_not_claim_or_send_other_work(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed, terminal=True).id
        directory(db, seed)
        register_pending(db, seed)
    publish = tasks.publish_directory

    def rollback(db, **kwargs):
        publish(db, **kwargs)
        db.execute(text("SELECT 1/0"))

    sent = []
    monkeypatch.setattr(tasks, "publish_directory", rollback)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: sent.append(a))
    with pytest.raises(DBAPIError):
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
            publish_only=True,
        )
    assert not sent
    with factory() as db:
        assert db.get(AdDirectoryRun, identity).published_version is None
        assert not db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()


@pytest.mark.parametrize("failure", [RuntimeError, SoftTimeLimitExceeded])
def test_refill_failure_keeps_publication_and_beat_can_resume(
    committed_sync_seed, monkeypatch, redis_client, failure
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed, terminal=True).id
        directory(db, seed)
        register_pending(db, seed)
    original = job_tasks._dispatch_directories

    def unavailable(db, now, **kwargs):
        # 先真实领取，再中断，验证领取事务回滚而业务发布提交不受影响。
        original(db, now, **kwargs)
        raise failure("temporary local query failure")

    monkeypatch.setattr(job_tasks, "_dispatch_directories", unavailable)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    with factory() as db:
        assert db.get(AdDirectoryRun, identity).published_version is not None
    monkeypatch.setattr(job_tasks, "_dispatch_directories", original)
    sent = []
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: sent.append(a))
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 1
    assert sent == [("ads.targeted_step",)]


def test_concurrent_completions_preserve_global_directory_window(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 8)
    with factory() as db, db.begin():
        completed = [directory(db, seed, terminal=True).id for _ in range(2)]
        waiting = {directory(db, seed).id for _ in range(20)}
        register_pending(db, seed)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: None)
    barrier = Barrier(2)

    def finish(identity):
        barrier.wait(timeout=5)
        return tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
            publish_only=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(finish, completed)) == ["READY", "READY"]
    with factory() as db:
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 1  # 同scope当前只有一个有效头；并发补位不能重复领取。
        assert {row.task_name for row in rows} == {"ads.targeted_step"}
        assert {row.payload["run_id"] for row in rows} <= {str(i) for i in waiting}


@pytest.mark.parametrize("deferred", [False, True])
def test_background_continuation_returns_borrowed_slot_to_facts(
    committed_sync_seed, monkeypatch, redis_client, deferred
):
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.sync_control.models import SyncSchedule

    seed, factory = committed_sync_seed
    now = datetime.now(UTC) + timedelta(seconds=1)
    with factory() as db, db.begin():
        identities = []
        for index in range(4):
            run = directory(db, seed)
            run.workload = "background"
            run.query = run.query | {"page_size": 100 + index}
            identities.append(run.id)
        register_pending(db, seed)
        assert dispatch_directories_due(db, redis_client, now=now, limit=40) == 4
        identity = identities[0]
        generation = db.get(AdDirectoryRun, identity).claim_generation
        held = {
            head.lease_execution_id: head.lease_token
            for head in db.exec(
                select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
            ).all()
            if head.lease_execution_id != identity
        }
    with monkeypatch.context() as patch:
        patch.setattr(outbox.celery_app, "send_task", lambda *_a, **_kw: None)
        outbox.flush_dispatch()
    # 满池后才到达历史事实；当前页成功或准入延期释放一个槽，均应即时补给事实。
    with factory() as db, db.begin():
        day = now.date() - timedelta(days=3)
        fact_id = enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            kind="material_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            due_at=datetime.now(UTC),
        )

    def read(_query):
        if deferred:
            raise AccountAdmissionDeferred(retry_after_ms=1000, reason="endpoint_rate")
        return page(complete=False)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    sent = []
    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append(name)
    )
    result = tasks.run_directory_step(
        database_engine=engine,
        redis_client=redis_client,
        context=seed.context,
        run_id=identity,
        claim_generation=generation,
    )
    assert result == ("WAIT" if deferred else "CONTINUE")
    assert sent == ["reporting.backfill_step"]
    with factory() as db:
        current = db.get(AdDirectoryRun, identity)
        assert current.next_page == (1 if deferred else 2)
        assert current.claimed_until is None and current.status in (
            "RUNNING",
            "WAITING_REMOTE",
        )
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert (
            len([row for row in rows if row.payload.get("job_id") == str(fact_id)]) == 1
        )
        assert (
            len([row for row in rows if row.payload.get("run_id") == str(identity)])
            == 1
        )
        actual = {
            head.lease_execution_id: head.lease_token
            for head in db.exec(
                select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
            ).all()
        }
        assert all(actual.get(identity) == token for identity, token in held.items())


def test_concurrent_borrowed_pages_preserve_progress_and_single_successor(
    committed_sync_seed, monkeypatch, redis_client
):
    from datetime import UTC, datetime

    from tests.modules.reporting.test_background_execution_share import (
        dispatch_pool,
        pending,
    )

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identities = []
        for i in range(2):
            row = directory(db, seed)
            row.workload = "background"
            row.query = row.query | {"page_size": 100 + i}
            identities.append(row.id)
        pending(db, seed, "material_day", 0, datetime.now(UTC))
        register_pending(db, seed)
    with factory() as db, db.begin():
        assert (
            dispatch_pool(db, redis_client, datetime.now(UTC) + timedelta(seconds=1))
            >= 2
        )
        generations = {
            identity: db.get(AdDirectoryRun, identity).claim_generation
            for identity in identities
        }
    with monkeypatch.context() as patch:
        patch.setattr(outbox.celery_app, "send_task", lambda *_a, **_kw: None)
        outbox.flush_dispatch()
    barrier = Barrier(2)

    def read(_query):
        barrier.wait(timeout=10)
        return page(complete=False)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **kw: None)

    def advance(identity):
        return tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=generations[identity],
        )

    with ThreadPoolExecutor(2) as executor:
        assert list(executor.map(advance, identities)) == ["CONTINUE", "CONTINUE"]
    with factory() as db, db.begin():
        for identity in identities:
            assert db.get(AdDirectoryRun, identity).next_page == 2
        messages = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        successors = [row for row in messages if row.task_key.endswith(":page:2")]
        if not successors:
            assert job_tasks._dispatch_directories(db, datetime.now(UTC)) > 0
            successors = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_key.endswith(":page:2"),
                )
            ).all()
        assert 1 <= len(successors) <= 2
        assert len({row.payload["run_id"] for row in successors}) == len(successors)
        assert all(
            row.payload["claim_generation"] > generations[UUID(row.payload["run_id"])]
            for row in successors
        )
