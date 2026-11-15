"""Real API/PG/Redis/workers; only provider transports are synthetic."""

import subprocess
from datetime import UTC, datetime, timedelta
from hashlib import md5, sha256
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlmodel import Session, select

from app.api.main import api_router
from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError, domain_error_handler
from app.core.security import create_access_token
from app.jobs.models import PendingDispatch
from app.modules.accounts.routing import freeze_route
from app.modules.materials import storage
from app.modules.materials.cleanup import run_cleanup
from app.modules.materials.ingest_models import (
    ObjectBudget,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
)
from app.modules.materials.models import MaterialFile
from app.modules.materials.object_validation import validate_original
from tests.modules.materials.test_cleanup import cleanup_case as cleanup_case
from tests.modules.materials.test_cleanup_abandoned import MultipartStorage, cancelled
from tests.modules.materials.test_distribution import queue, state
from tests.modules.materials.test_distribution import run as target_run
from tests.modules.materials.test_ingest_api import FakeR2, identity, receive
from tests.modules.materials.test_readiness import read, target
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import operation
from tests.modules.materials.test_url_ingest import run as source_run


class PipelineStorage(FakeR2):
    def __init__(self, content):
        super().__init__()
        self.content = content
        self.lose_delete = False
        self.received_bodies = {}

    def receive_bytes(self, row):
        receive(self, row)
        self.received_bodies[self.uploads[row["upload_id"]]["Key"]] = self.content

    def get_object(self, **values):
        self.record("get", values)
        return {
            **self.head_object(**values),
            "Body": BytesIO(self.received_bodies[values["Key"]]),
        }

    def generate_presigned_url(self, operation, **values):
        if operation == "get_object":
            # Signing is local and deliberately holds the object/use lock.
            self.calls.append(("sign_get", values))
            return "https://pipeline.r2.cloudflarestorage.com/original?signed=transient"
        return super().generate_presigned_url(operation, **values)

    def head_object(self, **values):
        self.record("head", values)
        if values["Key"] not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return self.objects[values["Key"]]

    def delete_object(self, **values):
        self.record("delete", values)
        self.objects.pop(values["Key"], None)
        if self.lose_delete:
            self.lose_delete = False
            raise TimeoutError("synthetic lost receipt")
        return {}

    def abort_multipart_upload(self, **values):
        self.record("abort", values)
        self.uploads.pop(values["UploadId"], None)
        return {}

    def list_parts(self, **values):
        if values["UploadId"] not in self.uploads:
            self.record("list_parts", values)
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchUpload"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "ListParts",
            )
        return super().list_parts(**values)


@pytest.fixture
def pipeline(source_env, monkeypatch, tmp_path, wire):
    assert wire[0] == []
    video = tmp_path / "acceptance.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x240:d=0.2",
            "-an",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    remote = PipelineStorage(video.read_bytes())
    for key, value in {
        "MATERIAL_INGEST_ENABLED": True,
        "MATERIAL_CLEANUP_ENABLED": True,
        "OBJECT_STORAGE_PROVIDER": "r2",
        "S3_ENDPOINT_URL": "https://pipeline.r2.cloudflarestorage.com",
        "S3_BUCKET": "pipeline-private",
        "S3_ACCESS_KEY_ID": "synthetic-only",
        "S3_SECRET_ACCESS_KEY": "synthetic-only",
        "MATERIAL_REMOTE_MEDIA_HOSTS": frozenset({"media.vetted.example"}),
    }.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(storage, "make_object_s3", lambda _obj: remote)
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(api_router, prefix="/api")
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer " + create_access_token(
                source_env["context"].actor_id, timedelta(minutes=10)
            )
            yield source_env, client, remote
    finally:
        # Parent fixture deletes this tenant; subtract only its remaining delta.
        with Session(engine) as db, db.begin():
            own = db.get(ObjectBudget, f"tenant:{source_env['context'].tenant_id}")
            if own:
                db.execute(
                    update(ObjectBudget)
                    .where(ObjectBudget.scope_key == "global")
                    .values(
                        reserved_bytes=ObjectBudget.reserved_bytes - own.reserved_bytes,
                        stored_bytes=ObjectBudget.stored_bytes - own.stored_bytes,
                    )
                )


def registered(pipeline, *, count=1):
    env, client, remote = pipeline
    base = f"/api/tenants/{env['context'].tenant_id}/materials/ingest-sessions"
    created = client.post(
        base,
        json={
            "request_id": str(uuid4()),
            "bc_id": env["bc_id"],
            "file_count": count,
            "total_bytes": count * len(remote.content),
        },
    )
    assert created.status_code == 201, created.text
    url = base + "/" + created.json()["session_id"]
    items = []
    for start in range(0, count, 200):
        body = {
            "request_id": str(uuid4()),
            "files": [
                {
                    "client_index": index,
                    "file_name": "acceptance.mp4",
                    "size": len(remote.content),
                    "mime_type": "video/mp4",
                    "last_modified_ms": 1000 + index,
                }
                for index in range(start, min(count, start + 200))
            ],
        }
        result = client.post(url + "/chunks", json=body)
        assert result.status_code == 201, result.text
        assert client.post(url + "/chunks", json=body).json() == result.json()
        items.extend(result.json()["items"])
    assert client.post(url + "/seal").json()["sealed"]
    return url, items


def stored(pipeline, *, fault=None):
    env, client, remote = pipeline
    parent, rows = registered(pipeline)
    url = parent + "/files/" + rows[0]["material_id"]
    remote.create_unknown = fault == "create"
    response = client.post(url + "/resume", json=identity(rows[0]))
    if fault == "create":
        assert response.status_code == 409
        remote.create_unknown = False
        response = client.post(url + "/resume", json=identity(client.get(url).json()))
    assert response.status_code == 200, response.text
    row = response.json()
    signed = client.post(
        url + "/part-urls", json={**identity(row), "part_numbers": [1]}
    )
    assert signed.status_code == 200 and signed.headers["cache-control"] == "no-store"
    remote.receive_bytes(row)
    remote.complete_unknown = fault == "complete"
    response = client.post(url + "/complete", json=identity(row))
    if fault == "complete":
        assert response.status_code == 409
        remote.complete_unknown = False
        response = client.post(url + "/complete", json=identity(client.get(url).json()))
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["temporary_storage_status"] == "stored"
    with Session(engine) as db:
        dispatch = db.get(PendingDispatch, UUID(row["task_id"]))
        payload, dispatch_id = dict(dispatch.payload), dispatch.id
    validate_original(
        database_engine=engine,
        context=env["context"],
        object_id=UUID(payload["object_id"]),
        generation=payload["generation"],
        revision=payload["revision"],
        dispatch_id=dispatch_id,
        s3=remote,
    )
    env = {
        **env,
        "material_id": UUID(row["material_id"]),
        "object_id": UUID(payload["object_id"]),
        "generation": payload["generation"],
        "s3": remote,
    }
    with Session(engine) as db:
        obj = db.get(TemporaryMaterialObject, env["object_id"])
        assert obj.status == "verified", obj.error_code
        material = db.get(MaterialFile, env["material_id"])
        assert material.sha256 == sha256(remote.content).hexdigest()
        assert material.video_md5 == md5(remote.content).hexdigest()
        dispatches = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == env["context"].tenant_id,
                PendingDispatch.task_name == "materials.upload_original",
            )
        ).all()
        assert len(dispatches) == 1
        assert dispatches[0].payload == {
            "material_id": str(env["material_id"]),
            "object_id": str(env["object_id"]),
            "generation": env["generation"],
        }
    return env, parent


def video_info(remote, vid):
    return {
        "list": [
            {
                "video_id": vid,
                "material_id": "mid-" + vid,
                "file_name": "acceptance.mp4",
                "signature": md5(remote.content).hexdigest(),
                "displayable": True,
                "width": 160,
                "height": 240,
                "duration": 0.2,
                "size": len(remote.content),
                "format": "mp4",
                "preview_url": "https://media.vetted.example/video?signature=transient",
            }
        ]
    }


def search_info(remote, vid):
    row = video_info(remote, vid)["list"][0]
    # MID 共享查询按来源 MID 识别同一素材；VID 仍由目标账户回执决定。
    row["material_id"] = "mid-pipeline-source"
    return {
        "list": [row],
        "page_info": {
            "page": 1,
            "page_size": 100,
            "total_page": 1,
            "total_number": 1,
        },
    }


@pytest.mark.parametrize(
    "fault", [None, "create", "complete", "delete", "source_unknown", "target_unknown"]
)
def test_api_to_verified_source_cleanup_target_and_cover(
    pipeline, redis_client, wire, monkeypatch, fault
):
    _, client, remote = pipeline
    env, parent = stored(pipeline, fault=fault)
    if fault == "source_unknown":
        from urllib3.exceptions import ReadTimeoutError

        wire[1].append(
            ReadTimeoutError(None, "https://synthetic.invalid", "lost reply")
        )
        source_run(env, redis_client)
        op = operation(env)
        assert op.status == "result_unknown", op.remote_response
        with Session(engine) as db:
            assert (
                db.exec(select(OriginalUse).where(OriginalUse.operation_id == op.id))
                .one()
                .status
                == "active"
            )
            assert (
                db.exec(
                    select(ObjectCleanup).where(
                        ObjectCleanup.material_id == env["material_id"]
                    )
                ).all()
                == []
            )
        candidate = video_info(remote, "pipeline-source")["list"][0]
        wire[1].append(
            {
                "list": [{**candidate, "file_name": op.remote_response["remote_name"]}],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
        )
        source_run(env, redis_client, kind="verify", operation_id=op.id)
        wire[1].append(video_info(remote, "pipeline-source"))
    else:
        wire[1].extend(
            [[{"video_id": "pipeline-source"}], video_info(remote, "pipeline-source")]
        )
        source_run(env, redis_client)
    source_run(env, redis_client, kind="verify", operation_id=operation(env).id)
    assert operation(env).status == "succeeded", operation(env).remote_response
    source_operation_id = operation(env).id
    with Session(engine) as db:
        cleanup = db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == env["material_id"])
        ).one()
        cleanup_id = cleanup.id
        assert (
            db.exec(
                select(OriginalUse).where(
                    OriginalUse.material_id == env["material_id"],
                    OriginalUse.status == "active",
                )
            ).all()
            == []
        )
    remote.lose_delete = fault == "delete"
    run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
    if fault == "delete":
        assert client.get(parent).json()["reserved_bytes"] == len(remote.content)
        with Session(engine) as db, db.begin():
            db.get(ObjectCleanup, cleanup_id).next_attempt_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
    summary = client.get(parent).json()
    assert summary["ready_count"] == summary["cleaned_count"] == 1
    assert summary["reserved_bytes"] == summary["stored_bytes"] == 0
    assert not remote.objects
    assert sum(call[0] == "delete" for call in remote.calls) == 1
    original_reads = sum(call[0] == "get" for call in remote.calls)
    for key in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(settings, key, "")
    with Session(engine) as db, db.begin():
        env["target"] = target(db, env)
    assert read(env, env["target"]).state == "preparable"
    prepared = queue(env, env["target"])
    if fault == "source_unknown":
        # 上传丢回执后的恢复未留下可复用 MID；共享前须重新读取来源详情。
        wire[1].append(video_info(remote, "pipeline-source"))
    if fault == "target_unknown":
        from urllib3.exceptions import ReadTimeoutError

        wire[1].append(
            ReadTimeoutError(None, "https://synthetic.invalid", "lost reply")
        )
        target_run(env, redis_client, prepared.task_id, kind="prepare")
        op = state(prepared.task_id)[1]
        assert op.status == "result_unknown", op.remote_response
        wire[1].append(search_info(remote, "pipeline-target"))
    else:
        # 来源上传核验已保存 MID 和文件名，共享直接复用，无需再读来源详情。
        wire[1].append({})
        target_run(env, redis_client, prepared.task_id, kind="prepare")
        # 同 BC 原生共享先回读源 VID 候选；未命中时从目标库存确认实际 VID。
        found = search_info(remote, "pipeline-target")
        wire[1].extend([{"list": []}, found])
    target_run(env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[0].status == "ready"
    assert state(prepared.task_id)[2].video_id == "pipeline-target"
    from app.modules.materials import covers
    from tests.modules.materials.test_cover_throughput import page
    from tests.modules.materials.test_covers import image_info, job_state, scopes
    from tests.modules.materials.test_covers import run as cover_run

    scopes(env)
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.source_cover_service import prepare_source_cover
    from tests.modules.materials.test_source_cover_events import source_event

    # 先消费来源成功事件并完成源封面，再让目标封面走正式共享流程。
    with Session(engine) as db, db.begin():
        prepare_source_cover(
            db,
            context=env["context"],
            operation_id=source_operation_id,
            dispatch_id=source_event(db, env, operation_id=source_operation_id).id,
        )
        source_cover = (
            db.exec(
                select(MaterialCoverJob).where(
                    MaterialCoverJob.material_id == env["material_id"],
                    MaterialCoverJob.purpose == "SOURCE",
                )
            )
            .one()
            .id
        )
    response = video_info(remote, "pipeline-source")
    response["list"][0]["video_cover_url"] = "https://image.example.test/cover"
    wire[1].extend([response, {"image_id": "source-image", "signature": "a" * 32}])
    cover_run(env, redis_client, source_cover)
    source_image = image_info(
        source_cover, image_id="source-image", width=160, height=240
    )
    wire[1].append(source_image)
    cover_run(env, redis_client, source_cover, read=True)
    assert job_state(source_cover).status == "READY"
    with Session(engine) as db, db.begin():
        cover = covers.ensure_cover(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id=env["target"],
            task_key=f"acceptance:{uuid4()}",
            route=freeze_route(
                db,
                context=env["context"],
                bc_id=env["bc_id"],
                connection_id=env["connection_id"],
            ),
        )
    wire[1].extend([page([]), {}])
    cover_run(env, redis_client, cover.task_id)
    target_image = {**source_image["list"][0], "image_id": "target-image"}
    wire[1].append(page([target_image]))
    cover_run(env, redis_client, cover.task_id, read=True)
    mapping = read(env, env["target"]).mapping
    assert mapping.image_id == "target-image"
    from app.integrations.tiktok.contracts.builds import VideoCreative
    from app.modules.builds.request_compiler import ad_assets

    compiled = ad_assets(
        [VideoCreative(video_id=mapping.video_id, cover_image_id=mapping.image_id)],
        text="Watch.",
        url="https://example.test/minis",
        identity={
            "identity_type": "BC_AUTH_TT",
            "identity_id": "offline-identity",
            "identity_authorized_bc_id": env["bc_id"],
        },
    )
    creative = compiled["creative_list"][0]["creative_info"]
    assert creative["video_info"]["video_id"] == "pipeline-target"
    assert creative["image_info"] == [{"web_uri": "target-image"}]
    posts = [
        dict(call[2]["fields"])
        for call in wire[0]
        if call[0] == "POST" and "upload_type" in dict(call[2].get("fields", []))
    ]
    # 原件只上传到来源账户一次；同 BC 目标账户通过原生共享取得视频。
    assert len(posts) == 1
    shares = [
        call for call in wire[0] if call[0] == "POST" and "/asset/share/" in call[1]
    ]
    assert len(shares) == 2  # 视频与封面各共享一次。
    assert not wire[1]
    assert all(
        post["upload_type"] == "UPLOAD_BY_URL" and "video_file" not in post
        for post in posts
    )
    assert sum(call[0] == "create" for call in remote.calls) == 1
    assert sum(call[0] == "complete" for call in remote.calls) == 1
    assert sum(call[0] == "get" for call in remote.calls) == original_reads == 1
    with Session(engine) as db:
        from app.modules.materials.models import MaterialAssetOperation

        material = db.get(MaterialFile, env["material_id"])
        assert material.file_name == "acceptance.mp4"
        assert material.video_md5 == md5(remote.content).hexdigest()
        operations = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == env["material_id"]
            )
        ).all()
        dispatches = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == env["context"].tenant_id
            )
        ).all()
        serialized = repr([op.remote_response for op in operations]) + repr(
            [dispatch.payload for dispatch in dispatches]
        )
        assert "signed=transient" not in serialized
        assert "signature=transient" not in serialized


def test_cancel_cleanup_late_receipt_cannot_overwrite_new_claim(cleanup_case):
    cleanup_id = cancelled(cleanup_case)
    remote = MultipartStorage()
    newer = uuid4()
    original_abort = remote.abort_multipart_upload

    def replace_claim(**values):
        original_abort(**values)
        with Session(engine) as db, db.begin():
            row = db.get(ObjectCleanup, cleanup_id)
            row.claim_token = newer
            row.claimed_until = datetime.now(UTC) + timedelta(minutes=1)

    remote.abort_multipart_upload = replace_claim
    run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
    with Session(engine) as db:
        assert db.get(ObjectCleanup, cleanup_id).claim_token == newer
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 5


def test_1000_manifest_files_each_receive_a_bounded_fault_and_keep_identity(
    pipeline, monkeypatch, wire
):
    """1000 metadata identities, 400 actual control-worker recoveries, zero SDK."""
    from sqlalchemy import event

    env, client, remote = pipeline
    parent, rows = registered(pipeline, count=1000)
    assert len({row["material_id"] for row in rows}) == 1000
    limit = settings.MATERIAL_STORAGE_TENANT_BYTES
    outcomes = dict.fromkeys(
        ("capacity", "stale", "cancel", "create_unknown", "complete_unknown"), 0
    )
    for index, initial in enumerate(rows):
        url = parent + "/files/" + initial["material_id"]
        kind = index % 5
        if kind == 0:
            monkeypatch.setattr(
                settings, "MATERIAL_STORAGE_TENANT_BYTES", len(remote.content) - 1
            )
            before = len(remote.calls)
            response = client.post(url + "/resume", json=identity(initial))
            assert (
                response.status_code == 200
                and response.json()["temporary_storage_status"] == "waiting_capacity"
            )
            assert len(remote.calls) == before
            monkeypatch.setattr(settings, "MATERIAL_STORAGE_TENANT_BYTES", limit)
            outcomes["capacity"] += 1
        elif kind == 1:
            before = len(remote.calls)
            response = client.post(
                url + "/resume", json={**identity(initial), "operation_revision": 999}
            )
            assert response.status_code == 409 and len(remote.calls) == before
            outcomes["stale"] += 1
        elif kind == 2:
            before = len(remote.calls)
            response = client.post(url + "/cancel", json=identity(initial))
            assert response.status_code == 200 and len(remote.calls) == before
            outcomes["cancel"] += 1
        else:
            remote.create_unknown = kind == 3
            response = client.post(url + "/resume", json=identity(initial))
            if kind == 3:
                assert response.status_code == 409
                remote.create_unknown = False
                current = client.get(url).json()
                response = client.post(url + "/resume", json=identity(current))
            assert response.status_code == 200, response.text
            current = response.json()
            if kind == 4:
                receive(remote, current)
                remote.complete_unknown = True
                assert (
                    client.post(url + "/complete", json=identity(current)).status_code
                    == 409
                )
                remote.complete_unknown = False
                current = client.get(url).json()
                response = client.post(url + "/complete", json=identity(current))
                assert response.status_code == 200, response.text
                current = response.json()
            assert (
                client.post(url + "/cancel", json=identity(current)).status_code == 200
            )
            with Session(engine) as db:
                cleanup = db.exec(
                    select(ObjectCleanup).where(
                        ObjectCleanup.material_id == UUID(initial["material_id"])
                    )
                ).one()
                cleanup_id = cleanup.id
            run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
            with Session(engine) as db:
                cleanup = db.get(ObjectCleanup, cleanup_id)
                assert cleanup.status == "deleted", cleanup.error_code
            outcomes["create_unknown" if kind == 3 else "complete_unknown"] += 1
    assert set(outcomes.values()) == {200}
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        summary = client.get(parent).json()
        summary_statements = len(statements)
        first = client.get(parent + "/files", params={"limit": 100}).json()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) <= 20
    # 会话汇总仍读取持久计数；目录接口为 total 做一次有界作用域统计。
    assert not any(
        "COUNT(" in sql.upper() or "SUM(" in sql.upper()
        for sql in statements[:summary_statements]
    )
    assert sum("COUNT(" in sql.upper() for sql in statements) == 1
    assert not any("SUM(" in sql.upper() for sql in statements)
    assert first["total"] == 1000
    assert summary["accepted_count"] == 1000
    assert summary["uploaded_count"] == 200 and summary["cleaned_count"] == 400
    assert summary["ready_count"] == 0
    assert summary["reserved_bytes"] == summary["stored_bytes"] == 0
    seen = [row["material_id"] for row in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = client.get(
            parent + "/files", params={"limit": 100, "cursor": cursor}
        ).json()
        seen.extend(row["material_id"] for row in page["items"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 1000
    assert set(seen) == {row["material_id"] for row in rows}
    assert sum(call[0] == "create" for call in remote.calls) == 400
    assert sum(call[0] == "complete" for call in remote.calls) == 200
    assert sum(call[0] == "abort" for call in remote.calls) == 200
    assert sum(call[0] == "delete" for call in remote.calls) == 200
    assert not remote.objects and wire[0] == []
