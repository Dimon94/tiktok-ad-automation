"""Cover jobs use real PostgreSQL/Redis and only fake official SDK transport."""

from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.routing import freeze_route
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from tests.modules.materials.test_readiness import asset
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def queue(env, *, source=True):
    with Session(engine) as session, session.begin():
        return (covers.ensure_source_cover if source else covers.ensure_cover)(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id="actual-account",
            task_key=f"build:{uuid4()}",
            route=freeze_route(
                session,
                context=env["context"],
                bc_id=env["bc_id"],
                connection_id=env["connection_id"],
            ),
        )


def seed(env):
    with Session(engine) as session, session.begin():
        row = asset(session, env, "actual-account")
        return row.id


def test_shared_cover_job_is_local_and_reuses_actual_video_identity(source_env, wire):
    seed(source_env)
    first, second = queue(source_env), queue(source_env)
    assert first.state == second.state == "queued"
    assert first.task_id == second.task_id
    with Session(engine) as session:
        job = session.get(MaterialCoverJob, first.task_id)
        assert job.video_id == "vid-actual-account"
        assert job.connection_id == source_env["connection_id"]
        assert job.request_armed_at is None and job.known_image_id is None
        assert len(session.exec(select(MaterialCoverJob)).all()) == 1
    assert not wire[0]


def test_existing_image_is_ready_without_enqueue_or_source_copy(source_env, wire):
    identity = seed(source_env)
    with Session(engine) as session, session.begin():
        session.get(AccountMaterial, identity).image_id = "verified-target-image"
    result = queue(source_env, source=False)
    assert result.state == "ready"
    assert result.mapping.image_id == "verified-target-image"
    assert result.task_id is None and not wire[0]


def job_state(identity):
    with Session(engine) as session:
        value = session.get(MaterialCoverJob, identity)
        session.expunge(value)
        return value


def run(env, redis_client, identity, *, read=False, dispatch=None, revision=None):
    job = job_state(identity)
    covers.run_cover(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        job_id=identity,
        dispatch_id=dispatch or job.dispatch_id,
        revision=revision or job.revision,
        read=read,
    )


def scopes(env):
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import TikTokConnection

    with Session(engine) as session, session.begin():
        session.get(
            TikTokConnection, env["connection_id"]
        ).credential_ciphertext = encrypt_credentials(
            tenant_id=env["context"].tenant_id,
            value={"access_token": "offline-token-secret", "scope": "[6]"},
        )


def video_info(url="https://example.com/temporary-secret"):
    from tests.modules.materials.test_source_uploads import MD5

    return {
        "list": [
            {
                "video_id": "vid-actual-account",
                "displayable": True,
                "signature": MD5,
                "width": 720,
                "height": 1280,
                "video_cover_url": url,
            }
        ]
    }


def image_info(identity, **changes):
    return {
        "list": [
            {
                "image_id": "target-image",
                "material_id": "900001",
                "file_name": job_state(identity).remote_name,
                "displayable": True,
                "signature": "a" * 32,
                "width": 360,
                "height": 640,
                **changes,
            }
        ]
    }


def successful_upload(env, redis_client, wire):
    scopes(env)
    seed(env)
    result = queue(env)
    wire[1].extend([video_info(), {"image_id": "target-image", "signature": "a" * 32}])
    run(env, redis_client, result.task_id)
    return result.task_id


def test_stale_ready_cover_explicit_reconciliation_only_refreshes_known_image(
    source_env, redis_client, wire
):
    from datetime import UTC, datetime, timedelta

    from app.jobs.models import PendingDispatch

    identity = successful_upload(source_env, redis_client, wire)
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    with Session(engine) as session, session.begin():
        job = session.get(MaterialCoverJob, identity)
        job.updated_at = datetime.now(UTC) - timedelta(hours=1)
        session.add(job)
    before = job_state(identity)
    with Session(engine) as session, session.begin():
        result = covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
        assert result.state == "queued"
        job = session.get(MaterialCoverJob, identity)
        assert job.known_image_id == before.known_image_id
        assert job.request_armed_at == before.request_armed_at
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.verify_cover"
        )
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert [call[0] for call in wire[0]] == ["GET", "POST", "GET", "GET"]


def test_upload_arms_before_wire_and_readback_updates_only_target(
    source_env, redis_client, wire
):
    import json

    scopes(source_env)
    mapping_id = seed(source_env)
    identity = queue(source_env).task_id

    def upload():
        assert job_state(identity).request_armed_at is not None
        return {"image_id": "target-image", "signature": "a" * 32}

    wire[1].extend([video_info(), upload])
    old = job_state(identity)
    run(source_env, redis_client, identity)
    assert job_state(identity).known_image_id == "target-image"
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    with Session(engine) as session:
        mapping = session.get(AccountMaterial, mapping_id)
        assert (
            mapping.image_id == "target-image"
            and mapping.video_id == "vid-actual-account"
        )
        assert mapping.cover_url is None
    run(
        source_env,
        redis_client,
        identity,
        dispatch=old.dispatch_id,
        revision=old.revision,
    )
    assert [call[0] for call in wire[0]] == ["GET", "POST", "GET"]
    assert json.loads(wire[0][1][2]["body"])["upload_type"] == "UPLOAD_BY_URL"
    assert "temporary-secret" not in job_state(identity).model_dump_json()


def test_unknown_upload_empty_search_stops_until_explicit_read_only_reconcile(
    source_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    wire[1].extend([video_info(), ReadTimeoutError(None, "offline", "SECRET")])
    run(source_env, redis_client, identity)
    assert queue(source_env).reason_code == "cover_result_unknown"
    wire[1].append(
        {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 0,
                "total_number": 0,
            },
        }
    )
    run(source_env, redis_client, identity, read=True)
    assert (
        job_state(identity).status == "UNKNOWN"
        and job_state(identity).dispatch_id is None
    )
    assert queue(source_env).state == "blocked"
    with Session(engine) as session, session.begin():
        assert covers.repair_cover_dispatches(session) == 0
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(
        {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 0,
                "total_number": 0,
            },
        }
    )
    run(source_env, redis_client, identity, read=True)
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_old_ready_cover_get_and_ensure_reuse_without_readback(
    source_env, redis_client, wire
):
    from datetime import timedelta

    identity = successful_upload(source_env, redis_client, wire)
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    with Session(engine) as session, session.begin():
        job = session.get(MaterialCoverJob, identity)
        job.updated_at -= timedelta(days=1)
    with Session(engine) as session, session.begin():
        result = covers.get_cover_status(
            session, context=source_env["context"], job_id=identity
        )
        assert result.state == "ready"
    assert job_state(identity).dispatch_id is None
    assert queue(source_env).state == "ready"
    assert job_state(identity).status == "READY"
    assert [call[0] for call in wire[0]].count("POST") == 1
    assert len(wire[0]) == 3


def test_unarmed_permission_failure_can_explicitly_retry_same_identity(
    source_env, redis_client, wire
):
    seed(source_env)
    identity = queue(source_env).task_id
    run(source_env, redis_client, identity)
    old = job_state(identity)
    assert old.status == "BLOCKED" and old.request_armed_at is None
    scopes(source_env)
    with Session(engine) as session, session.begin():
        value = covers.request_cover_retry(
            session, context=source_env["context"], job_id=identity
        )
        assert value.task_id == identity and value.state == "queued"
    assert job_state(identity).remote_name == old.remote_name
    wire[1].extend([video_info(), {"image_id": "target-image", "signature": "a" * 32}])
    run(source_env, redis_client, identity)
    assert job_state(identity).known_image_id == "target-image"


def unknown(env, redis_client, wire):
    from urllib3.exceptions import ReadTimeoutError

    scopes(env)
    seed(env)
    identity = queue(env).task_id
    wire[1].extend([video_info(), ReadTimeoutError(None, "offline", "SECRET")])
    run(env, redis_client, identity)
    return identity


def search_page(rows, *, page=1, total=None, page_size=100):
    total = len(rows) if total is None else total
    return {
        "list": rows,
        "page_info": {
            "page": page,
            "page_size": page_size,
            "total_page": (total + page_size - 1) // page_size,
            "total_number": total,
        },
    }


def test_search_completes_every_page_before_unique_name_info(
    source_env, redis_client, wire
):
    from app.modules.materials.cover_models import MaterialCoverJobPage

    identity = unknown(source_env, redis_client, wire)
    first = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    first[0] = image_info(identity)["list"][0]
    wire[1].append(search_page(first, total=101))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).next_page == 2
    assert job_state(identity).known_image_id is None
    wire[1].append(
        search_page([{"image_id": "last", "file_name": "other.jpg"}], page=2, total=101)
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).known_image_id == "target-image"
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    with Session(engine) as session:
        pages = session.exec(
            select(MaterialCoverJobPage).where(MaterialCoverJobPage.job_id == identity)
        ).all()
        assert sorted(len(page.image_ids) for page in pages) == [1, 100]
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_search_ambiguity_after_first_page_match_never_marks_ready(
    source_env, redis_client, wire
):
    identity = unknown(source_env, redis_client, wire)
    rows = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    rows[0] = image_info(identity)["list"][0]
    wire[1].append(search_page(rows, total=101))
    run(source_env, redis_client, identity, read=True)
    wire[1].append(
        search_page(
            image_info(identity, image_id="second-matching-image")["list"],
            page=2,
            total=101,
        )
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).known_image_id is None
    assert job_state(identity).dispatch_id is None


def test_duplicate_cross_page_id_and_persistent_total_change_stop_incomplete_scan(
    source_env, redis_client, wire
):
    identity = unknown(source_env, redis_client, wire)
    rows = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    wire[1].append(search_page(rows, total=101))
    run(source_env, redis_client, identity, read=True)
    wire[1].append(search_page([rows[0]], page=2, total=101))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "VERIFYING"
    # 跨页重复允许有界补齐；三轮仍只有100个唯一项不能冒充101项完整。
    for size in (71, 53):
        wire[1].extend(
            [
                search_page(rows[:size], total=101, page_size=size),
                search_page(rows[size - 1 :], page=2, total=101, page_size=size),
            ]
        )
        run(source_env, redis_client, identity, read=True)
        run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).error_code == "cover_search_incomplete"
    with Session(engine) as session, session.begin():
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    wire[1].append(search_page(rows, total=101))
    run(source_env, redis_client, identity, read=True)
    wire[1].append(
        search_page(
            [
                {"image_id": "new", "file_name": "new.jpg"},
                {"image_id": "new2", "file_name": "new2.jpg"},
            ],
            page=2,
            total=102,
        )
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "VERIFYING"
    # 显式核查有独立预算；库存持续变化三次才停止，不能拼接不同轮证据。
    for total in (102, 103):
        wire[1].append(search_page(rows, total=total))
        run(source_env, redis_client, identity, read=True)
        wire[1].append(
            search_page(
                [
                    {"image_id": f"changed-{i}", "file_name": "new.jpg"}
                    for i in range(total + 1 - 100)
                ],
                page=2,
                total=total + 1,
            )
        )
        run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).error_code == "cover_search_incomplete"


def test_search_at_official_10000_visibility_cap_is_incomplete(
    source_env, redis_client, wire
):
    identity = unknown(source_env, redis_client, wire)
    rows = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    rows[0] = image_info(identity)["list"][0]
    wire[1].append(search_page(rows, total=10000))
    run(source_env, redis_client, identity, read=True)
    assert (
        job_state(identity).status == "UNKNOWN"
        and job_state(identity).known_image_id is None
    )
    assert job_state(identity).error_code == "cover_search_incomplete"


def test_known_id_wrong_hash_or_geometry_never_ready(source_env, redis_client, wire):
    identity = successful_upload(source_env, redis_client, wire)
    wire[1].append(image_info(identity, file_name="foreign.jpg", signature="b" * 32))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    with Session(engine) as session, session.begin():
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    wire[1].append(image_info(identity, signature="b" * 32))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    with Session(engine) as session, session.begin():
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    wire[1].append(image_info(identity, width=640, height=360))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert all("search" not in call[1] for call in wire[0])


def test_changed_video_during_image_read_does_not_attach_old_cover(
    source_env, redis_client, wire
):
    identity = successful_upload(source_env, redis_client, wire)

    def change_video():
        data = image_info(identity)
        with Session(engine) as session, session.begin():
            session.get(
                AccountMaterial, job_state(identity).asset_id
            ).video_id = "replaced-vid"
        return data

    wire[1].append(change_video)
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    with Session(engine) as session:
        assert (
            session.get(AccountMaterial, job_state(identity).asset_id).image_id is None
        )


def test_revoke_between_video_get_and_post_blocks_without_arming(
    source_env, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id

    def revoke():
        with Session(engine) as session, session.begin():
            grant = session.get(
                BCAccountAccess,
                (
                    source_env["context"].tenant_id,
                    source_env["bc_id"],
                    "actual-account",
                    source_env["connection_id"],
                ),
            )
            grant.can_upload = False
        return video_info()

    wire[1].append(revoke)
    run(source_env, redis_client, identity)
    assert job_state(identity).request_armed_at is None
    assert job_state(identity).status == "BLOCKED"
    assert [call[0] for call in wire[0]] == ["GET"]


def test_known_receipt_commits_before_sdk_cleanup_interrupt(
    source_env, redis_client, wire, monkeypatch
):
    import urllib3
    from billiard.exceptions import SoftTimeLimitExceeded

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    original = urllib3.PoolManager.clear
    seen = []

    def cleanup(pool):
        original(pool)
        if len(wire[0]) == 2:
            job = job_state(identity)
            seen.append(job.known_image_id)
            raise SoftTimeLimitExceeded()

    monkeypatch.setattr(urllib3.PoolManager, "clear", cleanup)
    wire[1].extend([video_info(), {"image_id": "target-image", "signature": "a" * 32}])
    run(source_env, redis_client, identity)
    assert seen == ["target-image"]
    assert job_state(identity).status == "VERIFYING"
    # 完整HTTP与DTO已返回后物理租约正常结束；后续工厂清理中断不抹掉持久ID。
    from app.core.config import settings
    from app.jobs.admission import admission_keys

    keys = admission_keys(
        settings.TIKTOK_APP_ID,
        "materials.upload_image_url",
        source_env["context"].tenant_id,
        "actual-account",
    )
    assert all(redis_client.zcard(key) == 0 for key in keys[2:])
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_receipt_transaction_failure_appends_id_before_cleanup_then_only_reads(
    source_env, redis_client, wire, monkeypatch
):
    from datetime import timedelta

    import urllib3
    from sqlalchemy import event

    from app.modules.materials.cover_models import MaterialCoverReceipt

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    failed = []
    original = urllib3.PoolManager.clear

    def reject_receipt(_conn, _cursor, statement, _params, _context, _many):
        if (
            "UPDATE material_cover_job SET" in statement
            and "known_image_id=" in statement
            and not failed
        ):
            failed.append(True)
            raise RuntimeError("synthetic receipt commit failure")

    def cleanup(pool):
        if len(wire[0]) == 2:
            with Session(engine) as session:
                assert (
                    session.exec(
                        select(MaterialCoverReceipt.image_id).where(
                            MaterialCoverReceipt.job_id == identity
                        )
                    ).one()
                    == "target-image"
                )
        original(pool)

    monkeypatch.setattr(urllib3.PoolManager, "clear", cleanup)
    event.listen(engine, "before_cursor_execute", reject_receipt)
    try:
        wire[1].extend(
            [video_info(), {"image_id": "target-image", "signature": "a" * 32}]
        )
        run(source_env, redis_client, identity)
    finally:
        event.remove(engine, "before_cursor_execute", reject_receipt)
    assert failed
    with Session(engine) as session, session.begin():
        job = session.get(MaterialCoverJob, identity)
        job.claimed_until = job.repair_after = covers._now() - timedelta(seconds=1)
        covers.repair_cover_dispatches(session)
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_armed_dead_worker_is_repaired_to_read_only_and_old_nonce_cannot_write(
    source_env, redis_client, wire
):
    from datetime import timedelta

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    old = job_state(identity)
    claim = covers._claim(
        engine,
        source_env["context"],
        identity,
        old.dispatch_id,
        old.revision,
        read=False,
    )
    with Session(engine) as session, session.begin():
        job = session.get(MaterialCoverJob, identity)
        job.request_armed_at = covers._now()
        job.claimed_until = job.repair_after = covers._now() - timedelta(seconds=1)
        assert covers.repair_cover_dispatches(session) == 1
    wire[1].append(search_page([]))
    run(
        source_env,
        redis_client,
        identity,
        dispatch=old.dispatch_id,
        revision=old.revision,
    )
    assert not wire[0]
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert [call[0] for call in wire[0]] == ["GET"]
    with Session(engine) as session:
        assert (
            covers._fenced(session, source_env["context"], identity, claim[1]) is None
        )


def test_concurrent_callers_and_duplicate_workers_share_one_post(
    source_env, redis_client, wire
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    scopes(source_env)
    seed(source_env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: queue(source_env), range(2)))
    assert results[0].task_id == results[1].task_id
    identity = results[0].task_id
    entered, release = Event(), Event()

    def video():
        entered.set()
        assert release.wait(timeout=5)
        return video_info()

    wire[1].extend([video, {"image_id": "target-image", "signature": "a" * 32}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(run, source_env, redis_client, identity)
        assert entered.wait(timeout=5)
        duplicate = pool.submit(run, source_env, redis_client, identity)
        duplicate.result(timeout=5)
        release.set()
        original.result(timeout=5)
    assert [call[0] for call in wire[0]] == ["GET", "POST"]


def test_retry_refuses_every_armed_known_receipt_and_active_dispatch(
    source_env, redis_client, wire
):
    import pytest

    from app.core.errors import DomainError
    from app.modules.materials.cover_models import MaterialCoverReceipt

    identity = successful_upload(source_env, redis_client, wire)
    with Session(engine) as session, session.begin():
        with pytest.raises(DomainError, match="只能核查"):
            covers.request_cover_retry(
                session, context=source_env["context"], job_id=identity
            )
        job = session.get(MaterialCoverJob, identity)
        job.status = "BLOCKED"
        job.known_image_id = None
        job.request_armed_at = None
        job.claim_token = job.claimed_until = job.dispatch_id = None
        session.add(
            MaterialCoverReceipt(
                tenant_id=job.tenant_id, job_id=job.id, image_id="late-image"
            )
        )
        session.flush()
        with pytest.raises(DomainError, match="只能核查"):
            covers.request_cover_retry(
                session, context=source_env["context"], job_id=identity
            )
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_suggestion_url_is_uploaded_but_its_id_and_url_are_not_stored(
    source_env, redis_client, wire
):
    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    wire[1].extend(
        [
            video_info(None),
            {
                "list": [
                    {
                        "id": "not-image-id",
                        "url": "https://example.com/suggest-secret",
                        "width": 360,
                        "height": 640,
                    }
                ]
            },
            {"image_id": "target-image", "signature": "a" * 32},
        ]
    )
    run(source_env, redis_client, identity)
    assert [call[0] for call in wire[0]] == ["GET", "GET", "POST"]
    assert job_state(identity).known_image_id == "target-image"
    assert "suggest-secret" not in job_state(identity).model_dump_json()


def test_repair_does_not_replace_live_claim_even_if_repair_timestamp_is_due(source_env):
    from datetime import timedelta

    seed(source_env)
    identity = queue(source_env).task_id
    old = job_state(identity)
    claim = covers._claim(
        engine,
        source_env["context"],
        identity,
        old.dispatch_id,
        old.revision,
        read=False,
    )
    with Session(engine) as session, session.begin():
        session.get(MaterialCoverJob, identity).repair_after = (
            covers._now() - timedelta(seconds=1)
        )
        session.flush()
        assert covers.repair_cover_dispatches(session) == 0
    assert job_state(identity).claim_token == claim[1]


def test_wrong_task_kind_cannot_consume_current_prepare_dispatch(
    source_env, redis_client, wire
):
    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    before = job_state(identity)
    run(source_env, redis_client, identity, read=True)
    after = job_state(identity)
    assert (after.status, after.revision, after.claim_token) == (
        before.status,
        before.revision,
        None,
    )
    assert not wire[0]


def test_scope_and_viewer_reads_are_local_but_cannot_retry_or_prepare(
    source_env, redis_client, wire
):
    import pytest

    from app.core.errors import DomainError
    from app.modules.tenants.models import TenantMembership
    from tests.modules.conftest import create_context

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    with Session(engine) as session, session.begin():
        temporary = session.begin_nested()
        other = create_context(session)
        with pytest.raises(DomainError):
            covers.get_cover_status(session, context=other, job_id=identity)
        temporary.rollback()
        membership = session.get(
            TenantMembership,
            (source_env["context"].tenant_id, source_env["context"].actor_id),
        )
        membership.role = "viewer"
    with Session(engine) as session:
        value = covers.get_cover_status(
            session, context=source_env["context"], job_id=identity
        )
        assert value.task_id == identity and value.state == "queued"
        with pytest.raises(DomainError):
            covers.request_cover_reconciliation(
                session, context=source_env["context"], job_id=identity
            )
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "BLOCKED" and not wire[0]


def test_every_sdk_call_releases_db_and_separately_checks_shared_admission(
    source_env, redis_client, wire
):
    from sqlalchemy import text

    from app.core.config import settings
    from app.jobs.admission import admission_keys

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id

    def inspect(endpoint, response):
        with Session(engine) as session:
            current = session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction' AND pid<>pg_backend_pid()"
                )
            ).scalar_one()
            assert current == 0
        keys = admission_keys(
            settings.TIKTOK_APP_ID,
            endpoint,
            source_env["context"].tenant_id,
            "actual-account",
        )
        assert all(redis_client.zcard(key) == 1 for key in keys[2:])
        return response

    wire[1].extend(
        [
            # 准入已按真实物理接口共桶；逻辑操作名不是Redis端点身份。
            lambda: inspect("GET /open_api/v1.3/file/video/ad/info/", video_info()),
            lambda: inspect(
                "POST /open_api/v1.3/file/image/ad/upload/",
                {"image_id": "target-image", "signature": "a" * 32},
            ),
        ]
    )
    run(source_env, redis_client, identity)
    wire[1].append(
        lambda: inspect("GET /open_api/v1.3/file/image/ad/info/", image_info(identity))
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"


def test_repair_preserves_unpublished_broker_backoff_and_published_generation(
    source_env,
):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch

    seed(source_env)
    identity = queue(source_env).task_id
    old = job_state(identity)
    future = covers._now() + timedelta(hours=1)
    with Session(engine) as session, session.begin():
        session.get(MaterialCoverJob, identity).repair_after = (
            covers._now() - timedelta(seconds=1)
        )
        dispatch = session.get(PendingDispatch, old.dispatch_id)
        dispatch.available_at, dispatch.attempts = future, 4
        session.flush()
        covers.repair_cover_dispatches(session)
    assert (job_state(identity).dispatch_id, job_state(identity).revision) == (
        old.dispatch_id,
        old.revision,
    )
    with Session(engine) as session, session.begin():
        dispatch = session.get(PendingDispatch, old.dispatch_id)
        assert dispatch.available_at == future and dispatch.attempts == 4
        dispatch.published_at = covers._now() - timedelta(seconds=120)
        dispatch.available_at = covers._now() - timedelta(seconds=120)
        session.get(MaterialCoverJob, identity).repair_after = (
            covers._now() - timedelta(seconds=1)
        )
        session.flush()
        assert covers.repair_cover_dispatches(session) == 1
    with Session(engine) as session:
        assert session.get(PendingDispatch, old.dispatch_id).published_at is None
    assert (job_state(identity).dispatch_id, job_state(identity).revision) == (
        old.dispatch_id,
        old.revision,
    )


def test_cover_worker_rejects_unbounded_execution_before_any_sdk(source_env, wire):
    import pytest

    from app.core.errors import DomainError
    from app.modules.materials import cover_tasks

    for task in (cover_tasks.prepare_cover, cover_tasks.verify_cover):
        assert task.time_limit == 45 and task.soft_time_limit == 40
        with pytest.raises(DomainError, match="prefork"):
            task.run(
                tenant_id=str(source_env["context"].tenant_id),
                actor_id=str(source_env["context"].actor_id),
                payload={"job_id": str(uuid4()), "revision": 1},
            )
    assert not wire[0]


def test_cover_worker_rejects_boolean_revision_and_extra_payload_before_db(
    source_env, wire, monkeypatch
):
    import pytest

    from app.core.errors import DomainError
    from app.modules.materials import cover_tasks

    monkeypatch.setattr(
        cover_tasks, "require_bounded_worker", lambda *args, **kwargs: None
    )
    for payload in (
        {"job_id": str(uuid4()), "revision": True},
        {"job_id": str(uuid4()), "revision": 1, "url": "https://example.com/untrusted"},
    ):
        with pytest.raises(DomainError, match="参数无效"):
            cover_tasks.prepare_cover.run(
                tenant_id=str(source_env["context"].tenant_id),
                actor_id=str(source_env["context"].actor_id),
                payload=payload,
            )
    assert not wire[0]


def test_deduplicated_video_cover_with_false_image_displayable_reuses_known_id(
    source_env, redis_client, wire
):
    identity = successful_upload(source_env, redis_client, wire)
    before = len(wire[0])
    wire[1].append(
        image_info(identity, file_name="renamed-by-dedup.jpg", displayable=False)
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert len(wire[0]) == before + 1
    assert "image/ad/info" in wire[0][-1][1]
