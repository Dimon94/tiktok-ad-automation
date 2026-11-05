"""Real tenant rows and continuations; only external provider transport is fake."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sqlmodel import Session, select

from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.modules.providers.connections import open_provider_session, verify_connection
from app.modules.providers.models import (
    LinkPreparationItem,
    ProviderApplication,
    ProviderConnection,
    ProviderSessionRefresh,
)
from app.modules.providers.tasks import process_item
from app.modules.tenants.models import TenantMembership
from tests.modules.providers.test_connection_isolation import connections as connections
from tests.modules.providers.test_connection_isolation import transport
from tests.modules.providers.test_link_recovery import add_item
from tests.modules.providers.test_link_recovery import (
    workflow as base_workflow,  # noqa: F401
)


@pytest.fixture
def workflow(base_workflow):  # noqa: F811
    from sqlalchemy import delete

    from app.jobs.models import DispatchTenantCursor, PendingDispatch

    try:
        yield base_workflow
    finally:
        with Session(engine) as session, session.begin():
            for model in (PendingDispatch, DispatchTenantCursor):
                session.exec(
                    delete(model).where(model.tenant_id == base_workflow[0].tenant_id)
                )


class ExpiringRemote:
    def __init__(self):
        self.calls = []
        self.login_hook = None
        self.login_error = None
        self.forbidden = False

    def __call__(self, request):
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/login"):
            if self.login_hook:
                self.login_hook(request)
            if self.login_error:
                return self.login_error(request)
            return httpx.Response(
                200, json={"code": "0000", "data": {"session": "new-token"}}
            )
        if self.forbidden:
            return httpx.Response(403, json={})
        if request.headers.get("session") != "new-token":
            return httpx.Response(401, json={})
        if path.endswith("getAppSwitchList"):
            data = [{"appid": "external-app", "name": "Application"}]
        elif path.endswith("getOptions"):
            data = {"channel_prefix": "fixture_"}
        else:
            data = {"data": [], "count": 0}
        return httpx.Response(200, json={"code": "0000", "data": data})


def read(context, identity, remote):
    with open_provider_session(
        database_engine=engine,
        context=context,
        connection_id=identity,
        application_id="external-app",
        action="provider_write",
        transport=httpx.MockTransport(remote),
    ) as scope:
        return scope.client.search("Moon", 1)


def verified(connections):
    contexts, ids = connections
    for context, identity in zip(contexts, ids, strict=True):
        verify_connection(
            database_engine=engine,
            context=context,
            connection_id=identity,
            transport=transport(),
        )
    return contexts, ids


def expire(context, identity, remote):
    with pytest.raises(DomainError) as error:
        read(context, identity, remote)
    assert error.value.code == "provider_session_refreshing"


def resume(context, identity, remote):
    for _ in range(8):
        before = len(remote.calls)
        try:
            result = read(context, identity, remote)
        except DomainError as error:
            assert error.code == "provider_session_refreshing", error.code
        else:
            return result
        finally:
            assert len(remote.calls) - before <= 1
    pytest.fail("session did not recover")


def test_expired_session_resumes_without_changing_application_generation(connections):
    contexts, ids = verified(connections)
    with Session(engine) as s, s.begin():
        generation = s.get(ProviderConnection, ids[0]).verification_token
        app = s.exec(
            select(ProviderApplication).where(
                ProviderApplication.connection_id == ids[0]
            )
        ).one()
        app.tiktok_minis_id = "mn-configured"
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    assert resume(contexts[0], ids[0], remote)["items"] == []
    assert remote.calls.count("/User/login") == 1
    with Session(engine) as s:
        row = s.get(ProviderConnection, ids[0])
        assert row.status == "active" and row.verification_token == generation
        assert (
            s.exec(
                select(ProviderApplication).where(
                    ProviderApplication.connection_id == ids[0]
                )
            )
            .one()
            .tiktok_minis_id
            == "mn-configured"
        )
        assert s.get(ProviderConnection, ids[1]).status == "active"


def test_refresh_single_claim_no_database_lock_and_tenant_isolation(connections):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    entered, release = Event(), Event()

    def wait(request):
        assert json.loads(request.content)["username"] == "fixture-0"
        with Session(engine) as s, s.begin():
            s.connection().exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            s.exec(
                select(ProviderConnection)
                .where(ProviderConnection.id == ids[0])
                .with_for_update()
            ).one()
        entered.set()
        assert release.wait(5)

    remote.login_hook = wait

    def attempt():
        with pytest.raises(DomainError) as error:
            read(contexts[0], ids[0], remote)
        return error.value.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt)
        assert entered.wait(5)
        try:
            assert attempt() == "provider_session_refreshing"
            with pytest.raises(DomainError) as foreign:
                read(contexts[1], ids[0], remote)
            assert foreign.value.code == "resource_not_found"
        finally:
            release.set()
        assert first.result() == "provider_session_refreshing"
    assert remote.calls.count("/User/login") == 1


@pytest.mark.parametrize("change", ["disabled", "password", "revoked"])
def test_stale_login_never_promotes_after_connection_or_actor_change(
    connections, change
):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    with Session(engine) as s:
        original = s.get(ProviderConnection, ids[0]).encrypted_credentials

    def mutate(_request):
        with Session(engine) as s, s.begin():
            row = s.get(ProviderConnection, ids[0])
            if change == "disabled":
                row.status = "disabled"
            elif change == "password":
                row.credential_version += 1
                row.status = "pending"
            else:
                s.get(
                    TenantMembership, (contexts[0].tenant_id, contexts[0].actor_id)
                ).active = False

    remote.login_hook = mutate
    with pytest.raises(DomainError):
        read(contexts[0], ids[0], remote)
    with Session(engine) as s:
        row = s.get(ProviderConnection, ids[0])
        assert row.encrypted_credentials == original and row.status != "active"


def test_permission_denial_does_not_relogin(connections):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    remote.forbidden = True
    with pytest.raises(DomainError) as error:
        read(contexts[0], ids[0], remote)
    assert error.value.code == "provider_application_forbidden"
    assert "/User/login" not in remote.calls


class WorkflowRemote(ExpiringRemote):
    def __init__(self, business):
        super().__init__()
        self.business = business
        self.token_expired = True

    def __call__(self, request):
        path = request.url.path
        if path.endswith(("/login", "getAppSwitchList", "getOptions")):
            response = super().__call__(request)
            if path.endswith("getOptions"):
                return httpx.Response(
                    200, json={"code": "0000", "data": {"channel_prefix": "test_"}}
                )
            return response
        if self.token_expired and request.headers.get("session") != "new-token":
            self.calls.append(path)
            return httpx.Response(401, json={})
        self.calls.append(path)
        return self.business(request)


def workflow_password(workflow):
    context, identity, _ = workflow
    with Session(engine) as s, s.begin():
        row = s.get(ProviderConnection, identity)
        row.encrypted_credentials = encrypt_credentials(
            tenant_id=context.tenant_id,
            value={
                "username": "local-user",
                "password": "local-password",
                "session": "old-token",
            },
        )


def task_once(workflow, identity, remote):
    context, _, _ = workflow
    with Session(engine) as s, s.begin():
        item = s.get(LinkPreparationItem, identity)
        work = dict(item.resolved.get("_work", {}))
        work.pop("next_dispatch_at", None)
        item.resolved = {**item.resolved, "_work": work}
        revision = work.get("dispatch_revision", 0)
    before = len(remote.calls)
    process_item(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"item_id": str(identity), "revision": revision},
        transport=httpx.MockTransport(remote),
    )
    assert len(remote.calls) - before <= 1
    with Session(engine) as s:
        item = s.get(LinkPreparationItem, identity)
        return item.status, item.resolved


def test_actual_task_continuations_resume_expired_login_and_create_once(workflow):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    for _ in range(25):
        status, result = task_once(workflow, identity, remote)
        if status == "ready":
            break
    else:
        pytest.fail(str((status, result)))
    assert remote.calls.count("/User/login") == 1
    assert workflow[2].calls.count("create") == 1
    assert workflow[2].calls.count("generateGuideUrl") == 1
    assert workflow[2].calls.count("saveGuideUrl") == 1


def test_session_recovery_waits_for_another_claim_without_hot_loop(workflow):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    task_once(workflow, identity, remote)
    task_once(workflow, identity, remote)
    with Session(engine) as db, db.begin():
        refresh = db.get(ProviderSessionRefresh, workflow[1])
        refresh.claim_token = uuid4()
        refresh.claimed_until = datetime.now(UTC) + timedelta(seconds=30)
    before = len(remote.calls)
    _, result = task_once(workflow, identity, remote)
    assert len(remote.calls) == before
    assert datetime.fromisoformat(result["_work"]["next_dispatch_at"]) > datetime.now(
        UTC
    ) + timedelta(seconds=3)


def test_unknown_saved_write_then_expired_read_recovers_without_second_write(workflow):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    remote.token_expired = False
    workflow[2].fail = "save_unknown"
    for _ in range(20):
        status, result = task_once(workflow, identity, remote)
        if status == "result_unknown":
            break
    else:
        pytest.fail("did not reach unknown saved write")
    remote.token_expired = True
    for _ in range(15):
        status, result = task_once(workflow, identity, remote)
        if status == "ready":
            break
    else:
        pytest.fail(str((status, result)))
    assert workflow[2].calls.count("saveGuideUrl") == 1
    assert workflow[2].calls.count("create") == 1
    assert remote.calls.count("/User/login") == 1


def test_wrong_password_stops_automatic_login_and_never_exposes_raw_message(
    connections,
):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    remote.login_error = lambda request: httpx.Response(
        200, json={"code": "invalid", "message": "private-password", "data": {}}
    )
    with pytest.raises(DomainError) as error:
        read(contexts[0], ids[0], remote)
    assert error.value.code == "provider_auth_failed" and "private-password" not in str(
        error.value
    )
    for _ in range(3):
        with pytest.raises(DomainError):
            read(contexts[0], ids[0], remote)
    assert remote.calls.count("/User/login") == 1
    with Session(engine) as s:
        assert s.get(ProviderConnection, ids[0]).error_code == "provider_auth_failed"


def test_network_failures_cool_down_after_three_claims_then_recover_automatically(
    connections,
):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    remote.login_error = lambda request: httpx.Response(503, json={})
    for _attempt in range(3):
        with pytest.raises(DomainError) as error:
            read(contexts[0], ids[0], remote)
        assert error.value.code == "provider_session_refreshing"
        calls = len(remote.calls)
        with pytest.raises(DomainError):
            read(contexts[0], ids[0], remote)
        assert len(remote.calls) == calls
        with Session(engine) as s, s.begin():
            refresh = s.get(ProviderSessionRefresh, ids[0])
            assert refresh.claim_token is None
            assert refresh.due_at > datetime.now(UTC)
            refresh.due_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DomainError) as error:
        read(contexts[0], ids[0], remote)
    assert error.value.code == "provider_session_refreshing"
    assert remote.calls.count("/User/login") == 3
    with Session(engine) as session, session.begin():
        refresh = session.get(ProviderSessionRefresh, ids[0])
        assert refresh.due_at > datetime.now(UTC) + timedelta(seconds=590)
        assert refresh.unavailable_since is not None and refresh.cooldown_rounds == 1
        refresh.due_at = datetime.now(UTC) - timedelta(seconds=1)
    remote.login_error = None
    assert resume(contexts[0], ids[0], remote)["items"] == []
    assert remote.calls.count("/User/login") == 4
    with Session(engine) as session:
        refresh = session.get(ProviderSessionRefresh, ids[0])
        assert refresh.unavailable_since is None and refresh.cooldown_rounds == 0


def test_wangyan_refresh_uses_only_own_email_and_new_cookie(connections):
    contexts, ids = verified(connections)
    with Session(engine) as s, s.begin():
        row = s.get(ProviderConnection, ids[0])
        row.kind = "wangyan"
        row.encrypted_credentials = encrypt_credentials(
            tenant_id=contexts[0].tenant_id,
            value={
                "email": "fixture@example.com",
                "password": "fixture-password",
                "token": "old-token",
            },
        )
        from app.modules.providers.models import ProviderApplication

        app = s.exec(
            select(ProviderApplication).where(
                ProviderApplication.connection_id == ids[0]
            )
        ).one()
        app.channel_config = {
            "is_tt": True,
            "verification_token": str(row.verification_token),
        }

    class Wangyan:
        def __init__(self):
            self.calls = []

        def __call__(self, request):
            self.calls.append(request.url.path)
            if request.url.path.endswith("/pwd_login"):
                assert json.loads(request.content) == {
                    "email": "fixture@example.com",
                    "password": "fixture-password",
                }
                assert request.headers.get("cookie", "") == ""
                return httpx.Response(
                    200,
                    json={"code": 0},
                    headers={"Set-Cookie": "x-ds-admin-token=new-token; Path=/"},
                )
            if request.headers.get("cookie") != "x-ds-admin-token=new-token":
                return httpx.Response(401, json={})
            if request.url.path.endswith("/group/apps"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [
                            {
                                "package_name": "external-app",
                                "name": "Application",
                                "is_tt": 1,
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"code": 0, "data": []})

    remote = Wangyan()
    expire(contexts[0], ids[0], remote)
    assert resume(contexts[0], ids[0], remote)["items"] == []
    assert remote.calls.count("/api/login/pwd_login") == 1


def test_late_old_session_expiry_cannot_invalidate_successful_refresh(connections):
    from app.modules.providers.session_refresh import mark_expired

    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    with Session(engine) as s:
        row = s.get(ProviderConnection, ids[0])
        ciphertext, version, verification = (
            row.encrypted_credentials,
            row.credential_version,
            row.verification_token,
        )
    expire(contexts[0], ids[0], remote)
    resume(contexts[0], ids[0], remote)
    mark_expired(
        database_engine=engine,
        context=contexts[0],
        connection_id=ids[0],
        version=version,
        verification=verification,
        ciphertext=ciphertext,
    )
    assert read(contexts[0], ids[0], remote)["items"] == []
    assert remote.calls.count("/User/login") == 1


def test_expired_refresh_claim_is_recovered_without_second_completed_login(connections):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    with pytest.raises(DomainError):
        read(contexts[0], ids[0], remote)
    from uuid import uuid4

    with Session(engine) as s, s.begin():
        row = s.get(ProviderSessionRefresh, ids[0])
        assert row.phase == "applications"
        row.claim_token = uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        row.attempts = 1
    assert resume(contexts[0], ids[0], remote)["items"] == []
    assert remote.calls.count("/User/login") == 1


def test_changed_channel_configuration_blocks_old_business_generation(workflow):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    # Deliberately discover another prefix after re-login; old plan must not write.
    original = remote.__class__.__call__

    class Changed(WorkflowRemote):
        def __call__(self, request):
            response = original(self, request)
            if request.url.path.endswith("getOptions"):
                return httpx.Response(
                    200, json={"code": "0000", "data": {"channel_prefix": "changed_"}}
                )
            return response

    remote = Changed(workflow[2])
    for _ in range(10):
        status, result = task_once(workflow, identity, remote)
        if status == "blocked_auth":
            break
    else:
        pytest.fail(str((status, result)))
    assert result["error_code"] == "provider_credentials_changed"
    assert "create" not in workflow[2].calls


def test_unknown_generate_is_never_replayed_by_session_refresh(workflow):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    remote.token_expired = False
    workflow[2].fail = "generate_unknown"
    for _ in range(20):
        status, _ = task_once(workflow, identity, remote)
        if status == "result_unknown":
            break
    remote.token_expired = True
    for _ in range(10):
        status, _ = task_once(workflow, identity, remote)
    assert status == "result_unknown"
    assert workflow[2].calls.count("generateGuideUrl") == 1
    assert "saveGuideUrl" not in workflow[2].calls
    assert remote.calls.count("/User/login") == 1


def test_watchdog_rearms_lost_refresh_continuation_with_original_identity(workflow):
    from app.jobs.models import PendingDispatch
    from app.modules.providers.tasks import recover_preparations

    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    status, result = task_once(workflow, identity, remote)
    assert status == "retryable_error"
    with Session(engine) as s, s.begin():
        item = s.get(LinkPreparationItem, identity)
        work = dict(item.resolved["_work"])
        revision = work["dispatch_revision"]
        work["repair_after"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        work["next_dispatch_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        item.resolved = {**item.resolved, "_work": work}
        dispatch = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_key == f"provider-item:{identity}:{revision}",
                PendingDispatch.tenant_id == workflow[0].tenant_id,
            )
        ).one()
        dispatch.published_at = datetime.now(UTC) - timedelta(seconds=100)
        dispatch.available_at = dispatch.published_at
        dispatch_id = dispatch.id
    assert recover_preparations(database_engine=engine) > 0
    with Session(engine) as s:
        dispatch = s.get(PendingDispatch, dispatch_id)
        assert (
            dispatch.published_at is None and dispatch.payload["revision"] == revision
        )
    for _ in range(25):
        status, _ = task_once(workflow, identity, remote)
        if status == "ready":
            break
    assert status == "ready" and remote.calls.count("/User/login") == 1


def test_refresh_progress_uses_short_continuation_not_exponential_business_backoff(
    workflow,
):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    for _ in range(4):
        status, result = task_once(workflow, identity, remote)
        assert status == "retryable_error"
        due = datetime.fromisoformat(result["_work"]["next_dispatch_at"])
        assert due <= datetime.now(UTC)


def test_initial_wrong_password_is_terminal_credentials_error_not_automatic_expiry(
    connections,
):
    contexts, ids = connections
    with pytest.raises(DomainError) as error:
        verify_connection(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"code": "10001", "data": {}})
            ),
        )
    assert error.value.code == "provider_auth_failed"
    with Session(engine) as s:
        row = s.get(ProviderConnection, ids[0])
        assert row.status == "error" and row.error_code == "provider_auth_failed"


def test_transient_outage_cools_original_task_then_resumes_without_user_action(
    workflow,
):
    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    remote.login_error = lambda request: httpx.Response(503, json={})
    task_once(workflow, identity, remote)  # discover expiry
    for _ in range(3):
        status, _ = task_once(workflow, identity, remote)
        assert status == "retryable_error"
        with Session(engine) as session, session.begin():
            refresh = session.get(ProviderSessionRefresh, workflow[1])
            refresh.due_at = datetime.now(UTC) - timedelta(seconds=1)
    status, result = task_once(workflow, identity, remote)
    assert status == "retryable_error"
    due = datetime.fromisoformat(result["_work"]["next_dispatch_at"])
    assert due > datetime.now(UTC) + timedelta(seconds=590)
    assert remote.calls.count("/User/login") == 3
    with Session(engine) as session, session.begin():
        refresh = session.get(ProviderSessionRefresh, workflow[1])
        refresh.due_at = datetime.now(UTC) - timedelta(seconds=1)
    remote.login_error = None
    for _ in range(25):
        status, _ = task_once(workflow, identity, remote)
        if status == "ready":
            break
    assert status == "ready"
    assert workflow[2].calls.count("create") == 1
    assert remote.calls.count("/User/login") == 4


@pytest.mark.parametrize("invalid", ["empty_apps", "malformed_json"])
def test_unknown_discovery_shape_stops_instead_of_infinite_network_retry(
    connections, invalid
):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    with pytest.raises(DomainError):
        read(contexts[0], ids[0], remote)  # successful login

    def malformed(_request):
        if invalid == "empty_apps":
            return httpx.Response(200, json={"code": "0000", "data": []})
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(DomainError) as error:
        read(contexts[0], ids[0], malformed)
    assert error.value.code in {
        "provider_application_discovery_unverified",
        "provider_schema_unsupported",
    }
    with Session(engine) as session:
        assert session.get(ProviderSessionRefresh, ids[0]).phase == "failed"
        assert session.get(ProviderConnection, ids[0]).status == "error"


@pytest.mark.parametrize("phase", ["applications", "options"])
def test_candidate_expiry_restarts_bounded_login_without_manual_verification(
    connections, phase
):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    with pytest.raises(DomainError):
        read(contexts[0], ids[0], remote)
    if phase == "options":
        with pytest.raises(DomainError):
            read(contexts[0], ids[0], remote)
    with pytest.raises(DomainError) as error:
        read(
            contexts[0],
            ids[0],
            lambda request: httpx.Response(
                401 if phase == "applications" else 200,
                json={"code": "10001", "data": {}},
            ),
        )
    assert error.value.code == "provider_session_refreshing"
    with Session(engine) as session, session.begin():
        row = session.get(ProviderSessionRefresh, ids[0])
        assert row.phase == "login" and row.login_attempts == 1
        assert row.candidate_ciphertext is None and row.applications == []
        assert session.get(ProviderConnection, ids[0]).status == "reauth_required"
        row.due_at = datetime.now(UTC) - timedelta(seconds=1)
    assert resume(contexts[0], ids[0], remote)["items"] == []
    assert remote.calls.count("/User/login") == 2


def test_repeated_candidate_expiry_cools_without_login_storm(connections):
    contexts, ids = verified(connections)
    remote = ExpiringRemote()
    expire(contexts[0], ids[0], remote)
    for _ in range(3):
        with pytest.raises(DomainError):
            read(contexts[0], ids[0], remote)
        with pytest.raises(DomainError) as error:
            read(contexts[0], ids[0], lambda request: httpx.Response(401, json={}))
        assert error.value.code == "provider_session_refreshing"
        with Session(engine) as session, session.begin():
            session.get(ProviderSessionRefresh, ids[0]).due_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
    with pytest.raises(DomainError):
        read(contexts[0], ids[0], remote)
    assert remote.calls.count("/User/login") == 3
    with Session(engine) as session:
        refresh = session.get(ProviderSessionRefresh, ids[0])
        assert refresh.cooldown_rounds == 1
        assert refresh.due_at > datetime.now(UTC) + timedelta(seconds=590)
        assert session.get(ProviderConnection, ids[0]).status == "reauth_required"


@pytest.mark.parametrize("phase", ["login", "applications"])
def test_worker_soft_deadline_preserves_original_task_recovery(workflow, phase):
    from billiard.exceptions import SoftTimeLimitExceeded

    workflow_password(workflow)
    identity = add_item(workflow)
    remote = WorkflowRemote(workflow[2])
    task_once(workflow, identity, remote)
    if phase == "applications":
        task_once(workflow, identity, remote)

    class Interrupted:
        calls = []

        def __call__(self, request):
            self.calls.append(request.url.path)
            raise SoftTimeLimitExceeded()

    status, _ = task_once(workflow, identity, Interrupted())
    assert status == "retryable_error"
    with Session(engine) as session, session.begin():
        refresh = session.get(ProviderSessionRefresh, workflow[1])
        assert refresh.phase == phase
        assert session.get(ProviderConnection, workflow[1]).status == "reauth_required"
        refresh.due_at = datetime.now(UTC) - timedelta(seconds=1)
    for _ in range(25):
        status, _ = task_once(workflow, identity, remote)
        if status == "ready":
            break
    assert status == "ready" and workflow[2].calls.count("create") == 1
