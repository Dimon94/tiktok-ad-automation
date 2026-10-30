from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import col, select

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
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun


def _route(seed):
    return {
        "tenant_id": str(seed.context.tenant_id),
        "bc_id": seed.bc_id,
        "connection_id": str(seed.connection.id),
        "channel": seed.connection.kind,
        "authorization_revision": seed.connection.authorization_revision,
        "binding_revision": 0,
        "adapter_contract_revision": seed.connection.adapter_contract_revision,
    }


def _authority(session, seed):
    session.add(
        BCConnectionBinding(
            tenant_id=seed.context.tenant_id,
            bc_id=seed.bc_id,
            connection_id=seed.connection.id,
            kind=seed.connection.kind,
        )
    )
    session.add(
        ConnectionAuthorization(
            tenant_id=seed.context.tenant_id,
            connection_id=seed.connection.id,
            authorization_revision=seed.connection.authorization_revision,
            source="OFFLINE_TEST",
            verified_at=datetime.now(UTC),
            permission_summary={"read_authorized": True},
        )
    )
    seed.grant.checked_at = datetime.now(UTC)
    session.flush()


def _run(session, seed, *, kind="campaign", ad_type="REGULAR"):
    run = AdDirectoryRun(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        bc_id=seed.bc_id,
        actor_id=seed.context.actor_id,
        connection_id=seed.connection.id,
        channel=seed.connection.kind,
        frozen_route=_route(seed),
        partition_key=(uuid4().hex * 2)[:64],
        query={
            "advertiser_id": seed.ref.advertiser_id,
            "kind": kind,
            "ad_type": ad_type,
            "page": 1,
            "page_size": 100,
            "ids": [],
            "parent_ids": [],
            "include_deleted": False,
        },
        claim_generation=1,
        kind=kind,
        ad_type=ad_type,
    )
    session.add(run)
    session.flush()
    return run


def _claim_directory(session, run, redis_client, monkeypatch):
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.modules.sync_control.directory_demand import register_directory_run
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"directory-worker-{uuid4()}")
    register_directory_run(session, run)
    assert (
        dispatch_directories_due(
            session, redis_client, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )
        == 1
    )
    return run.claim_generation


def _page(
    seed,
    *,
    page=1,
    complete=True,
    next_page=None,
    name="版权方-剧名-备注",
    ad_type="REGULAR",
):
    ref = EntityRef(
        seed.context.tenant_id, seed.ref.advertiser_id, "campaign", "campaign-1"
    )
    entity = AdEntity(
        ref=ref,
        parent_ref=None,
        ad_type=ad_type,
        name=name,
        configuration={"budget": "1.2"},
        operation_status="ENABLE",
        review_status="APPROVED",
        delivery_status="DELIVERING",
        observed_at=datetime.now(UTC),
    )
    return DirectoryPage(
        items=(entity,),
        materials=(),
        next_page=next_page,
        complete=complete,
        evidence=CallEvidence(request_id="test-request"),
        page=page,
    )


def test_staging_is_idempotent_and_conflicts_are_rejected(session, directory_seed):
    run = _run(session, directory_seed)
    page = _page(directory_seed)
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    with pytest.raises(DomainError):
        stage_directory_page(
            session,
            run_id=run.id,
            page=_page(directory_seed, name="版权方-另一剧-备注"),
            claim_generation=1,
        )
    assert len(session.exec(select(AdDirectoryPage)).all()) == 1


def test_staging_rejects_pages_after_terminal_page(session, directory_seed):
    run = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed), claim_generation=1
    )
    with pytest.raises(DomainError):
        stage_directory_page(
            session,
            run_id=run.id,
            page=_page(directory_seed, page=2),
            claim_generation=1,
        )


def test_staging_allows_earlier_page_after_out_of_order_terminal(
    session, directory_seed
):
    run = _run(session, directory_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, page=2),
        claim_generation=1,
    )
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, page=1, complete=False, next_page=2),
        claim_generation=1,
    )


def test_staging_rejects_wrong_automation_type(session, directory_seed):
    run = _run(session, directory_seed, kind="campaign")
    with pytest.raises(DomainError):
        stage_directory_page(
            session,
            run_id=run.id,
            page=_page(directory_seed, ad_type="SMART_PLUS"),
            claim_generation=1,
        )


def test_publish_requires_terminal_page_and_projects_name(session, directory_seed):
    _authority(session, directory_seed)
    run = _run(session, directory_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, complete=False, next_page=2),
        claim_generation=1,
    )
    with pytest.raises(DomainError):
        publish_directory(session, run_id=run.id, claim_generation=1)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed, page=2), claim_generation=1
    )
    version = publish_directory(session, run_id=run.id, claim_generation=1)
    assert version > 0
    campaign = session.get(
        AdObject,
        (directory_seed.context.tenant_id, "account-a", "campaign", "campaign-1"),
    )
    assert campaign is not None and campaign.published_version == version


@pytest.mark.parametrize(
    "include_deleted,pending_change,expected",
    [(False, None, 1), (True, None, 1), (True, "live_only", 2), (True, "route", 2)],
)
@pytest.mark.parametrize("workload", ["live", "background"])
def test_missing_parent_queues_only_same_type_parent_and_coalesces_follow_up(
    session,
    directory_seed,
    monkeypatch,
    include_deleted,
    pending_change,
    expected,
    workload,
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    run = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    run.workload = workload
    run.query = run.query | {"include_deleted": include_deleted}
    parent = EntityRef(
        directory_seed.context.tenant_id, "account-a", "adgroup", "missing-group"
    )
    entity = AdEntity(
        ref=directory_seed.ref,
        parent_ref=parent,
        ad_type="SMART_PLUS",
        name="ad",
        configuration={},
        operation_status="ENABLE",
        review_status=None,
        delivery_status=None,
        observed_at=datetime.now(UTC),
    )
    page = DirectoryPage(
        items=(entity,),
        materials=(),
        next_page=None,
        complete=True,
        evidence=CallEvidence(),
    )
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    publish_directory(session, run_id=run.id, claim_generation=1)
    targets = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == run.tenant_id, AdDirectoryRun.id != run.id
        )
    ).all()
    assert len(targets) == 1
    assert targets[0].kind == "adgroup" and targets[0].ad_type == "SMART_PLUS"
    assert targets[0].query["ids"] == ["missing-group"]
    assert targets[0].query["parent_ids"] == []
    assert targets[0].workload == workload
    assert targets[0].query["include_deleted"] is include_deleted
    assert all(t.frozen_route == run.frozen_route for t in targets)
    if pending_change == "live_only":
        targets[0].query = targets[0].query | {"include_deleted": False}
    if pending_change == "route":
        targets[0].frozen_route = targets[0].frozen_route | {"binding_revision": 1}
    session.flush()
    second = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    second.workload = workload
    second.query = second.query | {"include_deleted": include_deleted}
    next_entity = replace(
        entity, ref=EntityRef(run.tenant_id, "account-a", "ad", "second-ad")
    )
    stage_directory_page(
        session,
        run_id=second.id,
        page=replace(page, items=(next_entity,)),
        claim_generation=1,
    )
    publish_directory(session, run_id=second.id, claim_generation=1)
    assert (
        session.get(AdObject, (run.tenant_id, "account-a", "ad", "second-ad"))
        is not None
    )
    assert (
        len(
            session.exec(
                select(AdDirectoryRun).where(AdDirectoryRun.kind == "adgroup")
            ).all()
        )
        == expected
    )


def test_late_older_run_is_fenced_after_newer_run_publishes(session, directory_seed):
    _authority(session, directory_seed)
    older = _run(session, directory_seed)
    newer = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=newer.id, page=_page(directory_seed), claim_generation=1
    )
    publish_directory(session, run_id=newer.id, claim_generation=1)
    stage_directory_page(
        session, run_id=older.id, page=_page(directory_seed), claim_generation=1
    )
    with pytest.raises(DomainError):
        publish_directory(session, run_id=older.id, claim_generation=1)


def test_disabled_switch_does_not_execute_queued_directory_task(
    monkeypatch, directory_seed
):
    from app.modules.ads import tasks

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)

    def unexpected_gateway(**_kwargs):
        raise AssertionError("关闭总开关后不能发起目录请求")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", unexpected_gateway)
    assert (
        tasks.run_directory_step(
            database_engine=None,
            redis_client=None,
            context=directory_seed.context,
            run_id=uuid4(),
            claim_generation=1,
        )
        == "WAIT"
    )


@pytest.mark.parametrize("kind", ["ad", "campaign"])
def test_large_publish_has_bounded_database_round_trips(
    session, directory_seed, kind, record_property
):
    """千条对象/素材不能逐条往返数据库，且首次与重复观察都要保留完整事实。"""
    _authority(session, directory_seed)
    template = _page(directory_seed).items[0]
    items = tuple(
        replace(
            template,
            ref=EntityRef(
                directory_seed.context.tenant_id, "account-a", kind, f"bulk-{i}"
            ),
            name=f"版权方-剧{i}-备注",
        )
        for i in range(1200)
    )
    materials = (
        tuple(
            AdMaterialUsage(
                use_ref=MaterialUseRef(item.ref, f"vid-{i}", None, "VIDEO"),
                local_material_id=None,
                operation_status="ENABLE",
                complete=True,
                name=f"视频{i}",
            )
            for i, item in enumerate(items)
        )
        if kind == "ad"
        else ()
    )
    counts = []
    elapsed = []
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    for _repeat in range(2):
        run = _run(session, directory_seed, kind=kind)
        page = DirectoryPage(items, materials, None, True, CallEvidence())
        stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
        if kind == "ad":
            payload = session.get(AdDirectoryPage, (run.id, 1)).materials[0]
            assert set(payload) == {
                "use_ref",
                "local_material_id",
                "operation_status",
                "complete",
                "name",
                "creative_ids",
            }
        statements.clear()

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", record)
        try:
            started = monotonic()
            version = publish_directory(session, run_id=run.id, claim_generation=1)
            elapsed.append(round(monotonic() - started, 3))
        finally:
            event.remove(connection, "before_cursor_execute", record)
        counts.append(len(statements))
        objects = session.exec(
            select(AdObject).where(
                AdObject.kind == kind,
                AdObject.tenant_id == directory_seed.context.tenant_id,
            )
        ).all()
        assert len(objects) == (1201 if kind == "ad" else 1200)
        assert all(
            obj.published_version == version
            for obj in objects
            if obj.remote_id.startswith("bulk-")
        )
        if kind == "ad":
            usages = session.exec(
                select(AdMaterialReference).where(
                    AdMaterialReference.tenant_id == directory_seed.context.tenant_id
                )
            ).all()
            assert len(usages) == 1200
            assert all(u.complete and u.published_version == version for u in usages)
        else:
            projections = session.exec(
                select(CampaignNameProjection).where(
                    CampaignNameProjection.tenant_id == directory_seed.context.tenant_id
                )
            ).all()
            assert len(projections) == 1200
            assert all(p.name_revision == 1 for p in projections)
    record_property("sql_counts", counts)
    record_property("publish_seconds", elapsed)
    assert max(counts) < 90, f"1200 条目录不应逐条查询/更新，实际 SQL 次数 {counts}"


def test_publish_preserves_newer_objects_and_complete_materials(
    session, directory_seed
):
    _authority(session, directory_seed)
    current = directory_seed.row
    current.parent_kind = current.parent_remote_id = None
    old_name = current.name
    template = _page(directory_seed).items[0]
    entity = replace(
        template, ref=current.ref, observed_at=current.observed_at - timedelta(days=1)
    )
    usage = AdMaterialUsage(
        MaterialUseRef(current.ref, "vid", None, "VIDEO"),
        None,
        "ENABLE",
        True,
        name="完整素材",
    )
    # 完整素材属于已有的新 owner 观察；旧目录不能再凭更晚发布版本补写旧素材。
    session.add(
        AdMaterialReference(
            tenant_id=current.tenant_id,
            advertiser_id=current.advertiser_id,
            ad_remote_id=current.remote_id,
            platform_material_id="vid",
            ad_material_id=None,
            material_type="VIDEO",
            name="完整素材",
            operation_status="ENABLE",
            complete=True,
            published_version=current.published_version,
        )
    )
    first = _run(session, directory_seed, kind="ad")
    stage_directory_page(
        session,
        run_id=first.id,
        page=DirectoryPage((entity,), (usage,), None, True, CallEvidence()),
        claim_generation=1,
    )
    publish_directory(session, run_id=first.id, claim_generation=1)
    second = _run(session, directory_seed, kind="ad")
    incomplete = replace(usage, complete=False, name="", operation_status=None)
    stage_directory_page(
        session,
        run_id=second.id,
        page=DirectoryPage((entity,), (incomplete,), None, True, CallEvidence()),
        claim_generation=1,
    )
    publish_directory(session, run_id=second.id, claim_generation=1)
    session.refresh(current)
    material = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == directory_seed.context.tenant_id
        )
    ).one()
    assert current.name == old_name and current.published_version == 1
    assert material.complete and material.name == "完整素材"
    assert material.published_version == 1
    assert first.coverage == second.coverage == "MATERIALS_INCOMPLETE"


def test_staged_terminal_page_publishes_without_another_provider_call(
    session, directory_seed, monkeypatch
):
    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed), claim_generation=1
    )

    def unexpected_gateway(**_kwargs):
        raise AssertionError("已暂存末页的恢复/发布不能再次请求远端")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", unexpected_gateway)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=None,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    session.refresh(run)
    assert run.status == "COMPLETE" and run.claimed_until is None
    version = run.published_version
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=None,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    session.refresh(run)
    assert run.published_version == version
    wakeups = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == run.tenant_id,
            col(PendingDispatch.task_key).startswith(
                f"directory-refill:{run.tenant_id}:{run.actor_id}:"
            ),
        )
    ).all()
    assert not wakeups


def test_directory_version_allocation_does_not_scan_business_tables(session):
    """版本由独立序列分配，不能在每次持租户锁时扫描几十万行取 MAX。"""
    from app.modules.ads.sync import _next_directory_version

    first = _next_directory_version(session)
    second = _next_directory_version(session)
    assert second > first > 0
    assert (
        session.execute(
            text("SELECT last_value FROM ad_directory_published_version_seq")
        ).scalar_one()
        == second
    )


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("workload", ["live", "background"])
def test_terminal_fetch_commits_page_and_durable_publish_successor(
    session, directory_seed, monkeypatch, redis_client, targeted, workload
):

    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    run.workload = workload
    session.flush()
    if targeted:
        run.query = {**run.query, "ids": ["campaign-1"], "include_deleted": False}
        session.flush()
    page = _page(directory_seed)

    generation = _claim_directory(session, run, redis_client, monkeypatch)

    class Reader:
        def read_page(self, query):
            assert query.page == 1
            return page

    @contextmanager
    def gateway(**_kwargs):
        assert _kwargs["workload"] == workload
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=generation,
        )
        == "CONTINUE"
    )
    session.refresh(run)
    assert run.published_version is None and run.claimed_until is None
    assert run.next_page == 2
    assert session.get(AdDirectoryPage, (run.id, 1)).complete
    assert run.claim_generation > generation  # 同次refill已持久领取本地publisher。
    generation = run.claim_generation
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_key
            == f"{'ads.targeted_step' if targeted and workload == 'live' else 'ads.sync_step'}:{run.id}:{generation}:page:2"
        )
    ).one()
    assert dispatch.task_name == "ads.publish_step"
    assert dispatch.payload == {"run_id": str(run.id), "claim_generation": generation}

    def unexpected_gateway(**_kwargs):
        raise AssertionError("本地发布续步不得请求不存在的远端第二页")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", unexpected_gateway)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=generation,
            publish_only=True,
        )
        == "READY"
    )
    session.refresh(run)
    assert run.status == "COMPLETE"


def test_invalid_material_rolls_back_the_entire_bulk_publish(session, directory_seed):
    _authority(session, directory_seed)
    run = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed), claim_generation=1
    )
    staged = session.get(AdDirectoryPage, (run.id, 1))
    staged.materials = [
        {
            "use_ref": {
                "ad_ref": {
                    "tenant_id": str(run.tenant_id),
                    "advertiser_id": "foreign-account",
                    "kind": "ad",
                    "remote_id": "foreign",
                }
            }
        }
    ]
    session.flush()
    with pytest.raises(DomainError, match="暂存素材使用身份无效"):
        publish_directory(session, run_id=run.id, claim_generation=1)
    session.refresh(run)
    assert run.published_version is None
    assert (
        session.get(
            AdObject, (run.tenant_id, run.advertiser_id, "campaign", "campaign-1")
        )
        is None
    )


def test_old_page_response_cannot_clear_a_new_generation_lease(
    session, directory_seed, monkeypatch, redis_client
):

    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    page = _page(directory_seed, complete=False, next_page=2)
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    new_token = uuid4()
    new_until = datetime.now(UTC) + timedelta(minutes=2)

    generation = _claim_directory(session, run, redis_client, monkeypatch)

    class Reader:
        def read_page(self, _query):
            run.claim_generation = generation + 1
            run.claim_token = new_token
            run.claimed_until = new_until
            session.flush()
            return page

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=generation,
        )
        == "FAILED"
    )
    session.refresh(run)
    assert run.claim_generation == generation + 1 and run.claim_token == new_token
    assert run.claimed_until == new_until and run.next_page == 1


def test_publish_only_rejects_missing_terminal_without_gateway(
    session, directory_seed, monkeypatch
):
    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)

    def forbidden(**_kwargs):
        raise AssertionError("发布任务不能读取远端")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", forbidden)
    with pytest.raises(DomainError) as caught:
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=None,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=1,
            publish_only=True,
        )
    assert caught.value.code == "directory_incomplete"


@pytest.mark.parametrize("wrong_generation", [False, True])
def test_rejected_publish_does_not_wake_dispatch(
    session, directory_seed, monkeypatch, wrong_generation
):
    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(
            directory_seed,
            complete=wrong_generation,
            next_page=None if wrong_generation else 2,
        ),
        claim_generation=1,
    )
    with pytest.raises(DomainError):
        publish_directory(
            session, run_id=run.id, claim_generation=2 if wrong_generation else 1
        )
    assert not session.exec(
        select(PendingDispatch).where(
            col(PendingDispatch.task_key).startswith(
                f"directory-refill:{run.tenant_id}:{run.actor_id}:"
            )
        )
    ).all()


def test_publish_rollback_also_rolls_back_refill(session, directory_seed, monkeypatch):
    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed), claim_generation=1
    )
    with pytest.raises(RuntimeError, match="rollback"):
        with session.begin_nested():
            publish_directory(session, run_id=run.id, claim_generation=1)
            raise RuntimeError("rollback")
    session.refresh(run)
    assert run.published_version is None
    assert not session.exec(
        select(PendingDispatch).where(
            col(PendingDispatch.task_key).startswith(
                f"directory-refill:{run.tenant_id}:{run.actor_id}:"
            )
        )
    ).all()


@pytest.mark.parametrize("late", [False, True])
def test_superseded_directory_task_finishes_without_poisoning_window(
    session, directory_seed, monkeypatch, late
):
    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    older = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=older.id, page=_page(directory_seed), claim_generation=1
    )
    newer = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=newer.id, page=_page(directory_seed), claim_generation=1
    )
    publish_directory(session, run_id=newer.id, claim_generation=1)
    if late:
        monkeypatch.setattr(tasks, "directory_run_superseded", lambda *_: False)

    def unexpected_gateway(**_kwargs):
        raise AssertionError("已不可发布的旧目录不能再次调用平台")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", unexpected_gateway)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=None,
            context=directory_seed.context,
            run_id=older.id,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    session.refresh(older)
    assert older.status == "STALE" and older.error_code == "directory_run_stale"
    assert older.published_version is None and older.claimed_until is None
    assert session.get(AdDirectoryPage, (older.id, 1)) is not None
    wakeups = session.exec(
        select(PendingDispatch).where(
            col(PendingDispatch.task_key).startswith(
                f"directory-refill:{older.tenant_id}:{older.actor_id}:"
            )
        )
    ).all()
    assert not wakeups
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=None,
            context=directory_seed.context,
            run_id=older.id,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(
                    col(PendingDispatch.task_key).startswith(
                        f"directory-refill:{older.tenant_id}:{older.actor_id}:"
                    )
                )
            ).all()
        )
        == 0
    )


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("retry_ms", [50, 2500])
def test_directory_admission_wait_releases_lease_and_persists_due_successor(
    session, directory_seed, monkeypatch, redis_client, targeted, retry_ms
):

    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    if targeted:
        run.query = {**run.query, "ids": ["campaign-1"]}
    run.next_page = 2
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, complete=False, next_page=2),
        claim_generation=1,
    )
    session.flush()

    generation = _claim_directory(session, run, redis_client, monkeypatch)

    class Reader:
        def read_page(self, query):
            assert query.page == 2
            raise AccountAdmissionDeferred(retry_ms)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    before = datetime.now(UTC)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=generation,
        )
        == "WAIT"
    )
    session.refresh(run)
    assert run.claim_token is None and run.claimed_until is None
    assert run.claim_generation == generation + 1 and run.next_page == 2
    assert run.status == "WAITING_REMOTE" and run.error_code == "admission_deferred"
    assert run.next_attempt_at >= before + timedelta(milliseconds=max(1000, retry_ms))
    assert run.published_version is None and run.observed_at is None
    assert session.get(AdDirectoryPage, (run.id, 1)) is not None
    from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

    head = session.exec(
        select(SyncSchedule)
        .join(SyncExecutionScope, SyncExecutionScope.scope_id == SyncSchedule.scope_id)
        .where(SyncExecutionScope.directory_run_id == run.id)
    ).one()
    assert head.active and head.next_eligible_at == run.next_attempt_at
    assert head.leased_until is None
    # 等待只保留到期头；不再为每个限流scope写一条读取重试消息。
    assert not session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_key
            == f"{'ads.targeted_step' if targeted else 'ads.sync_step'}:{run.id}:{generation + 1}:page:2"
        )
    ).all()
    # 旧消息不能再次读远端，也不能取消新代数的恢复消息。
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=generation,
        )
        == "FAILED"
    )


def test_admission_wait_cannot_release_a_replaced_directory_claim(
    session, directory_seed, monkeypatch, redis_client
):

    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    new_token = uuid4()

    generation = _claim_directory(session, run, redis_client, monkeypatch)

    class Reader:
        def read_page(self, query):
            session.refresh(run)
            run.claim_generation = generation + 1
            run.claim_token = new_token
            run.claimed_until = datetime.now(UTC) + timedelta(minutes=2)
            session.flush()
            raise AccountAdmissionDeferred(1000)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    assert (
        tasks.run_directory_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=directory_seed.context,
            run_id=run.id,
            claim_generation=generation,
        )
        == "WAIT"
    )
    session.refresh(run)
    assert run.claim_generation == generation + 1 and run.claim_token == new_token
    assert run.claimed_until > datetime.now(UTC)
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == directory_seed.context.tenant_id
                )
            ).all()
        )
        == 1
    )


def test_directory_recovers_after_admission_wait_from_the_same_page(
    session, directory_seed, monkeypatch, redis_client
):

    from app.modules.ads import tasks

    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    run.status = "WAITING_REMOTE"
    run.error_code = "admission_deferred"
    run.claim_generation = 2
    run.next_page = 2
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, complete=False, next_page=2),
        claim_generation=2,
    )
    session.flush()

    generation = _claim_directory(session, run, redis_client, monkeypatch)

    class Reader:
        def read_page(self, query):
            assert query.page == 2
            return _page(directory_seed, page=2)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    args = {
        "database_engine": session.connection(),
        "redis_client": redis_client,
        "context": directory_seed.context,
        "run_id": run.id,
        "claim_generation": generation,
    }
    assert tasks.run_directory_step(**args) == "CONTINUE"
    session.refresh(run)
    assert run.next_page == 3 and run.error_code is None
    assert run.claim_generation > generation
    args["claim_generation"] = run.claim_generation
    assert tasks.run_directory_step(**args, publish_only=True) == "READY"
    session.refresh(run)
    assert run.status == "COMPLETE" and run.published_version is not None
