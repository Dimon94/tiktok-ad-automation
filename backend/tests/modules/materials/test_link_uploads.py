from uuid import uuid4

import pytest

from app.core.errors import DomainError
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,  # noqa: F401
)

V1 = "v1c033g50000dbk8b4nog65o4ru0ciu0"
V2 = "v14033g50000dbljvbfog65kb0udour0"
HOST = "https://ad-creative-studio-platform.tiktok-row.net/preview?vid="


def test_links_preserve_input_line_numbers_and_deduplicate():
    from app.modules.materials.link_schemas import parse_links

    parsed = parse_links(f"\n{HOST}{V1}\n\n{HOST}{V1}\n{HOST}{V2}\n")
    assert [(p.line_no, p.video_id, p.duplicate_of) for p in parsed] == [
        (2, V1, None),
        (4, V1, 2),
        (5, V2, None),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/preview?vid=" + V1,
        HOST + V1 + "&vid=" + V2,
        HOST + "truncated",
        "http://ad-creative-studio-platform.tiktok-row.net/preview?vid=" + V1,
        HOST + V1 + "#fragment",
        HOST + V1 + "&access_token=secret",
        "",
        "\n".join([HOST + V1] * 501),
    ],
)
def test_invalid_links_are_rejected_without_echoing_urls(text):
    from app.modules.materials.link_schemas import parse_links

    with pytest.raises(DomainError) as error:
        parse_links(text)
    assert "https://" not in error.value.message


def test_batch_contract_rejects_mass_fanout_and_duplicate_accounts():
    from pydantic import ValidationError

    from app.modules.materials.link_schemas import LinkBatchCreate

    with pytest.raises(ValidationError):
        LinkBatchCreate(
            request_id=uuid4(),
            bc_id="bc",
            text=HOST + V1,
            advertiser_ids=["123", "123"],
        )


@pytest.fixture
def link_env(source_env, monkeypatch):
    from sqlmodel import Session

    from app.core.config import settings
    from app.core.db import engine
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    monkeypatch.setattr(settings, "MATERIAL_INGEST_ENABLED", True)
    env = source_env
    with Session(engine) as db, db.begin():
        db.add(
            AdvertiserAccount(
                tenant_id=env["context"].tenant_id,
                advertiser_id="123",
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            )
        )
        db.flush()
        db.add(
            BCAccountAccess(
                tenant_id=env["context"].tenant_id,
                bc_id=env["bc_id"],
                connection_id=env["connection_id"],
                advertiser_id="123",
                in_bc=True,
                authorized=True,
                active=True,
                can_upload=True,
                permission_state="VERIFIED",
                checked_at=__import__("datetime").datetime.now(
                    __import__("datetime").UTC
                ),
            )
        )
    return env


def make_batch(env, *, request_id=None, text=None, advertiser_ids=None):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem
    from app.modules.materials.link_schemas import LinkBatchCreate
    from app.modules.materials.link_service import create_batch

    with Session(engine) as db, db.begin():
        batch = create_batch(
            db,
            context=env["context"],
            body=LinkBatchCreate(
                request_id=request_id or uuid4(),
                bc_id=env["bc_id"],
                text=text or HOST + V1,
                advertiser_ids=advertiser_ids or ["123"],
            ),
        )
        items = db.exec(
            select(MaterialLinkItem).where(MaterialLinkItem.batch_id == batch.id)
        ).all()
        return batch.id, [item.id for item in items]


def test_batch_idempotency_and_changed_request_conflict(link_env):
    request = uuid4()
    first = make_batch(link_env, request_id=request, text=HOST + V1 + "\n" + HOST + V1)
    assert first == make_batch(
        link_env, request_id=request, text=HOST + V1 + "\n" + HOST + V1
    )
    assert len(first[1]) == 1
    with pytest.raises(DomainError, match="不能更改"):
        make_batch(link_env, request_id=request, text=HOST + V2)


def test_worker_vid_changes_and_duplicate_delivery_uses_one_wire_upload(
    link_env, redis_client, monkeypatch
):
    import json

    from sqlmodel import Session
    from urllib3.response import HTTPResponse

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem
    from app.modules.materials.link_worker import run_link_item

    _, ids = make_batch(link_env)
    calls = []
    actual = "v10033g50000dbk82c7og65i8qejbr60"

    def wire(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            body = json.loads(kwargs["body"])
            assert (
                body["upload_type"] == "UPLOAD_BY_VIDEO_ID" and body["video_id"] == V1
            )
            data = [{"video_id": V1, "material_id": "777"}]
        elif "search" in url:
            uploaded = any(m == "POST" for m, _, _ in calls)
            rows = [row()] if uploaded else []
            data = {
                "list": rows,
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1 if uploaded else 0,
                    "total_number": len(rows),
                },
            }
        else:
            data = {"list": [row()]}
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "test-request", "data": data}
            ).encode(),
            status=200,
        )

    def row():
        return {
            "video_id": actual,
            "material_id": "777",
            "file_name": f"20261116_{V1}_001.mp4",
            "signature": "a" * 32,
            "width": 1080,
            "height": 1920,
            "size": 120,
            "duration": 10,
            "format": "mp4",
            "displayable": True,
            "allowed_placements": ["PLACEMENT_TIKTOK"],
        }

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", wire)
    for _ in range(4):
        with Session(engine) as db:
            revision = db.get(MaterialLinkItem, ids[0]).revision
        for _ in range(2):
            run_link_item(
                database_engine=engine,
                redis_client=redis_client,
                context=link_env["context"],
                item_id=ids[0],
                revision=revision,
            )
    with Session(engine) as db:
        item = db.get(MaterialLinkItem, ids[0])
        assert item.status == "success", (
            item.status,
            item.error_code,
            item.error_message,
        )
        assert item.video_id == actual and item.material_id
    assert sum(m == "POST" for m, _, _ in calls) == 1


def run_current(env, redis):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem
    from app.modules.materials.link_worker import run_link_item

    with Session(engine) as db:
        item = db.exec(
            select(MaterialLinkItem).where(
                MaterialLinkItem.tenant_id == env["context"].tenant_id
            )
        ).first()
        item_id, revision = item.id, item.revision
    run_link_item(
        database_engine=engine,
        redis_client=redis,
        context=env["context"],
        item_id=item_id,
        revision=revision,
    )
    with Session(engine) as db:
        return db.get(MaterialLinkItem, item_id).model_copy(deep=True)


@pytest.fixture
def link_wire(monkeypatch):
    import json
    from collections import deque

    from urllib3.response import HTTPResponse

    replies, calls = deque(), []

    def wire(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        value = replies.popleft()
        if isinstance(value, Exception):
            raise value
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": value, "request_id": "fixture"}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", wire)
    return replies, calls


def empty_search():
    return {
        "list": [],
        "page_info": {"page": 1, "page_size": 100, "total_page": 0, "total_number": 0},
    }


def test_unknown_upload_only_searches_and_never_reuploads(
    link_env, redis_client, link_wire
):
    replies, calls = link_wire
    make_batch(link_env)
    replies.append(empty_search())
    assert run_current(link_env, redis_client).status == "sending"
    replies.append(TimeoutError("wire lost"))
    item = run_current(link_env, redis_client)
    assert item.sent_at and item.status == "unknown"
    for _ in range(3):
        replies.append(empty_search())
        assert run_current(link_env, redis_client).status == "unknown"
    assert sum(method == "POST" for method, _, _ in calls) == 1
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.link_service import retry_item

    with Session(engine) as db, db.begin(), pytest.raises(DomainError):
        retry_item(
            db, context=link_env["context"], batch_id=item.batch_id, item_id=item.id
        )


def test_sent_crash_recovery_is_read_only(link_env, redis_client, link_wire):
    from datetime import timedelta

    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem, now
    from app.modules.materials.link_worker import repair_link_items

    _, ids = make_batch(link_env)
    with Session(engine) as db, db.begin():
        item = db.get(MaterialLinkItem, ids[0])
        item.status = "sending"
        item.sent_at = now() - timedelta(minutes=20)
        item.next_run_at = now() - timedelta(seconds=1)
        db.add(item)
    with Session(engine) as db, db.begin():
        assert repair_link_items(db) == 1
    replies, calls = link_wire
    replies.append(empty_search())
    assert run_current(link_env, redis_client).status == "unknown"
    assert not any(method == "POST" for method, _, _ in calls)


def test_revoked_account_blocks_wire_and_original_route_remains(
    link_env, redis_client, link_wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.models import BCAccountAccess

    make_batch(link_env)
    with Session(engine) as db, db.begin():
        grant = db.get(
            BCAccountAccess,
            (
                link_env["context"].tenant_id,
                link_env["bc_id"],
                "123",
                link_env["connection_id"],
            ),
        )
        grant.can_upload = False
        db.add(grant)
    item = run_current(link_env, redis_client)
    assert item.status == "blocked" and item.connection_id == link_env["connection_id"]
    assert not link_wire[1]


def test_concurrent_delivery_claims_single_platform_read(
    link_env, redis_client, monkeypatch
):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlmodel import Session
    from urllib3.response import HTTPResponse

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem
    from app.modules.materials.link_worker import run_link_item

    _, ids = make_batch(link_env)
    with Session(engine) as db:
        revision = db.get(MaterialLinkItem, ids[0]).revision
    entered, release = Event(), Event()
    calls = []

    def wire(_pool, method, _url, **_kwargs):
        calls.append(method)
        entered.set()
        assert release.wait(5)
        return HTTPResponse(
            body=json.dumps({"code": 0, "data": empty_search()}).encode(), status=200
        )

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", wire)

    def run():
        run_link_item(
            database_engine=engine,
            redis_client=redis_client,
            context=link_env["context"],
            item_id=ids[0],
            revision=revision,
        )

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run)
        assert entered.wait(5)
        pool.submit(run).result(timeout=3)
        release.set()
        first.result(timeout=5)
    assert calls == ["GET"]


def test_api_scope_roles_and_source_listing(link_env):
    from datetime import timedelta

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.core.db import engine
    from app.core.errors import domain_error_handler
    from app.core.security import create_access_token
    from app.modules.materials.link_api import router
    from app.modules.tenants.models import TenantMembership

    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api")
    owner = link_env["context"]
    path = f"/api/tenants/{owner.tenant_id}/materials/link-uploads"
    body = {
        "request_id": str(uuid4()),
        "bc_id": link_env["bc_id"],
        "text": HOST + V1,
        "advertiser_ids": ["123"],
    }
    token = create_access_token(owner.actor_id, timedelta(minutes=5))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        response = client.post(path, json=body)
        assert response.status_code == 201, response.text
        batch = response.json()
        assert client.post(path, json=body).json()["id"] == batch["id"]
        assert (
            client.post(
                path, json={**body, "text": "https://example.com/bad"}
            ).status_code
            == 422
        )
        assert client.get(path + "/" + str(uuid4())).status_code == 404
        assert client.get(path, params={"bc_id": "other-bc"}).status_code in (
            403,
            404,
            409,
        )
        items = client.get(path + "/" + batch["id"] + "/items").json()
        assert len(items["items"]) == 1
        assert "frozen_route" not in str(items) and "sent_at" not in str(items)
        with Session(engine) as db, db.begin():
            member = db.get(TenantMembership, (owner.tenant_id, owner.actor_id))
            member.role = "viewer"
            db.add(member)
        assert client.get(path + "/" + batch["id"]).status_code == 200
        assert (
            client.post(path, json={**body, "request_id": str(uuid4())}).status_code
            == 403
        )


def test_account_validation_is_atomic_for_all_targets(link_env):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkBatch
    from app.modules.materials.link_schemas import LinkBatchCreate
    from app.modules.materials.link_service import create_batch

    with Session(engine) as db, db.begin(), pytest.raises(DomainError):
        create_batch(
            db,
            context=link_env["context"],
            body=LinkBatchCreate(
                request_id=uuid4(),
                bc_id=link_env["bc_id"],
                text=HOST + V1,
                advertiser_ids=["123", "999"],
            ),
        )
    with Session(engine) as db:
        assert not db.exec(
            select(MaterialLinkBatch).where(
                MaterialLinkBatch.tenant_id == link_env["context"].tenant_id
            )
        ).all()


def test_concurrent_create_reuses_one_batch(link_env):
    from concurrent.futures import ThreadPoolExecutor

    request = uuid4()
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(make_batch, link_env, request_id=request) for _ in range(2)
        ]
        results = [f.result(timeout=5) for f in futures]
    assert results[0] == results[1]


def test_platform_merges_sources_without_overwriting_local_identity(link_env):
    from sqlmodel import Session

    from app.core.db import engine
    from app.integrations.tiktok.contracts.common import CallEvidence
    from app.integrations.tiktok.contracts.materials import VideoRecord
    from app.modules.materials.link_models import MaterialLinkItem
    from app.modules.materials.link_service import item_public
    from app.modules.materials.link_worker import _register

    _, ids = make_batch(link_env, text=HOST + V1 + "\n" + HOST + V2)
    with Session(engine) as db, db.begin():
        first, second = [db.get(MaterialLinkItem, item_id) for item_id in ids]
        record = VideoRecord(
            "123",
            "actual-vid",
            "same-mid",
            "a" * 32,
            first.requested_file_name,
            1080,
            1920,
            100,
            10,
            "mp4",
            True,
            CallEvidence(),
        )
        first.video_id = second.video_id = record.video_id
        first.mid = second.mid = record.mid
        _register(db, first, record)
        db.add(first)
        db.flush()
        _register(db, second, record)
        assert first.material_id == second.material_id
        assert second.duplicate_of == first.id
        assert item_public(second, db).duplicate_line_no == first.line_no
        assert second.requested_file_name != second.actual_file_name


def test_recheck_preserves_sent_identity_but_definitive_failure_can_retry(link_env):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem, now
    from app.modules.materials.link_service import retry_item

    batch, ids = make_batch(link_env)
    with Session(engine) as db, db.begin():
        item = db.get(MaterialLinkItem, ids[0])
        item.sent_at = now()
        item.mid, item.video_id = "known-mid", "known-vid"
        item.status = "needs_review"
        db.add(item)
    with Session(engine) as db, db.begin():
        item = retry_item(
            db, context=link_env["context"], batch_id=batch, item_id=ids[0]
        )
        assert item.status == "submitted" and item.sent_at and item.mid == "known-mid"
        item.status = "failed"
        db.add(item)
    with Session(engine) as db, db.begin():
        item = retry_item(
            db, context=link_env["context"], batch_id=batch, item_id=ids[0]
        )
        assert item.status == "pending" and item.sent_at is None and item.mid is None


def test_disabled_ingest_cannot_create_or_retry(link_env, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MATERIAL_INGEST_ENABLED", False)
    with pytest.raises(DomainError) as error:
        make_batch(link_env)
    assert error.value.code == "material_ingest_disabled"


def platform_row(name, vid="actual-vid", mid="777"):
    return {
        "video_id": vid,
        "material_id": mid,
        "file_name": name,
        "signature": "a" * 32,
        "width": 1080,
        "height": 1920,
        "size": 120,
        "duration": 10,
        "format": "mp4",
        "displayable": True,
        "allowed_placements": ["PLACEMENT_TIKTOK"],
    }


def search_result(rows, page=1, pages=1):
    if pages > 1 and page == 1:
        rows = rows + [
            platform_row(f"other-{i}.mp4", f"other-vid-{i}", f"other-mid-{i}")
            for i in range(100 - len(rows))
        ]
    return {
        "list": rows,
        "page_info": {
            "page": page,
            "page_size": 100,
            "total_page": pages,
            "total_number": 101 if pages > 1 else len(rows),
        },
    }


def test_receipt_without_mid_reads_returned_vid_after_platform_merge(
    link_env, redis_client, link_wire
):
    make_batch(link_env)
    replies, calls = link_wire
    replies.append(empty_search())
    run_current(link_env, redis_client)
    replies.append([{"video_id": "actual-vid"}])
    item = run_current(link_env, redis_client)
    assert item.video_id == "actual-vid" and item.mid is None
    replies.append({"list": [platform_row("old-platform-name.mp4")]})
    item = run_current(link_env, redis_client)
    assert item.status == "success" and item.mid == "777"
    assert item.actual_file_name == "old-platform-name.mp4"
    assert "search" not in calls[-1][1] and calls[-1][0] == "GET"
    assert sum(c[0] == "POST" for c in calls) == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_search_checks_all_pages_before_accepting_unique_name(
    link_env, redis_client, link_wire, ambiguous
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem

    _, ids = make_batch(link_env)
    with Session(engine) as db:
        name = db.get(MaterialLinkItem, ids[0]).requested_file_name
    replies, calls = link_wire
    replies.append(search_result([platform_row(name)], pages=2))
    item = run_current(link_env, redis_client)
    assert item.status == "pending" and item.search_page == 2, (
        item.error_code,
        item.error_message,
    )
    assert item.video_id is None and item.mid is None
    replies.append(
        search_result(
            [platform_row(name, "different-vid", "888")]
            if ambiguous
            else [platform_row("unrelated.mp4", "other-vid", "999")],
            page=2,
            pages=2,
        )
    )
    item = run_current(link_env, redis_client)
    assert item.status == ("needs_review" if ambiguous else "verifying")
    if not ambiguous:
        replies.append({"list": [platform_row(name)]})
        assert run_current(link_env, redis_client).status == "success"
    assert all(c[0] == "GET" for c in calls)


def test_midless_receipt_vid_changes_falls_back_to_name_without_resending(
    link_env, redis_client, link_wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem

    _, ids = make_batch(link_env)
    with Session(engine) as db:
        name = db.get(MaterialLinkItem, ids[0]).requested_file_name
    replies, calls = link_wire
    replies.append(empty_search())
    run_current(link_env, redis_client)
    replies.append([{"video_id": V1}])
    assert run_current(link_env, redis_client).status == "verifying"
    replies.append({"list": []})
    assert run_current(link_env, redis_client).status == "submitted"
    replies.append(search_result([platform_row(name)]))
    assert run_current(link_env, redis_client).status == "verifying"
    replies.append({"list": [platform_row(name)]})
    assert run_current(link_env, redis_client).status == "success"
    assert sum(c[0] == "POST" for c in calls) == 1


def test_worker_cannot_claim_other_tenant_item(link_env, redis_client, link_wire):
    from sqlmodel import Session

    from app.core.context import TenantContext
    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem
    from app.modules.materials.link_worker import run_link_item

    _, ids = make_batch(link_env)
    with Session(engine) as db:
        revision = db.get(MaterialLinkItem, ids[0]).revision
    run_link_item(
        database_engine=engine,
        redis_client=redis_client,
        context=TenantContext(
            tenant_id=uuid4(), actor_id=link_env["context"].actor_id, role="owner"
        ),
        item_id=ids[0],
        revision=revision,
    )
    assert not link_wire[1]
    assert run_current_is_pending(ids[0])


def run_current_is_pending(item_id):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.link_models import MaterialLinkItem

    with Session(engine) as db:
        item = db.get(MaterialLinkItem, item_id)
        return (
            item.status == "pending" and item.claim_id is None and item.sent_at is None
        )


def test_multiple_accounts_each_get_one_task_per_unique_source(link_env):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.materials.link_models import MaterialLinkItem

    with Session(engine) as db, db.begin():
        db.add(
            AdvertiserAccount(
                tenant_id=link_env["context"].tenant_id,
                advertiser_id="456",
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            )
        )
        db.flush()
        grant = db.get(
            BCAccountAccess,
            (
                link_env["context"].tenant_id,
                link_env["bc_id"],
                "123",
                link_env["connection_id"],
            ),
        )
        db.add(BCAccountAccess(**{**grant.model_dump(), "advertiser_id": "456"}))
    _, ids = make_batch(
        link_env,
        text=HOST + V1 + "\n" + HOST + V1 + "\n" + HOST + V2,
        advertiser_ids=["123", "456"],
    )
    with Session(engine) as db:
        items = db.exec(
            select(MaterialLinkItem).where(MaterialLinkItem.id.in_(ids))
        ).all()
        assert {(item.advertiser_id, item.line_no) for item in items} == {
            ("123", 1),
            ("456", 1),
            ("123", 3),
            ("456", 3),
        }
        assert len({item.requested_file_name for item in items}) == 2
