from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds import previews
from app.modules.materials.models import AccountMaterial
from tests.acceptance.scenario import SCALE


# 完整搭建只跑 wangyan：提交只读取冻结预览，与版权方无关；jiashu（默认版权方）的真实
# 准备与冻结由 test_preparation_and_freeze_are_real_and_have_no_tiktok_writes 覆盖且断言更严。
@pytest.mark.parametrize("acceptance_scenario", ["wangyan"], indirect=True)
def test_real_preparation_freeze_submission_and_official_sdk(acceptance_scenario):
    scenario = acceptance_scenario
    scenario.prepare()
    preview_id = scenario.freeze()
    with Session(scenario.database_engine) as session:
        preview = previews.get_preview_summary(
            session, context=scenario.scope.context, preview_id=preview_id
        )
        assert preview.status == "FROZEN"
        assert (preview.campaign_count, preview.adgroup_count, preview.ad_count) == (
            SCALE.campaigns,
            SCALE.adgroups,
            SCALE.ads,
        )
        assert preview.daily_budget_sum == Decimal(SCALE.daily_budget)
        assert all(
            call["method"] == "GET" for call in scenario.runtime.wire.smart.calls
        )
    scenario.submit()
    scenario.runtime.drive_until(
        lambda: scenario.view().status not in {"QUEUED", "RUNNING"},
        # 满规模 138 份目标素材及源封面共享保留真实调度/等待间隔，给整批充分完成时间。
        timeout=360,
    )
    view = scenario.view()
    assert view.succeeded.model_dump() == {
        "campaign_count": SCALE.campaigns,
        "adgroup_count": SCALE.adgroups,
        "ad_count": SCALE.ads,
    }, scenario.runtime.diagnostics()
    assert Decimal(view.daily_budget_sum) == Decimal(SCALE.daily_budget)
    wire = scenario.runtime.wire
    posts = [call for call in wire.smart.calls if call["method"] == "POST"]
    assert len(posts) == SCALE.posts
    assert all(
        row["operation_status"] == "ENABLE"
        for rows in wire.smart.store.values()
        for row in rows.values()
    )
    # 来源视频（满规模 46 个）原生共享到全部目标账户；封面各上传一次后共享，避免重复上传。
    assert wire.calls["/open_api/v1.3/file/video/ad/upload/"] == 0
    assert wire.calls["/open_api/v1.3/file/image/ad/upload/"] == SCALE.source_videos
    assert wire.calls["/open_api/v1.3/creative/asset/share/"] > 0
    assert len(wire.videos) == SCALE.target_videos
    assert wire.calls["/open_api/v1.3/smart_plus/adgroup/create/"] == SCALE.adgroups
    with Session(scenario.database_engine) as session:
        targets = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == scenario.scope.context.tenant_id,
                AccountMaterial.advertiser_id.in_(scenario.scope.accounts),
            )
        ).all()
        assert len(targets) == SCALE.target_videos
        assert all(
            row.video_id.startswith("target-vid-")
            and row.mid.startswith("source-mid-")
            and row.image_id
            for row in targets
        )


def test_preparation_and_freeze_are_real_and_have_no_tiktok_writes(acceptance_scenario):
    scenario = acceptance_scenario
    scenario.prepare()
    preview_id = scenario.freeze()
    with Session(scenario.database_engine) as session:
        summary = previews.get_preview_summary(
            session, context=scenario.scope.context, preview_id=preview_id
        )
        assert summary.status == "FROZEN"
        assert (summary.campaign_count, summary.adgroup_count, summary.ad_count) == (
            SCALE.campaigns,
            SCALE.adgroups,
            SCALE.ads,
        )
        assert summary.daily_budget_sum == Decimal(SCALE.daily_budget)
        assert summary.blocked_count == 0
        assert summary.preparing_count == SCALE.campaigns
        assert (
            session.exec(
                select(AccountMaterial).where(
                    AccountMaterial.tenant_id == scenario.scope.context.tenant_id,
                    AccountMaterial.advertiser_id.in_(scenario.scope.accounts),
                )
            ).all()
            == []
        )
    from collections import defaultdict

    from app.modules.builds.preview_models import BuildUnit, PlannedAd, PlannedGroup
    from app.modules.materials.models import MaterialUploadAttempt

    with Session(scenario.database_engine) as session:
        rows = session.exec(
            select(BuildUnit, PlannedGroup, PlannedAd)
            .join(PlannedGroup, PlannedGroup.unit_id == BuildUnit.id)
            .join(PlannedAd, PlannedAd.group_id == PlannedGroup.id)
            .where(BuildUnit.preview_id == preview_id)
        ).all()
        copies = defaultdict(set)
        for unit, group, ad in rows:
            copies[(unit.drama_id, group.group_no, ad.creative_no)].add(
                (ad.copy_id, ad.text)
            )
        assert len(copies) == SCALE.copy_slots and all(
            len(values) == 1 for values in copies.values()
        )
        history = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.tenant_id == scenario.scope.context.tenant_id
            )
        ).all()
        assert len(history) == SCALE.source_videos
        assert {row.advertiser_id for row in history} == set(scenario.scope.sources)
        assert all(
            row.status == "succeeded"
            and row.connection_id == scenario.scope.connection_id
            for row in history
        )
    wire = scenario.runtime.wire
    for endpoint in [
        "identity/get",
        "minis/get",
        "creative/cta/recommend",
        "tool/vbo_status",
        "tool/region",
    ]:
        assert wire.calls[f"/open_api/v1.3/{endpoint}/"] == SCALE.target_accounts
    assert wire.calls["/open_api/v1.3/bc/asset/get/"] == 1
    assert all(call["method"] == "GET" for call in wire.smart.calls)
    assert not wire.videos and not wire.portfolios


@pytest.mark.parametrize("variant", ["currency", "minis", "multi_drama"])
def test_partial_input_and_material_matching_use_actual_preparation(
    acceptance_scenario, variant
):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.builds.preview_models import (
        PreviewCreativeUnitItem,
        PreviewDrama,
    )
    from app.modules.materials.models import MaterialFile

    scenario = acceptance_scenario
    excluded = SCALE.excluded_by_currency()
    if variant == "currency":
        with Session(scenario.database_engine) as session, session.begin():
            row = session.get(
                AdvertiserAccount,
                (scenario.scope.context.tenant_id, scenario.scope.accounts[0]),
            )
            row.currency = "EUR"
            session.add(row)
    elif variant == "minis":
        scenario.runtime.wire.minis_unavailable_accounts.add(scenario.scope.accounts[0])
    else:
        with Session(scenario.database_engine) as session, session.begin():
            row = session.get(MaterialFile, scenario.scope.material_ids[0])
            row.file_name = "The Bond Hidden Promise.mp4"
            session.add(row)
    scenario.prepare()
    if variant == "minis":
        # 当前共同地区规则要求全部账户具备已核实的 Minis 地区，缺失时整批拒绝冻结。
        with pytest.raises(DomainError) as error:
            scenario.freeze()
        assert error.value.code == "targeting_regions_unverified"
        assert scenario.preview_id is None and scenario.submission_id is None
        assert all(
            call["method"] == "GET" for call in scenario.runtime.wire.smart.calls
        )
        assert not scenario.runtime.wire.videos
        return
    preview_id = scenario.freeze()
    with Session(scenario.database_engine) as session:
        summary = previews.get_preview_summary(
            session, context=scenario.scope.context, preview_id=preview_id
        )
        if variant == "currency":
            assert (
                summary.campaign_count,
                summary.adgroup_count,
                summary.ad_count,
            ) == (
                SCALE.campaigns - excluded["campaign_count"],
                SCALE.adgroups - excluded["adgroup_count"],
                SCALE.ads - excluded["ad_count"],
            )
            assert summary.daily_budget_sum == Decimal(
                SCALE.daily_budget - SCALE.budget_per_campaign * SCALE.dramas
            )
        else:
            assert summary.campaign_count == SCALE.campaigns
            assert summary.input_issue_count == 0
            rows = session.exec(
                select(PreviewCreativeUnitItem, PreviewDrama)
                .join(
                    PreviewDrama,
                    (PreviewDrama.preview_id == PreviewCreativeUnitItem.preview_id)
                    & (PreviewDrama.drama_id == PreviewCreativeUnitItem.drama_id),
                )
                .where(
                    PreviewCreativeUnitItem.tenant_id
                    == scenario.scope.context.tenant_id,
                    PreviewDrama.tenant_id == scenario.scope.context.tenant_id,
                    PreviewCreativeUnitItem.preview_id == preview_id,
                    PreviewCreativeUnitItem.material_id
                    == scenario.scope.material_ids[0],
                )
            ).all()
            assert {drama.title for _, drama in rows} == {"The Bond", "Hidden Promise"}
    scenario.submit()
    submitted = scenario.view()
    if variant == "currency":
        assert submitted.submitted.model_dump() == {
            "campaign_count": SCALE.campaigns - excluded["campaign_count"],
            "adgroup_count": SCALE.adgroups - excluded["adgroup_count"],
            "ad_count": SCALE.ads - excluded["ad_count"],
        }
        assert Decimal(submitted.daily_budget_sum) == Decimal(
            SCALE.daily_budget - SCALE.budget_per_campaign * SCALE.dramas
        )
        assert submitted.excluded_unit_count == excluded["campaign_count"]
        assert submitted.excluded.model_dump() == excluded
        for name, planned_count in submitted.planned.model_dump().items():
            assert planned_count == getattr(submitted.submitted, name) + getattr(
                submitted.excluded, name
            )
    else:
        assert submitted.submitted.campaign_count == SCALE.campaigns
    assert all(call["method"] == "GET" for call in scenario.runtime.wire.smart.calls)
    assert not scenario.runtime.wire.videos


@pytest.mark.parametrize("acceptance_scenario", ["wangyan"], indirect=True)
def test_wangyan_lost_create_reply_recovers_full_batch_without_repost(
    acceptance_scenario,
):
    wire = acceptance_scenario.runtime.wire
    wire.wangyan_lose_reply = True
    test_real_preparation_freeze_submission_and_official_sdk(acceptance_scenario)
    assert wire.calls["provider:/api/distribute_admin/promote/link/create"] == 2
    assert sum(len(rows) for rows in wire.wangyan_links.values()) == 2
    from app.modules.providers.models import PromotionLink, ProviderEffect

    with Session(acceptance_scenario.database_engine) as session:
        links = session.exec(select(PromotionLink)).all()
        assert len(links) == 2 and all(link.status == "ready" for link in links)
        assert all(link.protected_base.startswith("{b7") for link in links)
        assert all(
            link.protected_base.endswith(("-The Bond", "-Hidden Promise"))
            for link in links
        )
        creates = session.exec(
            select(ProviderEffect).where(ProviderEffect.step == "create")
        ).all()
        assert len(creates) == 2 and all(
            effect.status == "succeeded" for effect in creates
        )


@pytest.mark.parametrize("acceptance_scenario", ["wangyan"], indirect=True)
def test_wangyan_ambiguous_create_recovery_blocks_without_second_post(
    acceptance_scenario,
):
    from app.modules.providers.models import LinkPreparationItem

    scenario = acceptance_scenario
    wire = scenario.runtime.wire
    wire.wangyan_lose_reply = True
    wire.wangyan_duplicate_created = True
    scenario.prepare()

    def scanned():
        with Session(scenario.database_engine) as session:
            items = session.exec(
                select(LinkPreparationItem).where(
                    LinkPreparationItem.tenant_id == scenario.scope.context.tenant_id
                )
            ).all()
            return len(items) == 2 and all(
                item.resolved.get("_work", {}).get("wy_scan", {}).get("done")
                for item in items
            )

    scenario.runtime.drive_until(scanned)
    assert wire.calls["provider:/api/distribute_admin/promote/link/create"] == 2
    assert sum(len(rows) for rows in wire.wangyan_links.values()) == 4
    with Session(scenario.database_engine) as session:
        items = session.exec(
            select(LinkPreparationItem).where(
                LinkPreparationItem.tenant_id == scenario.scope.context.tenant_id
            )
        ).all()
        assert all(item.status == "result_unknown" for item in items)
        assert all(item.resolved["_work"]["stage"] == "manual_review" for item in items)
    assert all(call["method"] == "GET" for call in wire.smart.calls) and not wire.videos


def test_runtime_executes_production_control_and_yields_bounded_chunks(
    acceptance_scenario,
):
    from uuid import uuid4

    from app.jobs.outbox import enqueue_after_commit

    scenario = acceptance_scenario
    with Session(scenario.database_engine) as session, session.begin():
        identities = [
            enqueue_after_commit(
                session,
                context=scenario.scope.context,
                task_name="jobs.probe",
                task_key="acceptance-probe-" + str(uuid4()),
                payload={},
            )
            for _ in range(12)
        ]
    runtime = scenario.runtime
    assert runtime.step_job()
    # The registered control drain publishes several fair rounds in one call.
    assert runtime.delivered[0]["name"] == "jobs.flush_dispatch"
    assert len(runtime.messages) == 11
    assert runtime.pump_jobs(max_steps=3) == 3
    assert len(runtime.messages) == 8
    runtime.drive_until(lambda: not runtime.messages)
    deliveries = [
        message for message in runtime.delivered if message["name"] == "jobs.probe"
    ]
    assert len(deliveries) == 12 and {
        message["task_id"] for message in deliveries
    } == set(map(str, identities))
    assert all(call["method"] == "GET" for call in runtime.wire.smart.calls)
