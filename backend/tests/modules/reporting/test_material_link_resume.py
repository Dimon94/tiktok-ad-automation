"""真实 PG 验证素材关联每个外部请求之后可重启续步。"""

from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_jobs import _route
from tests.modules.reporting.test_material_query import D, material_row, publish


def seed_job(factory, seed):
    with factory() as session:
        publish(session, seed, "acc-1", [material_row("old-post", "5", "same.mp4")])
        session.add_all(
            [
                AdMaterialReference(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    ad_remote_id=f"ad{i:03}",
                    platform_material_id=f"video{i}",
                    ad_material_id=f"material{i}",
                    material_type="VIDEO",
                    name="same.mp4",
                    complete=True,
                    published_version=1,
                )
                for i in range(200)
            ]
        )
        job = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="material_links",
            tier="material",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
        return job


def step(factory, job_id, reader):
    from tests.modules.reporting.test_material_resume import claim

    job = claim(factory, job_id)
    reader.calls = 0
    return execute_job(
        factory, job_id=job_id, claim_token=job.claim_token, reader=reader
    )


class SlowReader:
    def __init__(self):
        self.calls = 0
        self.history = []
        self.error = None

    def tick(self, query):
        self.calls += 1
        self.history.append(query)
        if self.calls * 20 > 170:
            raise DomainError("read_deadline_exceeded", "虚拟时钟已超过170秒")
        if self.error is not None:
            raise self.error

    def material_ad_links(self, *, advertiser_id, page, ad_ids=()):
        self.tick(("overview", page))
        return RawReportPage((), page, 1, 0)

    def material_ad_posts(self, *, advertiser_id, ad_ids, start, end, page):
        assert advertiser_id == "acc-1" and start == end == D
        self.tick((tuple(ad_ids), page))
        mid = "old-post" if page == 2 and "ad199" in ad_ids else f"unrelated-{page}"
        return RawReportPage(
            (
                {
                    "dimensions": {
                        "main_material_id": mid,
                        "main_material_type": "VIDEO_SPARK_ADS",
                    }
                },
            ),
            page,
            2,
            2,
        )


def test_slow_binary_search_and_pages_commit_across_worker_steps(committed_sync_seed):
    seed, factory = committed_sync_seed
    job_id, reader = seed_job(factory, seed), SlowReader()
    for _ in range(60):
        result = step(factory, job_id, reader)
        assert reader.calls <= 1, "一步只能发送一次请求，慢响应不能抹掉此前进度"
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            assert job.attempts == 0
            if job.error_code == "material_directory_pending":
                assert result == "WAITING"
                break
            assert job.error_code is None
    else:
        raise AssertionError("持久化定位不能完成")
    assert reader.history.count(("overview", 1)) == 1
    assert len(reader.history) == len(set(reader.history))
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert (link.ad_remote_id, link.platform_material_id) == ("ad199", None)


@pytest.mark.parametrize(
    "error_code,status",
    [("tiktok_response_error", "WAITING"), ("report_response_invalid", "FAILED")],
)
def test_completed_overview_and_query_page_survive_later_provider_error(
    committed_sync_seed,
    error_code,
    status,
):
    seed, factory = committed_sync_seed
    job_id, reader = seed_job(factory, seed), SlowReader()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.history == [("overview", 1)]
    assert step(factory, job_id, reader) == "WAITING"
    with factory() as session:
        saved = session.get(ReportSyncJob, job_id).progress
        assert saved["overview_complete"]
    reader.error = DomainError(error_code, "平台错误必须保留")
    assert step(factory, job_id, reader) == status
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.progress == saved and job.error_code == error_code
        assert job.status == status
    if status == "FAILED":
        return  # 终态不能由测试手改租约复活；只有暂时错误才续同一查询。
    reader.error = None
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.history[-1][1] == 2
    assert reader.history.count(("overview", 1)) == 1


def test_overview_filter_uses_report_window_and_freezes_across_pages(
    committed_sync_seed,
):
    from datetime import timedelta

    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    seed, factory = committed_sync_seed
    job_id = seed_job(factory, seed)
    with factory() as session:
        for adv, day, ads in [
            ("acc-1", D, ["ad000", "ad199"]),
            ("acc-1", D - timedelta(days=1), ["history-ad"]),
            ("acc-2", D, ["other-account-ad"]),
        ]:
            publish_ad_day(
                session,
                tenant_id=seed.tenant_id,
                job_id=uuid4(),
                accounts={adv: AccountMeta("USD", "Asia/Hong_Kong")},
                start=day,
                end=day,
                rows=[ad_row(ad, day, adv=adv, spend="0") for ad in ads],
            )
        job = session.get(ReportSyncJob, job_id)
        job.progress = {"next_page": 9}  # 旧的未筛选页不能接到新的筛选查询之后。
        session.commit()

    class Reader(SlowReader):
        def material_ad_links(self, *, advertiser_id, page, ad_ids=()):
            self.history.append((page, ad_ids))
            row = {
                "dimensions": {
                    "smart_plus_ad_id": "ad000",
                    "main_material_id": f"known-post-{page}",
                    "main_material_type": "VIDEO_SPARK_ADS",
                },
                "metrics": {"ad_material_id": "material0"},
            }
            return RawReportPage((row,), page, 2, 2)

    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.history == [(1, ("ad000", "ad199"))]
    with factory() as session:
        publish_ad_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
            start=D,
            end=D,
            rows=[ad_row("new-ad", D, adv="acc-1")],
        )
        session.commit()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.history[-1] == (2, ("ad000", "ad199"))


def test_wide_or_unknown_ad_scope_keeps_existing_full_scan_progress(
    committed_sync_seed,
):
    from app.modules.reporting.jobs import _material_link_overview_ads
    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    seed, factory = committed_sync_seed
    job_id = seed_job(factory, seed)
    for count in (0, 201):
        with factory() as session:
            if count:
                publish_ad_day(
                    session,
                    tenant_id=seed.tenant_id,
                    job_id=uuid4(),
                    accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
                    start=D,
                    end=D,
                    rows=[ad_row(f"ad{x:03}", D, adv="acc-1") for x in range(count)],
                )
            job = session.get(ReportSyncJob, job_id)
            job.progress = {"next_page": 9}
            assert _material_link_overview_ads(session, job) == ()
            assert job.progress["next_page"] == 9
