"""Remote-only target preparation after staged originals have been deleted."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.config import settings
from app.core.db import engine
from app.modules.accounts.routing import freeze_route
from app.modules.materials.ingest_models import TemporaryMaterialObject
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.modules.builds.test_execution import executable as executable
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import asset, read, target
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import CONTENT, MD5
from tests.modules.materials.test_url_ingest import url_env as url_env
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)

PREVIEW = "https://media.vetted.example/video?signature=never-persist"


def test_cross_bc_preview_checks_primary_once_per_group(remote_env, wire):
    from time import perf_counter

    from sqlalchemy import event, text

    from app.modules.materials.readiness import get_material_readiness_batch

    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, remote_env["material_id"])
        ids = [original.id]
        for _ in range(24):
            identity = uuid4()
            db.add(
                MaterialFile(
                    **(
                        original.model_dump()
                        | {
                            "id": identity,
                            "object_key": f"preview-batch/{identity}",
                        }
                    )
                )
            )
            ids.append(identity)
    counts, durations = [], []
    for group in ([ids[0]], ids):
        statements = []
        with Session(engine) as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            connection = db.connection()

            def record(_c, _cu, statement, _p, _ct, _many, sink=statements):
                sink.append(statement)

            event.listen(connection, "before_cursor_execute", record)
            started = perf_counter()
            try:
                rows = get_material_readiness_batch(
                    db,
                    context=remote_env["context"],
                    bc_id=remote_env["bc_id"],
                    material_ids=group,
                    advertiser_id=remote_env["target"],
                )
            finally:
                event.remove(connection, "before_cursor_execute", record)
            durations.append(perf_counter() - started)
            counts.append(len(statements))
            assert all(row.state == "preparable" for row in rows.values())
            assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    print(f"cross_bc_preview: queries={counts}, seconds={durations}")  # noqa: T201
    assert counts[0] == counts[1]
    assert wire[0] == []


def test_cross_bc_preview_rechecks_primary_permission_on_next_call(remote_env, wire):
    from app.modules.accounts.models import BCAccountAccess, TenantBC
    from app.modules.materials.readiness import get_material_readiness_batch

    with Session(engine) as db, db.begin():
        destination = target(db, remote_env, advertiser_id="second-target")
        db.get(
            TenantBC, (remote_env["context"].tenant_id, remote_env["bc_id"])
        ).material_advertiser_id = remote_env["target"]
    for allowed in (False, True):
        with Session(engine) as db, db.begin():
            grant = db.get(
                BCAccountAccess,
                (
                    remote_env["context"].tenant_id,
                    remote_env["bc_id"],
                    remote_env["target"],
                    remote_env["connection_id"],
                ),
            )
            grant.can_build = allowed
        with Session(engine) as db:
            rows = get_material_readiness_batch(
                db,
                context=remote_env["context"],
                bc_id=remote_env["bc_id"],
                material_ids=[remote_env["material_id"]],
                advertiser_id=destination,
            )
            row = rows[remote_env["material_id"]]
            assert row.state == ("preparable" if allowed else "blocked")
            if not allowed:
                assert row.reason_code == "account_access_denied"
    assert wire[0] == []


def test_target_and_source_routes_are_persisted_before_network(remote_env, wire):
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

    prepared = queue(remote_env, remote_env["target"])
    assert prepared.state == "queued", prepared
    dist, op, mapping = state(prepared.task_id)
    target_route = FrozenTikTokRoute.model_validate(dist.target_route)
    source_route = FrozenTikTokRoute.model_validate(dist.source_route)
    assert target_route.connection_id == remote_env["connection_id"]
    assert source_route.connection_id == remote_env["connection_id"]
    assert (
        target_route.tenant_id
        == source_route.tenant_id
        == remote_env["context"].tenant_id
    )
    assert op is not None and mapping is None and wire[0] == []
    assert FrozenTikTokRoute.model_validate(op.frozen_route) == target_route


@pytest.fixture
def remote_env(url_env, monkeypatch):
    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"media.vetted.example"})
    )
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, url_env["object_id"])
        obj.status = "deleted"
        obj.deleted_at = obj.reservation_released_at = datetime.now(UTC)
        material = db.get(MaterialFile, url_env["material_id"])
        material.storage_state = "unavailable"
        source = asset(db, url_env, "actual-account", mid=None)
        source_id = source.id
        # URL 转存用例使用真实跨 BC 范围；同 BC 原生共享另有独立回归。
        from app.modules.accounts.connection_models import (
            BCConnectionBinding,
            BCDefaultRoute,
        )
        from app.modules.accounts.models import TenantBC

        target_bc = "target-bc"
        db.add(TenantBC(tenant_id=material.tenant_id, bc_id=target_bc))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=material.tenant_id,
                bc_id=target_bc,
                connection_id=url_env["connection_id"],
                kind="OFFICIAL_API",
            )
        )
        db.flush()
        db.add(
            BCDefaultRoute(
                tenant_id=material.tenant_id,
                bc_id=target_bc,
                connection_id=url_env["connection_id"],
            )
        )
        target_file = MaterialFile(
            **(
                material.model_dump()
                | {"id": uuid4(), "bc_id": target_bc, "object_key": str(uuid4())}
            )
        )
        db.add(target_file)
        db.flush()
        destination = {**url_env, "bc_id": target_bc, "material_id": target_file.id}
        account = target(db, destination)
    for key in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(settings, key, "")
    return {
        **destination,
        "target": account,
        "source_id": source_id,
        "source_bc_id": url_env["bc_id"],
        "source_material_id": url_env["material_id"],
    }


def info(*, vid="vid-actual-account", **changes):
    return {
        "list": [
            {
                "video_id": vid,
                "signature": MD5,
                "displayable": True,
                "width": 1080,
                "height": 1920,
                "duration": 4.5,
                "size": len(CONTENT),
                "format": "mp4",
                "preview_url": PREVIEW,
                **changes,
            }
        ]
    }


def test_deleted_original_with_legal_vid_only_source_is_locally_preparable(
    remote_env, wire
):
    readiness = read(remote_env, remote_env["target"])
    assert (readiness.state, readiness.path) == ("preparable", "share_source")
    assert wire[0] == []


def test_remote_relay_uses_actual_source_and_target_vid_without_storage(
    remote_env, redis_client, wire
):
    prepared = queue(remote_env, remote_env["target"])
    assert prepared.state == "queued"
    wire[1].extend(
        [info(), [{"video_id": "actual-target", "material_id": "actual-target-mid"}]]
    )
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready" and mapping.video_id == "actual-target"
    assert op.path == "share_source" and op.remote_response["transport"] == "url_relay"
    assert op.remote_response["source_asset_id"] == str(remote_env["source_id"])
    fields = dict(wire[0][1][2]["fields"])
    assert fields["upload_type"] == "UPLOAD_BY_URL"
    assert fields["video_url"] == PREVIEW and "video_file" not in fields
    assert fields["advertiser_id"] == remote_env["target"]
    run(remote_env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready" and op.status == "succeeded"
    assert mapping.video_id == "actual-target" and mapping.mid == "actual-target-mid"
    assert PREVIEW not in repr(op.remote_response)
    assert [item[0] for item in wire[0]] == ["GET", "POST"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"video_id": "different-target"},
        {"size": 999},
        {"width": None},
        {"displayable": False},
    ],
)
def test_invalidated_relay_target_requires_exact_received_vid_and_strong_media(
    remote_env, redis_client, wire, mutation
):
    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([info(), [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    # 普通成功回执不再追加核查；只有实际映射已失效的恢复才读取平台。
    with Session(engine) as db, db.begin():
        db.get(AccountMaterial, state(prepared.task_id)[2].id).status = "unavailable"
    response = info(vid="actual-target")
    response["list"][0].update(mutation)
    wire[1].append(response)
    run(remote_env, redis_client, prepared.task_id, read_only=True)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status != "ready" and mapping.video_id == "actual-target"
    assert mapping.status != "available"
    assert op.remote_response["video_id"] == "actual-target"
    assert read(remote_env, remote_env["target"]).state != "ready"


def test_remote_preview_get_only_reads_and_never_persists_url(remote_env, wire):
    from datetime import timedelta

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import select

    from app.core.errors import DomainError, domain_error_handler
    from app.core.security import create_access_token
    from app.jobs.models import PendingDispatch
    from app.modules.materials.models import MaterialAssetOperation
    from app.modules.materials.router import router

    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api")
    wire[1].append(info())
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + create_access_token(
            remote_env["context"].actor_id, timedelta(minutes=5)
        )
        response = client.get(
            f"/api/tenants/{remote_env['context'].tenant_id}/materials/{remote_env['source_material_id']}/remote-preview",
            params={"bc_id": remote_env["source_bc_id"]},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["url"] == PREVIEW
    assert response.json()["video_id"] == "vid-actual-account"
    assert [call[0] for call in wire[0]] == ["GET"]
    with Session(engine) as db:
        assert (
            db.exec(
                select(MaterialAssetOperation).where(
                    MaterialAssetOperation.material_id == remote_env["material_id"]
                )
            ).all()
            == []
        )
        assert (
            db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == remote_env["context"].tenant_id
                )
            ).all()
            == []
        )


def test_revoked_representative_does_not_hide_another_legal_source(
    remote_env, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess

    with Session(engine) as db, db.begin():
        source_env = {
            **remote_env,
            "bc_id": remote_env["source_bc_id"],
            "material_id": remote_env["source_material_id"],
        }
        other = target(db, source_env, advertiser_id="second-source")
        legal = asset(db, source_env, other, mid=None)
        legal_id = legal.id
        db.get(
            BCAccountAccess,
            (
                remote_env["context"].tenant_id,
                remote_env["source_bc_id"],
                "actual-account",
                remote_env["connection_id"],
            ),
        ).authorized = False
    prepared = queue(remote_env, remote_env["target"])
    assert prepared.state == "queued"
    wire[1].extend([info(vid="vid-second-source"), [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[1].remote_response["source_asset_id"] == str(
        legal_id
    )
    assert dict(wire[0][0][2]["fields"])["advertiser_id"] == other


def test_all_sources_revoked_block_without_original_or_external_call(remote_env, wire):
    from app.modules.accounts.models import BCAccountAccess

    with Session(engine) as db, db.begin():
        db.get(
            BCAccountAccess,
            (
                remote_env["context"].tenant_id,
                remote_env["source_bc_id"],
                "actual-account",
                remote_env["connection_id"],
            ),
        ).authorized = False
    assert read(remote_env, remote_env["target"]).state == "blocked"
    assert queue(remote_env, remote_env["target"]).state == "blocked"
    assert wire[0] == []


def test_unknown_relay_only_checks_same_target_and_never_refreshes_source_url(
    remote_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([info(), ReadTimeoutError(None, PREVIEW, "unknown")])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    before = state(prepared.task_id)[1]
    assert before.status == "result_unknown"
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    wire[1].append(
        {
            "list": [
                {
                    "video_id": "other-content",
                    "file_name": before.remote_response["remote_name"],
                    "signature": "0" * 32,
                }
            ],
            "page_info": {"page": 1, "page_size": 100, "total_page": 1},
        }
    )
    run(remote_env, redis_client, prepared.task_id)
    after = state(prepared.task_id)[1]
    assert after.status == "result_unknown" and after.path == "share_source"
    assert (
        after.remote_response["source_asset_id"]
        == before.remote_response["source_asset_id"]
    )
    assert after.request_digest == before.request_digest
    assert [x[0] for x in wire[0]] == ["GET", "POST", "GET"]
    assert dict(wire[0][-1][2]["fields"])["advertiser_id"] == remote_env["target"]
    assert PREVIEW not in repr(after.remote_response)


def test_source_revoked_during_info_prevents_target_post(
    remote_env, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess

    def revoke():
        with Session(engine) as db, db.begin():
            db.get(
                BCAccountAccess,
                (
                    remote_env["context"].tenant_id,
                    remote_env["source_bc_id"],
                    "actual-account",
                    remote_env["connection_id"],
                ),
            ).authorized = False
        return info()

    prepared = queue(remote_env, remote_env["target"])
    wire[1].append(revoke)
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "blocked" and op.remote_response["definite_no_effect"] is True
    assert mapping is None
    assert [x[0] for x in wire[0]] == ["GET"]


def test_relay_received_id_survives_sdk_cleanup_failure(
    remote_env, redis_client, wire, monkeypatch
):
    from urllib3 import PoolManager

    real_clear = PoolManager.clear
    prepared = queue(remote_env, remote_env["target"])

    def failing_close(pool):
        real_clear(pool)
        if any(call[0] == "POST" for call in wire[0]):
            assert (
                state(prepared.task_id)[1].remote_response["video_id"]
                == "actual-target"
            )
            raise RuntimeError(PREVIEW)

    monkeypatch.setattr(PoolManager, "clear", failing_close)
    wire[1].extend(
        [info(), [{"video_id": "actual-target", "material_id": "actual-target-mid"}]]
    )
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    op = state(prepared.task_id)[1]
    assert op.remote_response["video_id"] == "actual-target"
    assert op.remote_response["upload_mid"] == "actual-target-mid"
    assert PREVIEW not in repr(op.remote_response)


def test_unknown_relay_unique_name_digest_search_then_info_can_finish(
    remote_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([info(), ReadTimeoutError(None, PREVIEW, "unknown")])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    op = state(prepared.task_id)[1]
    wire[1].append(
        {
            "list": [
                {
                    "video_id": "actual-target",
                    "signature": MD5,
                    "file_name": op.remote_response["remote_name"],
                }
            ],
            "page_info": {"page": 1, "page_size": 100, "total_page": 1},
        }
    )
    run(remote_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[1].remote_response["video_id"] == "actual-target"
    wire[1].append(info(vid="actual-target"))
    run(remote_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[0].status == "ready"
    assert [x[0] for x in wire[0]] == ["GET", "POST", "GET", "GET"]
    # 回查证明实际上传结果，但不得伪造丢失的上传响应。
    recovered = state(prepared.task_id)[1]
    assert recovered.remote_response.get("upload_video_id") is None
    assert recovered.remote_response["verified_upload_video_id"] == "actual-target"
    from tests.modules.materials.test_cover_throughput import enqueue
    from tests.modules.materials.test_covers import job_state, scopes
    from tests.modules.materials.test_covers import run as cover_run

    scopes(remote_env)
    cover = enqueue(remote_env, account=remote_env["target"])
    before = len(wire[0])
    cover_run(remote_env, redis_client, cover.task_id)
    assert (job_state(cover.task_id).purpose, job_state(cover.task_id).status) == (
        "SOURCE",
        "PENDING",
    )
    assert len(wire[0]) == before


def test_receipt_single_database_failure_retries_before_client_cleanup(
    remote_env, redis_client, wire, monkeypatch
):
    import urllib3
    from sqlalchemy import event

    failed, checked = [], []
    prepared = queue(remote_env, remote_env["target"])

    def reject_once(_conn, _cursor, statement, _params, _context, _many):
        if (
            "UPDATE material_asset_operation SET" in statement
            and len(wire[0]) == 2
            and not failed
        ):
            failed.append(True)
            raise RuntimeError("synthetic transaction failure")

    original = urllib3.PoolManager.clear

    def cleanup(pool):
        if len(wire[0]) == 2:
            assert (
                state(prepared.task_id)[1].remote_response.get("video_id")
                == "actual-target"
            )
            checked.append(True)
        original(pool)

    monkeypatch.setattr(urllib3.PoolManager, "clear", cleanup)
    event.listen(engine, "before_cursor_execute", reject_once)
    try:
        wire[1].extend([info(), [{"video_id": "actual-target"}]])
        run(remote_env, redis_client, prepared.task_id, kind="prepare")
    finally:
        event.remove(engine, "before_cursor_execute", reject_once)
    assert failed and checked
    assert state(prepared.task_id)[1].remote_response["video_id"] == "actual-target"


def test_nested_source_info_uses_bounded_read_budget_and_shared_quota(
    remote_env, redis_client, wire
):
    from app.integrations.tiktok.platform_quotas import sdk_endpoint
    from app.jobs.admission import admission_keys, admission_policy
    from app.modules.materials.source_uploads import READ_HARD_LIMIT, UPLOAD_HARD_LIMIT

    operation = "materials.get_videos"
    policy = admission_policy(operation)
    keys = admission_keys(
        settings.TIKTOK_APP_ID,
        sdk_endpoint(operation),
        remote_env["context"].tenant_id,
        "actual-account",
    )

    def inside_info():
        now_ms = datetime.now(UTC).timestamp() * 1000
        for key in keys[2:]:
            rows = redis_client.zrange(key, 0, -1, withscores=True)
            assert len(rows) == 1
            assert (
                now_ms + (READ_HARD_LIMIT - 5) * 1000
                < rows[0][1]
                < now_ms + UPLOAD_HARD_LIMIT * 1000
            )
        return info()

    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([inside_info, [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "ready"
    assert all(redis_client.zcard(key) == 0 for key in keys[2:])
    assert admission_policy(operation) == policy


def test_source_info_shared_admission_denial_never_reads_or_posts(
    remote_env, redis_client, wire
):
    from uuid import uuid4

    from app.jobs.admission import admission_policy, admit_call, release_call
    from app.modules.materials import sdk_assets as api

    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": api.INFO_ENDPOINT,
        "tenant_id": remote_env["context"].tenant_id,
        "advertiser_id": "actual-account",
        "lease_id": uuid4(),
    }
    assert admit_call(
        redis_client, **scope, policy=admission_policy(api.INFO_ENDPOINT)
    ).granted
    prepared = queue(remote_env, remote_env["target"])
    try:
        run(remote_env, redis_client, prepared.task_id, kind="prepare")
    finally:
        release_call(redis_client, **scope)
    assert state(prepared.task_id)[0].status == "queued"
    assert state(prepared.task_id)[1].status == "pending"
    assert wire[0] == []


def test_source_info_exception_releases_its_admission_and_never_posts(
    remote_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    from app.jobs.admission import admission_keys
    from app.modules.materials import sdk_assets as api

    prepared = queue(remote_env, remote_env["target"])
    wire[1].append(ReadTimeoutError(None, PREVIEW, "source info timeout"))
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    keys = admission_keys(
        settings.TIKTOK_APP_ID,
        api.INFO_ENDPOINT,
        remote_env["context"].tenant_id,
        "actual-account",
    )
    assert all(redis_client.zcard(key) == 0 for key in keys[2:])
    assert state(prepared.task_id)[1].remote_response["definite_no_effect"] is True
    assert [item[0] for item in wire[0]] == ["GET"]


def test_relay_unknown_search_rejects_unbounded_pagination(
    remote_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([info(), ReadTimeoutError(None, PREVIEW, "unknown")])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    wire[1].append(
        {
            "list": [{"video_id": "candidate"}],
            "page_info": {"page": 1, "page_size": 100, "total_page": 101},
        }
    )
    run(remote_env, redis_client, prepared.task_id)
    op = state(prepared.task_id)[1]
    assert op.remote_response["error_code"] == "material_reconciliation_bounded"
    assert op.status == "result_unknown"


def test_exact_source_connection_survives_new_preferred_legal_connection(
    remote_env, redis_client, wire
):
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.connection_models import BCDefaultRoute
    from app.modules.accounts.models import TikTokConnection
    from tests.modules.materials.route_support import second_connection

    prepared = queue(remote_env, remote_env["target"])
    with Session(engine) as db, db.begin():
        newer = second_connection(db, remote_env)
        db.get(TikTokConnection, newer).credential_ciphertext = encrypt_credentials(
            tenant_id=remote_env["context"].tenant_id,
            value={"access_token": "alternate-fixture-token"},
        )
        db.get(
            BCDefaultRoute, (remote_env["context"].tenant_id, remote_env["bc_id"])
        ).connection_id = newer
    wire[1].extend([info(), [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "ready"
    assert all(
        call[2]["headers"]["Access-Token"] == "offline-token-secret" for call in wire[0]
    )
    assert state(prepared.task_id)[1].remote_response["source_connection_id"] == str(
        remote_env["connection_id"]
    )


def test_role_change_blocks_prepared_relay_before_external_read(
    remote_env, redis_client, wire
):
    from sqlmodel import select

    from app.modules.tenants.models import TenantMembership

    prepared = queue(remote_env, remote_env["target"])
    with Session(engine) as db, db.begin():
        member = db.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == remote_env["context"].tenant_id
            )
        ).one()
        member.role = "viewer"
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "blocked"
    assert wire[0] == []


def test_deleted_original_target_cover_stays_independent_and_retry_is_read_only(
    remote_env, redis_client, wire
):
    from uuid import uuid4

    from app.modules.materials import covers
    from app.modules.materials.models import AccountMaterial
    from tests.modules.materials.test_covers import image_info, scopes
    from tests.modules.materials.test_covers import run as cover_run

    scopes(remote_env)
    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([info(), [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    run(remote_env, redis_client, prepared.task_id)
    with Session(engine) as db, db.begin():
        result = covers.ensure_cover(
            db,
            context=remote_env["context"],
            bc_id=remote_env["bc_id"],
            material_id=remote_env["material_id"],
            advertiser_id=remote_env["target"],
            task_key=f"cover:{uuid4()}",
            route=freeze_route(
                db,
                context=remote_env["context"],
                bc_id=remote_env["bc_id"],
                connection_id=remote_env["connection_id"],
            ),
        )
    wire[1].extend(
        [
            info(
                vid="actual-target", video_cover_url="https://image.example.test/cover"
            ),
            {"image_id": "target-image", "signature": "a" * 32},
        ]
    )
    cover_run(remote_env, redis_client, result.task_id)
    # URL relay 是实际上传所有者；首轮只确认上传回执并转入源封面任务。
    from app.modules.materials.cover_models import MaterialCoverJob

    with Session(engine) as db:
        source_job = db.get(MaterialCoverJob, result.task_id)
        assert (source_job.purpose, source_job.status) == ("SOURCE", "PENDING")
    cover_run(remote_env, redis_client, result.task_id)
    wire[1].append(image_info(result.task_id))
    cover_run(remote_env, redis_client, result.task_id, read=True)
    with Session(engine) as db:
        from sqlmodel import select

        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == remote_env["material_id"],
                AccountMaterial.advertiser_id == remote_env["target"],
            )
        ).one()
        assert (
            mapping.video_id == "actual-target" and mapping.image_id == "target-image"
        )
        assert (
            db.get(TemporaryMaterialObject, remote_env["object_id"]).status == "deleted"
        )
    before = len(wire[0])
    cover_run(remote_env, redis_client, result.task_id)
    assert len(wire[0]) == before


def test_frozen_material_steps_use_verified_targets_after_original_cleanup(
    executable, redis_client, monkeypatch
):
    from sqlmodel import select

    from app.modules.builds.execution_models import ExecutionStep
    from tests.modules.builds.test_execution import run as execute_kind

    db_engine, context, identities = executable
    with Session(db_engine) as db, db.begin():
        files = db.exec(
            select(MaterialFile).where(MaterialFile.tenant_id == context.tenant_id)
        ).all()
        material_ids = {file.id for file in files}
        for file in files:
            file.storage_state = "unavailable"
        before = {
            step.id: step.material_id
            for step in db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.tenant_id == context.tenant_id,
                    ExecutionStep.kind == "MATERIAL",
                )
            ).all()
        }

    def no_external(*_args, **_kwargs):
        pytest.fail("Frozen target mappings must not read or upload original bytes")

    monkeypatch.setattr("business_api_client.ApiClient.call_api", no_external)
    execute_kind(executable, redis_client, "MATERIAL")
    with Session(db_engine) as db:
        steps = [db.get(ExecutionStep, identity) for identity in identities["MATERIAL"]]
        assert all(
            step.status == "SUCCEEDED" and step.material_id in material_ids
            for step in steps
        )
        assert {step.id: step.material_id for step in steps} == before
        assert all(
            step.resolved["mapping"]["video_id"].startswith("target-") for step in steps
        )


def test_target_verification_keeps_sending_connection_when_preference_changes(
    remote_env, redis_client, wire
):
    from uuid import UUID

    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    prepared = queue(remote_env, remote_env["target"])
    wire[1].extend([info(), [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    with Session(engine) as db, db.begin():
        newer = UUID(int=remote_env["connection_id"].int // 2)
        db.add(
            TikTokConnection(
                id=newer,
                tenant_id=remote_env["context"].tenant_id,
                status="ACTIVE",
                credential_ciphertext=encrypt_credentials(
                    tenant_id=remote_env["context"].tenant_id,
                    value={"access_token": "alternate-fixture-token"},
                ),
            )
        )
        db.flush()
        old = db.get(
            BCAccountAccess,
            (
                remote_env["context"].tenant_id,
                remote_env["bc_id"],
                remote_env["target"],
                remote_env["connection_id"],
            ),
        )
        db.add(BCAccountAccess(**{**old.model_dump(), "connection_id": newer}))
    wire[1].append(info(vid="actual-target"))
    run(remote_env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready"
    assert op.remote_response["upload_connection_id"] == str(
        remote_env["connection_id"]
    )
    assert mapping.connection_id == remote_env["connection_id"]


def test_concurrent_relay_workers_issue_one_target_post(remote_env, redis_client, wire):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, finish = Event(), Event()
    prepared = queue(remote_env, remote_env["target"])

    def pending():
        entered.set()
        assert finish.wait(10)
        return [{"video_id": "actual-target"}]

    wire[1].extend([info(), pending])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run, remote_env, redis_client, prepared.task_id, kind="prepare"
        )
        try:
            assert entered.wait(10)
            second = pool.submit(
                run, remote_env, redis_client, prepared.task_id, kind="prepare"
            )
            second.result(timeout=10)
        finally:
            finish.set()
        first.result(timeout=10)
    assert [item[0] for item in wire[0]] == ["GET", "POST"]
    assert state(prepared.task_id)[1].remote_response["video_id"] == "actual-target"


def test_late_relay_receipt_keeps_id_without_clobbering_new_claim(
    remote_env, redis_client, wire
):
    from datetime import timedelta
    from uuid import uuid4

    from app.modules.materials.models import MaterialAssetOperation

    prepared = queue(remote_env, remote_env["target"])
    op_id = state(prepared.task_id)[1].id
    successor = uuid4()

    def takeover():
        with Session(engine) as db, db.begin():
            op = db.get(MaterialAssetOperation, op_id)
            op.status, op.attempt_token = "result_unknown", successor
            op.claimed_until = datetime.now(UTC) + timedelta(seconds=60)
        return [{"video_id": "actual-target", "material_id": "actual-mid"}]

    wire[1].extend([info(), takeover])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    op = state(prepared.task_id)[1]
    assert op.attempt_token == successor and op.status == "result_unknown"
    assert op.remote_response["video_id"] == "actual-target"
    assert op.remote_response["upload_mid"] == "actual-mid"


@pytest.mark.parametrize(
    "mutation",
    [
        {"preview_url": "http://media.vetted.example/video"},
        {"signature": "0" * 32},
        {"preview_url": "https://unverified.example/video"},
    ],
)
def test_untrusted_source_url_or_content_never_reaches_target_post(
    remote_env, redis_client, wire, mutation
):
    prepared = queue(remote_env, remote_env["target"])
    wire[1].append(info(**mutation))
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "blocked"
    assert [call[0] for call in wire[0]] == ["GET"]


def test_remote_preview_expired_budget_has_no_network(remote_env, redis_client, wire):
    from datetime import timedelta

    from app.core.errors import DomainError
    from app.modules.materials.remote_sources import read_remote_source

    with pytest.raises(DomainError) as error:
        read_remote_source(
            database_engine=engine,
            redis_client=redis_client,
            context=remote_env["context"],
            bc_id=remote_env["source_bc_id"],
            material_id=remote_env["source_material_id"],
            source_asset_id=remote_env["source_id"],
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
    assert error.value.code == "material_deadline"
    assert wire[0] == []


def test_cleaned_generation_known_target_recheck_still_requires_exact_vid(
    remote_env, redis_client, wire
):
    with Session(engine) as db, db.begin():
        asset(
            db,
            remote_env,
            remote_env["target"],
            seconds_old=1000,
            status="result_unknown",
        )
    prepared = queue(remote_env, remote_env["target"])
    wire[1].append(info(vid="another-video-with-same-content"))
    run(remote_env, redis_client, prepared.task_id)
    dist, _op, mapping = state(prepared.task_id)
    assert dist.status != "ready"
    assert mapping.video_id == "vid-target-account"


def test_new_generation_without_ready_source_never_selects_legacy_file(
    url_env, wire, monkeypatch
):
    for key in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(settings, key, "offline-fixture")
    with Session(engine) as db, db.begin():
        account = target(db, url_env)
        db.get(MaterialFile, url_env["material_id"]).storage_state = "stored"
    readiness = read(url_env, account)
    assert (
        readiness.state == "blocked"
        and readiness.reason_code == "material_result_pending"
    )
    assert wire[0] == []


def test_new_generation_old_file_dispatch_cannot_open_original(
    url_env, redis_client, wire, monkeypatch
):
    from app.modules.materials import distribution
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )

    with Session(engine) as db, db.begin():
        account = target(db, url_env)
        db.get(MaterialFile, url_env["material_id"]).storage_state = "stored"
        route = freeze_route(
            db,
            context=url_env["context"],
            bc_id=url_env["bc_id"],
            connection_id=url_env["connection_id"],
        )
        op = MaterialAssetOperation(
            tenant_id=url_env["context"].tenant_id,
            bc_id=url_env["bc_id"],
            material_id=url_env["material_id"],
            advertiser_id=account,
            path="upload_original",
            frozen_route=route.model_dump(mode="json"),
            request_digest="a" * 64,
        )
        db.add(op)
        db.flush()
        dist = MaterialDistribution(
            tenant_id=op.tenant_id,
            bc_id=op.bc_id,
            material_id=op.material_id,
            advertiser_id=account,
            actor_id=url_env["context"].actor_id,
            operation_id=op.id,
            path="upload_original",
            target_route=route.model_dump(mode="json"),
        )
        db.add(dist)
        db.flush()
        dist_id = dist.id

    def no_original(**_kwargs):
        pytest.fail("New generations may never enter untracked legacy FILE reads")

    monkeypatch.setattr(distribution, "open_original", no_original)
    for key in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(settings, key, "offline-fixture")
    run(url_env, redis_client, dist_id, kind="prepare")
    assert state(dist_id)[0].reason_code == "material_result_pending"
    assert wire[0] == []


def test_conflicting_relay_receipt_keeps_first_actual_vid(
    remote_env, redis_client, wire
):
    from app.modules.materials.models import MaterialAssetOperation

    prepared = queue(remote_env, remote_env["target"])
    op_id = state(prepared.task_id)[1].id

    def concurrent_known_id():
        with Session(engine) as db, db.begin():
            op = db.get(MaterialAssetOperation, op_id)
            op.remote_response = {
                **op.remote_response,
                "video_id": "first-actual-target",
            }
        return [{"video_id": "second-actual-target"}]

    wire[1].extend([info(), concurrent_known_id])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert op.remote_response["video_id"] == "first-actual-target"
    assert op.remote_response["conflicting_video_id"] == "second-actual-target"
    assert dist.status != "ready" and mapping is None
