"""封面只读短暂失败可自动接续；预算耗尽和未知写入不能重新上传。"""

from datetime import timedelta

import pytest
from sqlmodel import Session, select
from urllib3.exceptions import ReadTimeoutError

from app.core.db import engine
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_covers import (
    image_info,
    job_state,
    queue,
    run,
    scopes,
    seed,
    successful_upload,
    video_info,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def timeout():
    return ReadTimeoutError(None, "/offline-read", "offline timeout")


def test_source_video_read_timeout_recovers_without_manual_retry(
    source_env, redis_client, wire
):
    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    wire[1].append(timeout())
    run(source_env, redis_client, identity)
    failed = job_state(identity)
    assert failed.status == "PENDING" and failed.dispatch_id
    assert failed.failure_count == 1 and failed.request_armed_at is None
    wire[1].extend(
        [
            video_info(),
            {
                "image_id": "source-image",
                "signature": "a" * 32,
                "width": 720,
                "height": 1280,
                "displayable": False,
            },
        ]
    )
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "READY"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


@pytest.mark.parametrize("failure", ["timeout", "schema"])
def test_first_share_inventory_read_has_bounded_automatic_retry(
    source_env, redis_client, wire, failure
):
    identity = matrix(source_env, 1, 1)[0]
    bad = page([])
    bad["page_info"].update(total_number=1, total_page=1)
    wire[1].extend([{"list": [image(0)]}, timeout() if failure == "timeout" else bad])
    run(source_env, redis_client, identity)
    failed = job_state(identity)
    assert failed.dispatch_id and failed.status == "PENDING"
    with Session(engine) as db:
        assert (
            db.get(MaterialCoverShareBatch, failed.share_batch_id).status == "PREPARING"
        )
    assert failed.request_armed_at is None
    assert sum(call[0] == "POST" for call in wire[0]) == 0
    wire[1].extend([page([]), {"failed_infos": {}}, page([image(0, target_id=True)])])
    drive(source_env, redis_client, identity)
    drive(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_repeated_source_read_timeout_stops_after_three_attempts(
    source_env, redis_client, wire
):
    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    for attempt in range(3):
        wire[1].append(timeout())
        run(source_env, redis_client, identity)
        current = job_state(identity)
        assert current.failure_count == attempt + 1
        assert current.status == ("BLOCKED" if attempt == 2 else "PENDING")
        assert bool(current.dispatch_id) is (attempt < 2)
    assert len(wire[0]) == 3 and all(call[0] == "GET" for call in wire[0])


def test_source_image_details_timeout_resumes_same_shared_batch(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]
    wire[1].append(timeout())
    run(source_env, redis_client, identity)
    failed = job_state(identity)
    assert failed.status == "PENDING" and failed.dispatch_id
    assert failed.request_armed_at is None and failed.failure_count == 1
    wire[1].extend(
        [
            {"list": [image(0)]},
            page([]),
            {"failed_infos": {}},
            page([image(0, target_id=True)]),
        ]
    )
    drive(source_env, redis_client, identity)
    drive(source_env, redis_client, identity, read=True)
    current = job_state(identity)
    assert current.status == "READY" and current.share_batch_id == failed.share_batch_id
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_repeated_first_share_inventory_timeout_exhausts_budget_without_sending(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]
    wire[1].append({"list": [image(0)]})
    for attempt in range(3):
        wire[1].append(timeout())
        run(source_env, redis_client, identity)
        current = job_state(identity)
        assert current.failure_count == attempt + 1
        assert current.status == ("BLOCKED" if attempt == 2 else "PENDING")
        assert bool(current.dispatch_id) is (attempt < 2)
        assert current.request_armed_at is None
    assert all(call[0] == "GET" for call in wire[0])


def test_known_image_bulk_timeout_recovers_without_upload(
    source_env, redis_client, wire
):
    identity = successful_upload(source_env, redis_client, wire)
    before = len(wire[0])
    wire[1].append(timeout())
    run(source_env, redis_client, identity, read=True)
    current = job_state(identity)
    assert current.status == "VERIFYING" and current.dispatch_id
    assert current.known_image_id == "target-image" and current.request_armed_at
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert all(call[0] == "GET" for call in wire[0][before:])


@pytest.mark.parametrize(
    ("code", "failures", "resumed"),
    [
        ("tiktok_response_error", 1, True),
        ("tiktok_response_error", 3, False),
        ("cover_permission_unverified", 1, False),
    ],
)
def test_new_target_resumes_old_failed_source_only_with_remaining_read_budget(
    source_env, redis_client, wire, code, failures, resumed
):
    identity = matrix(source_env, 1, 1)[0]
    with Session(engine) as db, db.begin():
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == source_env["context"].tenant_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).one()
        source.status, source.error_code = "BLOCKED", code
        source.failure_count = failures
        source.created_at = covers._now() - timedelta(hours=6)
        source.request_armed_at = source.known_image_id = None
        from app.modules.materials.models import AccountMaterial

        db.get(AccountMaterial, source.asset_id).image_id = None
        source_id = source.id
    # 与实际导入一致：只允许从有原件上传正证据的位置接续，不从目标账户猜源。
    from tests.modules.materials.test_source_uploads import seed_operation

    seed_operation(
        source_env,
        status="succeeded",
        evidence={
            "video_id": "vid-actual-account",
            "upload_video_id": "vid-actual-account",
        },
    )
    run(source_env, redis_client, identity)
    source = job_state(source_id)
    assert source.status == ("PENDING" if resumed else "BLOCKED")
    assert bool(source.dispatch_id) is resumed
    assert source.failure_count == failures
    target = job_state(identity)
    assert target.error_code == (
        "cover_source_pending" if resumed else "cover_source_unavailable"
    )
    assert target.status == ("PENDING" if resumed else "BLOCKED")
    assert not wire[0]


def test_unknown_image_upload_timeout_only_reconciles_original_send(
    source_env, redis_client, wire
):
    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    wire[1].extend([video_info(), timeout()])
    run(source_env, redis_client, identity)
    sent = job_state(identity)
    assert sent.request_armed_at and sent.status == "VERIFYING"
    wire[1].append(timeout())
    run(source_env, redis_client, identity, read=True)
    current = job_state(identity)
    assert current.request_armed_at == sent.request_armed_at
    assert current.status == "VERIFYING" and current.dispatch_id
    assert sum(call[0] == "POST" for call in wire[0]) == 1
