from dataclasses import replace
from decimal import Decimal
from itertools import islice
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import VideoCreative
from app.modules.builds import previews
from app.modules.builds.drafts import create_draft, edit_material_groups, prepare_draft
from app.modules.builds.models import BuildDraft, DraftDrama, DraftInput
from app.modules.builds.preview_material_views import (
    AdMaterialView as PreviewAdMaterial,
)
from app.modules.builds.preview_material_views import (
    GroupMaterialView as PreviewGroupMaterial,
)
from app.modules.builds.preview_models import (
    BuildPreview,
    BuildUnit,
    PlannedAd,
    PlannedGroup,
)
from app.modules.builds.previews import iter_pairs, unit_readiness
from app.modules.builds.scene_schemas import SceneContext
from app.modules.providers.models import ProviderApplication
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.service import append_version
from tests.modules.builds.test_drafts import account, finish, ready_links
from tests.modules.builds.video_source import VIDEO_SOURCE
from tests.modules.materials.test_tenant_materials import material
from tests.modules.strategies.test_versions import config


def test_preview_product_is_lazy_and_covers_same_accounts_for_every_drama():
    pairs = iter_pairs(range(100_000), lambda: iter(("a", "b", "c")))
    assert list(islice(pairs, 7)) == [
        (0, "a"),
        (0, "b"),
        (0, "c"),
        (1, "a"),
        (1, "b"),
        (1, "c"),
        (2, "a"),
    ]
    assert len(list(iter_pairs(range(2), lambda: iter(("a", "b", "c"))))) == 6


def test_preview_readiness_requires_scene_currency_and_every_material_path():
    assert unit_readiness("USD", "USD", ["ready", "preparable"], []) == "PREPARING"
    assert unit_readiness("USD", "USD", ["ready"], []) == "READY"
    assert unit_readiness("USD", "EUR", ["ready"], []) == "BLOCKED"
    assert unit_readiness("USD", "USD", ["ready", "blocked"], []) == "BLOCKED"
    assert unit_readiness("USD", "USD", ["ready"], ["minis_unavailable"]) == "BLOCKED"
    assert unit_readiness("USD", "USD", [], []) == "BLOCKED"


def seed_targeting_directory(
    session, context, advertisers, minis_id="fixture-mini", route=None
):
    # 持久化离线目录，预览/执行继续使用合成场景，绝不调用真实 TikTok。
    from datetime import UTC, datetime, timedelta

    from app.modules.accounts.routing import freeze_route
    from app.modules.builds.scene import scene_scope_basis
    from app.modules.builds.scene_job_models import SceneJob, SceneJobPage

    route = route or freeze_route(session, context=context, bc_id="bc-draft")
    for advertiser in advertisers:
        basis = scene_scope_basis(
            route=route,
            business={
                "advertiser_id": advertiser,
                "currency": "USD",
                "timezone": "UTC",
                "minis_id": minis_id,
            },
        )
        job = SceneJob(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            bc_id=route.bc_id,
            advertiser_id=advertiser,
            connection_id=route.connection_id,
            credential_revision=0,
            frozen_route=route.model_dump(mode="json"),
            minis_id=minis_id,
            scope_basis=basis,
            status="COMPLETE",
            resource="done",
            first_observed_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            facts={
                "identity": {
                    "options": [
                        {
                            "identity_id": "identity-1",
                            "identity_type": "BC_AUTH_TT",
                            "identity_authorized_bc_id": "bc-draft",
                        }
                    ]
                },
                "minis": {
                    "matches": [
                        {
                            "minis_id": minis_id,
                            "status": "ACTIVE",
                            "type": "MINI_SERIES",
                            "regions": ["US"],
                        }
                    ]
                },
                "regions": {
                    "locations": [{"region_code": "US", "location_id": "6252001"}]
                },
            },
        )
        session.add(job)
        session.flush()
        session.add(
            SceneJobPage(
                tenant_id=context.tenant_id,
                job_id=job.id,
                resource="identity",
                page=1,
                endpoint="offline",
                source_revision="offline",
                scope_basis=basis,
                facts=job.facts["identity"],
                observed_at=datetime.now(UTC),
            )
        )
    session.flush()


@pytest.fixture
def prepared(session, context, intent, monkeypatch):
    from app.modules.builds import identity_selection

    version = session.get(StrategyVersion, intent["strategy_version_id"])
    intent = dict(
        intent,
        strategy_version_id=append_version(
            session,
            context=context,
            strategy_id=version.strategy_id,
            config=config(budget="100", ads_per_group=1, creative_count=2),
        ),
        drama_lines=["Moon", "Short Drama"],
        account_lines=["A", "B", "C"],
    )
    for name in intent["account_lines"]:
        account(session, context, name)
    for title in intent["drama_lines"]:
        for i in range(23):
            material(session, context, f"{title} {i:03}.mp4", bc="bc-draft")
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    finish(session, context, task)
    from app.modules.builds.mini_targets import remember_target

    remember_target(
        session,
        context=context,
        url="https://example.com/drama",
        minis_id="fixture-mini",
        source="USER",
    )
    seed_targeting_directory(session, context, intent["account_lines"])
    scene = SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="fixture-v1",
        name_limit=512,
        creative_limit=50,
        copy_length_limit=100,
        field_constraints={
            "target_regions": [{"region_code": "US", "location_id": "6252001"}],
            "name_limits": {"campaign": 512, "adgroup": 512, "ad": 512},
            "name_measurement": {
                "campaign": "cjk_weighted",
                "adgroup": "characters",
                "ad": "cjk_weighted",
            },
            "copy_measurement": "characters",
            "max_ads_per_adgroup": 30,
            "roas_bid": {"minimum": "0.01", "maximum": "1000"},
            "campaign_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "50",
                "maximum_exclusive": "10000000",
                "precision": "0.01",
            },
        },
        cta_fields={"asset_ids": ["cta-1"], "requires_portfolio_creation": True},
    )
    monkeypatch.setattr(previews, "read_scene_context", lambda *a, **kw: scene)
    monkeypatch.setattr(
        identity_selection,
        "require_preview_identity",
        lambda *a, **kw: {
            "identity_id": "fixture-identity",
            "identity_type": "TT_USER",
        },
    )
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(state="preparable", reason_code=None)
            for identity in kw["material_ids"]
        },
    )
    return draft


def drain(session, context, identity):
    for _ in range(200):
        if previews.continue_preview(session, context=context, preview_id=identity):
            return
    raise AssertionError("preview never finished")


def _clone_ready_draft(
    session, context, source_id, *, strategy_version_id, link_config
):
    """为同一策略版本准备第二份草稿，确保 A/B 冻结数据走真实持久化路径。"""
    source = session.get(BuildDraft, source_id)
    assert source is not None
    drama_lines = [
        row.raw_text
        for row in session.exec(
            select(DraftInput)
            .where(DraftInput.draft_id == source_id, DraftInput.kind == "drama")
            .order_by(DraftInput.line_no)
        ).all()
        if row.raw_text.strip()
    ]
    account_lines = [
        row.raw_text
        for row in session.exec(
            select(DraftInput)
            .where(DraftInput.draft_id == source_id, DraftInput.kind == "account")
            .order_by(DraftInput.line_no)
        ).all()
        if row.raw_text.strip() and row.duplicate_of is None
    ]
    # provider_drama.external_drama_id is unique within an application; a
    # cloned draft therefore needs its own application scope before the real
    # prepare/finish path creates link rows.
    application_id = f"{source.application_id}-clone-{uuid4().hex[:12]}"
    session.add(
        ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=source.provider_connection_id,
            external_id=application_id,
            name=f"{source.application_id} clone",
        )
    )
    session.flush()
    intent = {
        "bc_id": source.bc_id,
        "strategy_version_id": strategy_version_id,
        "provider_connection_id": source.provider_connection_id,
        "application_id": application_id,
        "drama_lines": drama_lines,
        "account_lines": account_lines,
        "link_config": link_config,
    }
    draft_id = create_draft(session, context=context, request_id=uuid4(), **intent)
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, task_id, intent)
    finish(session, context, task_id)
    return draft_id


def test_two_drafts_freeze_independent_preview_rows_and_keep_strategy_version(
    session, context, prepared
):
    """两个真实 draft/preview 的冻结行必须按 preview 隔离，策略版本不可变。"""
    source = session.get(BuildDraft, prepared)
    assert source is not None
    version_before = session.get(StrategyVersion, source.strategy_version_id)
    assert version_before is not None
    version_snapshot = (version_before.id, version_before.number, version_before.config)
    second = _clone_ready_draft(
        session,
        context,
        prepared,
        strategy_version_id=source.strategy_version_id,
        link_config={"episode": 2},
    )
    first_preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, first_preview)
    second_preview = previews.generate_preview(
        session, context=context, draft_id=second, expected_revision=1
    )
    drain(session, context, second_preview)

    first_materials = session.exec(
        select(PreviewAdMaterial).where(
            PreviewAdMaterial.tenant_id == context.tenant_id,
            PreviewAdMaterial.preview_id == first_preview,
        )
    ).all()
    second_materials = session.exec(
        select(PreviewAdMaterial).where(
            PreviewAdMaterial.tenant_id == context.tenant_id,
            PreviewAdMaterial.preview_id == second_preview,
        )
    ).all()
    first_groups = session.exec(
        select(PreviewGroupMaterial).where(
            PreviewGroupMaterial.tenant_id == context.tenant_id,
            PreviewGroupMaterial.preview_id == first_preview,
        )
    ).all()
    second_groups = session.exec(
        select(PreviewGroupMaterial).where(
            PreviewGroupMaterial.tenant_id == context.tenant_id,
            PreviewGroupMaterial.preview_id == second_preview,
        )
    ).all()
    first_ads = session.exec(
        select(PlannedAd).where(
            PlannedAd.tenant_id == context.tenant_id,
            PlannedAd.preview_id == first_preview,
        )
    ).all()
    second_ads = session.exec(
        select(PlannedAd).where(
            PlannedAd.tenant_id == context.tenant_id,
            PlannedAd.preview_id == second_preview,
        )
    ).all()
    assert (
        first_materials
        and second_materials
        and first_groups
        and second_groups
        and first_ads
        and second_ads
    )
    first_keys = {
        (row.preview_id, row.drama_id, row.group_no, row.base_ad_no, row.position)
        for row in first_materials
    }
    second_keys = {
        (row.preview_id, row.drama_id, row.group_no, row.base_ad_no, row.position)
        for row in second_materials
    }
    first_group_keys = {
        (row.preview_id, row.drama_id, row.group_no, row.position)
        for row in first_groups
    }
    second_group_keys = {
        (row.preview_id, row.drama_id, row.group_no, row.position)
        for row in second_groups
    }
    assert {row.preview_id for row in first_materials} == {first_preview}
    assert {row.preview_id for row in second_materials} == {second_preview}
    assert first_keys.isdisjoint(second_keys)
    assert {row.preview_id for row in first_groups} == {first_preview}
    assert {row.preview_id for row in second_groups} == {second_preview}
    assert first_group_keys.isdisjoint(second_group_keys)
    assert {row.preview_id for row in first_ads} == {first_preview}
    assert {row.preview_id for row in second_ads} == {second_preview}
    version_after = session.get(StrategyVersion, source.strategy_version_id)
    assert version_after is not None
    assert (
        version_after.id,
        version_after.number,
        version_after.config,
    ) == version_snapshot


@pytest.mark.parametrize(
    ("label", "changes", "expected_groups", "expected_ads"),
    [
        (
            "by-material-one",
            {
                "budget_strategy": "SERIES",
                "bid_strategy": "HIGHEST_VALUE",
                "group_generation_mode": "FIXED",
                "group_count": 1,
                "group_material_allocation": "SHARED",
                "max_materials_per_group": None,
                "ad_generation_mode": "BY_MATERIAL",
                "ads_per_group": None,
                "ad_material_allocation": None,
                "max_materials_per_ad": 1,
                "creative_count": 1,
                "target_roas": None,
            },
            1,
            20,
        ),
        (
            "average-two",
            {
                "budget_strategy": "SERIES",
                "bid_strategy": "TARGET_ROAS",
                "group_generation_mode": "FIXED",
                "group_count": 2,
                "group_material_allocation": "SEQUENTIAL_AVERAGE",
                "max_materials_per_group": None,
                "ad_generation_mode": "BY_MATERIAL",
                "ads_per_group": None,
                "ad_material_allocation": None,
                "max_materials_per_ad": 1,
                "creative_count": 1,
                "target_roas": "1.08",
            },
            2,
            20,
        ),
        (
            "shared-creatives",
            {
                "budget_strategy": "SERIES",
                "bid_strategy": "HIGHEST_VALUE",
                "group_generation_mode": "FIXED",
                "group_count": 1,
                "group_material_allocation": "SHARED",
                "max_materials_per_group": None,
                "ad_generation_mode": "FIXED",
                "ads_per_group": 2,
                "ad_material_allocation": "SHARED",
                "max_materials_per_ad": None,
                "creative_count": 3,
                "target_roas": None,
            },
            1,
            6,
        ),
        (
            "group-budget-highest",
            {
                "budget_strategy": "ADGROUP",
                "bid_strategy": "HIGHEST_VALUE",
                "group_generation_mode": "FIXED",
                "group_count": 2,
                "group_material_allocation": "SHARED",
                "max_materials_per_group": None,
                "ad_generation_mode": "BY_MATERIAL",
                "ads_per_group": None,
                "ad_material_allocation": None,
                "max_materials_per_ad": 2,
                "creative_count": 1,
                "target_roas": None,
            },
            2,
            20,
        ),
    ],
)
def test_strategy_structure_matrix_persists_material_ads_and_frozen_contract(
    session,
    context,
    prepared,
    monkeypatch,
    label,
    changes,
    expected_groups,
    expected_ads,
):
    """四种策略均经真实预览写入 PlannedAd/PreviewAdMaterial 后再核对合同。"""

    source = session.get(BuildDraft, prepared)
    assert source is not None
    source_version = session.get(StrategyVersion, source.strategy_version_id)
    assert source_version is not None
    version_id = append_version(
        session,
        context=context,
        strategy_id=source_version.strategy_id,
        config=config(**changes),
    )
    draft_id = _clone_ready_draft(
        session,
        context,
        prepared,
        strategy_version_id=version_id,
        link_config={"episode": 10 + expected_groups},
    )
    # 使用固定 20 条离线素材，让第四个矩阵精确得到每组 10 个广告。
    rows = session.exec(
        select(*VIDEO_SOURCE.c)
        .where(VIDEO_SOURCE.c.draft_id == draft_id)
        .order_by(VIDEO_SOURCE.c.drama_id, VIDEO_SOURCE.c.position)
    ).all()
    seen_by_drama = {}
    for row in rows:
        seen_by_drama[row.drama_id] = seen_by_drama.get(row.drama_id, 0) + 1
        if seen_by_drama[row.drama_id] > 20:
            from app.modules.builds.image_composition_models import (
                DraftCreativeUnit,
                DraftCreativeUnitItem,
            )

            item = session.get(
                DraftCreativeUnitItem,
                (row.tenant_id, row.draft_id, row.generation, row.unit_id, 1),
            )
            session.delete(item)
            session.flush()
            session.delete(
                session.get(
                    DraftCreativeUnit,
                    (row.tenant_id, row.draft_id, row.generation, row.unit_id),
                )
            )
    session.flush()
    base_scene = previews.read_scene_context(
        session,
        context=context,
        bc_id="bc-draft",
        advertiser_id="account-A",
        link_id=uuid4(),
        route=None,
    )
    constraints = dict(base_scene.field_constraints)
    constraints.update(
        {
            "adgroup_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "50",
                "maximum_exclusive": "10000000",
                "precision": "0.01",
            },
            "bid_capabilities": {
                "HIGHEST_VALUE": {
                    "optimization_goal": "VALUE",
                    "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
                    "deep_bid_type": "VO_HIGHEST_VALUE",
                },
                "TARGET_ROAS": {
                    "optimization_goal": "VALUE",
                    "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
                    "deep_bid_type": "VO_MIN_ROAS",
                },
            },
        }
    )
    monkeypatch.setattr(
        previews,
        "read_scene_context",
        lambda *args, **kwargs: replace(base_scene, field_constraints=constraints),
    )
    preview_id = previews.generate_preview(
        session, context=context, draft_id=draft_id, expected_revision=1
    )
    drain(session, context, preview_id)
    preview = session.get(BuildPreview, preview_id)
    assert preview is not None
    assert preview.config["budget_strategy"] == changes["budget_strategy"]
    assert preview.config["bid_strategy"] == changes["bid_strategy"]
    groups = session.exec(
        select(PreviewGroupMaterial).where(
            PreviewGroupMaterial.tenant_id == context.tenant_id,
            PreviewGroupMaterial.preview_id == preview_id,
        )
    ).all()
    mappings = session.exec(
        select(PreviewAdMaterial).where(
            PreviewAdMaterial.tenant_id == context.tenant_id,
            PreviewAdMaterial.preview_id == preview_id,
        )
    ).all()
    ads = session.exec(
        select(PlannedAd).where(
            PlannedAd.tenant_id == context.tenant_id,
            PlannedAd.preview_id == preview_id,
        )
    ).all()
    units = session.exec(
        select(BuildUnit).where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
        )
    ).all()
    assert len({(row.drama_id, row.group_no) for row in groups}) == expected_groups * 2
    assert len(ads) == expected_ads * 2 * 3  # 两剧三账户，每个组合都完整冻结。
    assert mappings
    for drama_id in {row.drama_id for row in mappings}:
        assert (
            len({row.material_id for row in mappings if row.drama_id == drama_id}) == 20
        )
    assert all(ad.cta_option_ids == ["cta-1"] for ad in ads)
    # 每个 drama/group 的冻结素材顺序是广告结构的输入合同；组间必须互斥。
    group_material_ids = {}
    for row in sorted(
        groups, key=lambda item: (item.drama_id, item.group_no, item.position)
    ):
        group_material_ids.setdefault((row.drama_id, row.group_no), []).append(
            row.material_id
        )
    assert len(group_material_ids) == expected_groups * 2
    for drama_id in {key[0] for key in group_material_ids}:
        drama_groups = [
            values
            for (row_drama, _), values in group_material_ids.items()
            if row_drama == drama_id
        ]
        for index, left in enumerate(drama_groups):
            for right in drama_groups[index + 1 :]:
                if changes["group_material_allocation"] == "SHARED":
                    assert left == right
                else:
                    assert set(left).isdisjoint(right)

    from app.modules.builds.request_compiler import ad_assets, compile_request

    groups_by_id = {
        row.id: row
        for row in session.exec(
            select(PlannedGroup).where(
                PlannedGroup.tenant_id == context.tenant_id,
                PlannedGroup.preview_id == preview_id,
            )
        ).all()
    }
    for unit in units:
        snapshot = unit.scene_snapshot
        campaign_fixed = {
            "advertiser_id": unit.advertiser_id,
            "campaign_name": unit.campaign_name,
            "budget_strategy": changes["budget_strategy"],
        }
        if changes["budget_strategy"] == "SERIES":
            campaign_fixed["budget"] = preview.budget
        campaign_body = compile_request(
            "campaign",
            fixed=campaign_fixed,
            resolved=dict(snapshot.get("campaign_fields", {})),
        )
        adgroup_fixed = {
            "advertiser_id": unit.advertiser_id,
            "campaign_id": "fixture-campaign",
            "adgroup_name": "fixture-adgroup",
            "budget_strategy": changes["budget_strategy"],
            "bid_strategy": changes["bid_strategy"],
        }
        if changes["budget_strategy"] == "ADGROUP":
            adgroup_fixed["budget"] = preview.budget
        if changes["bid_strategy"] == "TARGET_ROAS":
            adgroup_fixed["roas_bid"] = preview.target_roas
        adgroup_resolved = dict(snapshot.get("adgroup_fields", {}))
        if changes["bid_strategy"] == "HIGHEST_VALUE":
            adgroup_resolved.update(
                optimization_goal="VALUE",
                optimization_event="IMPRESSION_LEVEL_AD_REVENUE",
                deep_bid_type="VO_HIGHEST_VALUE",
            )
        else:
            adgroup_resolved["deep_bid_type"] = "VO_MIN_ROAS"
        adgroup_body = compile_request(
            "adgroup", fixed=adgroup_fixed, resolved=adgroup_resolved
        )
        if changes["budget_strategy"] == "SERIES":
            assert campaign_body["budget_mode"] == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
            assert "budget" in campaign_body
            assert "budget" not in adgroup_body
        else:
            assert campaign_body["budget_mode"] == "BUDGET_MODE_INFINITE"
            assert campaign_body["budget"] == 0
            assert adgroup_body["budget_mode"] == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
            assert "budget" in adgroup_body
        if changes["bid_strategy"] == "HIGHEST_VALUE":
            assert adgroup_body["deep_bid_type"] == "VO_HIGHEST_VALUE"
            assert adgroup_body["optimization_goal"] == "VALUE"
            assert "roas_bid" not in adgroup_body
        else:
            assert adgroup_body["deep_bid_type"] == "VO_MIN_ROAS"
            assert Decimal(adgroup_body["roas_bid"]) == Decimal("1.08")

    # 每条冻结广告都必须把自己的素材集合编译成带视频与封面的 creative_list；
    # 这里仍使用本地目标资产证据，不发起平台请求。
    for ad in ads:
        group = groups_by_id[ad.group_id]
        group_ids = group_material_ids[(group.drama_id, group.group_no)]
        if label == "shared-creatives":
            expected_ids = group_ids
        else:
            width = 2 if label == "group-budget-highest" else 1
            start = (ad.base_ad_no - 1) * width
            expected_ids = group_ids[start : start + width]
        frozen_rows = session.exec(
            select(PreviewAdMaterial)
            .where(
                PreviewAdMaterial.tenant_id == context.tenant_id,
                PreviewAdMaterial.preview_id == preview_id,
                PreviewAdMaterial.drama_id == group.drama_id,
                PreviewAdMaterial.group_no == group.group_no,
                PreviewAdMaterial.base_ad_no == ad.base_ad_no,
            )
            .order_by(PreviewAdMaterial.position)
        ).all()
        frozen_ids = [row.material_id for row in frozen_rows]
        assert frozen_ids == expected_ids
        assert [row.position for row in frozen_rows] == list(
            range(1, len(expected_ids) + 1)
        )
        body = ad_assets(
            [
                VideoCreative(
                    kind="VIDEO",
                    video_id=f"fixture-video-{material_id}",
                    cover_image_id=f"fixture-cover-{material_id}",
                )
                for material_id in frozen_ids
            ],
            text=ad.text,
            url="https://example.test/minis",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "fixture-identity",
                "identity_authorized_bc_id": "bc-draft",
            },
        )
        compiled_ad = compile_request(
            "ad",
            fixed={
                "advertiser_id": "account-A",
                "campaign_id": "fixture-campaign",
                "adgroup_id": str(group.id),
                "ad_name": ad.name,
                "budget_strategy": changes["budget_strategy"],
                "bid_strategy": changes["bid_strategy"],
            },
            resolved={
                **body,
                "ad_configuration": {"call_to_action_id": ad.cta_option_ids[0]},
            },
        )
        assert len(compiled_ad["creative_list"]) == len(expected_ids)
        assert compiled_ad["operation_status"] == "ENABLE"
        assert "budget" not in compiled_ad
        assert "budget_mode" not in compiled_ad
        assert "roas_bid" not in compiled_ad
        assert "deep_bid_type" not in compiled_ad
        assert compiled_ad["ad_configuration"] == {"call_to_action_id": "cta-1"}
        assert [
            creative["creative_info"]["video_info"]["video_id"]
            for creative in compiled_ad["creative_list"]
        ] == [f"fixture-video-{material_id}" for material_id in expected_ids]
        assert [
            creative["creative_info"]["image_info"][0]["web_uri"]
            for creative in compiled_ad["creative_list"]
        ] == [f"fixture-cover-{material_id}" for material_id in expected_ids]
        assert compiled_ad["ad_text_list"] == [{"ad_text": ad.text}]
        assert compiled_ad["landing_page_url_list"] == [
            {"landing_page_url": "https://example.test/minis"}
        ]
        assert ad.cta_option_ids == ["cta-1"]
    assert all(
        unit.scene_snapshot["budget_strategy"] == changes["budget_strategy"]
        for unit in units
    )
    assert all(
        unit.scene_snapshot["bid_strategy"] == changes["bid_strategy"] for unit in units
    )
    if changes["budget_strategy"] == "ADGROUP":
        assert all(
            "adgroup_daily_budget" in unit.scene_snapshot["field_constraints"]
            for unit in units
        )
    else:
        assert all(
            "campaign_daily_budget" in unit.scene_snapshot["field_constraints"]
            for unit in units
        )
    assert label in {
        "by-material-one",
        "average-two",
        "shared-creatives",
        "group-budget-highest",
    }


def test_identity_picker_keeps_same_named_authorizations_separate_and_saves_choice(
    session, context, prepared, monkeypatch
):
    from app.modules.builds import identity_selection
    from app.modules.builds.identity_selection import ChooseIdentityRequest
    from app.modules.builds.models import BuildDraft

    catalog_id = uuid4()
    job = SimpleNamespace(
        id=catalog_id,
        facts={
            "identity": {
                "options": [
                    {
                        "identity_id": "tt-user-id",
                        "identity_type": "TT_USER",
                        "display_name": "star_isle_drama",
                        "username": "star_isle_drama",
                    },
                    {
                        "identity_id": "bc-auth-id",
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": "bc-draft",
                        "display_name": "star_isle_drama",
                        "username": "star_isle_drama",
                    },
                ]
            }
        },
    )
    monkeypatch.setattr(identity_selection, "_catalog", lambda *a, **kw: ("A", job))

    catalog = identity_selection.draft_identities(
        session, context=context, draft_id=prepared
    )
    assert catalog.state == "choose"
    assert [item.identity_type for item in catalog.items] == [
        "TT_USER",
        "BC_AUTH_TT",
    ]

    revision = identity_selection.choose_identity(
        session,
        context=context,
        draft_id=prepared,
        body=ChooseIdentityRequest(
            request_id=uuid4(),
            expected_revision=1,
            catalog_job_id=catalog_id,
            identity_id="bc-auth-id",
            identity_type="BC_AUTH_TT",
            identity_authorized_bc_id="bc-draft",
        ),
    )
    draft = session.get(BuildDraft, prepared, populate_existing=True)
    assert revision == 2
    assert draft is not None
    assert draft.status == "PREPARING"
    assert (
        draft.identity_id,
        draft.identity_type,
        draft.identity_authorized_bc_id,
        draft.identity_display_name,
    ) == ("bc-auth-id", "BC_AUTH_TT", "bc-draft", "star_isle_drama")


def test_missing_mini_rejected_before_any_preview_or_dispatch(
    session, context, prepared
):
    from app.core.errors import DomainError
    from app.jobs.models import PendingDispatch
    from app.modules.builds.mini_targets import MiniTarget, url_key
    from app.modules.builds.preview_models import BuildPreview

    session.delete(
        session.get(
            MiniTarget, (context.tenant_id, url_key("https://example.com/drama"))
        )
    )
    session.flush()
    before = len(session.exec(select(PendingDispatch)).all())
    with pytest.raises(DomainError) as error:
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
    assert error.value.code == "minis_selection_required"
    assert (
        session.exec(
            select(BuildPreview).where(BuildPreview.draft_id == prepared)
        ).all()
        == []
    )
    assert len(session.exec(select(PendingDispatch)).all()) == before


def test_existing_preview_recovery_does_not_require_new_mini_selection(
    session, context, prepared
):
    from app.modules.builds.mini_targets import MiniTarget, url_key

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    session.delete(
        session.get(
            MiniTarget, (context.tenant_id, url_key("https://example.com/drama"))
        )
    )
    session.flush()
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
        == identity
    )


def test_partially_selected_or_conflicting_links_cannot_generate_preview(
    session, context, prepared
):
    from app.core.errors import DomainError
    from app.modules.providers.models import PromotionLink

    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    link = session.get(PromotionLink, drama.link_id)
    for url, code in [
        ("https://example.com/unselected", "minis_selection_required"),
        (
            "https://www.tiktok.com/minis/play?minis_id=other-mini",
            "minis_link_conflict",
        ),
    ]:
        link.url = url
        session.add(link)
        session.flush()
        with pytest.raises(DomainError) as error:
            previews.generate_preview(
                session, context=context, draft_id=prepared, expected_revision=1
            )
        assert error.value.code == code


def test_one_batch_loads_the_frozen_route_once(session, context, prepared):
    from sqlalchemy import event

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    statements = []

    def counted(_c, _cu, sql, _p, _ct, _many):
        statements.append(sql)

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", counted)
    try:
        assert previews.continue_preview(session, context=context, preview_id=identity)
        assert (
            len([sql for sql in statements if "FROM build_route_context" in sql]) == 1
        )
    finally:
        event.remove(conn, "before_cursor_execute", counted)


def test_frozen_full_product_budget_and_shared_copy(session, context, prepared):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    initial = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert initial.generation_progress.phase == "inputs"
    assert initial.generation_progress.total_units == 6
    assert initial.generation_progress.completed_units == 0
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
        == identity
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.status == "FROZEN"
    assert summary.generation_progress.phase == "complete"
    assert summary.generation_progress.total_units == 6
    assert summary.generation_progress.completed_units == 6
    assert (
        summary.generation_progress.updated_at >= initial.generation_progress.updated_at
    )
    assert (summary.campaign_count, summary.adgroup_count, summary.ad_count) == (
        6,
        18,
        36,
    )
    assert Decimal(summary.daily_budget_sum) == 600
    units = previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items
    assert len(units) == 6 and all(x.readiness == "PREPARING" for x in units)
    by_drama = {}
    for unit in units:
        frozen = previews.load_frozen_unit(
            session, context=context, unit_id=unit.unit_id
        )
        assert frozen.budget == 100 and frozen.campaign_name.startswith(
            frozen.protected_base
        )
        groups = previews.get_frozen_groups(
            session, context=context, unit_id=unit.unit_id
        ).items
        assert [g.material_count for g in groups] == [10, 10, 3]
        copies = [
            tuple(
                (ad.copy_id, ad.text)
                for ad in previews.get_frozen_ads(
                    session,
                    context=context,
                    unit_id=unit.unit_id,
                    group_id=group.group_id,
                    limit=5,
                ).items
            )
            for group in groups
        ]
        if unit.drama_id in by_drama:
            assert by_drama[unit.drama_id] == copies
        by_drama[unit.drama_id] = copies
    assert summary.content_digest and len(summary.content_digest) == 64


def test_edit_obsoletes_preview_but_preserves_frozen_intent(session, context, prepared):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    units = previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items
    before = previews.load_frozen_unit(
        session, context=context, unit_id=units[0].unit_id
    )
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    edit_material_groups(
        session,
        context=context,
        draft_id=prepared,
        drama_id=drama.drama_id,
        expected_revision=1,
        groups=[],
    )
    assert (
        previews.get_preview_summary(
            session, context=context, preview_id=identity
        ).status
        == "OBSOLETE"
    )
    assert (
        previews.load_frozen_unit(session, context=context, unit_id=units[0].unit_id)
        == before
    )


def test_per_pair_blocks_and_budget_are_exact(session, context, prepared, monkeypatch):
    from dataclasses import replace

    original = previews.read_scene_context

    def read(*args, **kwargs):
        scene = original(*args, **kwargs)
        return (
            replace(scene, supported=False, reason_codes=("minis_unavailable",))
            if kwargs["advertiser_id"] == "C"
            else scene
        )

    monkeypatch.setattr(previews, "read_scene_context", read)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert (
        summary.campaign_count,
        summary.blocked_count,
        summary.daily_budget_sum,
    ) == (4, 2, Decimal("400"))
    assert summary.adgroup_count == 12 and summary.ad_count == 24
    units = previews.get_preview_units(
        session, context=context, preview_id=identity, readiness="BLOCKED"
    ).items
    assert len(units) == 2 and all(u.advertiser_id == "C" for u in units)


def test_preview_tables_frozen_and_material_upload_does_not_expand_intent(
    session, context, prepared
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    before = previews.get_preview_summary(session, context=context, preview_id=identity)
    material(session, context, "Moon new.mp4", bc="bc-draft")
    assert (
        previews.get_preview_summary(session, context=context, preview_id=identity)
        == before
    )
    for table in [
        "build_preview",
        "build_unit",
        "planned_ad",
        "preview_group_creative_unit",
        "preview_copy",
        "preview_drama",
    ]:
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    f"DELETE FROM {table} WHERE "
                    + ("id=:id" if table == "build_preview" else "preview_id=:id")
                ),
                {"id": identity},
            )


def test_bounded_restart_and_paged_reads_preserve_rows(
    session, context, other_context, prepared, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.materials import service

    def forbidden(*_a, **_kw):
        raise AssertionError("Preview requested a remote material write")

    monkeypatch.setattr(service, "ensure_target_asset", forbidden)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert not previews.continue_preview(
        session, context=context, preview_id=identity, step_limit=1
    )
    original = session.get(BuildPreview, identity).batch_short_id
    session.expire_all()
    drain(session, context, identity)
    assert session.get(BuildPreview, identity).batch_short_id == original
    result = []
    cursor = None
    while True:
        page = previews.get_preview_units(
            session, context=context, preview_id=identity, cursor=cursor, limit=2
        )
        result.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(result) == len({u.unit_id for u in result}) == 6
    with pytest.raises(DomainError):
        previews.get_preview_units(
            session, context=other_context, preview_id=identity, cursor=cursor
        )
    with pytest.raises(DomainError):
        previews.get_preview_units(
            session,
            context=context,
            preview_id=identity,
            cursor=cursor,
            readiness="BLOCKED",
        )


def test_revision_during_build_keeps_partial_preview_obsolete(
    session, context, prepared
):
    from app.modules.builds.preview_models import BuildPreview

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert not previews.continue_preview(
        session, context=context, preview_id=identity, step_limit=1
    )
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    edit_material_groups(
        session,
        context=context,
        draft_id=prepared,
        drama_id=drama.drama_id,
        expected_revision=1,
        groups=[],
    )
    assert previews.continue_preview(session, context=context, preview_id=identity)
    row = session.get(BuildPreview, identity, populate_existing=True)
    assert row.status == "OBSOLETE" and row.content_digest is None


def test_identical_protected_bases_are_distinguished_by_external_drama_id(
    session, context, prepared
):
    from app.modules.providers.models import PromotionLink

    dramas = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).all()
    for drama in dramas:
        link = session.get(PromotionLink, drama.link_id)
        link.protected_base = "same-provider-base"
        session.add(link)
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.campaign_count == 6 and summary.blocked_count == 0
    assert all(
        "duplicate_campaign_name" not in u.reason_codes
        for u in previews.get_preview_units(
            session, context=context, preview_id=identity
        ).items
    )


def test_large_cjk_name_is_reported_without_database_index_error(
    session, context, prepared
):
    from app.modules.providers.models import PromotionLink

    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    link = session.get(PromotionLink, drama.link_id)
    link.protected_base = "剧" * 1000
    session.add(link)
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.blocked_count == 3 and summary.campaign_count == 3


def test_worker_duplicate_and_repair_reuse_published_identity(
    session, context, prepared
):
    from datetime import UTC, datetime, timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.builds.preview_tasks import process_preview, repair_previews

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    header = session.get(BuildPreview, identity)
    original = header.dispatch_id
    header.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch = session.get(PendingDispatch, original)
    dispatch.published_at = datetime.now(UTC) - timedelta(minutes=4)
    session.add_all([header, dispatch])
    session.flush()
    assert repair_previews(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    assert session.get(BuildPreview, identity).dispatch_id == original
    assert session.get(PendingDispatch, original).published_at is None
    for _ in range(2):
        process_preview(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload={"preview_id": str(identity), "generation": 0},
        )
    session.expire_all()
    assert session.get(BuildPreview, identity).status == "FROZEN"


def test_http_preview_request_and_readonly_pages(
    session, client, context, other_context, prepared
):
    from sqlalchemy import func

    from app.jobs.models import PendingDispatch
    from app.modules.tenants.models import TenantMembership
    from tests.modules.strategies.test_api import headers

    base = f"/api/tenants/{context.tenant_id}"
    response = client.post(
        f"{base}/build-drafts/{prepared}/previews",
        json={"expected_revision": 1},
        headers=headers(context),
    )
    assert response.status_code == 202
    identity = UUID(response.json()["preview_id"])
    assert (
        client.get(
            f"{base}/build-drafts/{prepared}/previews/1", headers=headers(context)
        ).json()
        == response.json()
    )
    drain(session, context, identity)
    before = session.exec(select(func.count()).select_from(PendingDispatch)).one()
    summary = client.get(f"{base}/build-previews/{identity}", headers=headers(context))
    assert (
        summary.status_code == 200
        and Decimal(summary.json()["daily_budget_sum"]) == 600
    )
    page = client.get(
        f"{base}/build-previews/{identity}/units?limit=2", headers=headers(context)
    ).json()
    unit = page["items"][0]["unit_id"]
    assert (
        client.get(
            f"{base}/build-units/{unit}/groups", headers=headers(context)
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{base}/build-previews/{identity}/inputs?kind=drama",
            headers=headers(context),
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-units/{unit}",
            headers=headers(other_context),
        ).status_code
        == 404
    )
    member = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == context.tenant_id,
            TenantMembership.user_id == context.actor_id,
        )
    ).one()
    member.role = "viewer"
    session.add(member)
    session.flush()
    assert (
        client.get(f"{base}/build-units/{unit}", headers=headers(context)).status_code
        == 200
    )
    assert (
        client.post(
            f"{base}/build-drafts/{prepared}/previews",
            json={"expected_revision": 1},
            headers=headers(context),
        ).status_code
        == 403
    )
    assert (
        session.exec(select(func.count()).select_from(PendingDispatch)).one() == before
    )


@pytest.mark.parametrize("mode", ["generate", "continue", "edit", "retry"])
def test_preview_concurrent_parent_locking(
    isolated_strategy_database, mode, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.builds import identity_selection
    from app.modules.builds.preview_models import BuildPreview
    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    monkeypatch.setattr(
        identity_selection,
        "require_preview_identity",
        lambda *a, **kw: {
            "identity_id": "fixture-identity",
            "identity_type": "TT_USER",
        },
    )
    with Session(engine) as session:
        values = create_intent(session, context)
        account(session, context)
        draft = create_draft(session, context=context, **values)
        task = prepare_draft(
            session, context=context, draft_id=draft, request_id=uuid4()
        )
        ready_links(session, context, task, values)
        finish(session, context, task)
        from app.modules.builds.mini_targets import remember_target

        remember_target(
            session,
            context=context,
            url="https://example.com/drama",
            minis_id="fixture-mini",
            source="USER",
        )
        seed_targeting_directory(session, context, ["account-A"])
        drama = (
            session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft))
            .first()
            .drama_id
        )
        identity = (
            previews.generate_preview(
                session, context=context, draft_id=draft, expected_revision=1
            )
            if mode != "generate"
            else None
        )
        if mode == "retry":
            failed = session.get(BuildPreview, identity)
            failed.status, failed.error_code = "FAILED", "naming_context_missing"
            session.add(failed)
        session.commit()
    barrier = Barrier(2)

    def work(index):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            if mode in ("generate", "retry"):
                result = previews.generate_preview(
                    session, context=context, draft_id=draft, expected_revision=1
                )
            elif mode == "edit" and index == 1:
                result = edit_material_groups(
                    session,
                    context=context,
                    draft_id=draft,
                    drama_id=drama,
                    expected_revision=1,
                    groups=[],
                )
            else:
                result = previews.continue_preview(
                    session, context=context, preview_id=identity
                )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(work, range(2)))
    with Session(engine) as session:
        rows = session.exec(
            select(BuildPreview).where(BuildPreview.draft_id == draft)
        ).all()
        assert len(rows) == (2 if mode == "retry" else 1)
        if mode in ("generate", "retry"):
            assert result[0] == result[1]
            if mode == "retry":
                assert result[0] != identity
                assert session.get(BuildDraft, draft).revision == 2
        elif mode == "continue":
            assert rows[0].status == "FROZEN"
        else:
            assert rows[0].status == "OBSOLETE"


def test_failed_preview_retry_preserves_history_and_deduplicates_requests(
    session, context, prepared
):
    from app.modules.builds.preview_tasks import process_preview

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    row = session.get(BuildPreview, identity)
    assert row is not None
    old_generation = row.generation
    row.status, row.error_code = "FAILED", "naming_context_missing"
    session.add(row)
    session.flush()
    progress = dict(row.progress)

    retried = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert retried != identity
    fresh = session.get(BuildPreview, retried)
    assert fresh.status == "BUILDING" and fresh.error_code is None
    assert fresh.draft_revision == 2 and fresh.progress["retry_of"] == str(identity)
    assert fresh.batch_short_id != row.batch_short_id
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
        == retried
    )
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=2
        )
        == retried
    )
    process_preview(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preview_id": str(identity), "generation": old_generation},
    )
    session.refresh(row)
    assert row.status == "FAILED" and row.progress == progress
    drain(session, context, retried)
    session.refresh(fresh)
    assert fresh.status == "FROZEN"
    assert session.get(BuildDraft, prepared).revision == 2


def test_failed_preview_retry_rejects_changed_draft(session, context, prepared):
    from app.core.errors import DomainError

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    row = session.get(BuildPreview, identity)
    row.status, row.error_code = "FAILED", "naming_context_missing"
    draft = session.get(BuildDraft, prepared)
    draft.revision = 2
    session.add_all([row, draft])
    session.flush()
    with pytest.raises(DomainError, match="草稿已变更"):
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )


def test_batch_cap_blocks_before_any_account_expansion(session, context, prepared):
    draft = session.get(BuildDraft, prepared)
    draft.structure_override = {
        "group_generation_mode": "FIXED",
        "group_count": 100,
        "group_material_allocation": "SHARED",
        "max_materials_per_group": None,
        "ad_generation_mode": "FIXED",
        "ads_per_group": 20,
        "ad_material_allocation": "SHARED",
        "max_materials_per_ad": None,
        "creative_count": 1,
    }
    session.flush()
    with pytest.raises(DomainError, match="拆批"):
        previews.generate_preview(
            session,
            context=context,
            draft_id=prepared,
            expected_revision=draft.revision,
        )
    assert not session.exec(
        select(BuildPreview).where(BuildPreview.draft_id == prepared)
    ).all()


def test_preview_freezes_effective_structure_override(session, context, prepared):
    draft = session.get(BuildDraft, prepared)
    draft.structure_override = {
        "group_generation_mode": "FIXED",
        "group_count": 1,
        "group_material_allocation": "SHARED",
        "max_materials_per_group": None,
        "ad_generation_mode": "BY_MATERIAL",
        "ads_per_group": None,
        "ad_material_allocation": None,
        "max_materials_per_ad": 6,
        "creative_count": 3,
    }
    session.flush()
    preview_id = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=draft.revision
    )
    drain(session, context, preview_id)
    frozen = session.get(BuildPreview, preview_id)
    assert frozen.config["max_materials_per_ad"] == 6
    assert frozen.config["creative_count"] == 3
    assert frozen.budget == Decimal("100")
    assert (
        len(
            session.exec(
                select(PlannedAd).where(PlannedAd.preview_id == preview_id)
            ).all()
        )
        == 72
    )


@pytest.mark.parametrize(
    "template",
    [
        "{drama_name}-{provider_label}-{drama_id}-{YYYYMMDD}",
        "{YYYYMMDD}_{drama_id}_{provider_label}_{drama_name}",
    ],
)
def test_free_naming_survives_preview_and_directory_identity(
    session, context, prepared, template
):
    from datetime import UTC, datetime

    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.ads.directory import append_campaign_name_projection
    from app.modules.ads.models import AdObject
    from app.modules.builds import submissions
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.naming_identity import built_campaign_identities
    from app.modules.builds.preview_models import PreviewDrama

    draft = session.get(BuildDraft, prepared)
    version = session.get(StrategyVersion, draft.strategy_version_id)
    draft.strategy_version_id = append_version(
        session,
        context=context,
        strategy_id=version.strategy_id,
        config=config(
            budget="100",
            ads_per_group=1,
            creative_count=2,
            campaign_name_template=template,
        ),
    )
    session.add(draft)
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.status == "FROZEN"
    unit = session.exec(
        select(BuildUnit).where(BuildUnit.preview_id == identity)
    ).first()
    drama = session.get(PreviewDrama, (context.tenant_id, identity, unit.drama_id))
    if template.startswith("{drama_name}"):
        assert unit.campaign_name.startswith(drama.title + "-")
    else:
        assert unit.campaign_name.split("_", 1)[0].isdigit()
    receipt = submissions.submit_preview(
        session, context=context, preview_id=identity, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id, limit=100
    ):
        pass
    step = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.unit_id == unit.id, ExecutionStep.kind == "CAMPAIGN"
        )
    ).one()
    step.remote_id = "fixture-campaign"
    session.add(step)
    session.add(
        AdObject(
            tenant_id=context.tenant_id,
            advertiser_id=unit.advertiser_id,
            kind="campaign",
            remote_id=step.remote_id,
            name=unit.campaign_name,
            ad_type="SMART_PLUS",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()
    ref = EntityRef(context.tenant_id, unit.advertiser_id, "campaign", step.remote_id)
    projection = append_campaign_name_projection(
        session, campaign_ref=ref, raw_name=unit.campaign_name, parser_revision=1
    )
    assert projection.drama_name == drama.title
    assert projection.status == "VALID"
    # 同名跨租户/账户不继承回执身份，平台改名也不继续套用原名称快照。
    assert (
        built_campaign_identities(
            session,
            (
                (
                    EntityRef(uuid4(), ref.advertiser_id, "campaign", ref.remote_id),
                    unit.campaign_name,
                ),
            ),
        )
        == {}
    )
    assert (
        built_campaign_identities(
            session,
            (
                (
                    EntityRef(
                        ref.tenant_id, "other-account", "campaign", ref.remote_id
                    ),
                    unit.campaign_name,
                ),
            ),
        )
        == {}
    )
    assert built_campaign_identities(session, ((ref, "external-renamed"),)) == {}
