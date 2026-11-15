from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
)
from app.modules.providers.tasks import prepare_item, process_item, recover_preparations
from tests.modules.providers.test_link_recovery import SimulatedCrash
from tests.modules.providers.test_preparation_api import (
    prepare,
)
from tests.modules.providers.test_preparation_api import (
    request_fixture as request_fixture,
)
from tests.modules.providers.test_preparation_api import (
    workflow as workflow,
)


def first_item(task_id):
    with Session(engine) as session:
        return session.exec(
            select(LinkPreparationItem.id)
            .where(
                LinkPreparationItem.preparation_id == task_id,
            )
            .order_by(LinkPreparationItem.line_no)
        ).first()


def process(workflow, item_id, *, transport=None):
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        revision = item.resolved["_work"]["dispatch_revision"]
    process_item(
        database_engine=engine,
        tenant_id=workflow[0].tenant_id,
        actor_id=workflow[0].actor_id,
        payload={"item_id": str(item_id), "revision": revision},
        transport=transport or httpx.MockTransport(workflow[2]),
    )


def make_due(task_id, *, all_items=False):
    with Session(engine) as session:
        items = session.exec(
            select(LinkPreparationItem).where(
                LinkPreparationItem.preparation_id == task_id,
            )
        ).all()
        for item in items if all_items else items[:1]:
            work = dict(item.resolved["_work"])
            past = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
            work.update(next_dispatch_at=past, repair_after=past)
            if work.get("claim_until"):
                work["claim_until"] = past
            item.resolved = {**item.resolved, "_work": work}
            session.add(item)
        session.commit()


def test_each_provider_unit_has_committed_watchdog_before_network(request_fixture):
    task_id = prepare(request_fixture)
    item_id = first_item(task_id)

    def observe(request):
        with Session(engine) as session:
            item = session.get(LinkPreparationItem, item_id)
            revision = item.resolved["_work"]["dispatch_revision"]
            watchdog = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == request_fixture[0].tenant_id,
                    PendingDispatch.task_key == f"provider-item:{item_id}:{revision}",
                )
            ).one()
            assert watchdog.available_at > datetime.now(UTC)
        return request_fixture[2](request)

    for _ in range(7):
        process(request_fixture, item_id, transport=httpx.MockTransport(observe))
    with Session(engine) as session:
        assert session.get(LinkPreparation, task_id).status == "ready"
        assert session.get(LinkPreparationItem, item_id).status == "ready"


def test_dead_worker_recovers_unknown_create_through_persisted_watchdog(
    request_fixture,
):
    task_id = prepare(request_fixture)
    item_id = first_item(task_id)
    process(request_fixture, item_id)
    process(request_fixture, item_id)
    request_fixture[2].fail = "create_crash"
    with pytest.raises(SimulatedCrash):
        process(request_fixture, item_id)
    make_due(task_id)
    for _ in range(8):
        process(request_fixture, item_id)
        make_due(task_id)
    with Session(engine) as session:
        assert session.get(LinkPreparationItem, item_id).status == "ready"
    assert request_fixture[2].calls.count("create") == 1


def test_periodic_repair_replaces_lost_broker_message_without_hot_loop(request_fixture):
    task_id = prepare(request_fixture)
    item_id = first_item(task_id)
    with Session(engine) as session:
        records = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id,
            )
        ).all()
        for record in records:
            record.published_at = datetime.now(UTC)
            session.add(record)
        session.commit()
    make_due(task_id)
    assert recover_preparations(database_engine=engine, limit=1) == 1
    assert recover_preparations(database_engine=engine, limit=1) == 0
    process(request_fixture, item_id)
    assert request_fixture[2].calls == ["getVideoList"]


@pytest.mark.parametrize("stage", ["invalid", "manual_review"])
def test_watchdog_does_not_republish_stopped_unknown(request_fixture, stage):
    task_id = prepare(request_fixture)
    item_id = first_item(task_id)
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        item.status = "result_unknown"
        item.resolved = {
            **item.resolved,
            "status": "result_unknown",
            "_work": {**item.resolved.get("_work", {}), "stage": stage},
        }
        session.add(item)
        session.commit()
    make_due(task_id)
    with Session(engine) as session:
        records = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id
            )
        ).all()
        for record in records:
            record.published_at = datetime.now(UTC)
            session.add(record)
        session.commit()
        published = {record.id: record.published_at for record in records}
    assert recover_preparations(database_engine=engine) == 0
    process(request_fixture, item_id)
    with Session(engine) as session:
        assert {
            record.id: record.published_at
            for record in session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == request_fixture[0].tenant_id
                )
            ).all()
        } == published
    assert request_fixture[2].calls == []


def test_repair_is_bounded_and_advances_past_first_batch(request_fixture):
    task_id = prepare(request_fixture, lines=[f"Drama {n}" for n in range(205)])
    make_due(task_id, all_items=True)
    assert recover_preparations(database_engine=engine) == 100
    assert recover_preparations(database_engine=engine) == 100
    assert recover_preparations(database_engine=engine) == 5
    assert recover_preparations(database_engine=engine) == 0


def test_stale_dispatch_revision_does_not_call_provider(request_fixture):
    task_id = prepare(request_fixture)
    item_id = first_item(task_id)
    process(request_fixture, item_id)
    process_item(
        database_engine=engine,
        tenant_id=request_fixture[0].tenant_id,
        actor_id=request_fixture[0].actor_id,
        payload={"item_id": str(item_id), "revision": 0},
        transport=httpx.MockTransport(request_fixture[2]),
    )
    assert request_fixture[2].calls == ["getVideoList"]


def test_unbounded_or_eager_production_worker_is_rejected():
    with pytest.raises(DomainError) as error:
        prepare_item.run(tenant_id="unused", actor_id="unused", payload={})
    assert error.value.code == "provider_worker_unbounded"


def test_invalid_manual_state_and_revoked_actor_do_not_loop(request_fixture):
    from app.modules.tenants.models import TenantMembership

    task_id = prepare(request_fixture, lines=["Moon", "Sun"])
    with Session(engine) as session:
        rows = session.exec(
            select(LinkPreparationItem)
            .where(LinkPreparationItem.preparation_id == task_id)
            .order_by(LinkPreparationItem.line_no)
        ).all()
        first, second = rows[0].id, rows[1].id
        rows[0].resolved = {**rows[0].resolved, "_work": {"dispatch_revision": True}}
        session.add(rows[0])
        member = session.get(
            TenantMembership,
            (request_fixture[0].tenant_id, request_fixture[0].actor_id),
        )
        member.active = False
        session.add(member)
        session.commit()
    assert recover_preparations(database_engine=engine) == 0
    process(request_fixture, second)
    make_due(task_id, all_items=True)
    assert recover_preparations(database_engine=engine) == 0
    with Session(engine) as session:
        assert (
            session.get(LinkPreparationItem, first).resolved["_work"]["stage"]
            == "invalid"
        )
        assert session.get(LinkPreparationItem, second).status == "blocked_auth"
    assert request_fixture[2].calls == []


def test_unknown_generation_repair_only_reads_and_keeps_original_write(request_fixture):
    task_id = prepare(request_fixture)
    item_id = first_item(task_id)
    request_fixture[2].fail = "generate_unknown"
    for _ in range(5):
        process(request_fixture, item_id)
    make_due(task_id)
    assert recover_preparations(database_engine=engine, limit=1) == 1
    process(request_fixture, item_id)
    assert request_fixture[2].calls.count("generateGuideUrl") == 1
    assert request_fixture[2].calls.count("saveGuideUrl") == 0
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        assert item.status == "result_unknown"
        assert datetime.fromisoformat(
            item.resolved["_work"]["next_dispatch_at"]
        ) > datetime.now(UTC)


@pytest.mark.parametrize(
    "name,daemon,eager,limit",
    [
        ("MainProcess", False, False, 45),
        ("ThreadPoolWorker-1", True, False, 45),
        ("ForkPoolWorker-1", True, True, 45),
        ("ForkPoolWorker-1", True, False, 60),
        ("ForkPoolWorker-1", True, False, True),
    ],
)
def test_worker_rejects_unenforceable_deadline_before_any_work(
    monkeypatch, name, daemon, eager, limit
):
    from types import SimpleNamespace

    from app.modules.providers import tasks

    monkeypatch.setattr(
        tasks, "current_process", lambda: SimpleNamespace(name=name, daemon=daemon)
    )
    prepare_item.push_request(
        called_directly=False, is_eager=eager, timelimit=(limit, 40)
    )
    try:
        with pytest.raises(DomainError) as error:
            prepare_item.run(
                tenant_id="must-not-parse", actor_id="must-not-parse", payload={}
            )
        assert error.value.code == "provider_worker_unbounded"
    finally:
        prepare_item.pop_request()
