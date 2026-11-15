"""JB27 R7: formal Spec independent publication expectations on real PG."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_parent_day,
)
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import D, _route


@pytest.mark.parametrize("level", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("empty", [False, True])
def test_same_observation_replay_noop_and_conflict(session, sync_seed, level, empty):
    at = datetime.now(UTC)
    kwargs = {
        "tenant_id": sync_seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
        "requested_at": at,
        "route": FrozenTikTokRoute(**_route(sync_seed)),
    }

    def publish(value, empty=False, at=at):
        row = (
            ad_row("a1", D, total=value)
            if level == "ad"
            else {
                "dimensions": {f"{level}_id": "same", "stat_time_day": str(D)},
                "metrics": {
                    "spend": "50",
                    "native_growth_total_ad_impression_value": value,
                },
            }
        )
        params = kwargs | {"rows": [] if empty else [row], "requested_at": at}
        return (
            publish_ad_day(session, **params)
            if level == "ad"
            else publish_parent_day(session, level=level, **params)
        )

    publish("40", empty)
    key = (sync_seed.tenant_id, "acc-1", f"{level}_day", D)
    before = session.get(ReportSyncState, key).model_dump()
    assert publish("40", empty) == 0
    session.expire_all()
    assert session.get(ReportSyncState, key).model_dump() == before
    with pytest.raises(DomainError) as error:
        publish("39")
    assert error.value.code == "report_publication_conflict"
    for i, value in enumerate(["2", "0", "-"], 1):
        publish(value, at=at + timedelta(seconds=i))
        session.expire_all()  # SQL整桶替换后重新读PG，避免复用ORM中的旧对象。
        assert (
            session.get(ReportSyncState, key).publication_version
            == before["publication_version"] + i
        )
        from decimal import Decimal

        from app.modules.reporting.store_models import AdReportDay, ParentReportDay

        fact = (
            session.get(AdReportDay, (sync_seed.tenant_id, "acc-1", D, "a1"))
            if level == "ad"
            else session.get(
                ParentReportDay, (sync_seed.tenant_id, "acc-1", level, "same", D)
            )
        )
        assert fact.revenue_total == (None if value == "-" else Decimal(value))
    assert publish("999", at=at - timedelta(seconds=1)) == 0


@pytest.mark.parametrize("level", ["campaign", "adgroup", "ad"])
def test_single_page_duplicate_is_domain_error(session, sync_seed, level):
    row = (
        ad_row("a1", D)
        if level == "ad"
        else {
            "dimensions": {f"{level}_id": "same", "stat_time_day": str(D)},
            "metrics": {"spend": "50"},
        }
    )
    params = {
        "tenant_id": sync_seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
        "requested_at": datetime.now(UTC),
        "route": FrozenTikTokRoute(**_route(sync_seed)),
        "rows": [row, row],
    }
    with pytest.raises(DomainError) as error:
        if level == "ad":
            publish_ad_day(session, **params)
        else:
            publish_parent_day(session, level=level, **params)
    assert error.value.code == "report_row_invalid"
    assert (
        session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", f"{level}_day", D))
        is None
    )


@pytest.mark.parametrize("kind", ["campaign_day", "adgroup_day", "ad_day"])
def test_query_dependency_detects_each_layer_evidence(session, sync_seed, kind):
    from app.modules.reporting.query_reuse import dependency_stamp
    from app.modules.reporting.schemas import ReportingFilter

    filters = ReportingFilter(
        dimension="ad", advertiser_ids=("acc-1",), start_date=D, end_date=D
    )

    def stamp():
        return dependency_stamp(
            session, context=sync_seed.context, bc_id="bc-a", filters=filters
        ).digest

    before = stamp()
    state = ReportSyncState(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        report_kind=kind,
        stat_date=D,
        status="FAILED",
        error_code="synthetic",
    )
    session.add(state)
    session.flush()
    after = stamp()
    assert after != before
    state.active_contract = "changed-contract"
    session.flush()
    assert stamp() != after


@pytest.mark.parametrize("level", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("invalidation", ["fence", "route"])
def test_two_workers_reject_lost_fence(committed_sync_seed, level, empty, invalidation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.jobs import enqueue_job, execute_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.claiming import claim_jobs

    seed, factory = committed_sync_seed
    with factory() as s:
        jid = enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            bc_id="bc-a",
            kind=f"{level}_day",
            tier="manual",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
        )
        token = next(
            j
            for j in claim_jobs(
                s, now=datetime.now(UTC) + timedelta(seconds=1), limit=4
            )
            if j.id == jid
        ).claim_token
        s.commit()
    entered, release = Event(), Event()

    class Reader:
        def __init__(self, old):
            self.old = old

        def read(self, **kwargs):
            if self.old:
                entered.set()
                assert release.wait(10)
            row = (
                ad_row("a1", D, total="999" if self.old else "2")
                if level == "ad"
                else {
                    "dimensions": {f"{level}_id": "same", "stat_time_day": str(D)},
                    "metrics": {
                        "spend": "50",
                        "native_growth_total_ad_impression_value": "999"
                        if self.old
                        else "2",
                    },
                }
            )
            return RawReportPage(
                () if empty and not self.old else (row,),
                1,
                1,
                0 if empty and not self.old else 1,
            )

        ad_day = campaign_day = adgroup_day = read

    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(
            execute_job, factory, job_id=jid, claim_token=token, reader=Reader(True)
        )
        try:
            assert entered.wait(10)
            with factory() as s:
                current = s.get(ReportSyncJob, jid, with_for_update=True)
                replacement = uuid4() if invalidation == "fence" else token
                if invalidation == "fence":
                    current.claim_token = replacement
                    current.claim_generation += 1
                else:
                    from app.modules.accounts.connection_models import (
                        BCConnectionBinding,
                    )

                    binding = s.get(
                        BCConnectionBinding,
                        (seed.tenant_id, "bc-a", seed.connection.id),
                    )
                    binding.revision += 1
                s.commit()
            new = pool.submit(
                execute_job,
                factory,
                job_id=jid,
                claim_token=replacement,
                reader=Reader(False),
            )
            assert new.result(timeout=10) == (
                "COMPLETE" if invalidation == "fence" else "FAILED"
            )
        finally:
            release.set()
        assert old.result(timeout=10) == "FAILED"
    with factory() as s:
        state = s.get(ReportSyncState, (seed.tenant_id, "acc-1", f"{level}_day", D))
        assert state.publication_version == (1 if invalidation == "fence" else 0)
        assert state.row_count == (0 if empty or invalidation == "route" else 1)
        assert s.get(ReportSyncJob, jid).status == (
            "COMPLETE" if invalidation == "fence" else "FAILED"
        )


@pytest.mark.parametrize("level", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize(
    "change",
    [
        "account",
        "date",
        "level",
        "route",
        "contract",
        "totals",
        "overlap",
        "single_duplicate",
        "dimensions",
        "metrics",
    ],
)
def test_worker_checkpoint_rejects_frozen_changes(
    committed_sync_seed, monkeypatch, level, change
):
    from dataclasses import replace

    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.reporting.jobs import enqueue_job, execute_job
    from app.modules.reporting.levels import LEVELS
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.claiming import claim_jobs

    seed, factory = committed_sync_seed
    kind = f"{level}_day"
    with factory() as s:
        jid = enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            bc_id="bc-a",
            kind=kind,
            tier="manual",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
        )
        token = next(
            j
            for j in claim_jobs(
                s, now=datetime.now(UTC) + timedelta(seconds=1), limit=4
            )
            if j.id == jid
        ).claim_token
        s.commit()

    class Reader:
        defer = True

        def read(self, *, page, **kwargs):
            if page == 2 and self.defer:
                raise AccountAdmissionDeferred(1000)
            rid = "same" if change in ("overlap", "single_duplicate") else f"id{page}"
            row = (
                ad_row(rid, D)
                if level == "ad"
                else {
                    "dimensions": {f"{level}_id": rid, "stat_time_day": str(D)},
                    "metrics": {"spend": "50"},
                }
            )
            if change == "single_duplicate":
                return RawReportPage((row, row), 1, 1, 2)
            return RawReportPage(
                (row,), page, 2, 3 if not self.defer and change == "totals" else 2
            )

        ad_day = campaign_day = adgroup_day = read

    reader = Reader()
    result = execute_job(factory, job_id=jid, claim_token=token, reader=reader)
    if change == "single_duplicate":
        assert result == "FAILED"
        with factory() as s:
            assert s.get(ReportSyncJob, jid).error_code == "report_row_invalid"
        return
    assert result == "WAITING"
    with factory() as s:
        retry = next(
            j
            for j in claim_jobs(
                s, now=datetime.now(UTC) + timedelta(seconds=5), limit=4
            )
            if j.id == jid
        )
        token = retry.claim_token
        if change == "account":
            retry.advertiser_ids = ["outside"]
        if change == "date":
            retry.end_date = D + timedelta(days=1)
        if change == "level":
            retry.kind = "creative_day"
        if change == "route":
            retry.frozen_route = retry.frozen_route | {"binding_revision": 999}
        s.commit()
    if change == "contract":
        monkeypatch.setitem(
            LEVELS, kind, replace(LEVELS[kind], contract="incompatible")
        )
    if change == "dimensions":
        monkeypatch.setitem(LEVELS, kind, replace(LEVELS[kind], dimension="changed_id"))
    if change == "metrics":
        monkeypatch.setitem(LEVELS, kind, replace(LEVELS[kind], metrics=("spend",)))
    reader.defer = False
    assert execute_job(factory, job_id=jid, claim_token=token, reader=reader) in (
        "FAILED",
        "WAITING",
    )
    with factory() as s:
        assert (
            s.get(ReportSyncState, (seed.tenant_id, "acc-1", kind, D)) is None
            or s.get(
                ReportSyncState, (seed.tenant_id, "acc-1", kind, D)
            ).last_success_at
            is None
        )
        job = s.get(ReportSyncJob, jid)
        expected_errors = (
            {"account_access_denied", "account_not_in_bc"}
            if change == "account"
            else {"route_binding_changed"}
            if change == "route"
            else {"report_snapshot_changed"}
            if change in {"totals", "overlap"}
            else {"report_query_invalid"}
        )
        assert job.error_code in expected_errors
        assert "basic_snapshot" not in job.progress
