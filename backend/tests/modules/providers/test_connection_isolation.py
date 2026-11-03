from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.core.credentials import decrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.models import User
from app.modules.providers.connections import (
    open_provider_session,
    save_connection,
    verify_connection,
)
from app.modules.providers.models import ProviderApplication, ProviderConnection
from app.modules.providers.schemas import ProviderKind
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from tests.modules.conftest import create_context


@pytest.fixture
def connections(monkeypatch):
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    contexts, ids = [], []
    with Session(engine) as session, session.begin():
        for index in range(2):
            context = create_context(session, role="tenant_admin")
            row = save_connection(
                session,
                context=context,
                kind="jiashu",
                display_name=f"Fixture {index}",
                credentials={
                    "username": f"fixture-{index}",
                    "password": "private-password",
                },
            )
            contexts.append(context)
            ids.append(row.id)
    try:
        yield contexts, ids
    finally:
        with Session(engine) as session, session.begin():
            tenants = [c.tenant_id for c in contexts]
            for model in (
                ProviderApplication,
                ProviderConnection,
                AuditEvent,
                TenantMembership,
            ):
                session.exec(delete(model).where(model.tenant_id.in_(tenants)))
            session.exec(delete(Tenant).where(Tenant.id.in_(tenants)))
            session.exec(
                delete(User).where(User.id.in_([c.actor_id for c in contexts]))
            )


def transport(callback=None):
    def handle(request):
        if callback:
            callback(request)
        if request.url.path == "/User/login":
            return httpx.Response(
                200, json={"code": "0000", "data": {"session": "private-login-session"}}
            )
        if request.url.path.endswith("getAppSwitchList"):
            data = [{"appid": "external-app", "name": "Application"}]
        elif request.url.path.endswith("getOptions"):
            data = {"channel_prefix": "fixture_"}
        else:
            data = {"data": [], "count": 0}
        return httpx.Response(200, json={"code": "0000", "data": data})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("kind", "credentials"),
    [
        ("duiba", {"account": "duiba-account", "password": "private-password"}),
        ("minglue", {"account": "minglue-account", "password": "private-password"}),
        (
            "gangganhao",
            {
                "portal_id": "56",
                "username": "portal-user",
                "password": "private-password",
            },
        ),
        (
            "rongliang",
            {"email": "rongliang@example.test", "password": "private-password"},
        ),
    ],
)
def test_new_provider_kinds_accept_their_exact_credential_fields(
    kind, credentials, monkeypatch
):
    # 独立提供加密密钥，不依赖其他用例 fixture 遗留的模块级设置。
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    assert kind in get_args(ProviderKind)
    with Session(engine) as session, session.begin():
        context = create_context(session, role="tenant_admin")
        row = save_connection(
            session,
            context=context,
            kind=kind,
            display_name=f"{kind} fixture",
            credentials=credentials,
        )
        assert row.kind == kind
        tenant_id, actor_id = context.tenant_id, context.actor_id
    with Session(engine) as session, session.begin():
        for model in (ProviderConnection, AuditEvent, TenantMembership):
            session.exec(delete(model).where(model.tenant_id == tenant_id))
        session.exec(delete(Tenant).where(Tenant.id == tenant_id))
        session.exec(delete(User).where(User.id == actor_id))


def test_provider_kind_does_not_expose_manual_other_kind():
    assert "other" not in get_args(ProviderKind)


def test_encrypted_connections_are_tenant_bound_and_verified_apps_are_external(
    connections,
):
    contexts, ids = connections
    with Session(engine) as session:
        row = session.get(ProviderConnection, ids[0])
        assert "private-password" not in repr(row) + row.encrypted_credentials
        assert (
            decrypt_credentials(
                tenant_id=contexts[0].tenant_id, ciphertext=row.encrypted_credentials
            )["username"]
            == "fixture-0"
        )
        with pytest.raises(DomainError):
            decrypt_credentials(
                tenant_id=contexts[1].tenant_id, ciphertext=row.encrypted_credentials
            )
    verify_connection(
        database_engine=engine,
        context=contexts[0],
        connection_id=ids[0],
        transport=transport(),
    )
    with open_provider_session(
        database_engine=engine,
        context=contexts[0],
        connection_id=ids[0],
        application_id="external-app",
        transport=transport(),
    ) as scope:
        assert scope.application_id == "external-app"
        assert scope.client.channel_for("123") == "fixture_123"
        assert scope.client.search("Moon", 1)["items"] == []
    assert scope.http.is_closed
    with pytest.raises(DomainError):
        with open_provider_session(
            database_engine=engine,
            context=contexts[1],
            connection_id=ids[0],
            application_id="external-app",
            transport=transport(),
        ):
            pytest.fail("must not yield foreign credentials")


def test_verification_network_holds_no_lock_and_stale_version_never_promotes(
    connections,
):
    contexts, ids = connections
    changed = False

    def callback(_request):
        nonlocal changed
        if changed:
            return
        changed = True
        with Session(engine) as session, session.begin():
            session.connection().exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
            row = session.exec(
                select(ProviderConnection)
                .where(ProviderConnection.id == ids[0])
                .with_for_update()
            ).one()
            row.credential_version += 1
            row.status = "pending"

    with pytest.raises(DomainError) as error:
        verify_connection(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            transport=transport(callback),
        )
    assert error.value.code == "version_conflict"
    with Session(engine) as session:
        assert session.get(ProviderConnection, ids[0]).status == "pending"
        assert not session.exec(
            select(ProviderApplication).where(
                ProviderApplication.connection_id == ids[0]
            )
        ).all()


def test_disable_during_verification_cannot_be_undone(connections):
    contexts, ids = connections

    def callback(request):
        if request.url.path.endswith("getOptions"):
            with Session(engine) as session, session.begin():
                session.get(ProviderConnection, ids[0]).status = "disabled"

    with pytest.raises(DomainError):
        verify_connection(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            transport=transport(callback),
        )
    with Session(engine) as session:
        assert session.get(ProviderConnection, ids[0]).status == "disabled"


def test_live_claim_rejects_duplicate_login_and_readonly_cannot_save(connections):
    contexts, ids = connections
    with Session(engine) as session, session.begin():
        row = session.get(ProviderConnection, ids[0])
        row.status = "verifying"
        row.verification_token = uuid4()
        row.verifying_started_at = datetime.now(UTC)
        session.get(
            TenantMembership, (contexts[1].tenant_id, contexts[1].actor_id)
        ).role = "viewer"

    def forbidden(_request):
        pytest.fail("No provider request permitted")

    with pytest.raises(DomainError) as error:
        verify_connection(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            transport=httpx.MockTransport(forbidden),
        )
    assert error.value.code == "provider_verification_in_progress"
    with Session(engine) as session, pytest.raises(DomainError):
        save_connection(
            session,
            context=contexts[1],
            kind="jiashu",
            display_name="Blocked",
            credentials={"username": "u", "password": "p"},
        )


def test_session_expiry_marks_only_current_connection_without_retry(connections):
    contexts, ids = connections
    for context, connection_id in zip(contexts, ids, strict=True):
        verify_connection(
            database_engine=engine,
            context=context,
            connection_id=connection_id,
            transport=transport(),
        )
    requests = []

    def expired(request):
        requests.append(request)
        return httpx.Response(
            200, json={"code": "10001", "message": "private-session", "data": {}}
        )

    with pytest.raises(DomainError) as error:
        with open_provider_session(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            application_id="external-app",
            transport=httpx.MockTransport(expired),
        ) as scope:
            scope.client.search("Moon", 1)
    assert error.value.code == "provider_session_refreshing" and len(requests) == 1
    with Session(engine) as session:
        assert session.get(ProviderConnection, ids[0]).status == "reauth_required"
        assert session.get(ProviderConnection, ids[1]).status == "active"


def test_successful_rediscovery_does_not_keep_old_application_usable(connections):
    contexts, ids = connections
    verify_connection(
        database_engine=engine,
        context=contexts[0],
        connection_id=ids[0],
        transport=transport(),
    )
    # 管理员保存的取链默认值与发现写入的 channel_config 分开，重新发现不覆盖。
    with Session(engine) as session, session.begin():
        app = session.exec(
            select(ProviderApplication).where(
                ProviderApplication.connection_id == ids[0],
                ProviderApplication.external_id == "external-app",
            )
        ).one()
        app.link_defaults = {"episode": 2}
    verify_connection(
        database_engine=engine,
        context=contexts[0],
        connection_id=ids[0],
        transport=transport(),
    )
    with Session(engine) as session:
        assert session.exec(
            select(ProviderApplication.link_defaults).where(
                ProviderApplication.connection_id == ids[0],
                ProviderApplication.external_id == "external-app",
            )
        ).one() == {"episode": 2}

    def replacement(request):
        if request.url.path.endswith("login"):
            data = {"session": "replacement-session"}
        elif request.url.path.endswith("getAppSwitchList"):
            data = [{"appid": "replacement-app", "name": "Replacement"}]
        else:
            data = {"channel_prefix": "replacement_"}
        return httpx.Response(200, json={"code": "0000", "data": data})

    verify_connection(
        database_engine=engine,
        context=contexts[0],
        connection_id=ids[0],
        transport=httpx.MockTransport(replacement),
    )
    with pytest.raises(DomainError) as error:
        with open_provider_session(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            application_id="external-app",
            transport=transport(),
        ):
            pytest.fail("unseen previous-generation app must not be usable")
    assert error.value.code == "provider_application_forbidden"
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(ProviderApplication).where(
                        ProviderApplication.connection_id == ids[0]
                    )
                ).all()
            )
            == 2
        )


def test_verification_failure_and_disabled_edit_do_not_expose_or_replace_credentials(
    connections,
):
    contexts, ids = connections
    with Session(engine) as session:
        ciphertext = session.get(ProviderConnection, ids[0]).encrypted_credentials
    with pytest.raises(DomainError):
        verify_connection(
            database_engine=engine,
            context=contexts[0],
            connection_id=ids[0],
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"code": "10005", "message": "raw-secret", "data": {}}
                )
            ),
        )
    with Session(engine) as session, session.begin():
        row = session.get(ProviderConnection, ids[0])
        assert row.encrypted_credentials == ciphertext
        assert row.error_code == "provider_application_forbidden"
        row.status = "disabled"
    with Session(engine) as session, pytest.raises(DomainError):
        save_connection(
            session,
            context=contexts[0],
            kind="jiashu",
            display_name="Changed",
            connection_id=ids[0],
            credentials={"username": "new", "password": "new"},
        )
