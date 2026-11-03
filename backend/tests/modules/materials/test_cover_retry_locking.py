"""真实 PG：共享批次互斥不能阻塞封面重试的外键 KEY SHARE 检查。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import text
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.materials import cover_sharing, covers
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from tests.modules.materials.test_cover_throughput import image, matrix, page
from tests.modules.materials.test_covers import job_state, run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_shared_batch_lock_allows_original_cover_retry_to_commit(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]
    incomplete = page([])
    incomplete["page_info"].update(total_number=1, total_page=1)
    wire[1].extend([{"list": [image(0)]}, incomplete])
    run(source_env, redis_client, identity)
    with Session(engine) as db, db.begin():
        current = db.get(MaterialCoverJob, identity)
        current.status, current.dispatch_id = "BLOCKED", None
        current.claim_token = current.claimed_until = None
        batch = db.get(MaterialCoverShareBatch, current.share_batch_id)
        db.expunge(batch)

    batch_locked = Event()

    def shared_worker():
        with Session(engine) as db, db.begin():
            db.execute(text("SET LOCAL statement_timeout='5s'"))
            cover_sharing._locked_batch(db, source_env["context"], batch)
            batch_locked.set()
            db.exec(
                select(MaterialCoverJob)
                .where(
                    MaterialCoverJob.id == identity,
                )
                .with_for_update()
            ).one()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as retry:
            retry.execute(text("SET LOCAL statement_timeout='2s'"))
            retry.exec(
                select(MaterialCoverJob)
                .where(
                    MaterialCoverJob.id == identity,
                )
                .with_for_update()
            ).one()
            future = pool.submit(shared_worker)
            assert batch_locked.wait(timeout=3)
            try:
                covers.request_cover_retry(
                    retry,
                    context=source_env["context"],
                    job_id=identity,
                )
                retry.commit()
            finally:
                retry.rollback()
            future.result(timeout=5)
    current = job_state(identity)
    assert current.status == "PENDING" and current.dispatch_id
    assert current.share_batch_id == batch.id and current.request_armed_at is None
