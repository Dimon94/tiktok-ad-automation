from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.models import User


def test_legacy_telemetry_configuration_does_not_capture_requests(monkeypatch):
    import runpy
    from unittest.mock import Mock

    import sentry_sdk
    from pydantic import HttpUrl

    import app.main

    initialize = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(settings, "SENTRY_DSN", HttpUrl("https://public/1"))
        patch.setattr(settings, "FASTAPI_ENV", None)
        patch.setattr(sentry_sdk, "init", initialize)
        original_app = app.main.app
        probe = runpy.run_path(app.main.__file__)
        assert probe["app"] is not original_app
        assert app.main.app is original_app
        initialize.assert_not_called()


@pytest.fixture
def empty_tiktok_app(monkeypatch):
    for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, name, "")


@pytest.mark.usefixtures("empty_tiktok_app")
def test_unconfigured_callback_is_explicit(client):
    response = client.get(
        "/api/integrations/tiktok/callback",
        params={"auth_code": "arbitrary-sensitive-code", "state": "private-state"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "code": "tiktok_app_not_configured",
        "message": "等待配置开发者应用",
        "retryable": False,
    }
    assert "arbitrary-sensitive-code" not in response.text
    assert "private-state" not in response.text


@pytest.mark.usefixtures("empty_tiktok_app")
def test_partial_app_lists_missing_names_without_values(client, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", "opaque-app-value")
    response = client.get("/api/integrations/tiktok/callback")
    assert response.status_code == 422
    data = response.json()
    assert data["code"] == "tiktok_app_incomplete"
    assert "TIKTOK_APP_SECRET" in data["message"]
    assert "TIKTOK_REDIRECT_URI" in data["message"]
    assert "TIKTOK_APP_ID" not in data["message"]
    assert "opaque-app-value" not in response.text


def test_configured_callback_requires_issued_state(client, monkeypatch):
    for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, name, "opaque-config-value")
    response = client.get(
        "/api/integrations/tiktok/callback", params={"auth_code": "sample"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_oauth_state"


def test_public_signup_is_closed_and_does_not_create_users(client, session):
    username = f"{uuid4().hex}"
    response = client.post(
        "/api/users/signup",
        json={"username": username, "password": "valid-test-password"},
    )
    assert response.status_code == 403
    assert session.exec(select(User).where(User.username == username)).first() is None
    assert client.post("/api/users/signup", json={}).status_code == 403


def test_bootstrap_schema_excludes_template_business_and_signup(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/integrations/tiktok/callback" in paths
    assert "/api/users/" in paths
    assert not any(
        "/items" in path or "/private/" in path or path.endswith("/signup")
        for path in paths
    )
