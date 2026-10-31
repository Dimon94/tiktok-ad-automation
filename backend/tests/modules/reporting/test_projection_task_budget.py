"""轻量待更新桶不能因固定条数上限闲置已有工作预算。"""

from datetime import date, timedelta

from sqlalchemy import text

from app.modules.reporting.read_models.models import ReadBucketState
from app.modules.reporting.read_models.tasks import project_read_models


def test_projection_drains_more_than_twenty_five_small_buckets(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        for i in range(32):
            session.add(
                ReadBucketState(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    kind="material_day",
                    bucket=date(2026, 8, 1) + timedelta(days=i),
                    source_revision=1,
                    derived_revision=0,
                )
            )
    assert project_read_models.run() == 32
    with factory() as session:
        assert (
            session.execute(
                text("""SELECT count(*) FROM read_bucket_state
          WHERE tenant_id=:tenant AND source_revision>derived_revision"""),
                {"tenant": seed.tenant_id},
            ).scalar_one()
            == 0
        )


def test_projection_yields_on_time_and_keeps_committed_prefix(
    committed_sync_seed, monkeypatch
):
    from app.modules.reporting.read_models import tasks

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        for i in range(3):
            session.add(
                ReadBucketState(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    kind="material_day",
                    bucket=date(2026, 8, 1) + timedelta(days=i),
                    source_revision=1,
                    derived_revision=0,
                )
            )
    ticks = iter([0, 1, 3, 5])
    with monkeypatch.context() as clock:
        clock.setattr(tasks, "monotonic", lambda: next(ticks))
        assert project_read_models.run() == 2
    # 单桶真实提交，时间预算耗尽仍能在下一任务接着完成剩余桶。
    assert project_read_models.run() == 1
