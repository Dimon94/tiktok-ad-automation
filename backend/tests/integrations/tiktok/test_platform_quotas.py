"""平台额度按应用/物理接口，而非业务动作计数。"""

from uuid import uuid4

import pytest

from app.integrations.tiktok.platform_quotas import sdk_endpoint, sdk_quota
from app.jobs.admission import AdmissionPolicy, admit_call, release_call


def test_management_budget_and_rename_share_actual_endpoint():
    assert (
        sdk_endpoint("management.rename", kind="campaign", smart=True)
        == sdk_endpoint("management.update_budget", kind="campaign", smart=True)
        == "POST /open_api/v1.3/smart_plus/campaign/update/"
    )
    assert sdk_endpoint(
        "management.set_status", kind="campaign", smart=False
    ) != sdk_endpoint("management.set_status", kind="ad", smart=False)


def test_create_uses_smart_endpoint_not_regular_ad_exception():
    assert (
        sdk_endpoint("build.create_ad") == "POST /open_api/v1.3/smart_plus/ad/create/"
    )
    assert sdk_quota("BASIC", sdk_endpoint("build.create_ad")) == (10, 600, 864000)
    assert sdk_quota("BASIC", "POST /open_api/v1.3/ad/create/") == (5, 150, 86400)
    assert sdk_quota("ULTIMATE", "POST /open_api/v1.3/ad/create/") == (15, 300, 86400)
    with pytest.raises(ValueError):
        sdk_quota("unknown", sdk_endpoint("build.create_ad"))


def test_minute_limit_is_shared_and_denial_consumes_no_daily_budget(redis_client):
    scope = {
        "app_scope": f"test-{uuid4()}",
        "endpoint": "physical",
        "tenant_id": uuid4(),
        "advertiser_id": "a",
    }
    policy = AdmissionPolicy(
        app_max_inflight=50,
        endpoint_max_inflight=50,
        tenant_max_inflight=50,
        advertiser_max_inflight=50,
        app_calls_per_window=50,
        endpoint_calls_per_window=50,
        window_ms=1000,
        lease_ms=60000,
        endpoint_calls_per_minute=2,
        endpoint_calls_per_day=3,
    )
    for _ in range(2):
        lease = uuid4()
        assert admit_call(redis_client, **scope, lease_id=lease, policy=policy).granted
        release_call(redis_client, **scope, lease_id=lease)
    denied = admit_call(redis_client, **scope, lease_id=uuid4(), policy=policy)
    assert not denied.granted and denied.reason == "endpoint_minute"
    # 同端点不同租户、账户也不能拆开分钟额度。
    denied = admit_call(
        redis_client,
        **(scope | {"tenant_id": uuid4(), "advertiser_id": "b"}),
        lease_id=uuid4(),
        policy=policy,
    )
    assert not denied.granted and denied.reason == "endpoint_minute"


def test_daily_limit_does_not_reset_when_inflight_released(redis_client):
    scope = {
        "app_scope": f"test-{uuid4()}",
        "endpoint": "physical",
        "tenant_id": uuid4(),
        "advertiser_id": "a",
    }
    policy = AdmissionPolicy(
        app_max_inflight=50,
        endpoint_max_inflight=50,
        tenant_max_inflight=50,
        advertiser_max_inflight=50,
        app_calls_per_window=50,
        endpoint_calls_per_window=50,
        window_ms=1000,
        lease_ms=60000,
        endpoint_calls_per_minute=50,
        endpoint_calls_per_day=1,
    )
    lease = uuid4()
    assert admit_call(redis_client, **scope, lease_id=lease, policy=policy).granted
    release_call(redis_client, **scope, lease_id=lease)
    result = admit_call(redis_client, **scope, lease_id=uuid4(), policy=policy)
    assert not result.granted and result.reason == "endpoint_day"
    assert result.retry_after_ms > 0


def test_sdk_policy_uses_declared_level_and_not_legacy_three(monkeypatch):
    from app.core.config import settings
    from app.jobs.admission import admission_policy

    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            "sdk_level": "BASIC",
            "base": {
                "app_max_inflight": 50,
                "endpoint_max_inflight": 50,
                "tenant_max_inflight": 50,
                "advertiser_max_inflight": 50,
                "app_calls_per_window": 10,
                "endpoint_calls_per_window": 3,
                "window_ms": 1000,
                "lease_ms": 60000,
            },
            "endpoints": {},
        },
    )
    policy = admission_policy(
        "build.create_ad",
        channel="OFFICIAL_API",
        physical_endpoint=sdk_endpoint("build.create_ad"),
    )
    assert policy.endpoint_calls_per_window == 10
    assert policy.endpoint_calls_per_minute == 600
    assert policy.endpoint_calls_per_day == 864000
    # MCP不是开发者应用的Basic桶，不从SDK额度推断服务额度。
    assert (
        admission_policy(
            "build.create_ad", channel="OFFICIAL_MCP"
        ).endpoint_calls_per_window
        == 3
    )


def test_sdk_physical_endpoints_have_independent_rate_but_aliases_share(redis_client):
    from app.integrations.tiktok.admission import admit_tiktok_call
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    route = FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc",
        connection_id=uuid4(),
        channel="OFFICIAL_API",
        authorization_revision=1,
        adapter_contract_revision="test",
    )
    scope = f"test-{uuid4()}"
    policy = AdmissionPolicy(
        app_max_inflight=50,
        endpoint_max_inflight=50,
        tenant_max_inflight=50,
        advertiser_max_inflight=50,
        app_calls_per_window=1,
        endpoint_calls_per_window=1,
        window_ms=1000,
        lease_ms=60000,
    )

    def admit(operation, endpoint):
        return admit_tiktok_call(
            redis_client,
            route=route,
            advertiser_id="a",
            operation=operation,
            scope=scope,
            policy=policy,
            physical_endpoint=endpoint,
        )

    endpoint = sdk_endpoint("management.rename", kind="campaign", smart=True)
    with admit("management.rename", endpoint):
        pass
    with pytest.raises(AccountAdmissionDeferred):
        with admit("management.update_budget", endpoint):
            pytest.fail("budget cannot bypass rename on same campaign/update endpoint")
    # 另一个物理接口可继续，不共享虚构的所有接口合计1QPS桶。
    with admit("build.create_ad", sdk_endpoint("build.create_ad")):
        pass


def test_declared_sdk_level_resolves_lease_only_callers(monkeypatch):
    from app.core.config import settings
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.jobs.admission import admission_policy

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    # 这些既有调用只读取lease_ms，不能因新额度配置而在进入gateway前失败。
    for operation in (
        "materials.upload_image_url",
        "materials.get_images",
        "materials.share_assets",
        "music.get",
    ):
        policy = admission_policy(operation, channel="OFFICIAL_API")
        assert policy.lease_ms == 960000
        assert policy.endpoint_calls_per_window == 10


def test_provider_cooldown_is_shared_by_physical_endpoint(redis_client):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admitted_scope

    scope = {
        "app_scope": f"test-{uuid4()}",
        "endpoint": "physical",
        "tenant_id": uuid4(),
        "advertiser_id": "a",
    }
    policy = AdmissionPolicy(
        app_max_inflight=50,
        endpoint_max_inflight=50,
        tenant_max_inflight=50,
        advertiser_max_inflight=50,
        app_calls_per_window=50,
        endpoint_calls_per_window=50,
        window_ms=1000,
        lease_ms=60000,
    )
    with pytest.raises(AccountAdmissionDeferred):
        with admitted_scope(
            redis_client, **scope, policy=policy, denied_error=AccountAdmissionDeferred
        ):
            raise AccountAdmissionDeferred(300000, source="provider", reason="qpm")
    result = admit_call(
        redis_client,
        **(scope | {"tenant_id": uuid4(), "advertiser_id": "b"}),
        lease_id=uuid4(),
        policy=policy,
    )
    assert not result.granted and result.reason == "provider_cooldown"
    assert result.retry_after_ms > 299000
    # 其它接口仍可使用额度；不能把一个端点的QPM暂停扩大到全部TikTok。
    assert admit_call(
        redis_client, **(scope | {"endpoint": "other"}), lease_id=uuid4(), policy=policy
    ).granted


def test_expired_request_still_releases_own_inflight_lease(redis_client):
    from datetime import UTC, datetime, timedelta

    from app.integrations.tiktok.bounded_resources import bounded_redis
    from app.jobs.admission import admission_keys, admitted_scope

    scope = {
        "app_scope": f"test-{uuid4()}",
        "endpoint": "physical",
        "tenant_id": uuid4(),
        "advertiser_id": "a",
    }
    policy = AdmissionPolicy(
        app_max_inflight=50,
        endpoint_max_inflight=50,
        tenant_max_inflight=50,
        advertiser_max_inflight=50,
        app_calls_per_window=50,
        endpoint_calls_per_window=50,
        window_ms=1000,
        lease_ms=60000,
    )
    keys = admission_keys(
        scope["app_scope"], scope["endpoint"], scope["tenant_id"], "a"
    )
    with bounded_redis(
        redis_client, task_deadline=datetime.now(UTC) + timedelta(seconds=10)
    ) as client:
        with admitted_scope(
            client, **scope, policy=policy, denied_error=lambda _: RuntimeError("quota")
        ):
            assert redis_client.zcard(keys[2]) == 1
            client._task_deadline = datetime.now(UTC) - timedelta(seconds=1)
    assert redis_client.zcard(keys[2]) == 0
    assert redis_client.zcard(keys[1]) == 1
