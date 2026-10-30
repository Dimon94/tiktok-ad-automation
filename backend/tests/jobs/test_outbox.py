from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlmodel import Session, delete, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.jobs.outbox import (
    compact_published_dispatches,
    enqueue_after_commit,
    flush_dispatch,
)
from app.jobs.tasks import dispatch_queue, register_dispatch_task


@pytest.fixture
def outbox_db(monkeypatch):
    """Actual committed transactions visible to independent publisher connections."""
    from app.core.db import engine
    from app.jobs import outbox

    monkeypatch.setattr(outbox, "engine", engine)
    with Session(engine) as session:
        session.exec(delete(PendingDispatch))
        session.exec(delete(DispatchTenantCursor))
        session.commit()
    yield engine
    with Session(engine) as session:
        session.exec(delete(PendingDispatch))
        session.exec(delete(DispatchTenantCursor))
        session.commit()


@pytest.fixture
def context():
    return TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: messages.append((args, kwargs))
    )
    return messages


def enqueue(session, context, key="probe-1", **kwargs):
    return enqueue_after_commit(
        session, context=context, task_name="jobs.probe", task_key=key, payload=kwargs
    )


def test_rollback_never_publishes(outbox_db, context, sent):
    with Session(outbox_db) as session:
        enqueue(session, context)
        assert flush_dispatch() == 0  # Separate connection cannot see uncommitted rows.
        session.rollback()
    assert flush_dispatch() == 0
    assert sent == []


def test_committed_dispatch_and_deduplication(outbox_db, context, sent):
    with Session(outbox_db) as session:
        first = enqueue(session, context, item_id=str(uuid4()))
        payload = session.get(PendingDispatch, first).payload
        due_at = session.get(PendingDispatch, first).available_at.timestamp()
        assert enqueue(session, context, **payload) == first
        session.commit()
    assert flush_dispatch() == 1
    assert flush_dispatch() == 0
    assert sent[0] == (
        ("jobs.probe",),
        {
            "kwargs": {
                "tenant_id": str(context.tenant_id),
                "actor_id": str(context.actor_id),
                "payload": payload,
            },
            "task_id": str(first),
            "queue": "control",
            "headers": {"diagnostic_due_at": due_at},
        },
    )
    with Session(outbox_db) as session:
        assert session.get(PendingDispatch, first).published_at is not None
        assert enqueue(session, context, **payload) == first


def test_compaction_keeps_dedup_identity_without_revivable_payload(outbox_db, context):
    register_dispatch_task("builds.execute_step", "builds")
    payload = {"step_id": str(uuid4()), "revision": 7}
    with Session(outbox_db) as session:
        identity = enqueue_after_commit(
            session,
            context=context,
            task_name="builds.execute_step",
            task_key="settled-step:7",
            payload=payload,
        )
        row = session.get(PendingDispatch, identity)
        row.published_at = datetime.now(UTC) - timedelta(days=8)
        session.commit()
    with Session(outbox_db) as session, session.begin():
        assert compact_published_dispatches(session, limit=10) == 1
    with Session(outbox_db) as session:
        row = session.get(PendingDispatch, identity)
        assert row.payload == {}
        assert row.payload_digest and row.compacted_at
        assert (
            enqueue_after_commit(
                session,
                context=context,
                task_name="builds.execute_step",
                task_key="settled-step:7",
                payload=payload,
            )
            == identity
        )
        with pytest.raises(DomainError) as failure:
            enqueue_after_commit(
                session,
                context=context,
                task_name="builds.execute_step",
                task_key="settled-step:7",
                payload=payload | {"revision": 8},
            )
        assert failure.value.code == "dispatch_key_conflict"


def test_compaction_skips_pending_recent_and_non_terminal_task_types(
    outbox_db, context
):
    register_dispatch_task("builds.execute_step", "builds")
    with Session(outbox_db) as session:
        pending = enqueue(session, context, key="pending")
        recent = enqueue_after_commit(
            session,
            context=context,
            task_name="builds.execute_step",
            task_key="recent",
            payload={"step_id": str(uuid4()), "revision": 1},
        )
        probe = enqueue(session, context, key="old-probe")
        session.get(PendingDispatch, recent).published_at = datetime.now(UTC)
        session.get(PendingDispatch, probe).published_at = datetime.now(
            UTC
        ) - timedelta(days=8)
        session.commit()
    with Session(outbox_db) as session, session.begin():
        assert compact_published_dispatches(session, limit=10) == 0
    with Session(outbox_db) as session:
        assert all(
            session.get(PendingDispatch, identity).compacted_at is None
            for identity in (pending, recent, probe)
        )


@pytest.mark.parametrize("change", ["actor", "payload", "task"])
def test_reused_key_conflict(outbox_db, context, change):
    register_dispatch_task("jobs.test_conflict", "control")
    with Session(outbox_db) as session:
        enqueue(session, context)
        session.commit()
        next_context = (
            TenantContext(context.tenant_id, uuid4(), "operator")
            if change == "actor"
            else context
        )
        with pytest.raises(DomainError) as exc:
            enqueue_after_commit(
                session,
                context=next_context,
                task_name="jobs.test_conflict" if change == "task" else "jobs.probe",
                task_key="probe-1",
                payload={"different": 1} if change == "payload" else {},
            )
        assert exc.value.code == "dispatch_key_conflict"


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "secret"},
        {"nested": {"cookie": "secret"}},
        {"file_body": "abc"},
        {"fileContent": "abc"},
        {"bytes": b"abc"},
        {"x": float("nan")},
        {"x": "a" * 100000},
        ["invalid"],
    ],
)
def test_invalid_payload_is_rejected(outbox_db, context, payload):
    with Session(outbox_db) as session:
        with pytest.raises(DomainError) as exc:
            enqueue_after_commit(
                session,
                context=context,
                task_name="jobs.probe",
                task_key="x",
                payload=payload,
            )
        assert exc.value.code == "dispatch_payload_invalid"
        assert (
            session.exec(select(func.count()).select_from(PendingDispatch)).one() == 0
        )


def test_unknown_task_and_queue_are_rejected(outbox_db, context):
    with Session(outbox_db) as session:
        with pytest.raises(DomainError) as exc:
            enqueue_after_commit(
                session,
                context=context,
                task_name="os.system",
                task_key="x",
                payload={},
            )
        assert exc.value.code == "dispatch_task_not_allowed"
    with pytest.raises(ValueError):
        register_dispatch_task("jobs.bad", "arbitrary")
    assert dispatch_queue("jobs.probe") == "control"
    with pytest.raises(DomainError):
        dispatch_queue("jobs.flush_dispatch")


def test_tasks_are_registered_and_probe_has_standard_signature():
    celery_app.loader.import_default_modules()
    assert "jobs.flush_dispatch" in celery_app.tasks
    assert "jobs.probe" in celery_app.tasks
    result = celery_app.tasks["jobs.probe"].run(
        tenant_id=str(uuid4()), actor_id=str(uuid4()), payload={}
    )
    assert result is None


def test_crash_after_send_republishes_same_task_id(outbox_db, context, monkeypatch):
    with Session(outbox_db) as session:
        first = enqueue(session, context)
        session.commit()
    sent = []

    class ProcessExit(BaseException):
        pass

    def crash(*_args, **kwargs):
        sent.append(kwargs["task_id"])
        raise ProcessExit()

    monkeypatch.setattr(celery_app, "send_task", crash)
    with pytest.raises(ProcessExit):
        flush_dispatch()
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: sent.append(kwargs["task_id"])
    )
    assert flush_dispatch() == 1
    assert sent == [
        str(first),
        str(first),
    ]  # Stable IDs do not provide consumer deduplication.


def test_failed_broker_schedules_retry(outbox_db, context, monkeypatch, sent):
    with Session(outbox_db) as session:
        first = enqueue(session, context)
        session.commit()

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(celery_app, "send_task", unavailable)
    assert flush_dispatch() == 0
    with Session(outbox_db) as session:
        row = session.get(PendingDispatch, first)
        assert row.published_at is None and row.attempts == 1
        assert row.available_at > datetime.now(UTC)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: sent.append(kwargs)
    )
    assert flush_dispatch() == 1


def test_first_round_is_fair_to_small_tenant(outbox_db, context, sent):
    other = TenantContext(uuid4(), uuid4(), "operator")
    with Session(outbox_db) as session:
        session.add(DispatchTenantCursor(tenant_id=context.tenant_id))
        session.bulk_insert_mappings(
            PendingDispatch,
            [
                {
                    "id": uuid4(),
                    "tenant_id": context.tenant_id,
                    "actor_id": context.actor_id,
                    "task_name": "jobs.probe",
                    "task_key": str(i),
                    "payload": {},
                }
                for i in range(10000)
            ],
        )
        enqueue(session, other)
        session.commit()
    assert flush_dispatch() == 6
    tenants = [entry[1]["kwargs"]["tenant_id"] for entry in sent]
    assert tenants.count(str(context.tenant_id)) == 5
    assert tenants.count(str(other.tenant_id)) == 1
    sent.clear()
    assert flush_dispatch(limit=2) == 2


def test_parallel_publishers_skip_locked_tenant(outbox_db, context, monkeypatch):
    with Session(outbox_db) as session:
        enqueue(session, context)
        session.commit()
    entered, unblock = Event(), Event()
    sent = []

    def paused(*_args, **kwargs):
        sent.append(kwargs["task_id"])
        entered.set()
        assert unblock.wait(5)

    monkeypatch.setattr(celery_app, "send_task", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(flush_dispatch)
        try:
            assert entered.wait(5)
            second = pool.submit(flush_dispatch)
            assert second.result(timeout=3) == 0
        finally:
            unblock.set()
        assert first.result(timeout=3) == 1
    assert len(sent) == 1


def test_json_boolean_and_number_are_different_dispatch_configurations(
    outbox_db, context
):
    with Session(outbox_db) as session:
        enqueue(session, context, enabled=True)
        session.commit()
        with pytest.raises(DomainError) as exc:
            enqueue(session, context, enabled=1)
        assert exc.value.code == "dispatch_key_conflict"


def test_concurrent_enqueue_returns_one_stable_id(outbox_db, context):
    def submit(_index):
        with Session(outbox_db) as session:
            record = enqueue(session, context)
            session.commit()
            return record

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, [1, 2]))
    assert ids[0] == ids[1]
    with Session(outbox_db) as session:
        assert (
            session.exec(select(func.count()).select_from(PendingDispatch)).one() == 1
        )


def test_same_key_is_isolated_by_tenant(outbox_db, context):
    with Session(outbox_db) as session:
        first = enqueue(session, context)
        second = enqueue(session, TenantContext(uuid4(), context.actor_id, "operator"))
        session.commit()
        assert first != second


def test_retry_round_retains_tenant_fairness(outbox_db, context, monkeypatch):
    other = TenantContext(uuid4(), uuid4(), "operator")
    with Session(outbox_db) as session:
        for i in range(12):
            enqueue(session, context, str(i))
        enqueue(session, other)
        session.commit()

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(celery_app, "send_task", unavailable)
    assert flush_dispatch() == 0
    with Session(outbox_db) as session:
        records = session.exec(select(PendingDispatch)).all()
        assert sum(row.attempts for row in records) == 6
        assert all(row.published_at is None for row in records)
        for row in records:
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    sent = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda *_args, **kwargs: sent.append(kwargs["kwargs"]["tenant_id"]),
    )
    assert flush_dispatch() == 6
    assert sent.count(str(context.tenant_id)) == 5
    assert sent.count(str(other.tenant_id)) == 1


def test_round_limit_is_clamped_to_one_hundred(outbox_db, context, sent):
    with Session(outbox_db) as session:
        for _ in range(21):
            tenant = TenantContext(uuid4(), context.actor_id, "operator")
            for i in range(5):
                enqueue(session, tenant, str(i))
        session.commit()
    assert flush_dispatch(limit=1000) == 100
    assert len(sent) == 100


def test_superseded_dispatch_is_not_sent_or_recreated(outbox_db, context, sent):
    """离线切换可退役已替代的未发送消息，但不能伪造broker发送或丢幂等身份。"""
    with Session(outbox_db) as session:
        identity = enqueue(session, context, key="retired-cutover", item_id="old")
        row = session.get(PendingDispatch, identity)
        row.superseded_at = datetime.now(UTC)
        digest = row.payload_digest
        session.commit()
    assert flush_dispatch() == 0
    assert sent == []
    with Session(outbox_db) as session:
        assert (
            enqueue(session, context, key="retired-cutover", item_id="old") == identity
        )
        row = session.get(PendingDispatch, identity)
        assert row.published_at is None
        assert row.superseded_at is not None
        assert row.payload == {"item_id": "old"} and row.payload_digest == digest
