"""真实 PostgreSQL：未知平台限流有界试探，跨领取保留退避与分页证据。"""

from datetime import datetime

import pytest

from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_material_resume import (
    Pages,
    claim,
    enqueue,
    facts,
    pages,
    run,
)


def _unknown():
    return AccountAdmissionDeferred(300_000, source="provider", reason="40100")


def _wait(factory, job_id, expected):
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        admission = job.progress["last_admission"]
        assert expected <= admission["retry_after_ms"] <= expected + 1000
        at = datetime.fromisoformat(admission["at"])
        assert (
            abs((job.due_at - at).total_seconds() * 1000 - admission["retry_after_ms"])
            < 10
        )
        assert job.attempts == 0
        assert job.progress["material_paging"]["observed_at"]
        return job.progress


@pytest.mark.parametrize("reason", ["40100", "http_429"])
def test_unknown_throttle_backoff_survives_claims_and_local_denial(
    committed_sync_seed, reason
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(
        count=3,
        failure=(
            2,
            AccountAdmissionDeferred(300_000, source="provider", reason=reason),
        ),
    )
    assert run(factory, job, reader) == "WAITING"
    original = _wait(factory, job.id, 5000)
    assert [p.page for p in pages(factory, job.id)] == [1]
    reader.failure = (2, AccountAdmissionDeferred(1000, reason="endpoint_rate"))
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    with factory() as session:
        assert (
            session.get(ReportSyncJob, job.id).progress["provider_throttle"]
            == original["provider_throttle"]
        )
    reader.failure = (
        2,
        AccountAdmissionDeferred(300_000, source="provider", reason=reason),
    )
    for expected in (10_000, 20_000, 40_000, 60_000, 300_000, 300_000):
        assert run(factory, claim(factory, job.id), reader) == "WAITING"
        progress = _wait(factory, job.id, expected)
        assert (
            progress["material_paging"]["observed_at"]
            == original["material_paging"]["observed_at"]
        )
    assert facts(factory, seed.tenant_id) == []
    assert reader.calls.count(1) == 1
    reader.failure = None
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert len(facts(factory, seed.tenant_id)) == 3
    with factory() as session:
        assert "provider_throttle" not in session.get(ReportSyncJob, job.id).progress


def test_successful_page_resets_unknown_probe_budget(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=3, failure=(2, _unknown()))
    assert run(factory, job, reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    _wait(factory, job.id, 10_000)
    reader.failure = (3, _unknown())
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    progress = _wait(factory, job.id, 5000)
    assert progress["provider_throttle"]["attempts"] == 1
    assert [p.page for p in pages(factory, job.id)] == [1, 2]


@pytest.mark.parametrize(
    "reason,delay",
    [("qps", 1000), ("qpm", 300_000), ("qpd", 3_600_000), ("retry_after", 120_000)],
)
def test_explicit_provider_wait_is_never_shortened(committed_sync_seed, reason, delay):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(
        count=3,
        failure=(2, AccountAdmissionDeferred(delay, source="provider", reason=reason)),
    )
    assert run(factory, job, reader) == "WAITING"
    progress = _wait(factory, job.id, delay)
    assert "provider_throttle" not in progress
    assert progress["last_admission"]["retry_after_ms"] == delay


def test_real_sdk_shared_throttle_controls_different_jobs_due_at(
    committed_sync_seed, redis_client
):
    import json
    from contextlib import contextmanager
    from datetime import UTC, timedelta
    from hashlib import sha256
    from time import sleep
    from types import SimpleNamespace
    from uuid import uuid4

    from business_api_client import ApiClient

    from app.integrations.tiktok.official.accounts import OfficialReadRequests
    from app.integrations.tiktok.platform_quotas import sdk_endpoint
    from app.jobs.admission import AdmissionPolicy, admission_keys, admitted_scope
    from tests.modules.reporting.test_material_resume import DAY

    seed, factory = committed_sync_seed
    app_scope = f"sdk-job-throttle-test-{uuid4()}"
    endpoint = sdk_endpoint("reports.material_breakdown")
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
    cooldown = (
        admission_keys(app_scope, endpoint, seed.tenant_id, "acc-1")[1] + ":cooldown"
    )

    @contextmanager
    def scope(advertiser_id, operation, _deadline):
        assert sdk_endpoint(operation) == endpoint
        with admitted_scope(
            redis_client,
            app_scope=app_scope,
            endpoint=endpoint,
            rate_group=endpoint,
            tenant_id=seed.tenant_id,
            advertiser_id=advertiser_id,
            policy=policy,
            denied_error=AccountAdmissionDeferred,
        ):
            yield

    client = ApiClient()
    requests = OfficialReadRequests(
        client, request_scope=scope, deadline=datetime.now(UTC) + timedelta(seconds=30)
    )
    response = SimpleNamespace(
        data=json.dumps({"code": 40100, "message": "Unknown quota type", "data": {}}),
        getheaders=lambda: {},
    )

    class Reader:
        def material_day(self, *, advertiser_id, start, end, page):
            return requests.invoke(
                "reports.material_breakdown",
                advertiser_id,
                lambda _: client.deserialize(response, "object"),
            )

    try:
        for offset, expected in enumerate([5000, 10000]):
            redis_client.delete(cooldown)
            sleep(0.005)
            day = DAY + timedelta(days=offset)
            job = enqueue(factory, seed, start=day, end=day)
            assert run(factory, job, Reader()) == "WAITING"
            with factory() as session:
                saved = session.get(ReportSyncJob, job.id)
                progress = saved.progress
                admission = progress["last_admission"]
                assert expected <= admission["retry_after_ms"] <= expected + 100
                at = datetime.fromisoformat(admission["at"])
                assert (
                    abs(
                        (saved.due_at - at).total_seconds() * 1000
                        - admission["retry_after_ms"]
                    )
                    < 10
                )
                assert saved.status == "WAITING" and saved.attempts == 0
                assert "provider_throttle" not in progress
                assert admission["reason"] == "40100"
    finally:
        digest = sha256(app_scope.encode()).hexdigest()
        keys = list(redis_client.scan_iter(match=f"tiktok:{{{digest}}}:*"))
        if keys:
            redis_client.delete(*keys)
