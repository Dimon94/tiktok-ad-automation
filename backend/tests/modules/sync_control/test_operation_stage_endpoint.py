"""操作日志按实际阶段派发；旧头纠正不能扣错接口或丢掉同scope其他任务。"""

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.jobs.admission import admission_keys
from app.jobs.models import PendingDispatch
from app.modules.reporting import job_tasks  # noqa: F401
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportDispatchCursor, ReportSyncJob
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from app.modules.sync_control.occupancy import occupancy_keys
from app.modules.sync_control.schedule import record_report_outcome
from tests.modules.operation_logs.test_worker import log_seed as log_seed
from tests.modules.reporting.test_jobs import _route

CREATE = "POST /open_api/v1.3/changelog/task/create/"
CHECK = "GET /open_api/v1.3/changelog/task/check/"
DOWNLOAD = "GET /open_api/v1.3/changelog/task/download/"


def _job(db, seed, *, phase="CREATE", day=1, due=None, advertiser="acc-1"):
    identity = enqueue_job(
        db,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind="operation_log",
        tier="history",
        advertiser_ids=[advertiser],
        start=date(2026, 10, day),
        end=date(2026, 10, day),
        frozen_route=_route(seed),
        actor_id=seed.actor_id,
        due_at=due or datetime.now(UTC),
    )
    row = db.get(ReportSyncJob, identity)
    row.progress = row.progress | {
        "operation_log": {"phase": phase, "task_id": "test-export"}
    }
    db.flush()
    return row


def _scope(seed, endpoint):
    return {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": endpoint,
        "rate_group": endpoint,
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-1",
    }


def _cooldown(redis, seed, endpoint):
    seconds, micros = redis.time()
    key = admission_keys(**_scope(seed, endpoint))[1] + ":cooldown"
    redis.set(key, seconds * 1000 + micros // 1000 + 60_000, px=60_000)
    return key


def _dispatch(db, redis, now):
    return dispatch_due(db, redis, now=now, physical_slots=36, max_claims=1)


@pytest.mark.parametrize("phase,endpoint", [("CHECK", CHECK), ("DOWNLOAD", DOWNLOAD)])
def test_old_create_head_repairs_without_charge_then_uses_actual_stage(
    session, sync_seed, redis_client, phase, endpoint
):
    job = _job(session, sync_seed, phase=phase)
    now = datetime.now(UTC) + timedelta(seconds=1)
    _cooldown(redis_client, sync_seed, CREATE)
    # CREATE冷却也不能阻挡路由纠正。该轮不发消息、不占位、不改变公平债务。
    assert _dispatch(session, redis_client, now) == 0
    head = session.exec(select(SyncSchedule)).one()
    assert head.active and head.physical_endpoint == endpoint
    assert head.next_eligible_at <= now and head.lease_token is None
    assert not session.exec(select(SyncCostLedger)).all()
    assert not session.exec(select(PendingDispatch)).all()
    cursor = session.get(ReportDispatchCursor, sync_seed.tenant_id)
    assert not cursor or all(not value for value in cursor.fairness_state.values())
    assert all(
        redis_client.zcard(key) == 0
        for ep in (CREATE, endpoint)
        for key in occupancy_keys(**_scope(sync_seed, ep))
    )
    assert _dispatch(session, redis_client, now) == 1
    ledger = session.exec(select(SyncCostLedger)).one()
    assert ledger.physical_endpoint == endpoint and ledger.reserved_units == 1
    assert ledger.generation == job.claim_generation == 1
    assert head.lease_execution_id == job.id
    assert head.lease_execution_generation == 1
    assert all(
        redis_client.zscore(key, str(job.claim_token)) is not None
        for key in occupancy_keys(**_scope(sync_seed, endpoint))
    )


@pytest.mark.parametrize("phase,endpoint", [("CHECK", CHECK), ("DOWNLOAD", DOWNLOAD)])
def test_actual_stage_cooldown_does_not_claim_zero_call_jobs(
    session, sync_seed, redis_client, phase, endpoint
):
    job = _job(session, sync_seed, phase=phase)
    now = datetime.now(UTC) + timedelta(seconds=1)
    cooldown = _cooldown(redis_client, sync_seed, endpoint)
    for _ in range(3):
        assert _dispatch(session, redis_client, now) == 0
    assert job.claim_generation == 0 and job.claim_token is None
    assert not session.exec(select(SyncCostLedger)).all()
    assert not session.exec(select(PendingDispatch)).all()
    head = session.exec(select(SyncSchedule)).one()
    assert head.physical_endpoint == endpoint and head.next_eligible_at > now
    redis_client.delete(cooldown)
    assert (
        _dispatch(session, redis_client, head.next_eligible_at + timedelta(seconds=1))
        == 1
    )


def test_outcome_routes_due_sibling_instead_of_finished_jobs_next_phase(
    session, sync_seed, redis_client
):
    now = datetime.now(UTC)
    current = _job(session, sync_seed, phase="CREATE", day=3, due=now)
    sibling = _job(session, sync_seed, phase="DOWNLOAD", day=2, due=now)
    assert _dispatch(session, redis_client, now + timedelta(seconds=1)) == 1
    assert current.claim_token is not None
    current.progress = current.progress | {
        "operation_log": {"phase": "CHECK", "task_id": "test-export"}
    }
    current.status, current.due_at = "WAITING", now + timedelta(seconds=30)
    record_report_outcome(session, current)
    current.claim_token = current.claimed_until = None
    session.flush()
    head = session.exec(select(SyncSchedule)).one()
    assert head.active and head.physical_endpoint == DOWNLOAD
    assert head.next_eligible_at == sibling.due_at and head.lease_token is None
    assert _dispatch(session, redis_client, now + timedelta(seconds=2)) == 1
    assert sibling.claim_generation == 1
    sibling.status = "COMPLETE"
    record_report_outcome(session, sibling)
    sibling.claim_token = sibling.claimed_until = None
    session.flush()
    assert head.active and head.physical_endpoint == CHECK
    assert head.next_eligible_at == current.due_at


def _gateway(monkeypatch, seed, redis, responses, *, opening_error=None):
    """只替换凭据网关入口和HTTP/对象存储运输；SDK、准入、计费、worker均真实。"""
    from urllib3.response import HTTPResponse

    from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
    from app.integrations.tiktok.attempt_observer import physical_attempt_scope
    from app.integrations.tiktok.gateway import _sdk_report_send
    from app.integrations.tiktok.platform_quotas import sdk_endpoint
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred, official_client
    from app.jobs.admission import admission_policy, admitted_scope
    from tests.modules.sync_control.test_archive import ObjectTransport

    calls = []

    def wire(pool, method, url, **_kw):
        endpoint = (
            f"{method} /open_api/v1.3/" + url.split("/open_api/v1.3/")[1].split("?")[0]
        )
        calls.append(endpoint)
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer is not None:
            observer[0].attempt(observer[1])
        # 到达物理发送时queued票必须已交接，而不是留到finally/TTL再清理。
        assert all(
            redis.zcard(key) == 0
            for ep in (CREATE, CHECK, DOWNLOAD)
            for key in occupancy_keys(**_scope(seed, ep))
        )
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": responses.pop(0), "request_id": "test"}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    objects = ObjectTransport()
    monkeypatch.setattr(
        "app.modules.operation_logs.archive.make_s3", lambda _config: objects
    )
    monkeypatch.setattr("app.core.config.settings.S3_BUCKET", "existing-material-test")

    @contextmanager
    def gateway(**kwargs):
        if opening_error is not None:
            raise opening_error
        with official_client(access_token="synthetic") as client:

            @contextmanager
            def scope(advertiser, operation, _deadline):
                endpoint = sdk_endpoint(operation)
                with (
                    admitted_scope(
                        redis,
                        **(_scope(seed, endpoint) | {"advertiser_id": advertiser}),
                        policy=admission_policy(
                            operation,
                            channel="OFFICIAL_API",
                            physical_endpoint=endpoint,
                        ),
                        denied_error=AccountAdmissionDeferred,
                    ),
                    physical_attempt_scope(client, endpoint),
                ):
                    yield

            yield SimpleNamespace(
                report_reader=SdkReportReader(
                    send=_sdk_report_send(client, scope, kwargs["task_deadline"])
                )
            )

    monkeypatch.setattr(job_tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    # 补位入口另有真实事务回归；这里逐步明确触发领取，观测阶段边界。
    monkeypatch.setattr(job_tasks, "_dispatch_pending", lambda *_a, **_kw: 0)
    return calls


def test_worker_hands_off_each_stage_and_charges_only_that_endpoint(
    log_seed, redis_client, monkeypatch
):
    from tests.modules.operation_logs.test_parser import export

    seed, factory = log_seed
    calls = _gateway(
        monkeypatch,
        seed,
        redis_client,
        [
            {"task_id": "test-export"},
            {"status": "SUCCESS"},
            {"status": "SUCCESS", "changelog": export()},
        ],
    )
    with factory() as db, db.begin():
        identity = _job(db, seed).id
    for generation, endpoint, next_endpoint in [
        (1, CREATE, CHECK),
        (2, CHECK, DOWNLOAD),
        (3, DOWNLOAD, None),
    ]:
        with factory() as db, db.begin():
            job = db.get(ReportSyncJob, identity)
            assert (
                _dispatch(
                    db,
                    redis_client,
                    max(job.due_at, datetime.now(UTC)) + timedelta(seconds=1),
                )
                == 1
            )
            token, database_engine = job.claim_token, db.get_bind()
        result = job_tasks.run_job_step(
            database_engine=database_engine,
            redis_client=redis_client,
            job_id=identity,
            claim_token=token,
        )
        assert result == ("COMPLETE" if next_endpoint is None else "WAITING")
        with factory() as db:
            job = db.get(ReportSyncJob, identity)
            head = db.exec(
                select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
            ).one()
            assert job.request_count == generation and job.claim_token is None
            assert head.lease_token is None
            assert head.active == (next_endpoint is not None)
            if next_endpoint:
                assert head.physical_endpoint == next_endpoint
            ledger = db.exec(
                select(SyncCostLedger).where(
                    SyncCostLedger.report_job_id == identity,
                    SyncCostLedger.generation == generation,
                )
            ).one()
            assert ledger.physical_endpoint == endpoint
            assert ledger.reserved_units == ledger.observed_units == 1 and ledger.closed
        assert all(
            redis_client.zscore(key, str(token)) is None
            for ep in (CREATE, CHECK, DOWNLOAD)
            for key in occupancy_keys(**_scope(seed, ep))
        )
    assert calls == [CREATE, CHECK, DOWNLOAD]


@pytest.mark.parametrize(
    "mode",
    ["send", "unavailable", "open_error", "expired", "wrong_token", "poll_limit"],
)
def test_old_create_ticket_for_check_uses_frozen_generation_on_exit(
    log_seed, redis_client, monkeypatch, mode
):
    from app.core.errors import DomainError

    seed, factory = log_seed
    calls = _gateway(
        monkeypatch,
        seed,
        redis_client,
        [{"status": "FAILED" if mode == "unavailable" else "PROCESSING"}],
        opening_error=DomainError("api_binding_required", "synthetic")
        if mode == "open_error"
        else None,
    )
    with factory() as db, db.begin():
        job = _job(db, seed)
        identity = job.id
        assert (
            _dispatch(db, redis_client, datetime.now(UTC) + timedelta(seconds=1)) == 1
        )
        token, database_engine = job.claim_token, db.get_bind()
        # 模拟升级前dispatcher为已有CHECK执行签发CREATE票和同代成本收据。
        job.progress = job.progress | {
            "operation_log": {
                "phase": "CHECK",
                "task_id": "test-export",
                "polls": 40 if mode == "poll_limit" else 0,
            }
        }
        if mode == "expired":
            job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        if mode == "wrong_token":
            job.claim_token = uuid4()
    result = job_tasks.run_job_step(
        database_engine=database_engine,
        redis_client=redis_client,
        job_id=identity,
        claim_token=token,
    )
    assert result == (
        "WAITING" if mode in {"send", "unavailable", "poll_limit"} else "FAILED"
    )
    assert calls == ([CHECK] if mode in {"send", "unavailable"} else [])
    assert all(
        redis_client.zscore(key, str(token)) is None
        for ep in (CREATE, CHECK, DOWNLOAD)
        for key in occupancy_keys(**_scope(seed, ep))
    )
    if mode in {"poll_limit", "unavailable"}:
        with factory() as db:
            job = db.get(ReportSyncJob, identity)
            head = db.exec(
                select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
            ).one()
            assert job.progress["operation_log"]["phase"] == "CREATE"
            assert head.active and head.physical_endpoint == CREATE
            assert job.request_count == (1 if mode == "unavailable" else 0)


def test_many_old_heads_repair_bounded_batch_without_holding_other_endpoint(
    committed_sync_seed, redis_client
):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        for number in range(24):
            advertiser = f"old-log-{number}"
            db.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=advertiser,
                    currency="USD",
                    timezone="UTC",
                    name=advertiser,
                )
            )
            db.flush()
            db.add(
                BCAccountAccess(
                    tenant_id=seed.tenant_id,
                    bc_id="bc-a",
                    advertiser_id=advertiser,
                    connection_id=seed.connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    checked_at=now,
                )
            )
            db.flush()
            _job(db, seed, phase="CHECK", advertiser=advertiser, due=now)
        live_id = enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="campaign_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=now.date(),
            end=now.date(),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            due_at=now,
        )
        # 让旧CREATE端点先获得发现机会；纠正预算不能把其他端点整轮饿死。
        live_head = db.exec(
            select(SyncSchedule).where(
                SyncSchedule.tenant_id == seed.tenant_id,
                SyncSchedule.physical_endpoint != CREATE,
            )
        ).one()
        live_head.last_dispatched_at = now - timedelta(seconds=1)
    with factory() as db, db.begin():
        assert (
            dispatch_due(
                db,
                redis_client,
                now=now + timedelta(seconds=1),
                physical_slots=36,
                max_claims=4,
            )
            == 1
        )
    with factory() as db:
        heads = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).all()
        assert sum(head.physical_endpoint == CHECK for head in heads) == 4
        assert sum(head.physical_endpoint == CREATE for head in heads) == 20
        assert db.get(ReportSyncJob, live_id).claim_generation == 1
        assert (
            len(
                db.exec(
                    select(SyncCostLedger).where(
                        SyncCostLedger.tenant_id == seed.tenant_id
                    )
                ).all()
            )
            == 1
        )


def test_stale_create_token_replay_cannot_release_new_check_generation(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        job = _job(db, seed)
        assert (
            _dispatch(db, redis_client, datetime.now(UTC) + timedelta(seconds=1)) == 1
        )
        identity, old_token, database_engine = job.id, job.claim_token, db.get_bind()
        # 上一代已经完成阶段跃迁，但Redis释放失败，旧票仍未到TTL。
        job.progress = job.progress | {
            "operation_log": {"phase": "CHECK", "task_id": "test-export"}
        }
        job.status, job.due_at = "WAITING", datetime.now(UTC)
        record_report_outcome(db, job)
        job.claim_token = job.claimed_until = None
    with factory() as db, db.begin():
        job = db.get(ReportSyncJob, identity)
        assert (
            _dispatch(db, redis_client, datetime.now(UTC) + timedelta(seconds=1)) == 1
        )
        new_token = job.claim_token
        assert job.claim_generation == 2 and new_token != old_token

    def snapshot():
        with factory() as db:
            return (
                db.get(ReportSyncJob, identity).model_dump(),
                [
                    row.model_dump()
                    for row in db.exec(
                        select(SyncSchedule).where(
                            SyncSchedule.tenant_id == seed.tenant_id
                        )
                    ).all()
                ],
                [
                    row.model_dump()
                    for row in db.exec(
                        select(SyncCostLedger)
                        .where(SyncCostLedger.tenant_id == seed.tenant_id)
                        .order_by(SyncCostLedger.id)
                    ).all()
                ],
                [
                    row.model_dump()
                    for row in db.exec(
                        select(PendingDispatch)
                        .where(PendingDispatch.tenant_id == seed.tenant_id)
                        .order_by(PendingDispatch.id)
                    ).all()
                ],
            )

    before = snapshot()
    assert all(
        redis_client.zscore(key, str(old_token)) is not None
        for key in occupancy_keys(**_scope(seed, CREATE))
    )
    assert (
        job_tasks.run_job_step(
            database_engine=database_engine,
            redis_client=redis_client,
            job_id=identity,
            claim_token=old_token,
        )
        == "FAILED"
    )
    assert snapshot() == before
    assert all(
        redis_client.zscore(key, str(old_token)) is None
        for ep in (CREATE, CHECK, DOWNLOAD)
        for key in occupancy_keys(**_scope(seed, ep))
    )
    assert all(
        redis_client.zscore(key, str(new_token)) is not None
        for key in occupancy_keys(**_scope(seed, CHECK))
    )


def test_daily_log_precedes_older_history_and_routes_its_stage(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.schedule import _pending_operation_endpoint

    now = datetime.now(UTC)
    _job(session, sync_seed, phase="CHECK", day=1, due=now)
    daily = _job(session, sync_seed, phase="DOWNLOAD", day=2, due=now)
    daily.priority = 3
    session.flush()
    head = session.exec(select(SyncSchedule)).one()
    assert _pending_operation_endpoint(session, head, now=now) == DOWNLOAD
    # 首轮修复旧端点，下一轮必须选中昨日任务，不能按创建时间发历史CHECK。
    assert _dispatch(session, redis_client, now + timedelta(seconds=1)) == 0
    assert head.physical_endpoint == DOWNLOAD
    assert _dispatch(session, redis_client, now + timedelta(seconds=1)) == 1
    assert daily.claim_token is not None
