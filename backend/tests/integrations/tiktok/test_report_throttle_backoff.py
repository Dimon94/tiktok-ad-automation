"""真实Redis验证未知报表限流按应用/实际接口共享，避免每个job各自短试探。"""

from hashlib import sha256
from time import sleep
from uuid import uuid4

import pytest

from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import AdmissionPolicy, admission_keys, admitted_scope


@pytest.fixture
def report_scope(redis_client):
    scope = {
        "app_scope": f"throttle-test-{uuid4()}",
        "endpoint": "GET /report-test/",
        "rate_group": "GET /report-test/",
        "tenant_id": uuid4(),
        "advertiser_id": "a",
    }
    digest = sha256(scope["app_scope"].encode()).hexdigest()
    try:
        yield scope
    finally:
        keys = list(redis_client.scan_iter(match=f"tiktok:{{{digest}}}:*"))
        if keys:
            redis_client.delete(*keys)


def _policy():
    return AdmissionPolicy(
        app_max_inflight=50,
        endpoint_max_inflight=50,
        tenant_max_inflight=50,
        advertiser_max_inflight=50,
        app_calls_per_window=50,
        endpoint_calls_per_window=50,
        window_ms=1000,
        lease_ms=60000,
    )


def _cooldown(scope):
    return (
        admission_keys(
            scope["app_scope"],
            scope["endpoint"],
            scope["tenant_id"],
            scope["advertiser_id"],
            scope["rate_group"],
        )[1]
        + ":cooldown"
    )


def _scope(redis, scope):
    return admitted_scope(
        redis,
        **scope,
        policy=_policy(),
        denied_error=AccountAdmissionDeferred,
    )


def _unknown():
    error = AccountAdmissionDeferred(5000, source="provider", reason="40100")
    error.shared_throttle_probe = True
    return error


def test_different_jobs_share_bounded_unknown_report_probe_rounds(
    redis_client, report_scope
):
    for expected in [5000, 10000, 20000, 40000, 60000, 300000, 300000]:
        # 只在本测试的接口中模拟冷却已到期；计数仍跨job/租户保留。
        redis_client.delete(_cooldown(report_scope))
        sleep(0.005)
        scope = report_scope | {"tenant_id": uuid4(), "advertiser_id": str(uuid4())}
        with pytest.raises(AccountAdmissionDeferred) as caught:
            with _scope(redis_client, scope):
                raise _unknown()
        assert expected - 100 <= caught.value.retry_after_ms <= expected + 100
        assert expected - 200 <= redis_client.pttl(_cooldown(scope)) <= expected


def test_old_inflight_failures_do_not_exhaust_probe_rounds(redis_client, report_scope):
    first, late = _scope(redis_client, report_scope), _scope(redis_client, report_scope)
    first.__enter__()
    late.__enter__()
    sleep(0.005)
    error = _unknown()
    assert first.__exit__(AccountAdmissionDeferred, error, None) is False
    # 旧在途响应即使晚于当前冷却结束，仍不是一个新的探测轮次。
    redis_client.delete(_cooldown(report_scope))
    error = _unknown()
    assert late.__exit__(AccountAdmissionDeferred, error, None) is False
    assert 4900 <= error.retry_after_ms <= 5100


def test_late_success_does_not_clear_recent_throttle(redis_client, report_scope):
    first, late = _scope(redis_client, report_scope), _scope(redis_client, report_scope)
    first.__enter__()
    late.__enter__()
    error = _unknown()
    assert first.__exit__(AccountAdmissionDeferred, error, None) is False
    late.__exit__(None, None, None)
    assert redis_client.pttl(_cooldown(report_scope)) > 4500


def test_unknown_report_probe_is_endpoint_local(redis_client, report_scope):
    with pytest.raises(AccountAdmissionDeferred):
        with _scope(redis_client, report_scope):
            raise _unknown()
    other = report_scope | {"endpoint": "GET /other/", "rate_group": "GET /other/"}
    with _scope(redis_client, other):
        pass
    with pytest.raises(AccountAdmissionDeferred) as caught:
        with _scope(redis_client, report_scope | {"tenant_id": uuid4()}):
            pass
    assert caught.value.admission_reason == "provider_cooldown"


def test_parallel_responses_share_one_probe_round(redis_client, report_scope):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(6)

    def request(_):
        with pytest.raises(AccountAdmissionDeferred) as caught:
            with _scope(redis_client, report_scope):
                barrier.wait(timeout=5)
                raise _unknown()
        return caught.value.retry_after_ms

    with ThreadPoolExecutor(max_workers=6) as pool:
        delays = list(pool.map(request, range(6)))
    assert all(4900 <= delay <= 5100 for delay in delays)
    assert redis_client.hget(_cooldown(report_scope) + ":probe", "attempts") == "1"


def test_late_old_failure_after_new_probe_does_not_upgrade_again(
    redis_client, report_scope
):
    first, old = _scope(redis_client, report_scope), _scope(redis_client, report_scope)
    first.__enter__()
    old.__enter__()
    error = _unknown()
    first.__exit__(AccountAdmissionDeferred, error, None)
    redis_client.delete(_cooldown(report_scope))
    sleep(0.005)
    with pytest.raises(AccountAdmissionDeferred) as caught:
        with _scope(redis_client, report_scope):
            raise _unknown()
    assert 9900 <= caught.value.retry_after_ms <= 10100
    error = _unknown()
    old.__exit__(AccountAdmissionDeferred, error, None)
    assert 9900 <= error.retry_after_ms <= 10100
    assert redis_client.hget(_cooldown(report_scope) + ":probe", "attempts") == "2"


def test_probe_preserves_existing_long_cooldown(redis_client, report_scope):
    with pytest.raises(AccountAdmissionDeferred) as caught:
        with _scope(redis_client, report_scope):
            now = redis_client.time()
            until = now[0] * 1000 + now[1] // 1000 + 3600000
            redis_client.set(_cooldown(report_scope), until, px=3600000)
            raise _unknown()
    assert 3599000 <= caught.value.retry_after_ms <= 3600000
    assert redis_client.pttl(_cooldown(report_scope)) > 3599000


@pytest.mark.parametrize("failure", ["missing_lease", "metrics", "lua"])
def test_uncoordinated_probe_keeps_conservative_delay(
    redis_client, report_scope, failure
):
    from redis.exceptions import ConnectionError

    class Unavailable:
        def __getattr__(self, name):
            return getattr(redis_client, name)

        def hincrby(self, *args, **kwargs):
            if failure == "metrics":
                raise ConnectionError("test metrics unavailable")
            return redis_client.hincrby(*args, **kwargs)

        def eval(self, script, keys, *args):
            if failure == "lua" and keys == 3:
                raise ConnectionError("test cooldown unavailable")
            return redis_client.eval(script, keys, *args)

    with pytest.raises(AccountAdmissionDeferred) as caught:
        with _scope(Unavailable(), report_scope):
            if failure == "missing_lease":
                keys = admission_keys(
                    report_scope["app_scope"],
                    report_scope["endpoint"],
                    report_scope["tenant_id"],
                    report_scope["advertiser_id"],
                )
                redis_client.delete(keys[2])
            raise _unknown()
    assert caught.value.admission_source == "provider"
    assert caught.value.retry_after_ms >= 300000


def test_probe_quiet_period_and_other_application_are_independent(
    redis_client, report_scope
):
    with pytest.raises(AccountAdmissionDeferred):
        with _scope(redis_client, report_scope):
            raise _unknown()
    probe = _cooldown(report_scope) + ":probe"
    assert 899000 <= redis_client.pttl(probe) <= 900000
    # 本测试仅让其私有探测计数过期，当前冷却仍能阻止本地重复领取且不增计数。
    redis_client.pexpire(probe, 1)
    sleep(0.01)
    redis_client.delete(_cooldown(report_scope))
    with pytest.raises(AccountAdmissionDeferred) as caught:
        with _scope(redis_client, report_scope):
            raise _unknown()
    assert 4900 <= caught.value.retry_after_ms <= 5100
    other = report_scope | {"app_scope": report_scope["app_scope"] + "-other"}
    try:
        with _scope(redis_client, other):
            pass
    finally:
        digest = sha256(other["app_scope"].encode()).hexdigest()
        keys = list(redis_client.scan_iter(match=f"tiktok:{{{digest}}}:*"))
        if keys:
            redis_client.delete(*keys)


def test_real_expiry_allows_next_probe_without_worker_sleep(redis_client, report_scope):
    from time import monotonic

    with pytest.raises(AccountAdmissionDeferred):
        with _scope(redis_client, report_scope):
            raise _unknown()
    deadline = monotonic() + 7
    while redis_client.exists(_cooldown(report_scope)):
        assert monotonic() < deadline
        sleep(0.05)
    with pytest.raises(AccountAdmissionDeferred) as caught:
        with _scope(redis_client, report_scope):
            raise _unknown()
    assert 9900 <= caught.value.retry_after_ms <= 10100
