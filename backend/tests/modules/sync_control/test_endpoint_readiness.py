"""只观察原平台账本；调度探测不能消费发送次数或增加拒绝指标。"""

from uuid import uuid4

import pytest

from app.jobs.admission import AdmissionPolicy, admission_keys, admit_call


def _policy(**changes):
    return AdmissionPolicy(
        **{
            "app_max_inflight": 10,
            "endpoint_max_inflight": 10,
            "tenant_max_inflight": 10,
            "advertiser_max_inflight": 10,
            "app_calls_per_window": 10,
            "endpoint_calls_per_window": 10,
            "window_ms": 1000,
            "lease_ms": 60000,
            **changes,
        }
    )


def _scope():
    return {
        "app_scope": f"test-readiness-{uuid4()}",
        "endpoint": "GET /open_api/v1.3/report/integrated/get/",
        "rate_group": "GET /open_api/v1.3/report/integrated/get/",
        "tenant_id": uuid4(),
        "advertiser_id": "test-account",
    }


def test_repeated_readiness_probes_do_not_consume_quota(redis_client):
    from app.modules.sync_control.readiness import endpoint_readiness

    scope, policy = _scope(), _policy()
    for _ in range(20):
        ready = endpoint_readiness(redis_client, policy=policy, **scope)
        assert ready.capacity_hint == 10
        assert ready.retry_after_ms == 0
    assert not any(redis_client.exists(key) for key in admission_keys(**scope))


@pytest.mark.parametrize(
    "limit,reason",
    [
        ("endpoint_calls_per_window", "QUOTA"),
        ("endpoint_calls_per_minute", "QUOTA"),
        ("endpoint_calls_per_day", "QUOTA"),
        ("app_max_inflight", "CAPACITY"),
        ("tenant_max_inflight", "CAPACITY"),
    ],
)
def test_readiness_uses_actual_admission_dimensions(redis_client, limit, reason):
    from app.modules.sync_control.readiness import endpoint_readiness

    scope, policy = _scope(), _policy(**{limit: 1})
    assert admit_call(redis_client, **scope, lease_id=uuid4(), policy=policy).granted
    keys = admission_keys(**scope)
    before = [redis_client.zcard(key) for key in keys]
    ready = endpoint_readiness(redis_client, policy=policy, **scope)
    assert ready.capacity_hint == 0
    assert ready.retry_after_ms > 0
    assert ready.reason == reason
    assert [redis_client.zcard(key) for key in keys] == before
    assert not any(
        name.startswith("local_denied")
        for name in redis_client.hgetall(keys[1] + ":metrics")
    )


def test_cooldown_is_shared_by_alias_but_not_independent_endpoint(redis_client):
    from app.modules.sync_control.readiness import endpoint_readiness

    scope, policy = _scope(), _policy()
    seconds, micros = redis_client.time()
    redis_client.set(
        admission_keys(**scope)[1] + ":cooldown",
        seconds * 1000 + micros // 1000 + 60000,
        px=61000,
    )
    blocked = endpoint_readiness(redis_client, policy=policy, **scope)
    assert blocked.reason == "PROVIDER_COOLDOWN"
    assert blocked.capacity_hint == 0
    independent = scope | {"endpoint": "other", "rate_group": "other"}
    assert (
        endpoint_readiness(redis_client, policy=policy, **independent).capacity_hint
        == 10
    )
