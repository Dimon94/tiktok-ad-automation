"""配乐从真实激活入口排队；平台只在 SDK 传输边界替换。"""

import json
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.modules.accounts.models import TikTokConnection
from app.modules.builds.carousel_music import advance_music, request_music
from app.modules.builds.carousel_music_models import MusicSelectionJob
from app.modules.builds.image_composition_models import DraftCreativeUnit
from app.modules.builds.image_composition_service import (
    adopt_candidates,
    refresh_candidates,
    resolve_image_scope,
)
from app.modules.builds.models import BuildDraft
from app.modules.materials.models import AccountMaterial
from tests.integrations.tiktok.test_music_adapters import envelope, music_row
from tests.migration_database import historical_database
from tests.modules.accounts.conftest import app_config as app_config  # noqa: F401
from tests.modules.accounts.conftest import policy as policy
from tests.modules.builds.test_drafts import create_intent
from tests.modules.builds.test_image_composition_migration import scaffold
from tests.modules.builds.test_image_composition_service import (  # noqa: F401
    apply_rule,
    finish_candidate_snapshot,
)
from tests.modules.builds.test_image_composition_service import (
    image_draft as image_draft,
)
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import material


def test_activation_enqueues_actual_music_jobs(session, image_draft):
    from sqlmodel import select

    from app.modules.builds.carousel_music_models import MusicSelectionJob

    units = apply_rule(session, image_draft)
    finish_music_preparation(session)
    jobs = session.exec(select(MusicSelectionJob)).all()
    assert len(jobs) == 2
    assert {j.targets[0]["unit_id"] for j in jobs} == {
        str(UUID(u["id"])) for u in units if len(u["ordered_material_ids"]) > 1
    }
    assert all(j.dispatch_id for j in jobs)


@pytest.fixture
def music_case(monkeypatch, policy, request):
    options = getattr(request, "param", {})
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    with historical_database(monkeypatch, "head") as (engine, _):
        with Session(engine) as session:
            context = create_context(session)
            intent = create_intent(session, context)
            intent["drama_lines"] = ["Moon"]
            draft, drama = scaffold(session, context, intent)
            scope = resolve_image_scope(session, context=context, draft_id=draft.id)
            connection = session.get(TikTokConnection, scope.route.connection_id)
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=context.tenant_id,
                value={"access_token": "synthetic", "scope": "[2,6]"},
            )
            from app.modules.accounts.connection_models import ConnectionAuthorization

            auth = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == scope.route.connection_id
                )
            ).one()
            auth.scopes = ["6"]
            for index in range(options.get("images", 7)):
                row = material(session, context, f"Moon_{index}.jpg", bc="bc-draft")
                row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
                row.width, row.height = 1080, 1920
                session.flush()
                session.add(
                    AccountMaterial(
                        tenant_id=context.tenant_id,
                        bc_id=scope.bc_id,
                        material_id=row.id,
                        advertiser_id="account-A",
                        connection_id=scope.route.connection_id,
                        media_type="IMAGE",
                        video_id=None,
                        image_id=f"image-{index}",
                        mid=f"mid-{index}",
                        is_carousel_usable=True,
                        status="available",
                        verified_at=datetime.now(UTC),
                        frozen_route=scope.route.model_dump(mode="json"),
                    )
                )
            session.flush()
            progress = refresh_candidates(session, scope, uuid4(), draft.revision)
            progress = finish_candidate_snapshot(session, scope, progress)
            adopt_candidates(
                session, scope, uuid4(), draft.revision, progress["snapshot_id"]
            )
            apply_rule(session, (draft, drama, scope))
            if options.get("prepare", True):
                finish_music_preparation(session)
            session.commit()
        yield engine, scope


@pytest.fixture
def wire(monkeypatch):
    state = {
        "calls": [],
        "rows": [music_row("recommended", duration=15)],
        "hook": None,
        "error": None,
        "total": None,
    }

    def http(_pool, _method, url, **_kwargs):
        query = parse_qs(urlsplit(url).query)
        state["calls"].append((urlsplit(url).path, query))
        if "/music/" in url:
            if state["hook"]:
                state["hook"]()
            if state["error"]:
                raise state["error"]
            body = envelope(
                state["rows"], page=int(query["page"][0]), total=state["total"]
            )
        elif "share" in url:
            body = {"code": 0, "data": {}}
        else:
            identity = json.loads(query["image_ids"][0])[0]
            body = {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "image_id": identity,
                            "material_id": identity.replace("image-", "mid-"),
                            "width": 1080,
                            "height": 1920,
                            "is_carousel_usable": True,
                            "image_url": f"https://images.example.test/{identity}?fresh=secret",
                        }
                    ]
                },
            }
        return HTTPResponse(
            body=BytesIO(json.dumps(body).encode()), status=200, preload_content=False
        )

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", http)
    return state


def job_ids(engine):
    with Session(engine) as session:
        return session.exec(
            select(MusicSelectionJob.id).order_by(MusicSelectionJob.id)
        ).all()


def run_job(engine, redis, identity):
    with Session(engine) as session:
        generation = session.get(MusicSelectionJob, identity).generation
    return advance_music(
        identity, generation, database_engine=engine, redis_client=redis
    )


def test_real_recommend_fresh_urls_first_window_and_no_draft_revision(
    music_case, wire, redis_client
):
    engine, scope = music_case
    with Session(engine) as session:
        revision = session.get(BuildDraft, scope.draft_id).revision
    wire["total"] = 101
    wire["rows"] = [music_row(f"zero-{i}", duration=0) for i in range(99)] + [
        music_row("recommended", duration=15)
    ]
    result = run_job(engine, redis_client, job_ids(engine)[0])
    assert result["status"] == "READY", result
    music_calls = [q for path, q in wire["calls"] if "/music/" in path]
    assert len(music_calls) == 1
    assert len(json.loads(music_calls[0]["filtering"][0])["image_urls"]) == 3
    with Session(engine) as session:
        job = session.get(MusicSelectionJob, UUID(result["job_id"]))
        assert job.seed_advertiser_id == "account-A"
        assert "secret" not in json.dumps(job.model_dump(mode="json"))
        assert session.get(BuildDraft, scope.draft_id).revision == revision
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        assert unit.music_id == "recommended" and unit.music_revision > 0


def test_manual_choice_wins_late_recommendation_and_replay(
    music_case, wire, redis_client
):
    engine, scope = music_case
    identity = job_ids(engine)[0]
    request = uuid4()

    def choose():
        with Session(engine) as session, session.begin():
            job = session.get(MusicSelectionJob, identity)
            unit = session.get(
                DraftCreativeUnit,
                (
                    scope.tenant_id,
                    scope.draft_id,
                    job.active_generation,
                    UUID(job.targets[0]["unit_id"]),
                ),
            )
            args = (
                session,
                scope,
                unit.id,
                unit.revision,
                unit.music_revision,
                request,
                "SET_MANUAL",
                "manual",
            )
            first = request_music(*args)
            assert request_music(*args) == first

    wire["hook"] = choose
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    with Session(engine) as session:
        job = session.get(MusicSelectionJob, identity)
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        assert unit.music_id == "manual" and unit.music_protected


@pytest.mark.parametrize("when", ["before", "during"])
def test_current_build_role_required_before_work_and_publication(
    music_case, wire, redis_client, when
):
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case

    def revoke():
        with Session(engine) as session, session.begin():
            session.get(
                TenantMembership, (scope.tenant_id, scope.actor_id)
            ).role = "viewer"

    if when == "before":
        revoke()
    else:
        wire["hook"] = revoke
    result = run_job(engine, redis_client, job_ids(engine)[0])
    assert result["status"] == "FAILED"
    with Session(engine) as session:
        assert not session.exec(
            select(DraftCreativeUnit).where(DraftCreativeUnit.music_id == "recommended")
        ).all()
    if when == "before":
        assert not wire["calls"]
    # 任务和单元必须一起结束，否则恢复权限后既不计失败也无法重试。
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, UUID(result["job_id"]))
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        assert unit.music_status == "FAILED"
        session.get(
            TenantMembership, (scope.tenant_id, scope.actor_id)
        ).role = "operator"
        session.flush()
        retried = request_music(
            session,
            scope,
            unit.id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "RETRY",
            None,
        )
        retry_id = UUID(retried["job_ids"][0])
    wire["hook"] = None
    assert run_job(engine, redis_client, retry_id)["status"] == "READY"


def test_zero_duration_needs_selection_not_invalid(music_case, wire, redis_client):
    wire["rows"] = [music_row("zero", duration=0)]
    engine, scope = music_case
    identity = job_ids(engine)[0]
    result = run_job(engine, redis_client, identity)
    assert result["error_code"] == "music_selection_required"
    with Session(engine) as session:
        job = session.get(MusicSelectionJob, identity)
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        assert unit.music_status == "NEEDS_SELECTION" and unit.music_id is None


def test_activation_only_enqueues_bounded_preparation(session, image_draft):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation

    apply_rule(session, image_draft)
    runs = session.exec(select(MusicPreparationRun)).all()
    assert len(runs) == 1
    assert not session.exec(select(MusicSelectionJob)).all()
    assert runs[0].dispatch_id
    result = advance_music_preparation(session, runs[0].id, runs[0].generation)
    assert result["status"] == "READY"
    assert len(session.exec(select(MusicSelectionJob)).all()) == 2


def finish_music_preparation(session):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation

    for run in session.exec(
        select(MusicPreparationRun).where(MusicPreparationRun.status == "PENDING")
    ).all():
        while run.status == "PENDING":
            advance_music_preparation(session, run.id, run.generation)


@pytest.mark.parametrize("mode", ["FIXED", "SINGLE"])
def test_manual_new_unit_uses_configured_song_without_recommend(
    music_case, wire, redis_client, mode
):
    from app.modules.builds.image_composition_commands import Create
    from app.modules.builds.image_composition_schemas import ImageRule
    from app.modules.builds.image_composition_service import (
        _unit_value,
        edit_creative_units,
    )
    from app.modules.builds.image_composition_state import ImageCompositionState

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        state = session.get(ImageCompositionState, (scope.tenant_id, scope.draft_id))
        state.rule = (
            ImageRule(music_mode="FIXED", fixed_music_id="configured").model_dump(
                mode="json"
            )
            if mode == "FIXED"
            else ImageRule(single_music_id="configured").model_dump(mode="json")
        )
        unit = session.exec(
            select(DraftCreativeUnit).where(DraftCreativeUnit.music_status == "PENDING")
        ).first()
        ids = _unit_value(session, unit).ordered_material_ids
        edit_creative_units(
            session,
            scope,
            uuid4(),
            draft.revision,
            Create(
                kind="CREATE",
                drama_id=unit.drama_id,
                material_ids=ids if mode == "FIXED" else ids[:1],
                allow_reuse=True,
            ),
        )
        finish_music_preparation(session)
        identity = session.exec(
            select(MusicSelectionJob.id).where(
                MusicSelectionJob.parameters["music_id"].astext == "configured"
            )
        ).first()
    wire["rows"] = [music_row("configured", duration=0)]
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    assert len(wire["calls"]) == 1
    assert wire["calls"][0][1]["search_type"] == ["SEARCH_BY_MUSIC_ID"]


def test_timeout_retries_bounded_and_explicit_retry_replays(
    music_case, wire, redis_client
):
    from urllib3.exceptions import ReadTimeoutError

    engine, scope = music_case
    identity = job_ids(engine)[0]
    wire["error"] = ReadTimeoutError(None, None, "synthetic")
    for attempt in range(3):
        result = run_job(engine, redis_client, identity)
        assert result["attempts"] == attempt + 1
    assert result["status"] == "FAILED" and result["error_code"] == "music_timeout"
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        args = (
            session,
            scope,
            unit.id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "RETRY",
        )
        first = request_music(*args)
        assert request_music(*args) == first
        retry_id = UUID(first["job_ids"][0])
        assert retry_id != identity
    wire["error"] = None
    assert run_job(engine, redis_client, retry_id)["status"] == "READY"


def test_worker_claim_duplicate_repair_and_redis_replay(
    music_case, wire, redis_client, redis_key_prefix
):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.builds.carousel_music_tasks import process_music, repair_music

    engine, scope = music_case
    identity = job_ids(engine)[0]
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        job.status, job.claim = "RUNNING", uuid4()
        job.repair_after = datetime.now(UTC) + timedelta(seconds=60)
    assert run_job(engine, redis_client, identity)["status"] == "RUNNING"
    assert not wire["calls"]
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        dispatch.published_at = datetime.now(UTC)
        payload, original = dispatch.payload, dispatch.id
    assert repair_music(database_engine=engine) == 1
    with Session(engine) as session:
        dispatch = session.get(PendingDispatch, original)
        assert dispatch.published_at is None and dispatch.payload == payload
    queue = redis_key_prefix + ":music"
    try:
        redis_client.rpush(queue, json.dumps(payload), json.dumps(payload))
        for _ in range(2):
            process_music(
                database_engine=engine,
                redis_client=redis_client,
                tenant_id=scope.tenant_id,
                actor_id=scope.actor_id,
                payload=json.loads(redis_client.lpop(queue)),
            )
    finally:
        redis_client.delete(queue)
    assert len([path for path, _ in wire["calls"] if "/music/" in path]) == 1


def test_2000_combinations_100_accounts_bounded_deduped_jobs(music_case, wire):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import (
        advance_music_preparation,
        queue_music_preparation,
    )
    from app.modules.builds.image_composition import music_rule_digest
    from app.modules.builds.image_composition_models import DraftCreativeUnitItem
    from app.modules.builds.image_composition_schemas import ImageRule
    from app.modules.builds.models import DraftAccount
    from app.modules.materials.models import MaterialFile

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        sample = session.exec(select(DraftCreativeUnit)).first()
        draft.active_image_generation = 2
        for index in range(99):
            name = f"target-{index}"
            session.add(
                AdvertiserAccount(tenant_id=scope.tenant_id, advertiser_id=name)
            )
            session.flush()
            session.add(
                BCAccountAccess(
                    tenant_id=scope.tenant_id,
                    bc_id=scope.bc_id,
                    advertiser_id=name,
                    connection_id=scope.route.connection_id,
                )
            )
            session.flush()
            session.add(
                DraftAccount(
                    tenant_id=scope.tenant_id,
                    draft_id=scope.draft_id,
                    advertiser_id=name,
                    bc_id=scope.bc_id,
                    connection_id=scope.route.connection_id,
                    currency="USD",
                    timezone="UTC",
                    first_line=index + 2,
                )
            )
        rule_digest = music_rule_digest(ImageRule(images_per_unit=5), 5)
        materials = []
        units = []
        memberships = []
        first_ids = None
        for index in range(2001):
            ids = [uuid4() for _ in range(5)] if index < 2000 else first_ids
            if index == 0:
                first_ids = ids
            if index < 2000:
                for position, identity in enumerate(ids):
                    materials.append(
                        MaterialFile(
                            id=identity,
                            tenant_id=scope.tenant_id,
                            bc_id=scope.bc_id,
                            media_type="IMAGE",
                            file_name=f"Moon-{index}-{position}.jpg",
                            object_key=f"synthetic/{identity}",
                            byte_size=100,
                            storage_state="stored",
                            sha256=f"{index * 5 + position:064x}",
                        )
                    )
            unit = DraftCreativeUnit(
                tenant_id=scope.tenant_id,
                draft_id=scope.draft_id,
                generation=2,
                bc_id=scope.bc_id,
                drama_id=sample.drama_id,
                kind="CAROUSEL",
                position=index + 1,
                content_digest=f"{index if index < 2000 else 0:064x}",
                music_status="PENDING",
                music_rule_digest=rule_digest,
                music_request_generation=1,
            )
            units.append(unit)
            memberships.extend(
                DraftCreativeUnitItem(
                    tenant_id=scope.tenant_id,
                    draft_id=scope.draft_id,
                    generation=2,
                    unit_id=unit.id,
                    position=position + 1,
                    bc_id=scope.bc_id,
                    drama_id=sample.drama_id,
                    material_id=identity,
                    kind="CAROUSEL",
                    media_type="IMAGE",
                )
                for position, identity in enumerate(ids)
            )
        session.add_all(materials)
        session.flush()
        session.add_all(units)
        session.flush()
        session.add_all(memberships)
        run_id = queue_music_preparation(session, scope, draft)
    # 实际事务回滚模拟扫描进程在提交前中断；恢复仍从原持久游标开始。
    with Session(engine) as session:
        result = advance_music_preparation(session, run_id, 1)
        assert result["processed"] == 20 and result["status"] == "PENDING"
        session.rollback()
    chunks = []
    for _ in range(102):
        with Session(engine) as session, session.begin():
            run = session.get(MusicPreparationRun, run_id)
            before = run.processed
            result = advance_music_preparation(session, run_id, run.generation)
            chunks.append(result["processed"] - before)
            if result["status"] == "READY":
                break
    assert max(chunks) == 20 and sum(chunks) == 2001
    with Session(engine) as session:
        jobs = session.exec(
            select(MusicSelectionJob).where(MusicSelectionJob.active_generation == 2)
        ).all()
        assert len(jobs) == 2000 and sum(len(j.targets) for j in jobs) == 2001
        assert len(session.exec(select(DraftAccount)).all()) == 100
    assert not wire["calls"]


def test_source_image_preparation_waits_for_true_target_readback(
    music_case, wire, redis_client, monkeypatch, policy
):
    from app.modules.materials.image_distribution import run_image_distribution
    from app.modules.materials.models import MaterialDistribution
    from tests.modules.builds.test_drafts import account

    engine, scope = music_case
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {"base": {**policy.model_dump(), "lease_ms": 970000}},
    )
    identity = job_ids(engine)[0]
    with Session(engine) as session, session.begin():
        account(session, scope.context, identity="account-B")
        job = session.get(MusicSelectionJob, identity)
        target_material = UUID(job.material_ids[0])
        source = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == target_material
            )
        ).one()
        source.advertiser_id = "account-B"
    result = run_job(engine, redis_client, identity)
    assert result["status"] == "PENDING" and result["attempts"] == 0
    assert not wire["calls"]
    with Session(engine) as session:
        dist_id = session.exec(
            select(MaterialDistribution.id).where(
                MaterialDistribution.material_id == target_material
            )
        ).one()
    run_image_distribution(
        database_engine=engine,
        redis_client=redis_client,
        context=scope.context,
        distribution_id=dist_id,
        kind="prepare",
    )
    with Session(engine) as session:
        assert session.get(MaterialDistribution, dist_id).status == "verifying"
    assert run_job(engine, redis_client, identity)["attempts"] == 0
    assert not any("/music/" in p for p, _ in wire["calls"])
    run_image_distribution(
        database_engine=engine,
        redis_client=redis_client,
        context=scope.context,
        distribution_id=dist_id,
        kind="verify",
    )
    with Session(engine) as session:
        assert session.get(MaterialDistribution, dist_id).status == "ready"
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    assert len([p for p, _ in wire["calls"] if "/music/" in p]) == 1


def test_old_generation_result_only_reused_through_new_job(
    music_case, wire, redis_client
):
    from app.modules.builds.carousel_music_tasks import queue_music_preparation
    from app.modules.builds.image_composition_models import DraftCreativeUnitItem

    engine, scope = music_case
    identity = job_ids(engine)[0]

    def switch():
        with Session(engine) as session, session.begin():
            draft = session.get(BuildDraft, scope.draft_id)
            job = session.get(MusicSelectionJob, identity)
            unit = session.get(
                DraftCreativeUnit,
                (scope.tenant_id, scope.draft_id, 1, UUID(job.targets[0]["unit_id"])),
            )
            session.add(
                DraftCreativeUnit(
                    **{**unit.model_dump(), "generation": 2, "music_status": "PENDING"}
                )
            )
            session.flush()
            items = session.exec(
                select(DraftCreativeUnitItem).where(
                    DraftCreativeUnitItem.unit_id == unit.id
                )
            ).all()
            for item in items:
                session.add(
                    DraftCreativeUnitItem(**{**item.model_dump(), "generation": 2})
                )
            draft.active_image_generation = 2
            queue_music_preparation(session, scope, draft)
            finish_music_preparation(session)

    wire["hook"] = switch
    assert run_job(engine, redis_client, identity)["status"] == "CANCELLED"
    with Session(engine) as session:
        old = session.get(MusicSelectionJob, identity)
        new = session.exec(
            select(MusicSelectionJob).where(MusicSelectionJob.active_generation == 2)
        ).one()
        new_id = new.id
        assert (
            old.fingerprint == new.fingerprint
            and old.result["music_id"] == "recommended"
        )
        unit = session.get(
            DraftCreativeUnit,
            (scope.tenant_id, scope.draft_id, 2, UUID(new.targets[0]["unit_id"])),
        )
        assert unit.music_id is None
    wire["hook"] = None
    assert run_job(engine, redis_client, new_id)["status"] == "READY"
    assert len([p for p, _ in wire["calls"] if "/music/" in p]) == 1


def test_explicit_recommend_changes_song_once_and_preserves_replay(
    music_case, wire, redis_client
):
    engine, scope = music_case
    identity = job_ids(engine)[0]
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        args = (
            scope,
            unit.id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "RECOMMEND",
        )
        result = request_music(session, *args)
        new_id = UUID(result["job_ids"][0])
    wire["rows"] = [music_row("new-song", duration=12)]
    assert run_job(engine, redis_client, new_id)["status"] == "READY"
    with Session(engine) as session, session.begin():
        assert request_music(session, *args) == result
        unit = session.get(
            DraftCreativeUnit,
            (scope.tenant_id, scope.draft_id, 1, args[1]),
        )
        assert unit.music_id == "new-song"
    assert run_job(engine, redis_client, new_id)["status"] == "READY"
    assert len([p for p, _ in wire["calls"] if "/music/" in p]) == 2


def test_pending_explicit_intent_survives_unchanged_real_regeneration(
    music_case, wire, redis_client
):
    from app.modules.builds.image_composition_schemas import ImageRule
    from app.modules.builds.image_composition_service import (
        advance_composition,
        apply_composition,
        preview_composition,
    )

    engine, scope = music_case
    identity = job_ids(engine)[0]
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        unit_id = UUID(job.targets[0]["unit_id"])
        unit = session.get(
            DraftCreativeUnit, (scope.tenant_id, scope.draft_id, 1, unit_id)
        )
        accepted = request_music(
            session,
            scope,
            unit_id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "RECOMMEND",
        )
        explicit_id = UUID(accepted["job_ids"][0])

    def regenerate():
        with Session(engine) as session, session.begin():
            draft = session.get(BuildDraft, scope.draft_id)
            plan = preview_composition(
                session,
                scope,
                uuid4(),
                draft.revision,
                ImageRule(),
                {},
                "PRESERVE_MANUAL",
            )
            run = apply_composition(
                session, scope, plan["plan_id"], uuid4(), draft.revision
            )
            assert (
                advance_composition(session, run["run_id"], run["generation"])["status"]
                == "APPLIED"
            )
            finish_music_preparation(session)

    wire["hook"] = regenerate
    wire["rows"] = [music_row("explicit-new", duration=10)]
    assert run_job(engine, redis_client, explicit_id)["status"] == "CANCELLED"
    with Session(engine) as session:
        old = session.get(MusicSelectionJob, explicit_id)
        new = session.exec(
            select(MusicSelectionJob).where(
                MusicSelectionJob.active_generation == 2,
                MusicSelectionJob.fingerprint == old.fingerprint,
            )
        ).one()
        new_id = new.id
    wire["hook"] = None
    assert run_job(engine, redis_client, new_id)["status"] == "READY"
    with Session(engine) as session:
        assert (
            session.get(
                DraftCreativeUnit, (scope.tenant_id, scope.draft_id, 2, unit_id)
            ).music_id
            == "explicit-new"
        )
    assert len([p for p, _ in wire["calls"] if "/music/" in p]) == 2


def test_preparation_repair_reuses_dispatch_and_stale_generation_is_noop(music_case):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import (
        advance_music_preparation,
        queue_music_preparation,
        repair_music_preparation,
    )

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        identity = queue_music_preparation(session, scope, draft)
        run = session.get(MusicPreparationRun, identity)
        run.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = session.get(PendingDispatch, run.dispatch_id)
        original, payload = dispatch.id, dispatch.payload
        dispatch.published_at = datetime.now(UTC)
    assert repair_music_preparation(database_engine=engine) == 1
    with Session(engine) as session, session.begin():
        dispatch = session.get(PendingDispatch, original)
        assert dispatch.published_at is None and dispatch.payload == payload
        assert advance_music_preparation(session, identity, 99)["processed"] == 0
        assert advance_music_preparation(session, identity, 1)["status"] == "READY"
        assert len(session.exec(select(MusicSelectionJob)).all()) == 2


def test_schema_upgrade_preserves_selected_unit_and_requires_draft_scope(monkeypatch):
    from alembic import command
    from sqlalchemy.exc import IntegrityError

    from tests.modules.builds.test_image_composition_migration import (
        historical_scaffold,
        unit,
    )

    with historical_database(monkeypatch, "0052_image_candidate_build") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            context = create_context(session)
            draft, drama = historical_scaffold(
                session, context, create_intent(session, context)
            )
            existing = unit(draft, drama, music_id="existing", music_revision=3)
            existing.music_status = "READY"
            session.add(existing)
            identity, draft_id, revision = existing.id, draft.id, draft.revision
        command.upgrade(config, "head")
        with Session(engine) as session:
            restored = session.get(
                DraftCreativeUnit, (context.tenant_id, draft_id, 1, identity)
            )
            assert (restored.music_id, restored.music_revision) == ("existing", 3)
            assert session.get(BuildDraft, draft_id).revision == revision
            assert not session.exec(select(MusicSelectionJob)).all()
            session.add(
                MusicSelectionJob(
                    tenant_id=uuid4(),
                    draft_id=draft_id,
                    bc_id="foreign",
                    actor_id=context.actor_id,
                    active_generation=1,
                    fingerprint="a" * 64,
                    route={},
                    parameters={},
                    material_ids=[],
                    targets=[],
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()


def test_deduped_job_skips_stale_twenty_subscribers_then_serves_remaining(
    music_case, wire, redis_client
):
    from app.modules.builds.image_composition_commands import Duplicate
    from app.modules.builds.image_composition_service import edit_creative_units

    engine, scope = music_case
    identity = job_ids(engine)[0]
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        unit_id = UUID(job.targets[0]["unit_id"])
        draft = session.get(BuildDraft, scope.draft_id)
        for _ in range(21):
            edit_creative_units(
                session,
                scope,
                uuid4(),
                draft.revision,
                Duplicate(kind="DUPLICATE", unit_id=unit_id),
            )
        finish_music_preparation(session)
        assert len(job.targets) == 22
        for target in job.targets[:20]:
            unit = session.get(
                DraftCreativeUnit,
                (scope.tenant_id, scope.draft_id, 1, UUID(target["unit_id"])),
            )
            request_music(
                session,
                scope,
                unit.id,
                unit.revision,
                unit.music_revision,
                uuid4(),
                "SET_MANUAL",
                "manual",
            )
    first = run_job(engine, redis_client, identity)
    assert first["status"] == "PENDING" and first["processed"] == 20
    assert not wire["calls"]
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    assert len([p for p, _ in wire["calls"] if "/music/" in p]) == 1


@pytest.fixture
def fixed_music_case(music_case):
    from app.modules.builds.image_composition_schemas import ImageRule
    from app.modules.builds.models import DraftDrama

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        drama = session.exec(
            select(DraftDrama).where(DraftDrama.draft_id == draft.id)
        ).one()
        units = apply_rule(
            session,
            (draft, drama, scope),
            ImageRule(music_mode="FIXED", fixed_music_id="configured"),
        )
        multi = next(unit for unit in units if len(unit["ordered_material_ids"]) > 1)
        unit_id = UUID(multi["id"])
    return engine, scope, unit_id


def test_fixed_multi_rejects_explicit_recommend_without_mutation(
    fixed_music_case, wire
):
    from app.core.errors import DomainError

    engine, scope, unit_id = fixed_music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        unit = session.get(
            DraftCreativeUnit,
            (scope.tenant_id, scope.draft_id, draft.active_image_generation, unit_id),
        )
        before, revision = unit.model_dump(), draft.revision
        jobs_before = session.exec(select(MusicSelectionJob.id)).all()
        with pytest.raises(DomainError) as error:
            request_music(
                session,
                scope,
                unit_id,
                unit.revision,
                unit.music_revision,
                uuid4(),
                "RECOMMEND",
            )
        assert error.value.code == "music_request_invalid"
        assert unit.model_dump() == before and draft.revision == revision
        assert session.exec(select(MusicSelectionJob.id)).all() == jobs_before
        # FIXED 仅禁止推荐；显式手工指定仍可覆写并保护该单元。
        request_music(
            session,
            scope,
            unit_id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "SET_MANUAL",
            "manual",
        )
        assert unit.music_id == "manual" and unit.music_protected
    assert not wire["calls"]


def test_cancelled_job_requeues_new_same_content_unit_from_real_edit(
    music_case, wire, redis_client
):
    from app.modules.builds.image_composition_commands import Create, SetMusic
    from app.modules.builds.image_composition_service import (
        _unit_value,
        edit_creative_units,
    )

    engine, scope = music_case
    identity = job_ids(engine)[0]
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        original_id = UUID(job.targets[0]["unit_id"])
        original = session.get(
            DraftCreativeUnit,
            (scope.tenant_id, scope.draft_id, job.active_generation, original_id),
        )
        material_ids = _unit_value(session, original).ordered_material_ids
        drama_id = original.drama_id
        draft = session.get(BuildDraft, scope.draft_id)
        edit_creative_units(
            session,
            scope,
            uuid4(),
            draft.revision,
            SetMusic(kind="SET_MUSIC", unit_id=original_id, music_id="manual"),
        )
        finish_music_preparation(session)
    cancelled = run_job(engine, redis_client, identity)
    assert cancelled["status"] == "CANCELLED" and not wire["calls"]
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        created = edit_creative_units(
            session,
            scope,
            uuid4(),
            draft.revision,
            Create(
                kind="CREATE",
                drama_id=drama_id,
                material_ids=material_ids,
                allow_reuse=True,
            ),
        )
        new_id = UUID(created["affected_units"][0])
        finish_music_preparation(session)
        job = session.get(MusicSelectionJob, identity)
        assert job.status == "PENDING" and job.generation > cancelled["generation"]
        assert len(job.targets) == 2 and job.cursor == 1
    stale = advance_music(
        identity,
        cancelled["generation"],
        database_engine=engine,
        redis_client=redis_client,
    )
    assert stale["status"] == "PENDING" and not wire["calls"]
    assert run_job(engine, redis_client, identity)["status"] == "READY"
    with Session(engine) as session:
        original = session.get(
            DraftCreativeUnit, (scope.tenant_id, scope.draft_id, 1, original_id)
        )
        new = session.get(
            DraftCreativeUnit, (scope.tenant_id, scope.draft_id, 1, new_id)
        )
        assert original.music_id == "manual" and original.music_protected
        assert new.music_id == "recommended" and new.music_status == "READY"
    assert len([path for path, _ in wire["calls"] if "/music/" in path]) == 1


def test_failed_fixed_retry_reads_configured_song_without_recommend(
    fixed_music_case, wire, redis_client
):
    from app.modules.builds.image_composition_commands import Create
    from app.modules.builds.image_composition_service import (
        _unit_value,
        edit_creative_units,
    )

    engine, scope, unit_id = fixed_music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        generation = draft.active_image_generation
        unit = session.get(
            DraftCreativeUnit, (scope.tenant_id, scope.draft_id, generation, unit_id)
        )
        result = edit_creative_units(
            session,
            scope,
            uuid4(),
            draft.revision,
            Create(
                kind="CREATE",
                drama_id=unit.drama_id,
                material_ids=_unit_value(session, unit).ordered_material_ids,
                allow_reuse=True,
            ),
        )
        created_id = UUID(result["affected_units"][0])
        finish_music_preparation(session)
        job = session.exec(
            select(MusicSelectionJob).where(
                MusicSelectionJob.active_generation == generation
            )
        ).one()
        failed_id = job.id
    wire["rows"] = []
    assert run_job(engine, redis_client, failed_id)["status"] == "FAILED"
    with Session(engine) as session, session.begin():
        unit = session.get(
            DraftCreativeUnit, (scope.tenant_id, scope.draft_id, generation, created_id)
        )
        assert unit.music_status == "FAILED"
        retried = request_music(
            session,
            scope,
            created_id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "RETRY",
        )
        retry_id = UUID(retried["job_ids"][0])
        assert session.get(MusicSelectionJob, retry_id).parameters == {
            "mode": "FIXED",
            "music_id": "configured",
        }
    wire["rows"] = [music_row("configured", duration=0)]
    assert run_job(engine, redis_client, retry_id)["status"] == "READY"
    assert len(wire["calls"]) == 2
    assert all(
        query["search_type"] == ["SEARCH_BY_MUSIC_ID"] for _, query in wire["calls"]
    )
    assert all(
        json.loads(query["filtering"][0])["music_ids"] == ["configured"]
        for _, query in wire["calls"]
    )


def test_selected_retry_repair_and_fixed_rule_only_reads_configured_song(
    fixed_music_case, wire, redis_client
):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import (
        advance_music_preparation,
        repair_music_preparation,
    )
    from app.modules.builds.image_music_batch import (
        batch_progress,
        recent_batches,
        start_music_retry,
    )

    engine, scope, unit_id = fixed_music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        unit = session.get(
            DraftCreativeUnit,
            (scope.tenant_id, draft.id, draft.active_image_generation, unit_id),
        )
        unit.music_status = "FAILED"
        result = start_music_retry(
            session, scope, uuid4(), draft.revision, [unit.drama_id]
        )
        run_id = UUID(result["run_id"])
        run = session.get(MusicPreparationRun, run_id)
        run.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = session.get(PendingDispatch, run.dispatch_id)
        dispatch.published_at = datetime.now(UTC)
        original_dispatch = dispatch.id
        revision = draft.revision
    assert repair_music_preparation(database_engine=engine) == 1
    with Session(engine) as session, session.begin():
        assert session.get(PendingDispatch, original_dispatch).published_at is None
        run = session.get(MusicPreparationRun, run_id)
        assert run.dispatch_id == original_dispatch
        advance_music_preparation(session, run_id, run.generation)
        progress = batch_progress(session, scope, run_id)
        assert progress["status"] == "READY" and progress["retried"] == 1
        assert recent_batches(session, scope)[0]["run_id"] == str(run_id)
        assert session.get(BuildDraft, scope.draft_id).revision == revision
        jobs = session.exec(
            select(MusicSelectionJob).where(
                MusicSelectionJob.active_generation
                == session.get(BuildDraft, scope.draft_id).active_image_generation
            )
        ).all()
        assert jobs and all(
            job.parameters["mode"] == "FIXED"
            and job.parameters["music_id"] == "configured"
            for job in jobs
        )
        job_id = jobs[-1].id
    advance_music(job_id, 1, database_engine=engine, redis_client=redis_client)
    music_paths = [path for path, _ in wire["calls"] if "/music/" in path]
    assert music_paths and all("/music/get/" in path for path in music_paths)


def test_revoked_music_failure_is_bounded_preserves_manual_and_can_batch_retry(
    music_case, wire, redis_client
):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation
    from app.modules.builds.image_composition_commands import Duplicate
    from app.modules.builds.image_composition_service import edit_creative_units
    from app.modules.builds.image_music_batch import batch_progress, start_music_retry
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case
    identity = job_ids(engine)[0]
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        original = UUID(job.targets[0]["unit_id"])
        draft = session.get(BuildDraft, scope.draft_id)
        for _ in range(21):
            edit_creative_units(
                session,
                scope,
                uuid4(),
                draft.revision,
                Duplicate(kind="DUPLICATE", unit_id=original),
            )
        finish_music_preparation(session)
        assert len(job.targets) == 22
        unit = session.get(
            DraftCreativeUnit,
            (scope.tenant_id, scope.draft_id, job.active_generation, original),
        )
        request_music(
            session,
            scope,
            unit.id,
            unit.revision,
            unit.music_revision,
            uuid4(),
            "SET_MANUAL",
            "manual",
        )
        session.get(TenantMembership, (scope.tenant_id, scope.actor_id)).role = "viewer"
    first = run_job(engine, redis_client, identity)
    assert first["status"] == "PENDING" and first["processed"] == 20
    second = run_job(engine, redis_client, identity)
    assert second["status"] == "FAILED" and second["processed"] == 22
    assert not wire["calls"]
    with Session(engine) as session, session.begin():
        job = session.get(MusicSelectionJob, identity)
        units = [
            session.get(
                DraftCreativeUnit,
                (
                    scope.tenant_id,
                    scope.draft_id,
                    job.active_generation,
                    UUID(t["unit_id"]),
                ),
            )
            for t in job.targets
        ]
        assert sum(u.music_status == "FAILED" for u in units) == 21
        selected = next(u for u in units if u.id == original)
        assert (
            selected.music_status == "READY"
            and selected.music_id == "manual"
            and selected.music_protected
        )
        session.get(
            TenantMembership, (scope.tenant_id, scope.actor_id)
        ).role = "operator"
        session.flush()
        draft = session.get(BuildDraft, scope.draft_id)
        result = start_music_retry(
            session, scope, uuid4(), draft.revision, [selected.drama_id]
        )
        run = session.get(MusicPreparationRun, UUID(result["run_id"]))
        while run.status == "PENDING":
            advance_music_preparation(session, run.id, run.generation)
        assert batch_progress(session, scope, run.id)["retried"] == 21


def test_late_revoked_failure_does_not_overwrite_new_manual_song(
    music_case, wire, redis_client
):
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case
    identity = job_ids(engine)[0]

    def choose_and_revoke():
        with Session(engine) as session, session.begin():
            job = session.get(MusicSelectionJob, identity)
            unit = session.get(
                DraftCreativeUnit,
                (
                    scope.tenant_id,
                    scope.draft_id,
                    job.active_generation,
                    UUID(job.targets[0]["unit_id"]),
                ),
            )
            request_music(
                session,
                scope,
                unit.id,
                unit.revision,
                unit.music_revision,
                uuid4(),
                "SET_MANUAL",
                "manual",
            )
            session.get(
                TenantMembership, (scope.tenant_id, scope.actor_id)
            ).role = "viewer"

    wire["hook"] = choose_and_revoke
    assert run_job(engine, redis_client, identity)["status"] == "FAILED"
    with Session(engine) as session:
        job = session.get(MusicSelectionJob, identity)
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                job.active_generation,
                UUID(job.targets[0]["unit_id"]),
            ),
        )
        assert (
            unit.music_status == "READY"
            and unit.music_id == "manual"
            and unit.music_protected
        )


@pytest.mark.parametrize(
    "music_case, partial",
    [({"prepare": False}, False), ({"prepare": False, "images": 67}, True)],
    indirect=["music_case"],
)
def test_failed_preparation_can_resume_selected_units_without_restarting_active_intents(
    music_case, partial, wire, redis_client
):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation
    from app.modules.builds.image_music_batch import start_music_retry
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        run = session.exec(select(MusicPreparationRun)).one()
        if partial:
            first = advance_music_preparation(session, run.id, run.generation)
            assert first == {"status": "PENDING", "processed": 20}
        session.get(TenantMembership, (scope.tenant_id, scope.actor_id)).role = "viewer"
        session.flush()
        assert (
            advance_music_preparation(session, run.id, run.generation)["status"]
            == "FAILED"
        )
        assert run.error_code == "action_forbidden"
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (scope.tenant_id, scope.actor_id)
        ).role = "operator"
        session.flush()
        draft = session.get(BuildDraft, scope.draft_id)
        units = session.exec(
            select(DraftCreativeUnit)
            .where(DraftCreativeUnit.music_status == "PENDING")
            .order_by(DraftCreativeUnit.id)
        ).all()
        protected = units[0]
        request_music(
            session,
            scope,
            protected.id,
            protected.revision,
            protected.music_revision,
            uuid4(),
            "SET_MANUAL",
            "manual",
        )
        # 中途失败后保留另一个单元的新请求，不由批量恢复重启它。
        if partial:
            active = units[1]
            request_music(
                session,
                scope,
                active.id,
                active.revision,
                active.music_revision,
                uuid4(),
                "RECOMMEND",
            )
            active_id, active_request = active.id, active.music_request_generation
        receipt = start_music_retry(
            session, scope, uuid4(), draft.revision, [protected.drama_id]
        )
        retry_id, protected_id = UUID(receipt["run_id"]), protected.id
    while True:
        with Session(engine) as session, session.begin():
            run = session.get(MusicPreparationRun, retry_id)
            result = advance_music_preparation(session, run.id, run.generation)
            if result["status"] != "PENDING":
                assert result["status"] == "READY"
                assert run.retried == (2 if partial else 1)
                assert run.skipped_protected == 1
                break
    assert not wire["calls"]
    for job_id in job_ids(engine):
        run_job(engine, redis_client, job_id)
    assert any("/music/" in path for path, _ in wire["calls"])
    with Session(engine) as session:
        units = session.exec(select(DraftCreativeUnit)).all()
        assert all(u.music_status in {"READY", "NEEDS_SELECTION"} for u in units)
        protected = next(u for u in units if u.id == protected_id)
        assert protected.music_id == "manual" and protected.music_protected
        if partial:
            assert (
                next(u for u in units if u.id == active_id).music_request_generation
                == active_request
            )


@pytest.mark.parametrize(
    "recover_at", ["after_failure", "while_running", "after_success"]
)
def test_failed_preparation_resumes_running_copy_without_restarting_existing_job(
    music_case, wire, redis_client, recover_at
):
    from app.modules.builds.carousel_music import _target
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation
    from app.modules.builds.image_composition_commands import Duplicate
    from app.modules.builds.image_composition_service import edit_creative_units
    from app.modules.builds.image_music_batch import start_music_retry
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case
    identity = job_ids(engine)[0]
    copied = {}

    def restore_and_retry():
        with Session(engine) as session, session.begin():
            session.get(
                TenantMembership, (scope.tenant_id, scope.actor_id)
            ).role = "operator"
            session.flush()
            draft = session.get(BuildDraft, scope.draft_id)
            unit = session.get(
                DraftCreativeUnit,
                (
                    scope.tenant_id,
                    scope.draft_id,
                    draft.active_image_generation,
                    copied["id"],
                ),
            )
            target = _target(unit)
            existing = session.get(MusicSelectionJob, identity)
            existing_generation = existing.generation
            # 第一次接回副本，第二次必须幂等，不能重启已经匹配的音乐任务。
            for repeat in range(2):
                receipt = start_music_retry(
                    session, scope, uuid4(), draft.revision, [unit.drama_id]
                )
                run = session.get(MusicPreparationRun, UUID(receipt["run_id"]))
                assert (
                    advance_music_preparation(session, run.id, run.generation)["status"]
                    == "READY"
                )
                jobs = session.exec(select(MusicSelectionJob)).all()
                assert sum(target in job.targets for job in jobs) == 1
                assert _target(unit) == target
                if repeat:
                    assert run.retried == 0
                else:
                    assert run.retried == (2 if recover_at == "after_failure" else 1)
                if recover_at == "while_running":
                    assert existing.status == "RUNNING"
                    assert existing.generation == existing_generation

    def copy_and_fail_preparation():
        wire["hook"] = None
        with Session(engine) as session, session.begin():
            job = session.get(MusicSelectionJob, identity)
            source = session.get(
                DraftCreativeUnit,
                (
                    scope.tenant_id,
                    scope.draft_id,
                    job.active_generation,
                    UUID(job.targets[0]["unit_id"]),
                ),
            )
            assert source.music_status == "RUNNING"
            draft = session.get(BuildDraft, scope.draft_id)
            receipt = edit_creative_units(
                session,
                scope,
                uuid4(),
                draft.revision,
                Duplicate(kind="DUPLICATE", unit_id=source.id),
            )
            copied["id"] = UUID(str(receipt["affected_units"][0]))
            unit = session.get(
                DraftCreativeUnit,
                (scope.tenant_id, scope.draft_id, job.active_generation, copied["id"]),
            )
            assert unit.music_status == "RUNNING"
            assert not any(
                target["unit_id"] == str(unit.id)
                for item in session.exec(select(MusicSelectionJob)).all()
                for target in item.targets
            )
            session.get(
                TenantMembership, (scope.tenant_id, scope.actor_id)
            ).role = "viewer"
            session.flush()
            prep = session.exec(
                select(MusicPreparationRun).where(
                    MusicPreparationRun.status == "PENDING"
                )
            ).one()
            assert (
                advance_music_preparation(session, prep.id, prep.generation)["status"]
                == "FAILED"
            )
            assert prep.error_code == "action_forbidden"
            if recover_at == "after_success":
                session.get(
                    TenantMembership, (scope.tenant_id, scope.actor_id)
                ).role = "operator"
        if recover_at == "while_running":
            restore_and_retry()

    wire["hook"] = copy_and_fail_preparation
    assert run_job(engine, redis_client, identity)["status"] == (
        "FAILED" if recover_at == "after_failure" else "READY"
    )
    if recover_at != "while_running":
        restore_and_retry()
    for job_id in job_ids(engine):
        run_job(engine, redis_client, job_id)
    # 成功缓存及运行中任务均复用原推荐，只有原失败结果需要真正重新请求。
    assert sum("/music/" in path for path, _ in wire["calls"]) == (
        4 if recover_at == "after_failure" else 2
    )
    with Session(engine) as session:
        draft = session.get(BuildDraft, scope.draft_id)
        unit = session.get(
            DraftCreativeUnit,
            (
                scope.tenant_id,
                scope.draft_id,
                draft.active_image_generation,
                copied["id"],
            ),
        )
        assert unit.music_status == "READY" and unit.music_id == "recommended"
        assert all(
            item.music_status in {"READY", "NEEDS_SELECTION"}
            for item in session.exec(select(DraftCreativeUnit)).all()
        )


@pytest.mark.parametrize("music_case", [{"prepare": False}], indirect=True)
@pytest.mark.parametrize("old_failure", [False, True])
def test_batch_retry_does_not_take_over_normal_or_new_generation_preparation(
    music_case, old_failure
):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation
    from app.modules.builds.image_music_batch import start_music_retry
    from app.modules.builds.models import DraftDrama
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, scope.draft_id)
        drama = session.exec(
            select(DraftDrama).where(DraftDrama.draft_id == draft.id)
        ).one()
        if old_failure:
            run = session.exec(select(MusicPreparationRun)).one()
            member = session.get(TenantMembership, (scope.tenant_id, scope.actor_id))
            member.role = "viewer"
            session.flush()
            assert (
                advance_music_preparation(session, run.id, run.generation)["status"]
                == "FAILED"
            )
            member.role = "operator"
            session.flush()
            apply_rule(session, (draft, drama, scope))
            assert draft.active_image_generation != run.active_generation
        receipt = start_music_retry(
            session, scope, uuid4(), draft.revision, [drama.drama_id]
        )
        run = session.get(MusicPreparationRun, UUID(receipt["run_id"]))
        assert (
            advance_music_preparation(session, run.id, run.generation)["status"]
            == "READY"
        )
        assert run.retried == 0 and run.skipped_recovered == 3
        assert not session.exec(select(MusicSelectionJob)).all()
        finish_music_preparation(session)
        assert len(session.exec(select(MusicSelectionJob)).all()) == 2
