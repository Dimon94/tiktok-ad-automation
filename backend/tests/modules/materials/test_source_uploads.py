"""Real Postgres/Redis, pinned SDK wire double, no external service operations."""

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import md5, sha256
from threading import Event
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete
from sqlmodel import Session, SQLModel, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.admission import admission_keys
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route
from app.modules.materials import sdk_assets as api
from app.modules.materials import tasks  # noqa: F401
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
    ObjectUpload,
    UploadBatch,
)
from app.modules.materials.source_uploads import run_source_upload
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.conftest import create_context

CONTENT = b"offline fixture video bytes"
MD5 = md5(CONTENT).hexdigest()


@pytest.fixture
def source_env(monkeypatch, redis_client, request):
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"test-material-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "offline-secret")
    monkeypatch.setattr(
        settings, "TIKTOK_REDIRECT_URI", "https://app.example.com/callback"
    )
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            "base": {
                "app_max_inflight": 10,
                "endpoint_max_inflight": 1,
                "tenant_max_inflight": 10,
                "advertiser_max_inflight": 1,
                "app_calls_per_window": 10000,
                "endpoint_calls_per_window": 10000,
                "window_ms": 1000,
                "lease_ms": 60000,
            },
            "endpoints": {
                api.UPLOAD_ENDPOINT: {"lease_ms": 970000},
                "materials.upload_video_url": {"lease_ms": 970000},
                "materials.share_assets": {"lease_ms": 970000},
                "materials.upload_video_file": {"lease_ms": 970000},
            },
        },
    )
    with Session(engine) as session:
        context = create_context(session)
        bc = TenantBC(
            tenant_id=context.tenant_id,
            bc_id=getattr(request, "param", {}).get("bc_id", "bc-fixture"),
        )
        connection = TikTokConnection(
            tenant_id=context.tenant_id,
            status="ACTIVE",
            credential_ciphertext=encrypt_credentials(
                tenant_id=context.tenant_id,
                value={"access_token": "offline-token-secret"},
            ),
        )
        session.add_all([bc, connection])
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                connection_id=connection.id,
                kind="OFFICIAL_API",
            )
        )
        session.flush()
        session.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id, bc_id=bc.bc_id, connection_id=connection.id
            )
        )
        session.add(
            ConnectionAuthorization(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                authorization_revision=connection.authorization_revision,
                scopes=["synthetic-read-upload-build"],
                source="SYNTHETIC_VERIFIED_EVIDENCE",
                issuer="https://business-api.tiktok.com",
                resource="https://business-api.tiktok.com/open_api/v1.3",
                permission_summary={
                    "read_authorized": True,
                    "build_authorized": True,
                    "upload_authorized": True,
                },
                verified_at=datetime.now(UTC),
            )
        )
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="actual-account",
            currency="USD",
            timezone="UTC",
            remote_status="ENABLE",
        )
        session.add(account)
        session.flush()
        grant = BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            advertiser_id=account.advertiser_id,
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            can_upload=True,
            can_build=True,
            permission_state="VERIFIED",
            checked_at=datetime.now(UTC),
        )
        material = MaterialFile(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            file_name="Moon.mp4",
            object_key=f"test/{uuid4()}",
            byte_size=len(CONTENT),
            sha256=sha256(CONTENT).hexdigest(),
            video_md5=MD5,
            storage_state="stored",
        )
        session.add_all([grant, material])
        session.flush()
        batch = UploadBatch(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            request_id=uuid4(),
            request_digest="a" * 64,
            status="stored",
            frozen_route=freeze_route(
                session, context=context, bc_id=bc.bc_id, connection_id=connection.id
            ).model_dump(mode="json"),
        )
        session.add(batch)
        session.flush()
        session.add(
            ObjectUpload(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                material_id=material.id,
                batch_id=batch.id,
                object_key=material.object_key,
                expected_size=len(CONTENT),
                status="stored",
            )
        )
        session.commit()
        ids = {
            "context": context,
            "material_id": material.id,
            "connection_id": connection.id,
            "bc_id": bc.bc_id,
            "batch_id": batch.id,
        }
    try:
        yield ids
    finally:
        for endpoint in (api.UPLOAD_ENDPOINT, api.INFO_ENDPOINT, api.SEARCH_ENDPOINT):
            redis_client.delete(
                *admission_keys(
                    settings.TIKTOK_APP_ID,
                    endpoint,
                    context.tenant_id,
                    "actual-account",
                )
            )
        # 产生不可变共享账本的集成用例使用独立临时库，统一由库级夹具销毁，
        # 不能禁用业务触发器或逐行删除不可变发送历史。
        if "retain_build_history" in request.fixturenames:
            assert request.getfixturevalue("retain_build_history") is engine
            assert engine.url.database.startswith("strategy_")
        else:
            with Session(engine) as cleanup:
                for table in reversed(SQLModel.metadata.sorted_tables):
                    if "tenant_id" in table.c:
                        cleanup.execute(
                            delete(table).where(table.c.tenant_id == context.tenant_id)
                        )
                cleanup.execute(delete(Tenant).where(Tenant.id == context.tenant_id))
                cleanup.execute(delete(User).where(User.id == context.actor_id))
                cleanup.commit()


@pytest.fixture
def wire(monkeypatch):
    calls, responses = [], []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = responses.pop(0)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": response, "request_id": "offline-request"}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    original = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        if host not in {"127.0.0.1", "localhost", "::1", None}:
            pytest.fail("No external network permitted")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    return calls, responses


def seed_operation(env, *, status="verifying", evidence=None):
    with Session(engine) as session:
        op = MaterialAssetOperation(
            tenant_id=env["context"].tenant_id,
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id="actual-account",
            path="upload_original",
            frozen_route=freeze_route(
                session,
                context=env["context"],
                bc_id=env["bc_id"],
                connection_id=env["connection_id"],
            ).model_dump(mode="json"),
            status=status,
            request_digest="a" * 64,
            remote_response=evidence
            if evidence is not None
            else {"video_id": "target-actual-vid", "mid": "received-mid"},
        )
        session.add(op)
        session.flush()
        attempt = MaterialUploadAttempt(
            tenant_id=op.tenant_id,
            bc_id=op.bc_id,
            material_id=op.material_id,
            advertiser_id=op.advertiser_id,
            connection_id=env["connection_id"],
            operation_id=op.id,
            status=status,
            request_digest=op.request_digest,
        )
        session.add(attempt)
        session.commit()
        return op.id


def run(env, redis_client, *, operation_id=None, kind="verify", **kwargs):
    run_source_upload(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        material_id=env["material_id"],
        operation_id=operation_id,
        kind=kind,
        **kwargs,
    )


def snapshot(env, op_id):
    with Session(engine) as session:
        op = session.get(MaterialAssetOperation, op_id)
        attempt = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == op_id
            )
        ).one()
        asset = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == env["material_id"]
            )
        ).first()
        session.expunge_all()
        return op, attempt, asset


def info(*, vid="target-actual-vid", displayable=True):
    return {
        "list": [
            {
                "video_id": vid,
                "material_id": "target-actual-mid",
                "signature": MD5,
                "displayable": displayable,
            }
        ]
    }


def test_readback_actual_target_vid_and_repeated_delivery(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env)
    calls, responses = wire
    responses.append(info())
    run(source_env, redis_client, operation_id=op_id)
    run(source_env, redis_client, operation_id=op_id)
    op, attempt, asset = snapshot(source_env, op_id)
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert dict(calls[0][2]["fields"])["advertiser_id"] == "actual-account"
    assert op.status == "succeeded" and attempt.status == "available"
    assert (
        asset.video_id == "target-actual-vid"
        and asset.mid == "target-actual-mid"
        and asset.verified_at
    )
    with Session(engine) as db:
        events = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id,
                PendingDispatch.task_name == "materials.prepare_source_cover",
            )
        ).all()
        assert len(events) == 1
        assert events[0].task_key == f"source-cover:{op_id}"
        assert events[0].payload == {"operation_id": str(op_id)}


@pytest.mark.parametrize(
    "response",
    [
        {"list": []},
        info(displayable=False),
        {"list": [{"video_id": "target-actual-vid", "displayable": True}]},
    ],
)
def test_success_not_yet_available_never_reuploads(
    source_env, redis_client, wire, response
):
    op_id = seed_operation(source_env)
    calls, responses = wire
    responses.append(response)
    run(source_env, redis_client, operation_id=op_id)
    run(source_env, redis_client, operation_id=op_id, kind="upload")
    op, _, asset = snapshot(source_env, op_id)
    assert op.status == "verifying" and asset is None
    assert len(calls) == 1 and calls[0][0] == "GET"


def test_search_unknown_requires_full_scan_then_info(source_env, redis_client, wire):
    op_id = seed_operation(source_env, status="result_unknown", evidence={})
    calls, responses = wire
    candidate = {
        **info()["list"][0],
        "file_name": "Moon.mp4",
    }
    responses.append(
        {
            "list": [candidate],
            "page_info": {"page": 1, "page_size": 100, "total_page": 2},
        }
    )
    run(source_env, redis_client, operation_id=op_id)
    assert snapshot(source_env, op_id)[0].status == "result_unknown"
    responses.append(
        {"list": [], "page_info": {"page": 2, "page_size": 100, "total_page": 2}}
    )
    run(source_env, redis_client, operation_id=op_id)
    assert snapshot(source_env, op_id)[0].status == "verifying"
    responses.append(info())
    run(source_env, redis_client, operation_id=op_id)
    assert snapshot(source_env, op_id)[2].status == "available"
    assert [x[0] for x in calls] == ["GET"] * 3


def test_unknown_empty_scan_never_confirms_absence_or_changes_account(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env, status="result_unknown", evidence={})
    calls, responses = wire
    responses.append(
        {"list": [], "page_info": {"page": 1, "page_size": 100, "total_page": 0}}
    )
    run(source_env, redis_client, operation_id=op_id)
    run(source_env, redis_client, kind="upload")
    op, attempt, asset = snapshot(source_env, op_id)
    assert (
        op.status == "result_unknown"
        and attempt.advertiser_id == "actual-account"
        and asset is None
    )
    assert len(calls) == 1


def test_concurrent_read_claim_and_no_database_lock_across_sdk(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env)
    calls, responses = wire
    entered, finish = Event(), Event()

    def response():
        with Session(engine) as session, session.begin():
            # NOWAIT asserts the first worker has committed its claim and
            # released both operation and file locks before entering transport.
            op = session.exec(
                select(MaterialAssetOperation)
                .where(MaterialAssetOperation.id == op_id)
                .with_for_update(nowait=True)
            ).one()
            session.exec(
                select(MaterialFile)
                .where(MaterialFile.id == source_env["material_id"])
                .with_for_update(nowait=True)
            ).one()
            assert op.attempt_token and op.claimed_until
        entered.set()
        assert finish.wait(5)
        return info()

    responses.append(response)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, source_env, redis_client, operation_id=op_id)
        assert entered.wait(5)
        second = pool.submit(run, source_env, redis_client, operation_id=op_id)
        second.result(5)
        finish.set()
        first.result(5)
    assert len(calls) == 1 and snapshot(source_env, op_id)[0].status == "succeeded"


def test_stale_attempt_token_cannot_publish_readiness(source_env, redis_client, wire):
    op_id = seed_operation(source_env)
    _, responses = wire

    def response():
        with Session(engine) as session, session.begin():
            op = session.get(MaterialAssetOperation, op_id)
            op.attempt_token = uuid4()
        return info()

    responses.append(response)
    run(source_env, redis_client, operation_id=op_id)
    assert snapshot(source_env, op_id)[2] is None


def test_revoked_upload_permission_before_request(source_env, redis_client, wire):
    op_id = seed_operation(source_env, status="pending", evidence={})
    calls, _ = wire
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership,
            (source_env["context"].tenant_id, source_env["context"].actor_id),
        ).role = "viewer"
    with pytest.raises(DomainError) as error:
        run(source_env, redis_client, operation_id=op_id, kind="upload")
    assert error.value.code == "action_forbidden"
    assert not calls


def test_production_refuses_eager_solo_and_overridden_deadlines():
    from types import SimpleNamespace

    from app.modules.materials.tasks import _run

    for request in [
        SimpleNamespace(timelimit=(None, None), called_directly=True, is_eager=False),
        SimpleNamespace(timelimit=(1000, None), called_directly=False, is_eager=False),
    ]:
        with pytest.raises(DomainError) as error:
            _run(
                SimpleNamespace(request=request, time_limit=900),
                kind="upload",
                hard_limit=900,
                tenant_id=str(uuid4()),
                actor_id=str(uuid4()),
                payload={"material_id": str(uuid4())},
            )
        assert error.value.code == "material_worker_unbounded"


@pytest.fixture
def original_s3(source_env, monkeypatch):
    import io

    import boto3
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    monkeypatch.setattr(settings, "S3_BUCKET", "offline-material-fixtures")
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="offline",
        aws_secret_access_key="offline",
    )
    stub = Stubber(client)
    with Session(engine) as session:
        key = session.get(MaterialFile, source_env["material_id"]).object_key
    body = StreamingBody(io.BytesIO(CONTENT), len(CONTENT))
    stub.add_response(
        "get_object",
        {
            "Body": body,
            "ContentLength": len(CONTENT),
            "Metadata": {
                "tenant-id": str(source_env["context"].tenant_id),
                "material-id": str(source_env["material_id"]),
            },
        },
        {"Bucket": "offline-material-fixtures", "Key": key},
    )
    with stub:
        yield client, stub
    client.close()


def test_original_to_sdk_once_uses_actual_receipt_without_readback(
    source_env, redis_client, wire, original_s3, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    calls, responses = wire
    responses.append(
        [
            {
                "video_id": "upload-vid",
                "material_id": "upload-mid",
                "displayable": True,
                "access_token": "offline-token-secret",
            }
        ]
    )
    run(source_env, redis_client, kind="upload", s3=original_s3[0])
    with Session(engine) as session:
        op = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == source_env["material_id"]
            )
        ).one()
        op_id = op.id
    op, attempt, asset = snapshot(source_env, op_id)
    assert (
        op.status == "succeeded"
        and attempt.advertiser_id == "actual-account"
        and attempt.connection_id == source_env["connection_id"]
    )
    assert asset.video_id == "upload-vid" and asset.mid == "upload-mid"
    assert dict(calls[0][2]["fields"])["video_file"][1] == CONTENT
    assert dict(calls[0][2]["fields"])["file_name"] == "Moon.mp4"
    run(source_env, redis_client, kind="upload", s3=original_s3[0])
    run(source_env, redis_client, operation_id=op_id)
    assert snapshot(source_env, op_id)[2].video_id == "upload-vid"
    assert (
        snapshot(source_env, op_id)[1].remote_response["upload_video_id"]
        == "upload-vid"
    )
    assert snapshot(source_env, op_id)[1].remote_response["upload_mid"] == "upload-mid"
    assert [row[0] for row in calls] == ["POST"]
    with Session(engine) as session:
        assert session.get(UploadBatch, source_env["batch_id"]).status == "available"
    assert "offline-token-secret" not in caplog.text
    assert "offline-token-secret" not in json.dumps(
        snapshot(source_env, op_id)[1].remote_response
    )
    original_s3[1].assert_no_pending_responses()


def test_sdk_timeout_preserves_unknown_and_only_recovers_by_search(
    source_env, redis_client, wire, original_s3
):
    from urllib3.exceptions import ReadTimeoutError

    calls, responses = wire
    responses.append(
        ReadTimeoutError(
            None,
            "https://example.test?token=offline-token-secret",
            "offline-token-secret",
        )
    )
    run(source_env, redis_client, kind="upload", s3=original_s3[0])
    with Session(engine) as session:
        op = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == source_env["material_id"]
            )
        ).one()
        op_id = op.id
    assert snapshot(source_env, op_id)[0].status == "result_unknown"
    run(source_env, redis_client, kind="upload", s3=original_s3[0])
    responses.append(
        {"list": [], "page_info": {"page": 1, "page_size": 100, "total_page": 0}}
    )
    run(source_env, redis_client, operation_id=op_id)
    assert [row[0] for row in calls] == ["POST", "GET"]
    assert "offline-token-secret" not in json.dumps(
        snapshot(source_env, op_id)[1].remote_response
    )


def test_engineering_upload_capacity_blocks_before_storage_or_tiktok(
    source_env, redis_client, wire
):
    with Session(engine) as session, session.begin():
        session.get(MaterialFile, source_env["material_id"]).byte_size = (
            settings.MATERIAL_SDK_MAX_UPLOAD_BYTES + 1
        )
    run(source_env, redis_client, kind="upload")
    with Session(engine) as session:
        attempt = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.material_id == source_env["material_id"]
            )
        ).one()
        assert (
            attempt.status == "blocked"
            and attempt.remote_response["error_code"] == "sdk_upload_capacity_exceeded"
        )
        assert (
            session.get(MaterialFile, source_env["material_id"]).storage_state
            == "stored"
        )
    assert not wire[0]


def test_completed_claim_watchdog_and_stale_revision_do_not_fork_recovery(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env)
    calls, responses = wire
    responses.append({"list": []})
    run(source_env, redis_client, operation_id=op_id, revision=0)
    with Session(engine) as session:
        dispatches = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).all()
        watchdog = next(row for row in dispatches if "claim_id" in row.payload)
        before = len(dispatches)
    run(
        source_env,
        redis_client,
        operation_id=op_id,
        recovery_claim_id=UUID(watchdog.payload["claim_id"]),
    )
    run(source_env, redis_client, operation_id=op_id, revision=0)
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == source_env["context"].tenant_id
                    )
                ).all()
            )
            == before
        )
    assert len(calls) == 1


def test_admission_wait_never_marks_sending_and_has_same_scope(
    source_env, redis_client, wire
):
    from app.jobs.admission import admission_policy, admit_call, release_call

    op_id = seed_operation(source_env)
    occupied = uuid4()
    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": api.INFO_ENDPOINT,
        "tenant_id": source_env["context"].tenant_id,
        "advertiser_id": "actual-account",
        "lease_id": occupied,
    }
    assert admit_call(
        redis_client, **scope, policy=admission_policy(api.INFO_ENDPOINT)
    ).granted
    try:
        run(source_env, redis_client, operation_id=op_id)
        op, _, asset = snapshot(source_env, op_id)
        assert op.status == "verifying" and op.attempt_token is None and asset is None
        assert op.remote_response["error_code"] == "admission_deferred"
        assert not wire[0]
    finally:
        release_call(redis_client, **scope)


def test_revoke_during_response_prevents_readiness(source_env, redis_client, wire):
    op_id = seed_operation(source_env)

    def response():
        with Session(engine) as session, session.begin():
            session.get(
                BCAccountAccess,
                (
                    source_env["context"].tenant_id,
                    source_env["bc_id"],
                    "actual-account",
                    source_env["connection_id"],
                ),
            ).authorized = False
        return info()

    wire[1].append(response)
    run(source_env, redis_client, operation_id=op_id)
    op, _, asset = snapshot(source_env, op_id)
    assert op.status == "result_unknown" and asset is None
    assert op.remote_response["error_code"] == "account_access_denied"


@pytest.mark.parametrize(
    "status", ["result_unknown", "verifying", "sending", "succeeded"]
)
def test_explicit_retry_rejects_unverified_and_successful_sends(source_env, status):
    from app.modules.materials.source_uploads import request_source_retry

    # Sending requires a token under the real Postgres constraint.
    op_id = seed_operation(source_env, status="verifying")
    with Session(engine) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        op.status = status
        op.attempt_token = uuid4() if status == "sending" else None
    with Session(engine) as session, pytest.raises(DomainError) as error:
        request_source_retry(
            session,
            context=source_env["context"],
            material_id=source_env["material_id"],
        )
    assert error.value.code == "material_retry_not_allowed"


def test_explicit_unsent_retry_selects_new_account_and_preserves_history(source_env):
    from app.modules.materials.source_uploads import request_source_retry

    old_id = seed_operation(
        source_env, status="failed", evidence={"error_code": "account_access_denied"}
    )
    with Session(engine) as session, session.begin():
        old_attempt = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == old_id
            )
        ).one()
        old_attempt.status = "blocked"
        old_grant = session.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                "actual-account",
                source_env["connection_id"],
            ),
        )
        old_grant.can_upload = False
        account = AdvertiserAccount(
            tenant_id=source_env["context"].tenant_id,
            advertiser_id="new-source-account",
            currency="USD",
            timezone="UTC",
            remote_status="ENABLE",
        )
        session.add(account)
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=source_env["context"].tenant_id,
                bc_id=source_env["bc_id"],
                advertiser_id="new-source-account",
                connection_id=source_env["connection_id"],
                can_upload=True,
                in_bc=True,
                authorized=True,
                active=True,
                permission_state="VERIFIED",
                checked_at=datetime.now(UTC),
            )
        )
    with Session(engine) as session, session.begin():
        task_id = request_source_retry(
            session,
            context=source_env["context"],
            material_id=source_env["material_id"],
        )
        assert (
            session.get(PendingDispatch, task_id).task_name
            == "materials.upload_original"
        )
    with Session(engine) as session:
        attempts = session.exec(
            select(MaterialUploadAttempt)
            .where(MaterialUploadAttempt.material_id == source_env["material_id"])
            .order_by(MaterialUploadAttempt.created_at)
        ).all()
        assert [(a.advertiser_id, a.status) for a in attempts] == [
            ("actual-account", "blocked"),
            ("new-source-account", "pending"),
        ]
        assert len({a.operation_id for a in attempts}) == 2


def test_expired_upload_claim_recovers_by_read_not_new_upload(
    source_env, redis_client, wire
):
    from datetime import UTC, datetime, timedelta

    op_id = seed_operation(source_env, evidence={})
    claim = uuid4()
    with Session(engine) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        op.status = "sending"
        op.attempt_token = claim
        op.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
    run(
        source_env,
        redis_client,
        operation_id=op_id,
        kind="upload",
        recovery_claim_id=claim,
    )
    op, _, _ = snapshot(source_env, op_id)
    assert op.status == "result_unknown"
    assert not wire[0]
    with Session(engine) as session:
        queued = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).one()
        assert queued.task_name == "materials.verify_original"


def test_revoked_connection_never_sends(source_env, redis_client, wire):
    op_id = seed_operation(source_env)
    with Session(engine) as session, session.begin():
        session.get(TikTokConnection, source_env["connection_id"]).status = "DISABLED"
    run(source_env, redis_client, operation_id=op_id)
    assert not wire[0]
    assert snapshot(source_env, op_id)[0].status == "result_unknown"


def test_another_valid_connection_does_not_rewrite_actual_upload_connection(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env)
    with Session(engine) as session, session.begin():
        second = TikTokConnection(
            id=UUID(int=source_env["connection_id"].int // 2),
            tenant_id=source_env["context"].tenant_id,
            status="ACTIVE",
            credential_ciphertext=encrypt_credentials(
                tenant_id=source_env["context"].tenant_id,
                value={"access_token": "different-connection-token"},
            ),
        )
        session.add(second)
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=source_env["context"].tenant_id,
                bc_id=source_env["bc_id"],
                advertiser_id="actual-account",
                connection_id=second.id,
                in_bc=True,
                authorized=True,
                active=True,
                can_upload=True,
                permission_state="VERIFIED",
            )
        )
    wire[1].append(info())
    run(source_env, redis_client, operation_id=op_id)
    assert wire[0][0][2]["headers"]["Access-Token"] == "offline-token-secret"
    _, attempt, asset = snapshot(source_env, op_id)
    assert attempt.connection_id == asset.connection_id == source_env["connection_id"]


def test_foreign_tenant_cannot_claim_source_file(source_env, redis_client, wire):
    from app.core.context import TenantContext

    foreign = TenantContext(
        tenant_id=uuid4(), actor_id=source_env["context"].actor_id, role="tenant_admin"
    )
    with pytest.raises(DomainError) as error:
        run_source_upload(
            database_engine=engine,
            redis_client=redis_client,
            context=foreign,
            material_id=source_env["material_id"],
        )
    assert error.value.code == "tenant_forbidden" and not wire[0]


def test_concurrent_upload_claim_commits_before_wire_and_sends_once(
    source_env, redis_client, wire, original_s3
):
    entered, finish = Event(), Event()

    def response():
        with Session(engine) as session, session.begin():
            op = session.exec(
                select(MaterialAssetOperation)
                .where(MaterialAssetOperation.material_id == source_env["material_id"])
                .with_for_update(nowait=True)
            ).one()
            material = session.exec(
                select(MaterialFile)
                .where(MaterialFile.id == source_env["material_id"])
                .with_for_update(nowait=True)
            ).one()
            assert op.status == "sending" and op.attempt_token
            assert (
                material.video_md5 == MD5
                and material.sha256 == sha256(CONTENT).hexdigest()
            )
            recovery = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == source_env["context"].tenant_id
                )
            ).one()
            assert recovery.payload["claim_id"] == str(op.attempt_token)
        entered.set()
        assert finish.wait(5)
        return [{"video_id": "upload-vid", "material_id": "upload-mid"}]

    wire[1].append(response)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run, source_env, redis_client, kind="upload", s3=original_s3[0]
        )
        assert entered.wait(5)
        second = pool.submit(
            run, source_env, redis_client, kind="upload", s3=original_s3[0]
        )
        second.result(5)
        finish.set()
        first.result(5)
    assert len(wire[0]) == 1
    original_s3[1].assert_no_pending_responses()


def test_credentials_reloaded_after_s3_io_and_before_sdk(
    source_env, redis_client, wire, original_s3
):
    def rotate(**_kwargs):
        with Session(engine) as session, session.begin():
            connection = session.get(TikTokConnection, source_env["connection_id"])
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=source_env["context"].tenant_id,
                value={"access_token": "rotated-after-download"},
            )
            connection.credential_revision += 1

    original_s3[0].meta.events.register("after-call.s3.GetObject", rotate)
    wire[1].append([{"video_id": "upload-vid"}])
    run(source_env, redis_client, kind="upload", s3=original_s3[0])
    assert wire[0][0][2]["headers"]["Access-Token"] == "rotated-after-download"


def test_same_account_distribution_mapping_prevents_new_source_send(
    source_env, redis_client, wire
):
    from datetime import UTC, datetime

    with Session(engine) as session, session.begin():
        session.add(
            AccountMaterial(
                tenant_id=source_env["context"].tenant_id,
                bc_id=source_env["bc_id"],
                material_id=source_env["material_id"],
                advertiser_id="actual-account",
                connection_id=source_env["connection_id"],
                video_id="already-verified-target",
                verified_at=datetime.now(UTC),
                status="available",
            )
        )
    run(source_env, redis_client, kind="upload")
    with Session(engine) as session:
        assert not session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == source_env["material_id"]
            )
        ).all()
    assert not wire[0]


def test_read_policy_cannot_expire_before_process_hard_deadline(
    source_env, redis_client, wire, monkeypatch
):
    op_id = seed_operation(source_env)
    config = {
        **settings.TIKTOK_CALL_POLICIES,
        "endpoints": {"materials.get_videos": {"lease_ms": 49000}},
    }
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", config)
    run(source_env, redis_client, operation_id=op_id)
    op, _, asset = snapshot(source_env, op_id)
    assert op.remote_response["error_code"] == "admission_policy_invalid"
    assert not wire[0] and asset is None


def test_unpublished_successor_survives_old_watchdog(source_env, redis_client, wire):
    op_id = seed_operation(source_env)
    wire[1].append({"list": []})
    run(source_env, redis_client, operation_id=op_id, revision=0)
    with Session(engine) as session:
        dispatches = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).all()
        watchdog = next(row for row in dispatches if "claim_id" in row.payload)
        successor = next(row for row in dispatches if "revision" in row.payload)
        due = successor.available_at
    for _ in range(3):
        run(
            source_env,
            redis_client,
            operation_id=op_id,
            recovery_claim_id=UUID(watchdog.payload["claim_id"]),
        )
    with Session(engine) as session:
        pending = session.get(PendingDispatch, successor.id)
        assert pending.published_at is None and pending.available_at == due
    wire[1].append(info())
    run(
        source_env,
        redis_client,
        operation_id=op_id,
        revision=successor.payload["revision"],
    )
    assert snapshot(source_env, op_id)[2].status == "available"
    assert len(wire[0]) == 2


def test_file_receipt_is_durable_before_actual_sdk_pool_cleanup(
    source_env, redis_client, wire, original_s3, monkeypatch
):
    import urllib3

    original_clear = urllib3.PoolManager.clear

    def cleanup_failure(pool):
        original_clear(pool)
        raise RuntimeError("synthetic pool cleanup failure")

    wire[1].append([{"video_id": "file-actual-vid", "material_id": "file-actual-mid"}])
    with monkeypatch.context() as patch:
        patch.setattr(urllib3.PoolManager, "clear", cleanup_failure)
        run(source_env, redis_client, kind="upload", s3=original_s3[0])
    with Session(engine) as db:
        op = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == source_env["material_id"]
            )
        ).one()
        attempt = db.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == op.id
            )
        ).one()
        assert op.remote_response.get("video_id") == "file-actual-vid"
        assert attempt.remote_response.get("upload_video_id") == "file-actual-vid"
    assert len([call for call in wire[0] if call[0] == "POST"]) == 1


def test_readback_different_vid_never_replaces_received_identity(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env)
    wire[1].append(info(vid="unexpected-remote-vid"))
    run(source_env, redis_client, operation_id=op_id)
    op, _, asset = snapshot(source_env, op_id)
    assert asset is None and op.status == "result_unknown"
    assert op.remote_response["video_id"] == "target-actual-vid"
    assert [call[0] for call in wire[0]] == ["GET"]
