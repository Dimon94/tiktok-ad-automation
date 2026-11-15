"""目标图片原子性、同曲核验与执行围栏；真实 PostgreSQL。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import col, select

from app.core.errors import DomainError
from app.modules.builds.creative_units import frozen_ad_creative_units
from app.modules.materials.models import AccountMaterial
from tests.modules.builds.test_carousel_freeze import frozen_case
from tests.modules.builds.test_image_composition_service import (
    image_draft as image_draft,
)


@pytest.mark.parametrize("missing_index", [2, 6])
def test_missing_one_member_never_returns_shrunken_ad(
    session, context, image_draft, missing_index
):
    from app.modules.builds.carousel_readiness import resolve_carousel_assets

    value, _ = frozen_case(session, context, image_draft)
    draft, drama, scope = image_draft
    units = frozen_ad_creative_units(
        session,
        tenant_id=context.tenant_id,
        preview_id=value.id,
        drama_id=drama.drama_id,
        group_no=1,
        base_ad_no=1,
    )
    members = [m for u in units for m in u.members]
    for index, member in enumerate(members):
        if index == missing_index:
            continue
        session.add(
            AccountMaterial(
                tenant_id=context.tenant_id,
                bc_id=draft.bc_id,
                material_id=member.material_id,
                advertiser_id="account-A",
                connection_id=scope.route.connection_id,
                media_type="IMAGE",
                video_id=None,
                image_id=f"image-{index}",
                is_carousel_usable=True,
                status="available",
                verified_at=datetime.now(UTC),
                frozen_route=scope.route.model_dump(mode="json"),
            )
        )
    session.flush()
    with pytest.raises(DomainError) as error:
        resolve_carousel_assets(
            session,
            context=context,
            route=scope.route,
            advertiser_id="account-A",
            units=units,
        )
    assert error.value.code in {"image_refresh_required", "image_source_unavailable"}
    session.add(
        AccountMaterial(
            tenant_id=context.tenant_id,
            bc_id=draft.bc_id,
            material_id=members[missing_index].material_id,
            advertiser_id="account-A",
            connection_id=scope.route.connection_id,
            media_type="IMAGE",
            video_id=None,
            image_id="last-image",
            is_carousel_usable=True,
            status="available",
            verified_at=datetime.now(UTC),
            frozen_route=scope.route.model_dump(mode="json"),
        )
    )
    session.flush()
    assets = resolve_carousel_assets(
        session,
        context=context,
        route=scope.route,
        advertiser_id="account-A",
        units=units,
    )
    assert [len(u.image_ids) for u in assets] == [3, 3, 1]
    assert [u.music_id for u in assets] == [u.music_id for u in units]


@pytest.fixture
def carousel_case(isolated_strategy_database, monkeypatch, request):
    """合成冻结事实，不伪造平台场景资格；直接验证执行合同的下层边界。"""
    from cryptography.fernet import Fernet
    from sqlmodel import Session, select

    from app.core.config import settings
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.connection_models import ConnectionAuthorization
    from app.modules.accounts.models import TikTokConnection
    from app.modules.builds import submissions
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.preview_models import (
        BuildUnit,
        PlannedAd,
        PlannedGroup,
        PreviewAdCreativeUnit,
        PreviewAdReadiness,
        PreviewGroupCreativeUnit,
    )
    from app.modules.builds.routes import save_preview_route
    from app.modules.strategies.models import CopyEntry, CopyPoolVersion
    from tests.modules.builds.test_drafts import account
    from tests.modules.builds.test_execution_admission import policy
    from tests.modules.builds.test_image_composition_service import image_draft

    engine, context, _ = isolated_strategy_database
    policy(monkeypatch)
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with Session(engine) as session:
        case = image_draft.__wrapped__(session, context)
        draft, drama, scope = case
        account(session, context, "account-B")
        preview, _ = frozen_case(session, context, case)
        preview.config = {
            "currency": "USD",
            "creative_count": 1,
            "budget_strategy": "ADGROUP",
        }
        save_preview_route(
            session, context=context, preview_id=preview.id, route=scope.route
        )
        connection = session.get(TikTokConnection, scope.route.connection_id)
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id,
            value={"access_token": "fixture", "scope": "[2,6]"},
        )
        auth = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection.id
            )
        ).one()
        auth.scopes = ["6"]
        mappings = session.exec(
            select(PreviewAdCreativeUnit)
            .where(PreviewAdCreativeUnit.preview_id == preview.id)
            .order_by(PreviewAdCreativeUnit.position)
        ).all()
        for index, mapping in enumerate(mappings, 1):
            mapping.base_ad_no, mapping.position = index, 1
            session.add(
                PreviewGroupCreativeUnit(
                    tenant_id=context.tenant_id,
                    preview_id=preview.id,
                    bc_id=draft.bc_id,
                    drama_id=drama.drama_id,
                    group_no=1,
                    position=index,
                    creative_unit_id=mapping.creative_unit_id,
                )
            )
        pool = CopyPoolVersion(name="synthetic")
        session.add(pool)
        session.flush()
        copy = CopyEntry(pool_version_id=pool.id, position=1, text="Synthetic copy")
        session.add(copy)
        session.flush()
        excluded = getattr(request, "param", False)
        unit_ids = []
        all_units = []
        for advertiser in ("account-A", "account-B"):
            unit = BuildUnit(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=draft.bc_id,
                drama_id=drama.drama_id,
                advertiser_id=advertiser,
                connection_id=scope.route.connection_id,
                currency="USD",
                timezone="UTC",
                campaign_name=f"synthetic-{advertiser}",
                campaign_digest="a" * 64,
                group_count=1,
                ad_count=3,
                complete=True,
                readiness="PREPARING",
                reason_codes=["carousel_music_unverified"],
                scene_snapshot={
                    "creative_fields": {
                        "creative_info": {
                            "identity_type": "TT_USER",
                            "identity_id": "identity",
                            "ad_format": "CAROUSEL_ADS",
                        }
                    },
                    "cta_fields": {
                        "asset_ids": ["cta"],
                        "recommend_assets": [
                            {"asset_ids": ["cta"], "asset_content": "Watch"}
                        ],
                    },
                },
            )
            session.add(unit)
            session.flush()
            unit_ids.append(unit.id)
            group = PlannedGroup(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=draft.bc_id,
                unit_id=unit.id,
                drama_id=drama.drama_id,
                group_no=1,
                name="group",
            )
            session.add(group)
            session.flush()
            for index in range(1, 4):
                ad = PlannedAd(
                    tenant_id=context.tenant_id,
                    preview_id=preview.id,
                    bc_id=draft.bc_id,
                    group_id=group.id,
                    base_ad_no=index,
                    creative_no=1,
                    name=f"ad-{index}",
                    copy_id=copy.id,
                    text=copy.text,
                    cta_option_ids=["cta"],
                )
                session.add(ad)
                session.flush()
                reasons = (
                    ["image_source_unavailable"]
                    if excluded and advertiser == "account-A" and index == 1
                    else []
                )
                session.add(
                    PreviewAdReadiness(
                        tenant_id=context.tenant_id,
                        preview_id=preview.id,
                        bc_id=draft.bc_id,
                        ad_id=ad.id,
                        included=not reasons,
                        reason_codes=reasons,
                    )
                )
                units = frozen_ad_creative_units(
                    session,
                    tenant_id=context.tenant_id,
                    preview_id=preview.id,
                    drama_id=drama.drama_id,
                    group_no=1,
                    base_ad_no=index,
                )
                all_units.extend(units)
                for member in units[0].members:
                    session.add(
                        AccountMaterial(
                            tenant_id=context.tenant_id,
                            bc_id=draft.bc_id,
                            material_id=member.material_id,
                            advertiser_id=advertiser,
                            connection_id=scope.route.connection_id,
                            media_type="IMAGE",
                            video_id=None,
                            image_id=f"{advertiser}-{member.material_id}",
                            is_carousel_usable=True,
                            status="available",
                            verified_at=datetime.now(UTC),
                            frozen_route=scope.route.model_dump(mode="json"),
                        )
                    )
        session.flush()
        preview.status, preview.content_digest = "FROZEN", "b" * 64
        session.add(preview)
        session.flush()
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview.id, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            pass
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        for step in steps:
            if step.kind in {"MATERIAL", "CTA", "CAMPAIGN", "ADGROUP"}:
                step.status, step.phase = "SUCCEEDED", "DONE"
                if step.kind != "MATERIAL":
                    step.remote_id = "remote-" + step.kind
        preview_id = preview.id
        session.commit()
    return engine, context, scope.route, receipt.submission_id, unit_ids, preview_id


def claim_ad(case, *, account_index=0, base=1):
    from sqlmodel import Session, select

    from app.modules.builds import submissions
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.preview_models import PlannedAd

    engine, context, _, submission, units, _ = case
    with Session(engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep)
            .join(PlannedAd, PlannedAd.id == ExecutionStep.planned_ad_id)
            .where(
                ExecutionStep.unit_id == units[account_index],
                ExecutionStep.kind == "AD",
                PlannedAd.base_ad_no == base,
            )
        ).one()
        return submissions.claim_step(
            session, context=context, step_id=step.id, owner=uuid4(), lease_seconds=60
        )


def prepare_ad(case, claim):
    from sqlmodel import Session

    from app.modules.builds.execution import prepare_request
    from app.modules.builds.previews import load_frozen_unit
    from app.modules.builds.request_compiler import create_arguments, decode_intent

    engine, context, *_ = case
    with Session(engine) as session, session.begin():
        frozen = load_frozen_unit(session, context=context, unit_id=claim.unit_id)
        intent = decode_intent(
            "AD", prepare_request(session, context=context, claim=claim, frozen=frozen)
        )
    return intent, create_arguments(
        attempt_id=claim.attempt_id, intent=intent, channel=claim.route.channel
    )[1]


@pytest.mark.parametrize("carousel_case", [True], indirect=True)
def test_frozen_ad_exclusion_counts_blueprints_and_other_account(carousel_case):
    from sqlmodel import Session, select

    from app.modules.builds import previews, submissions
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.preview_models import PreviewAdReadiness

    engine, context, _, submission, units, preview = carousel_case
    with Session(engine) as session:
        value = submissions.get_submission(
            session, context=context, submission_id=submission
        )
        assert value.planned.ad_count == 6 and value.submitted.ad_count == 5
        assert value.excluded.ad_count == 1 and value.submitted.adgroup_count == 2
        summary = previews.get_preview_summary(
            session, context=context, preview_id=preview
        )
        assert summary.ad_count == 5 and summary.adgroup_count == 2
        assert summary.preparing_count == 2
        excluded = session.exec(
            select(PreviewAdReadiness).where(
                PreviewAdReadiness.preview_id == preview,
                col(PreviewAdReadiness.included).is_(False),
            )
        ).one()
        assert not session.exec(
            select(ExecutionStep).where(ExecutionStep.planned_ad_id == excluded.ad_id)
        ).all()
        material_counts = [
            len(
                session.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.unit_id == unit, ExecutionStep.kind == "MATERIAL"
                    )
                ).all()
            )
            for unit in units
        ]
        assert material_counts == [4, 7]


def test_one_failed_ad_does_not_block_shared_parent_or_other_ad(carousel_case):
    from sqlmodel import Session, select

    from app.modules.builds.ad_readiness import carousel_material_state
    from app.modules.builds.dispatch import _dependency
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.preview_models import PlannedAd, PlannedGroup

    engine, context, _, submission, units, preview = carousel_case
    with Session(engine) as session, session.begin():
        ads = session.exec(
            select(ExecutionStep)
            .join(PlannedAd, PlannedAd.id == ExecutionStep.planned_ad_id)
            .where(ExecutionStep.unit_id == units[0], ExecutionStep.kind == "AD")
            .order_by(PlannedAd.base_ad_no)
        ).all()
        ad = session.exec(
            select(PlannedAd).where(PlannedAd.id == ads[0].planned_ad_id)
        ).one()
        group = session.exec(
            select(PlannedGroup).where(PlannedGroup.id == ads[0].group_id)
        ).one()
        frozen = frozen_ad_creative_units(
            session,
            tenant_id=context.tenant_id,
            preview_id=preview,
            drama_id=group.drama_id,
            group_no=1,
            base_ad_no=ad.base_ad_no,
        )
        failed = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == units[0],
                ExecutionStep.material_id == frozen[0].members[0].material_id,
            )
        ).one()
        failed.status = "FAILED"
        session.flush()
        assert (
            carousel_material_state(
                session,
                tenant_id=context.tenant_id,
                preview_id=preview,
                submission_id=submission,
                unit_id=units[0],
                group_id=group.id,
            )
            == "READY"
        )
        assert _dependency(session, context, ads[0]) == "FAILED"
        assert _dependency(session, context, ads[1]) == "READY"
        assert (
            carousel_material_state(
                session,
                tenant_id=context.tenant_id,
                preview_id=preview,
                submission_id=submission,
                unit_id=units[1],
            )
            == "READY"
        )


def test_arm_requires_current_attempt_song_and_exact_image_order(carousel_case):
    from copy import deepcopy

    from sqlmodel import Session

    from app.modules.builds.carousel_readiness import body_assets, music_proof
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.execution_state import arm_request

    engine, context, *_ = carousel_case
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    assert len(body["creative_list"][0]["creative_info"]["image_info"]) == 3
    with Session(engine) as session, session.begin():
        with pytest.raises(DomainError) as error:
            arm_request(session, context=context, claim=claim, body=body)
        assert error.value.code == "carousel_music_unverified"
        step = session.get(ExecutionStep, claim.step_id)
        step.resolved = {"carousel_music": music_proof(claim, body_assets(body))}
        session.flush()
        wrong = deepcopy(body)
        wrong["creative_list"][0]["creative_info"]["image_info"].reverse()
        with pytest.raises(DomainError) as error:
            arm_request(session, context=context, claim=claim, body=wrong)
        assert error.value.code == "execution_intent_changed"
        arm_request(session, context=context, claim=claim, body=body)
        assert step.phase == "REQUEST_ARMED" and step.request_body == body


@pytest.mark.parametrize("unit_count", [2000, 4000])
def test_async_large_shared_group_has_bounded_member_queries_and_per_ad_facts(
    session, context, image_draft, unit_count, record_property
):
    from time import monotonic

    from sqlalchemy import event
    from sqlmodel import select

    from app.modules.builds import previews
    from app.modules.builds.preview_models import (
        BuildUnit,
        PreviewAdCreativeUnit,
        PreviewAdReadiness,
        PreviewCopy,
        PreviewCreativeUnit,
        PreviewCreativeUnitItem,
        PreviewDrama,
        PreviewGroupCreativeUnit,
    )
    from app.modules.builds.routes import save_preview_route
    from app.modules.strategies.models import CopyEntry, CopyPoolVersion
    from tests.modules.strategies.test_versions import config

    preview, _ = frozen_case(session, context, image_draft)
    draft, drama, scope = image_draft
    shared = {
        "tenant_id": context.tenant_id,
        "preview_id": preview.id,
        "bc_id": draft.bc_id,
    }
    frozen_drama = session.get(
        PreviewDrama, (context.tenant_id, preview.id, drama.drama_id)
    )
    frozen_drama.provider_pinyin = "test"
    frozen_drama.display_drama_id = "42"
    source = session.exec(
        select(PreviewCreativeUnit)
        .where(PreviewCreativeUnit.preview_id == preview.id)
        .order_by(PreviewCreativeUnit.position)
    ).first()
    members = session.exec(
        select(PreviewCreativeUnitItem).where(
            PreviewCreativeUnitItem.preview_id == preview.id,
            PreviewCreativeUnitItem.creative_unit_id == source.id,
        )
    ).all()
    added_units, added_members, added_mappings = [], [], []
    for index in range(4, unit_count + 1):
        identity = uuid4()
        added_units.append(
            PreviewCreativeUnit(
                **(source.model_dump() | {"id": identity, "position": index})
            )
        )
        for member in members:
            added_members.append(
                PreviewCreativeUnitItem(
                    **(member.model_dump() | {"creative_unit_id": identity})
                )
            )
        added_mappings.append(
            PreviewAdCreativeUnit(
                **shared,
                drama_id=drama.drama_id,
                group_no=1,
                base_ad_no=index,
                position=1,
                creative_unit_id=identity,
            )
        )
    session.add_all(added_units)
    session.flush()
    session.add_all(added_members)
    session.add_all(added_mappings)
    session.flush()
    mappings = session.exec(
        select(PreviewAdCreativeUnit)
        .where(
            PreviewAdCreativeUnit.preview_id == preview.id,
            PreviewAdCreativeUnit.base_ad_no == 1,
        )
        .order_by(PreviewAdCreativeUnit.position)
    ).all()
    for index, mapping in enumerate(mappings, 1):
        mapping.base_ad_no, mapping.position = index, 1
    session.flush()
    creatives = session.exec(
        select(PreviewCreativeUnit)
        .where(PreviewCreativeUnit.preview_id == preview.id)
        .order_by(PreviewCreativeUnit.position)
    ).all()
    for index, unit in enumerate(creatives, 1):
        session.add(
            PreviewGroupCreativeUnit(
                **shared,
                drama_id=drama.drama_id,
                group_no=1,
                position=index,
                creative_unit_id=unit.id,
            )
        )
    all_members = session.exec(
        select(PreviewCreativeUnitItem).where(
            PreviewCreativeUnitItem.preview_id == preview.id
        )
    ).all()
    missing = next(
        m.material_id for m in all_members if m.creative_unit_id == creatives[2].id
    )
    for identity in {m.material_id for m in all_members} - {missing}:
        session.add(
            AccountMaterial(
                tenant_id=context.tenant_id,
                bc_id=draft.bc_id,
                material_id=identity,
                advertiser_id="account-A",
                connection_id=scope.route.connection_id,
                media_type="IMAGE",
                video_id=None,
                image_id=str(identity),
                is_carousel_usable=True,
                status="available",
                verified_at=datetime.now(UTC),
                frozen_route=scope.route.model_dump(mode="json"),
            )
        )
    pool = CopyPoolVersion(name="async")
    session.add(pool)
    session.flush()
    copy = CopyEntry(pool_version_id=pool.id, position=1, text="Offline copy")
    session.add(copy)
    session.flush()
    for base in range(1, unit_count + 1):
        session.add(
            PreviewCopy(
                **shared,
                drama_id=drama.drama_id,
                group_no=1,
                base_ad_no=base,
                creative_no=1,
                copy_id=copy.id,
                text=copy.text,
            )
        )
    unit = BuildUnit(
        **shared,
        drama_id=drama.drama_id,
        advertiser_id="account-A",
        connection_id=scope.route.connection_id,
        currency="USD",
        timezone="UTC",
        campaign_name="synthetic",
        campaign_digest="d" * 64,
        scene_snapshot={
            "creative_limit": 50,
            "copy_length_limit": 100,
            "cta_fields": {"asset_ids": ["cta"]},
            "field_constraints": {
                "name_limits": dict.fromkeys(("campaign", "adgroup", "ad"), 512),
                "name_measurement": dict.fromkeys(
                    ("campaign", "adgroup", "ad"), "characters"
                ),
                "copy_measurement": "characters",
                "max_ads_per_adgroup": 10000,
            },
        },
    )
    session.add(unit)
    session.flush()
    save_preview_route(
        session, context=context, preview_id=preview.id, route=scope.route
    )
    preview.config = config().model_dump(mode="json")
    preview.batch_short_id = uuid4().hex[:4].upper()
    preview.progress = {
        "phase": "units",
        "digest": "",
        "naming_drama_id": "display",
        "targeting_region_codes": ["US"],
        "current_drama": str(drama.drama_id),
        "unit_id": str(unit.id),
        "group_after": 0,
        "account_after": "account-A",
        "drama_after": None,
    }
    session.flush()
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _ctx, _many):
        statements.append(statement)

    event.listen(session.bind, "before_cursor_execute", observe)
    started = monotonic()
    try:
        assert not previews.continue_preview(
            session, context=context, preview_id=preview.id, step_limit=1
        )
    finally:
        elapsed = monotonic() - started
        event.remove(session.bind, "before_cursor_execute", observe)
    record_property("large_carousel_expand", {
        "units": unit_count, "sql_count": len(statements), "seconds": elapsed,
    })
    assert len(statements) < 100, (len(statements), elapsed)
    assert elapsed < 10, elapsed
    facts = session.exec(
        select(PreviewAdReadiness).where(PreviewAdReadiness.preview_id == preview.id)
    ).all()
    assert len(facts) == unit_count and sum(f.included for f in facts) == unit_count - 1
    assert next(f for f in facts if not f.included).reason_codes == [
        "image_source_unavailable"
    ]
    assert unit.readiness == "PREPARING"
    assert "carousel_music_unverified" in unit.reason_codes
    # 查询资源可去重，冻结创意之间的复用和每个单元内的顺序不能改变。
    original_members = tuple(
        m.material_id for m in sorted(members, key=lambda m: m.position)
    )
    reused = frozen_ad_creative_units(
        session,
        tenant_id=context.tenant_id,
        preview_id=preview.id,
        drama_id=drama.drama_id,
        group_no=1,
        base_ad_no=unit_count,
    )
    assert len(reused) == 1
    assert tuple(m.material_id for m in reused[0].members) == original_members
    assert len(all_members) == unit_count * 3 - 2
    print(  # noqa: T201 - retain bounded worker benchmark evidence
        f"async {unit_count} units / {len(all_members)} members / {unit_count} Ads: {len(statements)} SQL, {elapsed:.3f}s"
    )


def test_preview_original_generation_lookup_is_batched(session, context, image_draft):
    from sqlalchemy import event

    from app.modules.builds.carousel_readiness import image_preview_readiness
    from tests.modules.materials.test_tenant_materials import material

    draft, _, scope = image_draft
    files = []
    for index in range(100):
        file = material(session, context, f"original-{index}.png", bc=draft.bc_id)
        file.media_type, file.current_object_generation = "IMAGE", 1
        files.append(file.id)
    session.flush()
    statements = []

    def observe(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    event.listen(session.bind, "before_cursor_execute", observe)
    try:
        result = image_preview_readiness(
            session,
            context=context,
            route=scope.route,
            advertiser_id="account-A",
            material_ids=files,
        )
    finally:
        event.remove(session.bind, "before_cursor_execute", observe)
    assert all(item.state == "blocked" for item in result.values())
    original_reads = [
        sql for sql in statements if "temporary_material_object" in sql.lower()
    ]
    assert len(original_reads) <= 1, len(original_reads)


def test_composition_preview_gate_tracks_actual_music_state(
    session, context, image_draft
):
    from app.modules.builds.image_composition_models import DraftCreativeUnit
    from app.modules.builds.image_composition_service import composition_state

    draft, _, scope = image_draft
    assert not composition_state(session, scope)["preview_ready"]
    frozen_case(session, context, image_draft)
    assert composition_state(session, scope)["preview_ready"]
    unit = session.exec(
        select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft.id)
    ).first()
    unit.music_status = "PENDING"
    session.flush()
    state = composition_state(session, scope)
    assert not state["preview_ready"] and state["preview_reason_codes"]


def test_frozen_per_ad_exclusion_cannot_change(carousel_case):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlmodel import Session

    engine, _, _, _, _, preview = carousel_case
    with Session(engine) as session, pytest.raises(DBAPIError):
        session.execute(
            text(
                "UPDATE preview_ad_readiness SET included=false, reason_codes='[\"late_failure\"]'::jsonb WHERE preview_id=:preview"
            ),
            {"preview": preview},
        )
        session.flush()


def test_submission_image_pages_allow_other_origin_bc_with_exact_target_ids(
    carousel_case,
):
    from sqlmodel import Session

    from app.modules.accounts.models import TenantBC
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.preview_models import PlannedGroup
    from app.modules.builds.submission_catalog import get_submission_materials
    from app.modules.materials.models import MaterialFile
    from app.modules.materials.repository import asset_public

    engine, context, route, submission, units, _ = carousel_case
    with Session(engine) as session, session.begin():
        session.add(TenantBC(tenant_id=context.tenant_id, bc_id="original-other-bc"))
        session.flush()
        mappings = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == route.bc_id,
            )
        ).all()
        by_target = {(m.advertiser_id, m.material_id): m for m in mappings}
        for identity in {m.material_id for m in mappings}:
            session.get(MaterialFile, identity).bc_id = "original-other-bc"
        for index, unit_id in enumerate(units):
            for step in session.exec(
                select(ExecutionStep).where(
                    ExecutionStep.unit_id == unit_id, ExecutionStep.kind == "MATERIAL"
                )
            ).all():
                mapping = by_target[(f"account-{'AB'[index]}", step.material_id)]
                step.resolved = {
                    "mapping": asset_public(mapping).model_dump(mode="json")
                }
    # 跨原件 BC 仍沿当前冻结账户映射准备整条广告，不借原件来源替换目标 ID。
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    assert all(
        item["web_uri"].startswith("account-A-")
        for item in body["creative_list"][0]["creative_info"]["image_info"]
    )
    with Session(engine) as session:
        for index, unit_id in enumerate(units):
            group = session.exec(
                select(PlannedGroup).where(PlannedGroup.unit_id == unit_id)
            ).one()
            cursor, collected, page_sizes = None, [], []
            while True:
                page = get_submission_materials(
                    session,
                    context=context,
                    submission_id=submission,
                    unit_id=unit_id,
                    group_id=group.id,
                    cursor=cursor,
                    limit=2,
                )
                assert page.total == 7
                collected.extend(page.items)
                page_sizes.append(len(page.items))
                if page.next_cursor is None:
                    break
                assert page.next_cursor != cursor
                cursor = page.next_cursor
            assert page_sizes == [2, 2, 2, 1]
            assert [item.position for item in collected] == list(range(1, 8))
            assert len({item.material_id for item in collected}) == 7
            for item in collected:
                assert item.media_type == "IMAGE" and item.video_id is None
                assert item.image_id == f"account-{'AB'[index]}-{item.material_id}"
