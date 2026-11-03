"""真实 PG 的已 armed 20×10 批次续跑预算，不模拟数据库或放宽5秒期限。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.jobs.models import PendingDispatch
from app.modules.materials import cover_sharing, covers
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_covers import job_state
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def armed_rectangle(source_env, redis_client, wire, *, materials=20, targets=10):
    identities = matrix(source_env, materials, targets)
    wire[1].extend(
        [
            {"list": [image(index) for index in range(materials)]},
            *[page([]) for _ in range(targets)],
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    with Session(engine) as db:
        batch = db.get(MaterialCoverShareBatch, job_state(identities[0]).share_batch_id)
        first = job_state(batch.wake_job_id)
        armed_at = batch.armed_at
    assert armed_at is not None
    claimed = covers._claim(
        engine,
        source_env["context"],
        first.id,
        first.dispatch_id,
        first.revision,
        read=True,
    )
    assert claimed is not None
    return identities, claimed, armed_at


def member_snapshot(identities):
    with Session(engine) as db:
        return {
            job.id: job.model_dump()
            for job in db.exec(
                select(MaterialCoverJob).where(col(MaterialCoverJob.id).in_(identities))
            ).all()
        }


@pytest.mark.parametrize("phase", ["resume", "continue"])
def test_armed_twenty_by_ten_resume_and_continue_stay_inside_bounded_sql_budget(
    source_env, redis_client, wire, phase, record_property
):
    identities, claimed, armed_at = armed_rectangle(source_env, redis_client, wire)
    if phase == "continue":
        with Session(engine) as db, db.begin():
            batch, claims = cover_sharing._resume(db, source_env["context"], *claimed)
    statements = []

    def observe(connection, _cursor, statement, *_):
        if connection.engine.url.database == engine.url.database:
            statements.append(statement)

    started = monotonic()
    event.listen(Engine, "before_cursor_execute", observe)
    try:
        with (
            bounded_session(
                engine, task_deadline=datetime.now(UTC) + timedelta(seconds=30)
            ) as db,
            db.begin(),
        ):
            if phase == "resume":
                batch, claims = cover_sharing._resume(
                    db, source_env["context"], *claimed
                )
            else:
                cover_sharing._continue(db, source_env["context"], batch, claims)
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    elapsed = monotonic() - started
    record_property("phase", phase)
    record_property("sql_count", len(statements))
    record_property("elapsed_seconds", elapsed)
    print(  # noqa: T201 - 输出性能实测值，供独立服务器复验。
        f"{phase}: sql={len(statements)} elapsed={elapsed:.3f}s"
    )
    assert len(claims) == 200
    with Session(engine) as db:
        jobs = db.exec(
            select(MaterialCoverJob).where(col(MaterialCoverJob.id).in_(identities))
        ).all()
        assert all(
            job.request_armed_at == armed_at and job.status == "VERIFYING"
            for job in jobs
        )
        if phase == "continue":
            assert all(
                job.claim_token is None and job.claimed_until is None for job in jobs
            )
            wakes = [job for job in jobs if job.dispatch_id is not None]
            assert len(wakes) == 1
            dispatch = db.get(PendingDispatch, wakes[0].dispatch_id)
            assert dispatch.task_name == "materials.verify_cover"
            assert dispatch.payload == {
                "job_id": str(wakes[0].id),
                "revision": wakes[0].revision,
            }
        else:
            assert all(job.claim_token == claims[job.id] for job in jobs)
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    assert elapsed < 5
    # 批锁和批更新应保持固定查询数量，不能在200个成员间交替SELECT/UPDATE。
    assert len(statements) <= 30, (phase, len(statements), elapsed)
    # 验证预算在 4ms/语句延迟下仍有效：实测耗时 + 0.004 * 语句数 < 5秒。
    assert elapsed + 0.004 * len(statements) < 5


@pytest.mark.parametrize(
    "change", ["anchor_nonce", "anchor_revision", "anchor_dispatch", "peer_claim"]
)
def test_resume_rejects_stale_anchor_or_live_peer_before_reclaiming_members(
    source_env, redis_client, wire, change
):
    identities, claimed, _ = armed_rectangle(
        source_env, redis_client, wire, materials=2, targets=2
    )
    first, nonce = claimed
    peer_id = max(identity for identity in identities if identity != first.id)
    with Session(engine) as db, db.begin():
        job = db.get(MaterialCoverJob, peer_id if change == "peer_claim" else first.id)
        if change in {"anchor_nonce", "peer_claim"}:
            job.claim_token = uuid4()
            job.claimed_until = datetime.now(UTC) + timedelta(minutes=1)
        elif change == "anchor_revision":
            job.revision += 1
        else:
            job.dispatch_id = None
    before = member_snapshot(identities)
    with pytest.raises(DomainError):
        with Session(engine) as db, db.begin():
            cover_sharing._resume(db, source_env["context"], first, nonce)
    assert member_snapshot(identities) == before
    assert sum(call[0] == "POST" for call in wire[0]) == 1


@pytest.mark.parametrize("change", ["nonce", "expired", "membership"])
def test_continue_stale_peer_rolls_back_without_partial_wake_or_member_release(
    source_env, redis_client, wire, change
):
    identities, claimed, _ = armed_rectangle(
        source_env, redis_client, wire, materials=2, targets=2
    )
    with Session(engine) as db, db.begin():
        batch, claims = cover_sharing._resume(db, source_env["context"], *claimed)
    peer_id = max(identity for identity in identities if identity != batch.wake_job_id)
    with Session(engine) as db, db.begin():
        peer = db.get(MaterialCoverJob, peer_id)
        if change == "nonce":
            peer.claim_token = uuid4()
        elif change == "expired":
            peer.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        else:
            peer.share_batch_id = None
    before = member_snapshot(identities)
    with pytest.raises(DomainError):
        with Session(engine) as db, db.begin():
            cover_sharing._continue(db, source_env["context"], batch, claims)
    assert member_snapshot(identities) == before


@pytest.mark.parametrize("phase", ["continue", "publish"])
def test_continue_waits_for_batch_before_locking_any_member(
    source_env, redis_client, wire, phase
):
    identities, claimed, _ = armed_rectangle(
        source_env, redis_client, wire, materials=2, targets=2
    )
    with Session(engine) as db, db.begin():
        batch, claims = cover_sharing._resume(db, source_env["context"], *claimed)
    boundary, release = Event(), Event()

    def is_worker():
        return current_thread().name.startswith("cover-lock-order")

    def before(_connection, _cursor, statement, *_):
        if (
            is_worker()
            and statement.startswith("SELECT material_cover_share_batch.")
            and ("FOR UPDATE" in statement or "FOR NO KEY UPDATE" in statement)
        ):
            boundary.set()

    def after(_connection, _cursor, statement, *_):
        if (
            is_worker()
            and statement.startswith("SELECT material_cover_job.")
            and "FOR UPDATE" in statement
        ):
            boundary.set()
            assert release.wait(10)

    def continue_batch():
        with Session(engine) as db, db.begin():
            remaining = dict(claims)
            if phase == "publish":
                identity = identities[0]
                member = next(
                    member
                    for member in batch.members
                    if member["job_id"] == str(identity)
                )
                cover_sharing._publish(
                    db,
                    source_env["context"],
                    batch,
                    claims,
                    {
                        identity: {
                            "image_id": "target-proof",
                            "signature": member["signature"],
                        }
                    },
                )
                remaining.pop(identity)
            cover_sharing._continue(db, source_env["context"], batch, remaining)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cover-lock-order"
        ) as pool:
            try:
                with Session(engine) as holder, holder.begin():
                    holder.exec(
                        select(MaterialCoverShareBatch)
                        .where(MaterialCoverShareBatch.id == batch.id)
                        .with_for_update()
                    ).one()
                    future = pool.submit(continue_batch)
                    assert boundary.wait(10)
                    # 真正的第二连接 NOWAIT：等待 batch 的续跑不能先占住任意成员锁。
                    holder.exec(
                        select(MaterialCoverJob)
                        .where(col(MaterialCoverJob.id).in_(identities))
                        .with_for_update(nowait=True)
                    ).all()
            finally:
                release.set()
            future.result(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
