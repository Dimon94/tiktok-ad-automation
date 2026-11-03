"""素材子页使用真实暂存/发布与双通道 transport，主目录不被可选补读阻塞。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import (
    AdEntity,
    AdMaterialUsage,
    CallEvidence,
    DirectoryPage,
    EntityRef,
    MaterialUseRef,
)
from app.jobs.models import PendingDispatch
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import (
    directory_run_superseded,
    publish_directory,
    stage_directory_page,
)
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from tests.integrations.tiktok.gateway_support import business_calls, gateway
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.integrations.tiktok.test_ads_read_adapters import page, query, replies
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.ads.test_directory_sync import _authority, _run


def _source_page(seed, number, owners, *, terminal):
    entities, materials = [], []
    for ad, group in owners:
        ref = EntityRef(seed.context.tenant_id, seed.ref.advertiser_id, "ad", ad)
        entities.append(
            AdEntity(
                ref,
                EntityRef(ref.tenant_id, ref.advertiser_id, "adgroup", group),
                "SMART_PLUS",
                ad,
                {},
                "ENABLE",
                None,
                None,
                datetime.now(UTC),
            )
        )
        materials.append(
            AdMaterialUsage(
                MaterialUseRef(ref, f"video-{ad}", f"material-{ad}", "VIDEO"),
                None,
                "ENABLE",
                False,
                ad,
                (f"creative-{ad}",),
            )
        )
    return DirectoryPage(
        tuple(entities),
        tuple(materials),
        None if terminal else number + 1,
        terminal,
        CallEvidence(),
        number,
        False,
        "material_supplement_pending",
    )


def test_primary_publish_creates_deduplicated_child_without_retaining_source_pages(
    session,
    directory_seed,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    for number, ad in enumerate(("ad-a", "ad-b"), 1):
        stage_directory_page(
            session,
            run_id=source.id,
            claim_generation=1,
            page=_source_page(
                directory_seed, number, [(ad, "group")], terminal=number == 2
            ),
        )
    publish_directory(session, run_id=source.id, claim_generation=1)
    children = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.kind == "creative",
            AdDirectoryRun.tenant_id == source.tenant_id,
        )
    ).all()
    assert source.status == "COMPLETE" and source.coverage == "MATERIALS_INCOMPLETE"
    assert len(children) == 1, "跨主页面同组补读应合并为一个持久子运行"
    child = children[0]
    assert (
        child.frozen_route == source.frozen_route and child.actor_id == source.actor_id
    )
    assert child.workload == source.workload and child.next_page == 1
    publish_directory(session, run_id=source.id, claim_generation=1)
    assert (
        len(
            session.exec(
                select(AdDirectoryRun).where(
                    AdDirectoryRun.kind == "creative",
                    AdDirectoryRun.tenant_id == source.tenant_id,
                )
            ).all()
        )
        == 1
    )
    # 子运行进入统一需求头；同范围只发合并调度唤醒，由quota readiness再派发。
    wakes = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == source.tenant_id,
            PendingDispatch.task_name == "reporting.plan_due",
        )
    ).all()
    assert len(wakes) == 1
    session.connection().execute(
        delete(AdDirectoryPage).where(col(AdDirectoryPage.run_id) == source.id)
    )
    session.flush()
    from app.modules.ads.material_directory_children import material_child_scope

    scope = material_child_scope(session, child)
    assert scope is not None
    owners, bridges = scope
    assert {owner.ref.remote_id for owner in owners} == {"ad-a", "ad-b"}
    assert {bridge.use_ref.ad_material_id for bridge in bridges} == {
        "material-ad-a",
        "material-ad-b",
    }


def test_distinct_child_ranges_and_normal_creative_publish_do_not_supersede_children(
    session,
    directory_seed,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(
            directory_seed,
            1,
            [(f"ad-{n}", f"group-{n}") for n in range(101)],
            terminal=True,
        ),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    children = session.exec(
        select(AdDirectoryRun)
        .where(
            AdDirectoryRun.kind == "creative",
            AdDirectoryRun.tenant_id == source.tenant_id,
        )
        .order_by(col(AdDirectoryRun.request_sequence))
    ).all()
    assert len(children) == 2
    children[1].published_version = 100
    children[1].status = "COMPLETE"
    newer = _run(session, directory_seed, kind="creative", ad_type="SMART_PLUS")
    newer.published_version = 101
    newer.status = "COMPLETE"
    session.flush()
    assert not directory_run_superseded(session, children[0])


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_primary_read_defers_supplement_without_second_provider_call(
    database_engine,
    redis_client,
    gateway_case,
    gateway_wire,
):
    ad = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "ad-a",
        "adgroup_id": "group",
        "creative_list": [],
    }
    replies(gateway_wire, gateway_case[1].channel, {"smart_plus_ad_get": [page([ad])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_primary_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert result.complete and not result.materials_complete
    assert result.material_missing_reason == "material_supplement_pending"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1


@pytest.fixture
def durable_material_case(gateway_case, database_engine):
    yield gateway_case
    with Session(database_engine) as session, session.begin():
        for model in (AdDirectoryPage, AdDirectoryRun, AdMaterialReference, AdObject):
            session.connection().execute(
                delete(model).where(col(model.tenant_id) == gateway_case[0].tenant_id)
            )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_second_child_page_admission_resumes_without_replaying_primary_or_first_page(
    database_engine,
    redis_client,
    gateway_wire,
    durable_material_case,
    monkeypatch,
    policy,
):
    from app.modules.ads import tasks

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    context, route, advertiser = durable_material_case
    primary = {
        "advertiser_id": advertiser,
        "smart_plus_ad_id": "ad-a",
        "adgroup_id": "group",
        "creative_list": [
            {
                "smart_plus_creative_id": "creative-0",
                "ad_material_id": "material-0",
                "creative_info": {"video_info": {"video_id": "video-0"}},
            }
        ],
    }
    creatives = [
        {
            "advertiser_id": advertiser,
            "smart_plus_ad_id": "ad-a",
            "adgroup_id": "group",
            "ad_id": f"creative-{i}",
            "video_id": f"video-{i}",
        }
        for i in range(1001)
    ]
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            "smart_plus_ad_get": [page([primary])],
            "ad_get": [
                page(creatives[:1000], size=1000, total=1001),
                page(creatives[1000:], number=2, size=1000, total=1001),
            ],
        },
    )
    with Session(database_engine) as session, session.begin():
        source = AdDirectoryRun(
            tenant_id=context.tenant_id,
            advertiser_id=advertiser,
            bc_id=route.bc_id,
            actor_id=context.actor_id,
            connection_id=route.connection_id,
            channel=route.channel,
            frozen_route=route.model_dump(mode="json"),
            workload="background",
            kind="ad",
            ad_type="SMART_PLUS",
            partition_key="1" * 64,
            claim_generation=1,
            query={
                "advertiser_id": advertiser,
                "kind": "ad",
                "ad_type": "SMART_PLUS",
                "page": 1,
                "page_size": 10,
                "ids": [],
                "parent_ids": [],
                "include_deleted": True,
            },
        )
        session.add(source)
        session.flush()
        source_id = source.id
        from app.modules.sync_control.directory_demand import register_directory_run

        register_directory_run(session, source, new_execution=True)

    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

    # 分开执行正式canonical领取，便于断言限流期间没有创建下一页消息。
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_: None)

    def claim(identity, *, now=None):
        with Session(database_engine) as session, session.begin():
            run = session.get(AdDirectoryRun, identity)
            previous = run.claim_generation
            assert (
                dispatch_directories_due(
                    session,
                    redis_client,
                    now=now or datetime.now(UTC) + timedelta(seconds=1),
                    limit=40,
                )
                >= 1
            )
            session.refresh(run)
            assert (
                run.claim_generation > previous
            )  # 父目录补读也可同轮领取，目标执行必须换代。
            return run.claim_generation

    def run(identity, generation, *, publish_only=False):
        return tasks.run_directory_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            run_id=identity,
            claim_generation=generation,
            publish_only=publish_only,
        )

    source_generation = claim(source_id)
    assert run(source_id, source_generation) == "CONTINUE"
    assert len(business_calls(gateway_wire, "OFFICIAL_API")) == 1
    assert run(source_id, source_generation, publish_only=True) == "READY"
    with Session(database_engine) as session, session.begin():
        source = session.get(AdDirectoryRun, source_id)
        assert source is not None
        assert source.status == "COMPLETE" and source.coverage == "MATERIALS_INCOMPLETE"
        child = session.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == context.tenant_id,
                AdDirectoryRun.kind == "creative",
            )
        ).one()
        child_id = child.id
        session.connection().execute(
            delete(AdDirectoryPage).where(col(AdDirectoryPage.run_id) == source_id)
        )
    limited = policy.model_copy(update={"app_calls_per_window": 1, "window_ms": 60_000})
    original = gateway_wire["before"]["callback"]

    def limit_after_first_child():
        # 第一子页已准入后降低本地预算，让同一量子第二页在HTTP前被拒绝。
        if len(business_calls(gateway_wire, "OFFICIAL_API")) == 1:
            monkeypatch.setattr(
                settings, "TIKTOK_CALL_POLICIES", {"base": limited.model_dump()}
            )
        original()

    gateway_wire["before"]["callback"] = limit_after_first_child
    child_generation = claim(child_id)
    assert run(child_id, child_generation) == "WAIT"
    assert len(business_calls(gateway_wire, "OFFICIAL_API")) == 2
    with Session(database_engine) as session, session.begin():
        assert (
            dispatch_directories_due(
                session,
                redis_client,
                now=datetime.now(UTC) + timedelta(seconds=1),
                limit=40,
            )
            == 0
        )
        head = session.exec(
            select(SyncSchedule)
            .join(
                SyncExecutionScope, SyncExecutionScope.scope_id == SyncSchedule.scope_id
            )
            .where(SyncExecutionScope.directory_run_id == child_id)
        ).one()
        assert head.next_eligible_at > datetime.now(UTC)
        resume_at = head.next_eligible_at + timedelta(seconds=1)
    with Session(database_engine) as session:
        child = session.get(AdDirectoryRun, child_id)
        assert child is not None
        assert child.next_page == 2 and child.status == "WAITING_REMOTE"
        assert (
            child.claimed_until is None
            and child.claim_generation == child_generation + 1
        )
        assert session.get(AdDirectoryPage, (child_id, 1)) is not None
        assert session.get(AdDirectoryPage, (child_id, 2)) is None
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    resume_generation = claim(child_id, now=resume_at)
    assert run(child_id, resume_generation) == "CONTINUE"
    assert run(child_id, resume_generation, publish_only=True) == "READY"
    calls = business_calls(gateway_wire, "OFFICIAL_API")
    assert len(calls) == 3
    with Session(database_engine) as session:
        child = session.get(AdDirectoryRun, child_id)
        assert child is not None
        assert child.status == "COMPLETE" and child.coverage == "COMPLETE"
        materials = session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == context.tenant_id
            )
        ).all()
        assert len(materials) == 1001 and all(m.complete for m in materials)
        assert (
            next(
                m for m in materials if m.platform_material_id == "video-0"
            ).ad_material_id
            == "material-0"
        )


@pytest.mark.parametrize("newer_owner", [False, True])
@pytest.mark.parametrize("different_material_identity", [False, True])
def test_old_child_page_cannot_overwrite_newer_material_observation(
    session, directory_seed, monkeypatch, newer_owner, different_material_identity
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    original = _source_page(directory_seed, 1, [("ad-a", "group")], terminal=True)
    stage_directory_page(session, run_id=source.id, claim_generation=1, page=original)
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    owner = original.items[0]
    creative = AdEntity(
        EntityRef(
            owner.ref.tenant_id, owner.ref.advertiser_id, "creative", "creative-ad-a"
        ),
        owner.ref,
        "SMART_PLUS",
        "old",
        {},
        "ENABLE",
        None,
        None,
        datetime.now(UTC) - timedelta(hours=1),
    )
    usage = replace(original.materials[0], complete=True, name="old")
    stage_directory_page(
        session,
        run_id=child.id,
        claim_generation=1,
        page=DirectoryPage((creative,), (usage,), None, True, CallEvidence()),
    )
    session.add(
        AdObject(
            tenant_id=source.tenant_id,
            advertiser_id=source.advertiser_id,
            kind="creative",
            remote_id=creative.ref.remote_id,
            parent_kind="ad",
            parent_remote_id="ad-a",
            ad_type="SMART_PLUS",
            name="new",
            configuration={},
            operation_status="DISABLE",
            observed_at=datetime.now(UTC) - timedelta(hours=2)
            if newer_owner
            else datetime.now(UTC),
            published_version=999,
            source_connection_id=source.connection_id,
            source_channel=source.channel,
        )
    )
    material = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == source.tenant_id
        )
    ).one()
    (
        material.complete,
        material.name,
        material.operation_status,
        material.published_version,
    ) = True, "new", "DISABLE", 999
    if different_material_identity:
        material.ad_material_id = "newer-material-identity"
    if newer_owner:
        current_owner = session.get(
            AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-a")
        )
        current_owner.observed_at = datetime.now(UTC) + timedelta(seconds=1)
    session.flush()
    if newer_owner:
        with pytest.raises(DomainError, match="较早目录"):
            publish_directory(session, run_id=child.id, claim_generation=1)
    else:
        publish_directory(session, run_id=child.id, claim_generation=1)
    session.refresh(material)
    assert material.name == "new" and material.operation_status == "DISABLE"
    assert material.published_version == 999
    assert (
        len(
            session.exec(
                select(AdMaterialReference).where(
                    AdMaterialReference.tenant_id == source.tenant_id
                )
            ).all()
        )
        == 1
    )
    if not newer_owner:
        assert child.coverage == "MATERIALS_INCOMPLETE"
        assert child.missing_reason == "material_child_observation_stale"


@pytest.mark.parametrize("damage", ["tenant", "route"])
def test_child_frozen_scope_cannot_be_transplanted(
    session, directory_seed, monkeypatch, damage
):
    from app.modules.ads.material_directory_children import material_child_scope

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("ad-a", "group")], terminal=True),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    if damage == "tenant":
        child.tenant_id = uuid4()
    else:
        child.frozen_route = child.frozen_route | {"binding_revision": 999}
    with pytest.raises(DomainError, match="冻结范围"):
        material_child_scope(session, child)
    session.expire(child)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "native_id,bridge_id",
    [
        (None, "precise-material"),
        ("precise-material", "precise-material"),
        ("conflicting-material", "precise-material"),
        ("native-material", None),
    ],
)
def test_child_one_page_keeps_exact_native_creative_bridge(
    database_engine,
    redis_client,
    gateway_case,
    gateway_wire,
    native_id,
    bridge_id,
):
    context, _, advertiser = gateway_case
    ref = EntityRef(context.tenant_id, advertiser, "ad", "ad-a")
    owner = AdEntity(
        ref,
        EntityRef(context.tenant_id, advertiser, "adgroup", "group"),
        "SMART_PLUS",
        "",
        {},
        None,
        None,
        None,
        datetime.now(UTC),
    )
    bridge = AdMaterialUsage(
        MaterialUseRef(ref, "video", bridge_id, "VIDEO"),
        None,
        "DISABLE",
        False,
        "original",
        ("creative",),
    )
    row = {
        "advertiser_id": advertiser,
        "adgroup_id": "group",
        "smart_plus_ad_id": "ad-a",
        "ad_id": "creative",
        "video_id": "video",
        "operation_status": "ENABLE",
    }
    if native_id is not None:
        row["ad_material_id"] = native_id
    replies(gateway_wire, gateway_case[1].channel, {"ad_get": [page([row], size=1000)]})
    child_query = replace(query(gateway_case, "creative", "SMART_PLUS"), page_size=1000)
    with gateway(database_engine, redis_client, gateway_case) as client:
        if native_id == "conflicting-material":
            with pytest.raises(DomainError, match="身份不完整"):
                client.ads.read_material_page(
                    child_query, ads=(owner,), materials=(bridge,)
                )
            return
        result = client.ads.read_material_page(
            child_query, ads=(owner,), materials=(bridge,)
        )
    assert result.complete and result.materials_complete
    assert result.items[0].parent_ref == ref
    assert result.materials[0].use_ref.ad_material_id == (native_id or bridge_id)
    assert result.materials[0].creative_ids == ("creative",)
    assert result.items[0].operation_status == "ENABLE"
    assert result.materials[0].operation_status == "DISABLE"
    assert result.materials[0].name == "original"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1


def test_cleanup_does_not_supersede_child_or_use_child_as_global_replacement(
    session, directory_seed, monkeypatch
):
    from app.modules.reporting.job_tasks import _supersede_stalled_directory_runs

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    old_normal = _run(session, directory_seed, kind="creative", ad_type="SMART_PLUS")
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("ad-a", "group")], terminal=True),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
            AdDirectoryRun.id != old_normal.id,
        )
    ).one()
    now = datetime.now(UTC)
    old_normal.created_at = child.created_at = now - timedelta(days=2)
    child.status, child.published_version = "COMPLETE", 100
    session.flush()
    session.connection().execute(
        _supersede_stalled_directory_runs(now, candidate_ids=[old_normal.id])
    )
    session.refresh(old_normal)
    assert old_normal.status == "QUEUED"
    child.status, child.published_version = "QUEUED", None
    newer = _run(session, directory_seed, kind="creative", ad_type="SMART_PLUS")
    newer.status, newer.published_version = "COMPLETE", 101
    session.flush()
    session.connection().execute(
        _supersede_stalled_directory_runs(now, candidate_ids=[old_normal.id, child.id])
    )
    session.refresh(old_normal)
    session.refresh(child)
    assert old_normal.status == "STALE"
    assert child.status == "QUEUED"


def test_child_superseded_only_when_every_frozen_owner_has_newer_observation(
    session, directory_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(
            directory_seed, 1, [("ad-a", "group"), ("ad-b", "group")], terminal=True
        ),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    assert not directory_run_superseded(session, child)
    first = session.get(
        AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-a")
    )
    second = session.get(
        AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-b")
    )
    first.observed_at = datetime.now(UTC) + timedelta(seconds=1)
    session.flush()
    assert not directory_run_superseded(session, child)
    second.observed_at = datetime.now(UTC) + timedelta(seconds=1)
    session.flush()
    assert directory_run_superseded(session, child)
    session.delete(second)
    session.flush()
    assert not directory_run_superseded(session, child)


def test_partly_replaced_child_keeps_valid_owner_and_records_incomplete_coverage(
    session, directory_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    primary = _source_page(
        directory_seed, 1, [("ad-a", "group"), ("ad-b", "group")], terminal=True
    )
    stage_directory_page(session, run_id=source.id, claim_generation=1, page=primary)
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    creatives = tuple(
        replace(
            ad,
            ref=EntityRef(
                ad.ref.tenant_id,
                ad.ref.advertiser_id,
                "creative",
                f"creative-{ad.ref.remote_id}",
            ),
            parent_ref=ad.ref,
        )
        for ad in primary.items
    )
    stage_directory_page(
        session,
        run_id=child.id,
        claim_generation=1,
        page=DirectoryPage(
            creatives,
            tuple(replace(m, complete=True) for m in primary.materials),
            None,
            True,
            CallEvidence(),
        ),
    )
    first = session.get(
        AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-a")
    )
    assert first is not None
    first.observed_at = datetime.now(UTC) + timedelta(seconds=1)
    session.flush()
    assert not directory_run_superseded(session, child)
    publish_directory(session, run_id=child.id, claim_generation=1)
    values = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == source.tenant_id
        )
    ).all()
    assert {m.ad_remote_id: m.complete for m in values} == {"ad-a": False, "ad-b": True}
    assert child.status == "COMPLETE" and child.coverage == "MATERIALS_INCOMPLETE"
    assert child.missing_reason == "material_child_observation_stale"


def test_primary_publication_rollback_removes_child_and_refill(
    session, directory_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("ad-a", "group")], terminal=True),
    )
    with pytest.raises(RuntimeError, match="caller rollback"):
        with session.begin_nested():
            publish_directory(session, run_id=source.id, claim_generation=1)
            raise RuntimeError("caller rollback")
    assert not session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).all()
    assert not session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == source.tenant_id,
            PendingDispatch.task_name == "reporting.plan_due",
        )
    ).all()
    session.refresh(source)
    assert source.published_version is None


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize(
    "case",
    [
        "wrong_creative",
        "ambiguous",
        "tenant",
        "account",
        "response_group",
    ],
)
def test_child_material_identity_and_parent_scope_are_exact(
    database_engine, redis_client, gateway_case, gateway_wire, case
):
    context, _, advertiser = gateway_case
    ref = EntityRef(context.tenant_id, advertiser, "ad", "ad-a")
    parent = EntityRef(context.tenant_id, advertiser, "adgroup", "group")
    owner = AdEntity(
        ref, parent, "SMART_PLUS", "", {}, None, None, None, datetime.now(UTC)
    )
    bridge = AdMaterialUsage(
        MaterialUseRef(ref, "video", "precise-material", "VIDEO"),
        None,
        "DISABLE",
        False,
        "original",
        ("creative",),
    )
    bridges = (bridge,)
    if case == "wrong_creative":
        bridges = (replace(bridge, creative_ids=("unrelated-creative",)),)
    elif case == "ambiguous":
        bridges += (
            replace(
                bridge, use_ref=replace(bridge.use_ref, ad_material_id="other-material")
            ),
        )
    elif case == "tenant":
        foreign = uuid4()
        owner = replace(
            owner,
            ref=replace(ref, tenant_id=foreign),
            parent_ref=replace(parent, tenant_id=foreign),
        )
    elif case == "account":
        owner = replace(
            owner,
            ref=replace(ref, advertiser_id="other-account"),
            parent_ref=replace(parent, advertiser_id="other-account"),
        )
    row = {
        "advertiser_id": advertiser,
        "adgroup_id": "other-group" if case == "response_group" else "group",
        "smart_plus_ad_id": "ad-a",
        "ad_id": "creative",
        "video_id": "video",
    }
    replies(gateway_wire, gateway_case[1].channel, {"ad_get": [page([row], size=1000)]})
    child_query = replace(query(gateway_case, "creative", "SMART_PLUS"), page_size=1000)
    with gateway(database_engine, redis_client, gateway_case) as client:
        if case == "wrong_creative":
            result = client.ads.read_material_page(
                child_query, ads=(owner,), materials=bridges
            )
            assert result.materials[0].use_ref.ad_material_id is None
            assert result.materials[0].creative_ids == ("creative",)
        else:
            with pytest.raises(DomainError):
                client.ads.read_material_page(
                    child_query, ads=(owner,), materials=bridges
                )
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == (
        0 if case in {"tenant", "account"} else 1
    )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_large_frozen_bridge_set_preserves_each_exact_material_identity(
    database_engine, redis_client, gateway_case, gateway_wire
):
    context, _, advertiser = gateway_case
    ref = EntityRef(context.tenant_id, advertiser, "ad", "ad-a")
    owner = AdEntity(
        ref,
        EntityRef(context.tenant_id, advertiser, "adgroup", "group"),
        "SMART_PLUS",
        "",
        {},
        None,
        None,
        None,
        datetime.now(UTC),
    )
    bridges = tuple(
        AdMaterialUsage(
            MaterialUseRef(ref, f"video-{i}", f"material-{i}", "VIDEO"),
            None,
            "DISABLE",
            False,
            "",
            (f"creative-{i}",),
        )
        for i in range(50_000)
    )
    rows = [
        {
            "advertiser_id": advertiser,
            "adgroup_id": "group",
            "smart_plus_ad_id": "ad-a",
            "ad_id": f"creative-{i}",
            "video_id": f"video-{i}",
            "operation_status": "ENABLE",
        }
        for i in range(1000)
    ]
    replies(gateway_wire, "OFFICIAL_API", {"ad_get": [page(rows, size=1000)]})
    child_query = replace(query(gateway_case, "creative", "SMART_PLUS"), page_size=1000)
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_material_page(
            child_query, ads=(owner,), materials=bridges
        )
    assert len(result.materials) == 1000
    assert {usage.use_ref.ad_material_id for usage in result.materials} == {
        f"material-{i}" for i in range(1000)
    }
    assert all(usage.operation_status == "DISABLE" for usage in result.materials)
    assert len(business_calls(gateway_wire, "OFFICIAL_API")) == 1


def test_existing_incomplete_source_can_backfill_children_idempotently(
    session, directory_seed, monkeypatch
):
    from app.modules.ads.material_directory_children import (
        ensure_material_directory_children,
    )
    from app.modules.ads.sync import (
        _complete_chain,
        _ensure_route_authority,
        _route_for_run,
    )

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("ad-a", "group")], terminal=True),
    )
    version = publish_directory(session, run_id=source.id, claim_generation=1)
    assert not session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).all()
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    source = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.id == source.id).with_for_update()
    ).one()
    _ensure_route_authority(session, source, _route_for_run(source))
    ordered = _complete_chain(
        list(
            session.exec(
                select(AdDirectoryPage)
                .where(AdDirectoryPage.run_id == source.id)
                .order_by(col(AdDirectoryPage.page))
                .with_for_update()
            ).all()
        )
    )
    assert (
        ensure_material_directory_children(session, source=source, pages=ordered) == 1
    )
    assert (
        ensure_material_directory_children(session, source=source, pages=ordered) == 0
    )
    assert source.published_version == version and source.status == "COMPLETE"
    assert source.coverage == "MATERIALS_INCOMPLETE"
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    assert child.workload == source.workload and child.status == "QUEUED"


@pytest.mark.parametrize(
    ("service", "expected_children"),
    [("status", 0), ("status_missing", 0), ("identity", 1)],
)
def test_refresh_purpose_controls_optional_material_children(
    session, directory_seed, monkeypatch, service, expected_children
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    source.query = {**source.query, "directory_service_v1": service}
    known = AdMaterialReference(
        tenant_id=source.tenant_id,
        advertiser_id=source.advertiser_id,
        ad_remote_id="status-ad",
        platform_material_id="video-status-ad",
        ad_material_id="material-status-ad",
        material_type="VIDEO",
        name="known",
        creative_ids=["creative-status-ad"],
        complete=True,
        published_version=1,
    )
    session.add(known)
    session.flush()
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("status-ad", "group")], terminal=True),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    children = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).all()
    assert len(children) == expected_children
    session.refresh(known)
    assert known.complete
    ad = session.get(
        AdObject, (source.tenant_id, source.advertiser_id, "ad", "status-ad")
    )
    assert ad is not None and ad.operation_status == "ENABLE"
    assert source.status == "COMPLETE"
