"""JB-22 public HTTP with directory-only PostgreSQL facts."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_db
from app.core.errors import DomainError, domain_error_handler
from app.modules.ads.models import AdObject
from app.modules.reporting.api import router


@pytest.fixture
def navigation(session, directory_seed):
    seed = directory_seed
    from app.modules.accounts.models import BCAccountAccess, TenantBC

    session.add(TenantBC(tenant_id=seed.context.tenant_id, bc_id="222"))
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=seed.context.tenant_id,
            bc_id="222",
            advertiser_id="account-a",
            connection_id=seed.connection.id,
            in_bc=True,
            authorized=True,
            active=True,
        )
    )
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )

    session.add(
        BCConnectionBinding(
            tenant_id=seed.context.tenant_id,
            bc_id="222",
            connection_id=seed.connection.id,
            kind="OFFICIAL_API",
        )
    )
    session.flush()
    session.add(
        BCDefaultRoute(
            tenant_id=seed.context.tenant_id,
            bc_id="222",
            connection_id=seed.connection.id,
        )
    )
    seed.bc_id = "222"
    session.flush()
    seed.grant = session.get(
        BCAccountAccess,
        (seed.context.tenant_id, "222", "account-a", seed.connection.id),
    )
    for kind, identity, parent_kind, parent_id in [
        ("campaign", "c1", None, None),
        ("campaign", "c2", None, None),
        ("adgroup", "g1", "campaign", "c1"),
        ("adgroup", "g2", "campaign", "c2"),
        ("ad", "a1", "adgroup", "g1"),
        ("creative", "cr1", "ad", "a1"),
    ]:
        session.add(
            AdObject(
                **(
                    seed.row.model_dump()
                    | {
                        "kind": kind,
                        "remote_id": identity,
                        "parent_kind": parent_kind,
                        "parent_remote_id": parent_id,
                        "name": "重复名称",
                        "configuration": {"fixture": identity},
                    }
                )
            )
        )
    session.flush()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=seed.context.actor_id
    )
    with TestClient(app) as client:
        yield client, seed, session


def test_zero_report_navigation_and_scope_bound_pages(navigation):
    client, seed, session = navigation
    base = f"/api/tenants/{seed.context.tenant_id}"
    query = {
        "bc_id": seed.bc_id,
        "advertiser_ids": ["account-a"],
        "kind": "campaign",
        "limit": 1,
    }
    first = client.get(base + "/ad-directory", params=query)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [i["ref"]["remote_id"] for i in page["items"]] == ["c1"]
    assert page["next_cursor"]
    second = client.get(
        base + "/ad-directory", params=query | {"cursor": page["next_cursor"]}
    ).json()
    assert [i["ref"]["remote_id"] for i in second["items"]] == ["c2"]
    assert second["next_cursor"] is None
    for changed in [
        {"kind": "adgroup"},
        {"bc_id": "foreign"},
        {"advertiser_ids": ["other"]},
        {"cursor": page["next_cursor"] + "x"},
    ]:
        response = client.get(
            base + "/ad-directory",
            params=query | {"cursor": page["next_cursor"]} | changed,
        )
        assert response.status_code in (400, 403, 404, 422)
        assert "重复名称" not in response.text
    for kind, identity, parent_kind, parent_id in [
        ("adgroup", "g1", "campaign", "c1"),
        ("ad", "a1", "adgroup", "g1"),
        ("creative", "cr1", "ad", "a1"),
    ]:
        result = client.get(
            base + "/ad-directory",
            params=query
            | {"kind": kind, "parent_kind": parent_kind, "parent_remote_id": parent_id},
        ).json()
        assert [i["ref"]["remote_id"] for i in result["items"]] == [identity]
        detail = client.get(
            base + f"/ads/{kind}/{identity}",
            params={"bc_id": seed.bc_id, "advertiser_id": "account-a"},
        ).json()
        assert detail["ref"]["remote_id"] == identity
        assert detail["parent"]["ref"]["remote_id"] == parent_id
        assert detail["configuration"] == {"fixture": identity}
        assert detail["published_version"] == 1
        assert detail["source_channel"] == "OFFICIAL_API"
        assert detail["source_connection_id"] == str(seed.connection.id)
        assert detail["observed_at"]
    for tenant, changes in [
        (seed.other_context.tenant_id, {}),
        (
            seed.context.tenant_id,
            {
                "parent_kind": "campaign",
                "parent_remote_id": "c1",
                "parent_advertiser_id": "other",
                "kind": "adgroup",
            },
        ),
    ]:
        denied = client.get(
            f"/api/tenants/{tenant}/ad-directory", params=query | changes
        )
        assert denied.status_code in (403, 404)
        assert "重复名称" not in denied.text
    children = query | {
        "kind": "adgroup",
        "parent_kind": "campaign",
        "parent_remote_id": "c1",
    }
    assert (
        client.get(
            base + "/ad-directory", params=children | {"cursor": page["next_cursor"]}
        ).status_code
        == 400
    )
    for kind in ["campaign", "adgroup", "ad", "creative"]:
        denied = client.get(
            base + f"/ads/{kind}/a1",
            params={"bc_id": "foreign", "advertiser_id": "account-a"},
        )
        assert denied.status_code == 404
        assert "fixture" not in denied.text
    from sqlmodel import select

    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.query_models import QuerySnapshot

    assert (
        session.exec(
            select(QuerySnapshot).where(
                QuerySnapshot.tenant_id == seed.context.tenant_id
            )
        ).all()
        == []
    )
    assert (
        session.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == seed.context.tenant_id
            )
        ).all()
        == []
    )
    seed.grant.authorized = False
    session.flush()
    assert client.get(
        base + "/ad-directory", params=query | {"cursor": page["next_cursor"]}
    ).status_code in (403, 404)
    assert (
        client.get(
            base + "/ads/ad/a1",
            params={"bc_id": seed.bc_id, "advertiser_id": "account-a"},
        ).status_code
        == 404
    )


def test_directory_workspace_live_http(navigation):
    """Actual workspace and backend; only authentication is bound to test actor."""
    import os
    import socket
    import subprocess
    import threading
    from pathlib import Path

    import uvicorn

    from app.main import app
    from app.models import User

    _, seed, session = navigation
    # Enough duplicate names to require the production page size to continue.
    for number in range(3, 23):
        session.add(
            AdObject(
                **(
                    seed.row.model_dump()
                    | {
                        "kind": "campaign",
                        "remote_id": f"c{number:02}",
                        "parent_kind": None,
                        "parent_remote_id": None,
                        "name": "重复名称",
                    }
                )
            )
        )
    session.flush()
    from fastapi.routing import APIRoute

    def revoke_directory():
        seed.grant.authorized = False
        session.flush()
        return {"revoked": True}

    driver = APIRoute("/api/test-directory-revoke", revoke_directory, methods=["POST"])
    app.router.routes.insert(0, driver)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, seed.context.actor_id
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        result = subprocess.run(
            [
                "bun",
                "run",
                "test",
                "--project=workspace",
                "tests/ads-detail.spec.ts",
                "-g",
                "@directory-live",
                "--reporter=line",
            ],
            cwd=Path(__file__).resolve().parents[4] / "frontend",
            env={
                **os.environ,
                "API_PROXY_TARGET": f"http://127.0.0.1:{port}",
                "DIRECTORY_TENANT": str(seed.context.tenant_id),
                "DIRECTORY_BC": seed.bc_id,
                "PLAYWRIGHT_PORT": "5199",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        app.router.routes.remove(driver)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_cursor_separates_authorized_accounts_and_parent_ranges(navigation):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    client, seed, session = navigation
    session.add(
        AdvertiserAccount(
            tenant_id=seed.context.tenant_id,
            advertiser_id="account-b",
            currency="USD",
            timezone="UTC",
        )
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=seed.context.tenant_id,
            bc_id=seed.bc_id,
            advertiser_id="account-b",
            connection_id=seed.connection.id,
            in_bc=True,
            authorized=True,
            active=True,
        )
    )
    session.add(
        AdObject(
            **(
                seed.row.model_dump()
                | {
                    "kind": "campaign",
                    "remote_id": "c1",
                    "advertiser_id": "account-b",
                    "parent_kind": None,
                    "parent_remote_id": None,
                }
            )
        )
    )
    session.flush()
    base = f"/api/tenants/{seed.context.tenant_id}/ad-directory"
    query = {
        "bc_id": seed.bc_id,
        "kind": "campaign",
        "advertiser_ids": ["account-a", "account-b"],
        "limit": 2,
    }
    response = client.get(base, params=query)
    assert response.status_code == 200, response.text
    first = response.json()
    assert [
        (i["ref"]["advertiser_id"], i["ref"]["remote_id"]) for i in first["items"]
    ] == [("account-a", "c1"), ("account-a", "c2")]
    second = client.get(base, params=query | {"cursor": first["next_cursor"]}).json()
    assert [
        (i["ref"]["advertiser_id"], i["ref"]["remote_id"]) for i in second["items"]
    ] == [("account-b", "c1")]
    assert second["next_cursor"] is None
    assert (
        client.get(
            base,
            params=query
            | {"advertiser_ids": ["account-b"], "cursor": first["next_cursor"]},
        ).status_code
        == 400
    )
    assert (
        client.get(
            base,
            params=query
            | {
                "advertiser_ids": ["account-b"],
                "kind": "adgroup",
                "parent_kind": "campaign",
                "parent_remote_id": "c1",
                "parent_advertiser_id": "account-a",
            },
        ).status_code
        == 404
    )
    # Authentication must still guard the public route before any directory facts.
    client.app.dependency_overrides.pop(get_current_user)
    assert client.get(base, params=query).status_code == 401
