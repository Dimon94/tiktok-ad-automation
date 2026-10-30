"""周期目录以账户身份推进，失败隔离且不丢失未完成账户。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.config import settings
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.store_models import AdSyncBc


def _case(session, seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_ACCOUNTS", 1)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids = ["acc-1", "acc-2"]
    plan.screening_progress = {
        "rule_sync_original_phase": "keep",
        "active_screen": {"cursor": "keep"},
    }
    return now, plan


def _advertisers(session, plan):
    return set(
        session.exec(
            select(AdDirectoryRun.advertiser_id).where(
                AdDirectoryRun.tenant_id == plan.tenant_id
            )
        ).all()
    )


def test_directory_cursor_survives_due_list_shrinking_and_preserves_other_keys(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed, monkeypatch)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1", "acc-2"], now=now)
    assert _advertisers(session, plan) == {"acc-1"}
    assert (
        plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]["after"] == "acc-1"
    )
    assert plan.next_directory_at < now + planner.DIRECTORY_INTERVAL
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=5)
    )
    assert _advertisers(session, plan) == {"acc-1", "acc-2"}
    assert job_tasks.DIRECTORY_PLAN_PROGRESS not in plan.screening_progress
    assert plan.screening_progress == {
        "rule_sync_original_phase": "keep",
        "active_screen": {"cursor": "keep"},
    }
    assert plan.next_directory_at == now + planner.DIRECTORY_INTERVAL


def test_obsolete_minute_gate_is_removed_without_resetting_cursor(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed, monkeypatch)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1", "acc-2"], now=now)
    plan.screening_progress = {
        **plan.screening_progress,
        "directory_batch_not_before_v1": (now + timedelta(minutes=1)).isoformat(),
    }
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=5)
    )
    assert _advertisers(session, plan) == {"acc-1", "acc-2"}
    assert plan.screening_progress == {
        "rule_sync_original_phase": "keep",
        "active_screen": {"cursor": "keep"},
    }
    assert plan.next_directory_at == now + planner.DIRECTORY_INTERVAL


def test_directory_progress_rolls_back_with_batch_transaction(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed, monkeypatch)
    with pytest.raises(RuntimeError, match="rollback"), session.begin_nested():
        job_tasks._plan_directory_step(
            session, plan, accounts=["acc-1", "acc-2"], now=now
        )
        session.flush()
        raise RuntimeError("rollback")
    assert not _advertisers(session, plan)
    assert job_tasks.DIRECTORY_PLAN_PROGRESS not in plan.screening_progress
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1", "acc-2"], now=now)
    assert _advertisers(session, plan) == {"acc-1"}


def test_new_account_before_cursor_restarts_cycle(session, sync_seed, monkeypatch):
    now, plan = _case(session, sync_seed, monkeypatch)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-2", "acc-3"], now=now)
    assert _advertisers(session, plan) == {"acc-2"}
    job_tasks._plan_directory_step(
        session,
        plan,
        accounts=["acc-1", "acc-2", "acc-3"],
        now=now + timedelta(seconds=5),
    )
    assert _advertisers(session, plan) == {"acc-1", "acc-2"}
    assert (
        plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]["after"] == "acc-1"
    )


def test_directory_frozen_scope_change_restarts_account_cursor(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed, monkeypatch)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1", "acc-2"], now=now)
    old_scope = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]["scope"]
    binding = session.get(
        BCConnectionBinding, (plan.tenant_id, plan.bc_id, sync_seed.connection.id)
    )
    binding.revision += 1
    plan.frozen_route = plan.frozen_route | {"binding_revision": binding.revision}
    session.flush()
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=5)
    )
    state = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]
    assert state["scope"] != old_scope and state["after"] == "acc-1"
    assert _advertisers(session, plan) == {"acc-1"}
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == plan.tenant_id)
    ).all()
    assert {run.frozen_route["binding_revision"] for run in runs} == {0, 1}


def test_slow_account_is_retained_and_does_not_block_other_accounts(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed, monkeypatch)
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_ACCOUNTS", 2)
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_SECONDS", 0.5)
    original = job_tasks.directory_ad_types

    def slow(session, **kwargs):
        if "acc-1" in kwargs["advertiser_ids"]:
            session.execute(text("SELECT pg_sleep(1)"))
        return original(session, **kwargs)

    monkeypatch.setattr(job_tasks, "directory_ad_types", slow)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1", "acc-2"], now=now)
    state = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]
    assert (
        state["batch_size"] == 2
        and state["after"] == ""
        and not _advertisers(session, plan)
    )
    assert datetime.fromisoformat(state["deferred"]["acc-1"]) == now + timedelta(
        minutes=5
    )
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=5)
    )
    assert _advertisers(session, plan) == {"acc-2"}
    assert (
        "acc-1"
        in plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]["deferred"]
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_SECONDS", 3)
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=10)
    )
    assert _advertisers(session, plan) == {"acc-2"}
    assert job_tasks.DIRECTORY_PLAN_PROGRESS in plan.screening_progress
    assert plan.next_directory_at < now + planner.DIRECTORY_INTERVAL
    monkeypatch.setattr(job_tasks, "directory_ad_types", original)
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(minutes=6)
    )
    assert _advertisers(session, plan) == {"acc-1", "acc-2"}
    assert job_tasks.DIRECTORY_PLAN_PROGRESS not in plan.screening_progress


def test_full_64_account_batch_enqueues_all_types_within_budget(
    session, sync_seed, monkeypatch
):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.ads.models import AdObject

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_ACCOUNTS", 64)
    accounts = [f"batch-{index:02}" for index in range(64)]
    for account in accounts:
        session.add(
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=account,
                timezone="UTC",
                currency="USD",
            )
        )
    session.flush()
    for account in accounts:
        session.add(
            BCAccountAccess(
                tenant_id=sync_seed.tenant_id,
                bc_id="bc-a",
                advertiser_id=account,
                connection_id=sync_seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=now,
            )
        )
        for ad_type in ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS"):
            session.add(
                AdObject(
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id=account,
                    kind="ad",
                    remote_id=ad_type,
                    ad_type=ad_type,
                    observed_at=now,
                    published_version=1,
                )
            )
    session.flush()
    plan.last_active_advertiser_ids = accounts
    # scope/requirement登记加入后，每次仍遵守原预算；64户必须经有限续点全部完成，
    # 不能把“单入口完成”当作业务合同，也不能放宽每入口SQL/墙钟期限。
    prefixes = []
    for turn in range(8):
        job_tasks._plan_directory_step(
            session, plan, accounts=accounts, now=now + timedelta(seconds=turn)
        )
        prefixes.append(len(_advertisers(session, plan)))
        if job_tasks.DIRECTORY_PLAN_PROGRESS not in plan.screening_progress:
            break
    assert prefixes == sorted(prefixes) and all(count > 0 for count in prefixes)
    assert job_tasks.DIRECTORY_PLAN_PROGRESS not in plan.screening_progress
    assert _advertisers(session, plan) == set(accounts)
    runs = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == plan.tenant_id,
        )
    ).all()
    assert len(runs) == 64 * 9
    assert plan.next_directory_at == now + planner.DIRECTORY_INTERVAL
