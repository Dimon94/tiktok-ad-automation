from uuid import uuid4

import pytest

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from tests.modules.reporting.sync_conftest import (  # noqa: F401
    committed_sync_seed,
    sync_seed,
)


@pytest.fixture(autouse=True)
def isolated_sync_quota(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"sync-test-{uuid4()}")
