"""真实 PG/Redis 与官方 SDK 传输替身，验证账户内读取确实并行。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock

from sqlalchemy import text
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.db import engine
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from app.modules.builds.scene_jobs import process_scene_turn
from tests.modules.builds.scene.test_scene_reads import page
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_recovery import ready_for_get
from tests.modules.builds.scene_jobs.test_service import job_env as job_env
from tests.modules.builds.scene_jobs.test_service import responses
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire

PATHS = {
    "identity": "/identity/get/",
    "minis": "/minis/get/",
    "cta": "/creative/cta/recommend/",
    "vbo": "/tool/vbo_status/",
    "regions": "/tool/region/",
}


class Reads:
    def __init__(self, env, *, overlap=False):
        self.data = dict(zip(PATHS, responses(env), strict=True))
        self.calls = []
        self.active = self.peak = 0
        self.lock = Lock()
        self.barrier = Barrier(3) if overlap else None
        self.entered, self.release = Event(), Event()
        self.release.set()
        self.failure = None
        self.hook = None

    def __call__(self, _pool, method, url, **kwargs):
        resource = next(key for key, path in PATHS.items() if url.endswith(path))
        number = int(dict(kwargs.get("fields", [])).get("page", 1))
        with self.lock:
            self.calls.append((resource, number))
            position = len(self.calls)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            assert method == "GET"
            if self.barrier and position <= 3:
                self.barrier.wait(timeout=3)
                self.entered.set()
            assert self.release.wait(5)
            if self.hook:
                self.hook(resource)
            if resource == self.failure:
                self.failure = None
                raise OSError("offline read failed")
            data = self.data[resource]
            if callable(data):
                data = data(number)
            return HTTPResponse(
                body=json.dumps(
                    {"code": 0, "data": data, "request_id": "offline-parallel"}
                ).encode(),
                status=200,
            )
        finally:
            with self.lock:
                self.active -= 1


def turn(env, redis_client, job_id, payload=None):
    with Session(engine) as db, db.begin():
        job = db.get(SceneJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
        payload = payload or {"job_id": str(job.id), "revision": job.revision}
    process_scene_turn(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload=payload,
    )
    with Session(engine) as db:
        return db.get(SceneJob, job_id)


def setup_reads(env, wire, redis_client, monkeypatch, *, overlap=False, capacity=4):
    job = ready_for_get(env, wire, redis_client)
    policies = settings.TIKTOK_CALL_POLICIES
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **policies,
            "base": {**policies["base"], "advertiser_max_inflight": capacity},
        },
    )
    remote = Reads(env, overlap=overlap)
    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda pool, method, url, **kwargs: remote(pool, method, url, **kwargs),
    )
    return job, remote


def test_independent_reads_overlap_without_duplicate_delivery_or_database_lock(
    job_env, wire, redis_client, monkeypatch
):
    env = job_env
    job, remote = setup_reads(env, wire, redis_client, monkeypatch, overlap=True)
    remote.release.clear()
    with Session(engine) as db:
        current = db.get(SceneJob, job.id)
        payload = {"job_id": str(job.id), "revision": current.revision}
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(turn, env, redis_client, job.id)
        try:
            assert remote.entered.wait(5), "三类独立读取应同时进入传输边界"
            with Session(engine) as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout='200ms'"))
                db.exec(
                    select(SceneJob).where(SceneJob.id == job.id).with_for_update()
                ).one()
            duplicate = turn(env, redis_client, job.id, payload)
            assert duplicate.claim_token is not None
        finally:
            remote.release.set()
        result = running.result(timeout=15)
    assert result.status == "COMPLETE", result.error_code
    assert remote.peak == 3 and remote.active == 0
    assert sorted(remote.calls) == sorted((r, 1) for r in PATHS)
    before = list(remote.calls)
    turn(env, redis_client, job.id, payload)
    assert remote.calls == before


def test_failed_resource_preserves_other_successful_pages_and_only_retries_missing(
    job_env, wire, redis_client, monkeypatch
):
    env = job_env
    job, remote = setup_reads(env, wire, redis_client, monkeypatch)
    remote.failure = "vbo"
    result = turn(env, redis_client, job.id)
    assert result.status == "PENDING" and result.failure_count == 1
    with Session(engine) as db:
        pages = db.exec(select(SceneJobPage).where(SceneJobPage.job_id == job.id)).all()
        assert {p.resource for p in pages} == set(PATHS) - {"vbo"}
        saved = {p.id for p in pages}
    before = len(remote.calls)
    result = turn(env, redis_client, job.id)
    assert result.status == "COMPLETE"
    assert remote.calls[before:] == [("vbo", 1)]
    with Session(engine) as db:
        assert saved <= set(
            db.exec(select(SceneJobPage.id).where(SceneJobPage.job_id == job.id)).all()
        )


def test_minis_next_page_waits_for_its_own_checkpoint_not_other_resources(
    job_env, wire, redis_client, monkeypatch
):
    env = job_env
    job, remote = setup_reads(env, wire, redis_client, monkeypatch)
    first = remote.data["minis"]["list"][0]

    def minis(number):
        rows = (
            [{"minis_id": f"unselected-{i}"} for i in range(50)]
            if number == 1
            else [{"minis_id": "last-mini"}]
        )
        if number == 1:
            rows[0] = first
        return page(rows, number=number, total=2)

    remote.data["minis"] = minis
    result = turn(env, redis_client, job.id)
    assert result.status == "PENDING" and result.resource == "minis"
    assert result.facts["minis"]["seen"] == 50
    assert set(result.facts) >= set(PATHS)
    assert remote.calls.count(("minis", 1)) == 1
    assert ("minis", 2) not in remote.calls
    result = turn(env, redis_client, job.id)
    assert result.status == "COMPLETE" and result.facts["minis"]["seen"] == 51
    assert remote.calls[-1] == ("minis", 2)


def test_account_quota_of_one_is_respected_without_artificial_deferrals(
    job_env, wire, redis_client, monkeypatch
):
    job, remote = setup_reads(job_env, wire, redis_client, monkeypatch, capacity=1)
    result = turn(job_env, redis_client, job.id)
    assert result.status == "COMPLETE", result.error_code
    assert result.failure_count == 0 and remote.peak == 1
    assert len(remote.calls) == 5


def test_unknown_target_reads_account_facts_in_parallel_and_selection_reuses_them(
    job_env, wire, redis_client, monkeypatch
):
    from app.modules.builds.mini_targets import MiniTarget, url_key
    from app.modules.providers.models import PromotionLink, ProviderApplication
    from tests.modules.builds.scene_jobs.test_service import ensure

    env = job_env
    with Session(engine) as db, db.begin():
        link = db.get(PromotionLink, env["link_id"])
        db.delete(db.get(MiniTarget, (env["context"].tenant_id, url_key(link.url))))
        db.exec(
            select(ProviderApplication).where(
                ProviderApplication.tenant_id == link.tenant_id,
                ProviderApplication.connection_id == link.connection_id,
                ProviderApplication.external_id == link.application_id,
            )
        ).one().tiktok_minis_id = None
    job, remote = setup_reads(env, wire, redis_client, monkeypatch)
    result = turn(env, redis_client, job.id)
    assert result.status == "COMPLETE" and result.minis_id is None
    assert set(result.facts) >= set(PATHS)
    assert len(remote.calls) == 5
    with Session(engine) as db, db.begin():
        db.get(
            PromotionLink, env["link_id"]
        ).url = "https://www.tiktok.com/minis/fixture-minis"
    assert ensure(env).state == "ready"
    assert len(remote.calls) == 5


def test_authority_change_during_parallel_reads_cannot_publish_receipts(
    job_env, wire, redis_client, monkeypatch
):
    from app.modules.tenants.models import TenantMembership

    env = job_env
    job, remote = setup_reads(env, wire, redis_client, monkeypatch, overlap=True)
    lock, revoked = Lock(), Event()

    def revoke(_resource):
        with lock:
            if not revoked.is_set():
                with Session(engine) as db, db.begin():
                    db.get(
                        TenantMembership,
                        (env["context"].tenant_id, env["context"].actor_id),
                    ).role = "viewer"
                revoked.set()

    remote.hook = revoke
    result = turn(env, redis_client, job.id)
    assert result.status == "BLOCKED"
    assert result.error_code in {"action_forbidden", "tenant_forbidden"}
    assert remote.active == 0
    with Session(engine) as db:
        assert not db.exec(
            select(SceneJobPage.id).where(SceneJobPage.job_id == job.id)
        ).all()


def test_interrupted_batch_keeps_committed_page_and_resumes_only_missing(
    job_env, wire, redis_client, monkeypatch
):
    import pytest

    from app.modules.builds import scene_jobs
    from tests.modules.providers.test_link_recovery import SimulatedCrash

    env = job_env
    job, remote = setup_reads(env, wire, redis_client, monkeypatch)
    save = scene_jobs._save_scene_page

    def crash_after_checkpoint(db, *args):
        save(db, *args)
        db.commit()
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(scene_jobs, "_save_scene_page", crash_after_checkpoint)
        with pytest.raises(SimulatedCrash):
            turn(env, redis_client, job.id)
    assert remote.active == 0
    with Session(engine) as db, db.begin():
        saved_resource = db.exec(
            select(SceneJobPage.resource).where(SceneJobPage.job_id == job.id)
        ).one()
        current = db.get(SceneJob, job.id)
        assert current.status == "PENDING" and current.claim_token is not None
        current.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
    before = len(remote.calls)
    result = turn(env, redis_client, job.id)
    assert result.status == "COMPLETE"
    assert all(resource != saved_resource for resource, _ in remote.calls[before:])
