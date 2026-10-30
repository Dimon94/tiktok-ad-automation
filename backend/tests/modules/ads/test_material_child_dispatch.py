"""素材子运行沿用原请求年龄，仍遵守目录窗口与跨租户轮转。"""

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from inspect import unwrap
from types import SimpleNamespace

from sqlmodel import select

from app.core.config import settings
from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ads.material_directory_children import is_material_child
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.directory_demand import (
    directory_outcome,
    register_directory_run,
)
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_directory_children import _source_page
from tests.modules.reporting.directory_claiming import finish
from tests.modules.sync_control.conftest import (
    isolated_sync_quota as isolated_sync_quota,
)


def _child(session, seed, *, age, ad="ad-a"):
    source = _run(session, seed, kind="ad", ad_type="SMART_PLUS")
    source.created_at, source.workload = age, "background"
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(seed, 1, [(ad, "group")], terminal=True),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = next(
        run
        for run in session.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == source.tenant_id,
                AdDirectoryRun.kind == "creative",
            )
        ).all()
        if run.query.get("material_child_v1", {}).get("source_run_id") == str(source.id)
    )
    return source, child


def _finish_other_runs(session):
    for run in session.exec(select(AdDirectoryRun)).all():
        if not is_material_child(run):
            run.status = "COMPLETE"
            directory_outcome(session, run)
    session.flush()


def _sent(session):
    return session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.sync_step")
    ).all()


def test_child_keeps_source_age_within_canonical_due_order(
    session, directory_seed, monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        settings, "REDIS_URL", __import__("os").environ["TEST_REDIS_URL"]
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 1)
    _authority(session, directory_seed)
    now = datetime.now(UTC)
    source, child = _child(session, directory_seed, age=now - timedelta(hours=1))
    _finish_other_runs(session)
    earlier = _run(session, directory_seed, kind="campaign", ad_type="SMART_PLUS")
    younger = _run(session, directory_seed, kind="adgroup", ad_type="SMART_PLUS")
    legacy = _run(session, directory_seed, kind="creative", ad_type="SMART_PLUS")
    earlier.workload = younger.workload = legacy.workload = "background"
    earlier.created_at = now - timedelta(minutes=90)
    younger.created_at = now - timedelta(minutes=30)
    legacy.created_at = now - timedelta(hours=2)
    session.flush()
    for row in (earlier, younger, legacy):
        register_directory_run(session, row, new_execution=True)
    assert child.created_at == source.created_at
    # 创意不再是独立保底子队列；统一类按头年龄，子范围仍继承原请求年龄。
    for expected in (legacy, earlier, child, younger):
        before = {row.id for row in _sent(session)}
        assert (
            job_tasks._dispatch_directories(
                session, datetime.now(UTC) + timedelta(seconds=1)
            )
            == 1
        )
        sent = [row for row in _sent(session) if row.id not in before]
        assert [row.payload["run_id"] for row in sent] == [str(expected.id)]
        # 一个槽位未释放前不能继续发送。
        assert (
            job_tasks._dispatch_directories(
                session, datetime.now(UTC) + timedelta(seconds=1)
            )
            == 0
        )
        finish(session, redis_client, expected, now=datetime.now(UTC))


def test_child_age_does_not_override_cross_tenant_cursor(monkeypatch, redis_client):
    from tests.modules.reporting.sync_conftest import (
        committed_sync_seed as seed_fixture,
    )

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        settings, "REDIS_URL", __import__("os").environ["TEST_REDIS_URL"]
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 1)
    with ExitStack() as stack:
        seeds = [
            stack.enter_context(contextmanager(unwrap(seed_fixture))())
            for _ in range(2)
        ]
        factory, now = seeds[0][1], datetime.now(UTC)
        with factory() as session, session.begin():
            for rank, (seed, _) in enumerate(seeds):
                adapted = SimpleNamespace(
                    context=TenantContext(seed.tenant_id, seed.actor_id, "operator"),
                    ref=EntityRef(seed.tenant_id, "acc-1", "ad", "unused"),
                    bc_id="bc-a",
                    connection=seed.connection,
                )
                for index in range(2):
                    _child(
                        session,
                        adapted,
                        age=now - timedelta(hours=3 - rank, minutes=index),
                        ad=f"ad-{index}",
                    )
                session.add(
                    ReportDispatchCursor(
                        tenant_id=seed.tenant_id,
                        last_dispatched_at=now - timedelta(minutes=1 + rank),
                    )
                )
            _finish_other_runs(session)
        with factory() as session, session.begin():
            assert (
                job_tasks._dispatch_directories(
                    session, datetime.now(UTC) + timedelta(seconds=1)
                )
                == 1
            )
            messages = _sent(session)
            assert {row.tenant_id for row in messages} == {seeds[1][0].tenant_id}
            assert (
                job_tasks._dispatch_directories(
                    session, datetime.now(UTC) + timedelta(seconds=1)
                )
                == 0
            )
            from uuid import UUID

            finish(
                session,
                redis_client,
                session.get(AdDirectoryRun, UUID(messages[0].payload["run_id"])),
                now=datetime.now(UTC),
            )
        with factory() as session, session.begin():
            assert (
                job_tasks._dispatch_directories(
                    session, datetime.now(UTC) + timedelta(seconds=1)
                )
                == 1
            )
            assert {row.tenant_id for row in _sent(session)} == {
                seed.tenant_id for seed, _ in seeds
            }
            assert (
                job_tasks._dispatch_directories(
                    session, datetime.now(UTC) + timedelta(seconds=1)
                )
                == 0
            )
