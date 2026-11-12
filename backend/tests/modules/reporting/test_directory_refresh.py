from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import refresh_report_directory
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_creative_day,
)
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.reporting.test_creative_reporting import creative_row
from tests.modules.reporting.test_jobs import _route


def prepared(session, seed, *, day=None, spend="30", tier="hot", count=1, types=None):
    day = day or datetime.now(UTC).date()
    job_id = enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier=tier,
        advertiser_ids=["acc-2"],
        start=day,
        end=day,
        frozen_route=_route(seed),
        actor_id=seed.actor_id,
    )
    assert job_id is not None
    rows = [
        ad_row(
            f"a{x}", day, adv="acc-2", campaign=f"c{x}", adgroup=f"g{x}", spend=spend
        )
        for x in range(count)
    ]
    if types:
        for row, kind in zip(rows, types, strict=True):
            row["metrics"]["campaign_automation_type"] = kind
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=job_id,
        accounts={"acc-2": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=rows,
    )
    return session.get(ReportSyncJob, job_id)


def test_creative_report_refresh_requests_unmapped_ids_with_deleted_scope(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    day = datetime.now(UTC).date()
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="creative_day",
        tier="material",
        advertiser_ids=["acc-2"],
        start=day,
        end=day,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    row = creative_row("creative-1", spend="12", revenue="4")
    row["dimensions"]["stat_time_day"] = f"{day} 00:00:00"
    row["metrics"]["advertiser_id"] = "acc-2"
    publish_creative_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=job_id,
        accounts={"acc-2": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[row],
    )

    run = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    assert run.query["ids"] == ["creative-1"]
    assert run.query["include_deleted"] is True
    assert run.query["directory_service_v1"] == "identity"
    assert run.workload == "background"


@pytest.mark.parametrize("case", ["zero", "history", "daily", "disabled"])
def test_only_live_spending_hot_report_needs_directory(
    session, sync_seed, monkeypatch, case
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", case != "disabled")
    job = prepared(
        session,
        sync_seed,
        spend="0" if case == "zero" else "30",
        day=datetime.now(UTC).date() - timedelta(days=10)
        if case == "history"
        else None,
        tier="daily" if case == "daily" else "hot",
    )
    assert refresh_report_directory(session, job) == 0


@pytest.mark.parametrize("stale", [False, True])
def test_fresh_parent_chain_skips_read_and_stale_chain_refreshes(
    session, sync_seed, monkeypatch, stale
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    for kind, remote in [("ad", "a0"), ("adgroup", "g0"), ("campaign", "c0")]:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                kind=kind,
                remote_id=remote,
                ad_type="SMART_PLUS",
                observed_at=datetime.now(UTC) - timedelta(hours=2 if stale else 0),
                published_version=1,
            )
        )
    session.flush()
    assert refresh_report_directory(session, job) == (3 if stale else 0)


def test_report_refresh_batches_all_spending_objects_by_ad_type(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(
        session,
        sync_seed,
        count=3,
        types=["UPGRADED_SMART_PLUS", "MANUAL", "UPGRADED_SMART_PLUS"],
    )
    assert refresh_report_directory(session, job) == 6
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    ads = {r.ad_type: r.query["ids"] for r in runs if r.kind == "ad"}
    assert ads == {"SMART_PLUS": ["a0", "a2"], "REGULAR": ["a1"]}


@pytest.mark.parametrize(
    "mode,selected_bc,expected",
    [("BC", "bc-a", 3), ("BC", "bc-b", 0), ("ALL", None, 3), ("ACCOUNTS", None, 3)],
)
def test_existing_directory_refresh_prioritizes_exact_rule_scope(
    session, sync_seed, monkeypatch, mode, selected_bc, expected
):
    from app.modules.automation_rules.models import AutomationRule

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    for kind, remote in [("ad", "a0"), ("adgroup", "g0"), ("campaign", "c0")]:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                kind=kind,
                remote_id=remote,
                ad_type="SMART_PLUS",
                observed_at=now - timedelta(minutes=11),
                published_version=1,
            )
        )
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="scope-test",
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={
                "accounts": {
                    "mode": mode,
                    "bc_ids": [selected_bc] if selected_bc else [],
                    "advertiser_ids": ["acc-2"],
                }
            },
        )
    )
    session.flush()
    assert refresh_report_directory(session, job, now=now) == expected


def test_account_refresh_chunks_all_ids_and_does_not_duplicate_pending_work(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed, count=101)
    assert refresh_report_directory(session, job) == 6
    # 同一轮覆盖全部101个对象；每个候选 ID 均已在途，不重复创建运行。
    assert refresh_report_directory(session, job) == 0
    runs = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id, AdDirectoryRun.kind == "ad"
        )
    ).all()
    assert len(runs) == 2
    assert sum(len(run.query["ids"]) for run in runs) == 101


@pytest.mark.parametrize("stale_kind", ["ad", "adgroup", "campaign"])
def test_only_expired_layer_is_refreshed(session, sync_seed, monkeypatch, stale_kind):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    for kind, remote in [("ad", "a0"), ("adgroup", "g0"), ("campaign", "c0")]:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                kind=kind,
                remote_id=remote,
                ad_type="SMART_PLUS",
                observed_at=now - timedelta(hours=2 if kind == stale_kind else 0),
                published_version=1,
            )
        )
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 1
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert [run.kind for run in runs] == [stale_kind]


def test_missing_parents_are_chunked_together_and_pending_layer_is_skipped(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed, count=101)
    for x in range(101):
        for kind, remote in [("ad", f"a{x}"), ("adgroup", f"g{x}")]:
            session.add(
                AdObject(
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id="acc-2",
                    kind=kind,
                    remote_id=remote,
                    ad_type="SMART_PLUS",
                    observed_at=now,
                    published_version=1,
                )
            )
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 2
    assert refresh_report_directory(session, job, now=now) == 0
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert all(run.kind == "campaign" for run in runs)
    assert sum(len(run.query["ids"]) for run in runs) == 101


def directory_objects(session, seed, now, *, count=1, ages=None):
    ages = ages or {}
    for index in range(count):
        for kind, prefix in (("ad", "a"), ("adgroup", "g"), ("campaign", "c")):
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-2",
                    kind=kind,
                    remote_id=f"{prefix}{index}",
                    ad_type="SMART_PLUS",
                    published_version=1,
                    observed_at=now - timedelta(minutes=ages.get((kind, index), 0)),
                )
            )
    session.flush()


def runs_for(session, seed):
    return session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
    ).all()


def test_expiry_only_refreshes_due_objects_across_job_snapshots(
    session, sync_seed, monkeypatch
):
    from uuid import uuid4

    from app.modules.reporting.store_models import AdReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC).replace(hour=12)
    job = prepared(session, sync_seed, count=4)
    directory_objects(session, sync_seed, now, count=4, ages={("ad", 0): 26})
    facts = {
        row.ad_id: row
        for row in session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == sync_seed.tenant_id)
        ).all()
    }
    # 另一任务写入的新鲜广告不随到期邻居重读；零消耗与历史行也不能混入。
    facts["a1"].sync_job_id = uuid4()
    facts["a2"].spend = 0
    facts["a3"].stat_date -= timedelta(days=1)
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 1
    run = runs_for(session, sync_seed)[0]
    assert run.kind == "ad" and run.query["ids"] == ["a0"]


def test_missing_object_is_read_immediately_without_refreshing_fresh_neighbors(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed, count=2)
    directory_objects(session, sync_seed, now, count=2)
    missing = session.get(AdObject, (sync_seed.tenant_id, "acc-2", "adgroup", "g0"))
    session.delete(missing)
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 1
    run = runs_for(session, sync_seed)[0]
    assert run.kind == "adgroup" and run.query["ids"] == ["g0"]


@pytest.mark.parametrize(
    "age,rule,expected",
    [
        (22, False, 0),
        (22.5, False, 1),
        (52, False, 1),
        (52.5, False, 3),
        (7, True, 0),
        (7.5, True, 3),
    ],
)
def test_layer_refresh_intervals_include_half_hot_interval_margin(
    session, sync_seed, monkeypatch, age, rule, expected
):
    from app.modules.automation_rules.models import AutomationRule

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    directory_objects(
        session,
        sync_seed,
        now,
        ages={(kind, 0): age for kind in ("ad", "adgroup", "campaign")},
    )
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="interval-rule",
            enabled=rule,
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={"accounts": {"mode": "ALL"}},
        )
    )
    session.flush()
    assert refresh_report_directory(session, job, now=now) == expected


@pytest.mark.parametrize(
    "status", ["QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE"]
)
def test_live_run_for_other_ids_does_not_block_missing_objects(
    session, sync_seed, monkeypatch, status
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    for run in runs_for(session, sync_seed):
        run.query = run.query | {"ids": ["different-object"]}
        run.status = status
    session.flush()
    assert refresh_report_directory(session, job) == 3


@pytest.mark.parametrize(
    "difference",
    [
        "bc",
        "actor",
        "route",
        "account",
        "type",
        "layer",
        "workload",
        "complete",
        "tenant",
    ],
)
def test_inflight_dedup_does_not_cross_frozen_scope(
    session, sync_seed, monkeypatch, difference
):
    from uuid import uuid4

    from app.models import User

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    directory_objects(session, sync_seed, now, ages={("ad", 0): 31})
    assert refresh_report_directory(session, job, now=now) == 1
    run = runs_for(session, sync_seed)[0]
    if difference == "bc":
        run.bc_id = "bc-b"
        run.frozen_route = run.frozen_route | {"bc_id": "bc-b"}
    elif difference == "actor":
        actor = User(id=uuid4(), username=f"pending-{uuid4().hex}", hashed_password="x")
        session.add(actor)
        session.flush()
        run.actor_id = actor.id
    elif difference == "route":
        run.frozen_route = run.frozen_route | {"binding_revision": 1}
    elif difference == "account":
        run.advertiser_id = "acc-1"
    elif difference == "type":
        run.ad_type = "REGULAR"
    elif difference == "layer":
        run.kind = "campaign"
    elif difference == "workload":
        run.workload = "background"
    elif difference == "complete":
        run.status = "COMPLETE"
    elif difference == "tenant":
        other = globals()["sync_seed"].__wrapped__(session)
        run.tenant_id, run.actor_id, run.connection_id = (
            other.tenant_id,
            other.actor_id,
            other.connection.id,
        )
        run.frozen_route = _route(other)
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 1


def test_completed_refresh_waits_until_next_account_cycle(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC).replace(hour=12)
    job = prepared(session, sync_seed, count=2)
    directory_objects(
        session,
        sync_seed,
        now,
        count=2,
        ages={
            (kind, i): 61 for kind in ("ad", "adgroup", "campaign") for i in range(2)
        },
    )
    assert refresh_report_directory(session, job, now=now) == 3
    for run in runs_for(session, sync_seed):
        run.status = "COMPLETE"
    for obj in session.exec(
        select(AdObject).where(AdObject.tenant_id == sync_seed.tenant_id)
    ).all():
        obj.observed_at = now
    session.flush()
    assert refresh_report_directory(session, job, now=now + timedelta(minutes=15)) == 0
    assert refresh_report_directory(session, job, now=now + timedelta(minutes=30)) == 1
    ads = [run for run in runs_for(session, sync_seed) if run.kind == "ad"]
    assert all(run.query["ids"] == ["a0", "a1"] for run in ads)
    assert len(ads) == 2


def test_concurrent_reports_plan_only_one_chunked_account_round(
    committed_sync_seed, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        job = prepared(session, seed, count=101)
        job_id = job.id
    barrier = Barrier(2)

    def plan():
        with factory() as session, session.begin():
            job = session.get(ReportSyncJob, job_id)
            barrier.wait(timeout=5)
            return refresh_report_directory(session, job, now=now)

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(plan) for _ in range(2)]
        assert sorted(f.result(timeout=15) for f in futures) == [0, 6]
    with factory() as session:
        runs = runs_for(session, seed)
        assert len(runs) == 6
        assert all(1 <= len(run.query["ids"]) <= 100 for run in runs)


def test_expiry_does_not_refresh_fresh_objects_of_other_ad_types(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(
        session,
        sync_seed,
        count=3,
        types=["UPGRADED_SMART_PLUS", "MANUAL", "UPGRADED_SMART_PLUS"],
    )
    directory_objects(session, sync_seed, now, count=3, ages={("ad", 0): 26})
    assert refresh_report_directory(session, job, now=now) == 1
    runs = runs_for(session, sync_seed)
    assert {r.ad_type: r.query["ids"] for r in runs} == {"SMART_PLUS": ["a0"]}
    assert all(r.kind == "ad" for r in runs)


@pytest.mark.parametrize(
    "hour,timezone,offset", [(20, "Pacific/Kiritimati", 1), (2, "Etc/GMT+12", -1)]
)
def test_refresh_uses_account_local_today_and_job_account_boundary(
    session, sync_seed, monkeypatch, hour, timezone, offset
):
    from app.modules.reporting.store_models import AdReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC).replace(hour=hour)
    job = prepared(session, sync_seed, count=2)
    facts = session.exec(
        select(AdReportDay)
        .where(AdReportDay.tenant_id == sync_seed.tenant_id)
        .order_by(AdReportDay.ad_id)
    ).all()
    facts[0].timezone = timezone
    facts[0].stat_date = now.date() + timedelta(days=offset)
    facts[1].stat_date = now.date() - timedelta(days=2)
    # 同租户另一账户也有当地今日消耗，不得借当前任务触发其目录。
    session.add(
        AdReportDay(
            **(facts[0].model_dump() | {"advertiser_id": "acc-1", "ad_id": "foreign"})
        )
    )
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 3
    assert {r.kind: r.query["ids"] for r in runs_for(session, sync_seed)} == {
        "ad": ["a0"],
        "adgroup": ["g0"],
        "campaign": ["c0"],
    }


@pytest.mark.parametrize(
    "kind,ids,parents,remaining",
    [
        ("ad", ["a0"], [], ["a1"]),
        ("adgroup", ["g0"], [], ["g1"]),
        ("campaign", ["c0"], [], ["c1"]),
        ("ad", [], ["g0"], ["a1"]),
        ("ad", [], ["c0"], ["a0", "a1"]),
        ("adgroup", [], ["c0"], ["g1"]),
        ("campaign", [], ["c0"], ["c0", "c1"]),
        ("ad", ["a0", "a1"], ["g0"], ["a1"]),
        ("ad", ["a0"], ["g1"], ["a0", "a1"]),
        ("ad", [], [], []),
    ],
)
@pytest.mark.parametrize("include_deleted", [False, True])
def test_pending_query_only_covers_matching_ids_and_direct_parents(
    session, sync_seed, monkeypatch, kind, ids, parents, remaining, include_deleted
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed, count=2)
    directory_objects(
        session,
        sync_seed,
        now,
        count=2,
        ages={(kind, index): 61 for index in range(2)},
    )
    assert refresh_report_directory(session, job, now=now) == 1
    pending = runs_for(session, sync_seed)[0]
    pending.query = pending.query | {
        "ids": ids,
        "parent_ids": parents,
        "include_deleted": include_deleted,
    }
    session.flush()
    assert refresh_report_directory(session, job, now=now) == bool(remaining)
    fresh = [run for run in runs_for(session, sync_seed) if run.id != pending.id]
    assert [run.query["ids"] for run in fresh] == ([remaining] if remaining else [])


def test_material_child_scope_does_not_cover_generic_directory_refresh(
    session, sync_seed, monkeypatch
):
    from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    directory_objects(session, sync_seed, now, ages={("ad", 0): 31})
    assert refresh_report_directory(session, job, now=now) == 1
    pending = runs_for(session, sync_seed)[0]
    pending.query = pending.query | {MATERIAL_CHILD_KEY: {"source_run_id": "other"}}
    session.flush()
    assert refresh_report_directory(session, job, now=now) == 1
