from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.directory_requests import (
    DIRECTORY_AD_TYPES,
    KINDS_BY_TYPE,
    directory_ad_types,
    directory_unblocked_accounts,
    request_directory,
)
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed


def test_full_directory_creates_one_run_per_level_and_is_idempotent(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    route = {
        "tenant_id": str(sync_seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(sync_seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": route,
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "directory",
    }
    created = request_directory(session, **args)
    assert created == 10  # campaign/adgroup/ad × 3 类型 + Smart+ creative，与原调度一致
    assert request_directory(session, **args) == 0
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert all(r.query["include_deleted"] for r in runs)


def test_switch_and_mcp_route_never_create_runs(session, sync_seed, monkeypatch):
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "directory",
    }
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    assert (
        request_directory(session, frozen_route={"channel": "OFFICIAL_API"}, **args)
        == 0
    )
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    assert (
        request_directory(session, frozen_route={"channel": "OFFICIAL_MCP"}, **args)
        == 0
    )
    assert not session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()


def _route(seed):
    return {
        "tenant_id": str(seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }


def _complete_probes(session, seed, advertiser_id, *, completed_at):
    """模拟一轮全量目录已成功完成，留下每种类型的探测记录。"""
    for run in session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == seed.tenant_id,
            AdDirectoryRun.advertiser_id == advertiser_id,
        )
    ).all():
        run.status, run.completed_at = "COMPLETE", completed_at
        run.published_version = 1
        session.add(run)
    session.flush()


def _ad_object(seed, advertiser_id, ad_type):
    return AdObject(
        tenant_id=seed.tenant_id,
        advertiser_id=advertiser_id,
        kind="campaign",
        remote_id=f"c-{advertiser_id}-{ad_type}",
        ad_type=ad_type,
        observed_at=datetime.now(UTC),
        published_version=1,
    )


PROBES = dict.fromkeys(DIRECTORY_AD_TYPES, ("campaign",))


def test_unused_types_share_partition_calibration_freshness(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    args = {"tenant_id": sync_seed.tenant_id, "advertiser_ids": ["acc-1", "acc-2"]}

    # 首次：没有任何目录与探测记录，各类型只探测广告系列。
    first = directory_ad_types(session, **args, active_ids=[])
    assert first == {"acc-1": PROBES, "acc-2": PROBES}
    created = request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1", "acc-2"],
        scope="directory",
        ad_types=first,
    )
    assert created == 6
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {r.kind for r in runs} == {"campaign"}

    # 探测完成后：acc-1 只在用 Smart+（全部层级），acc-2 没有任何广告。
    _complete_probes(session, sync_seed, "acc-1", completed_at=now)
    _complete_probes(session, sync_seed, "acc-2", completed_at=now)
    session.add(_ad_object(sync_seed, "acc-1", "SMART_PLUS"))
    session.flush()
    later = now + timedelta(hours=3)
    choices = directory_ad_types(session, **args, active_ids=[])
    assert choices == {
        "acc-1": {**PROBES, "SMART_PLUS": ("campaign", "adgroup", "ad")},
        "acc-2": PROBES,
    }
    # 近期已发布的系列探测跳过；Smart+ 尚缺组/广告，单独创建这两个分区。
    assert (
        request_directory(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            frozen_route=_route(sync_seed),
            advertiser_ids=["acc-1", "acc-2"],
            scope="directory",
            ad_types=choices,
            periodic_intervals={"acc-1": timedelta(days=1), "acc-2": timedelta(days=7)},
            now=later,
        )
        == 2
    )
    assert directory_ad_types(session, **args, active_ids=["acc-2"])[
        "acc-2"
    ] == dict.fromkeys(DIRECTORY_AD_TYPES, ("campaign", "adgroup", "ad"))


def test_trimmed_request_only_creates_runs_for_used_types(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    created = request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1", "acc-2"],
        scope="directory",
        ad_types={"acc-1": {"SMART_PLUS": KINDS_BY_TYPE["SMART_PLUS"]}, "acc-2": {}},
    )
    # Smart+ 的广告系列、广告组、广告与创意共 4 个；空账户本轮不请求。
    assert created == 4
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {(r.advertiser_id, r.ad_type) for r in runs} == {("acc-1", "SMART_PLUS")}


def test_queued_run_absorbs_later_identical_requests(session, sync_seed, monkeypatch):
    """积压期间后续轮次的相同查询并入尚未开始的运行，不叠加；已领取后才新建。"""
    from uuid import uuid4

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    base = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "scope": "directory",
        "ad_types": {
            "acc-1": {"SMART_PLUS": ("campaign",)},
            "acc-2": {"SMART_PLUS": ("campaign",)},
        },
    }
    assert request_directory(session, advertiser_ids=["acc-1"], **base) == 1
    # 账户集合不同即是另一次请求；acc-1 的相同查询并入排队中的运行。
    assert request_directory(session, advertiser_ids=["acc-1", "acc-2"], **base) == 1
    queued = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
            AdDirectoryRun.advertiser_id == "acc-1",
        )
    ).one()
    queued.claim_token = uuid4()
    session.flush()
    # 已被领取的运行可能已读过首页，不能代替新请求，因此 acc-1 新建一条。
    assert request_directory(session, advertiser_ids=["acc-2", "acc-1"], **base) == 1


def test_targeted_read_is_queued_immediately_and_completed_run_is_never_reused(
    session, sync_seed, monkeypatch
):
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.jobs.models import PendingDispatch
    from app.jobs.tasks import dispatch_queue

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "targeted",
        "refs": [EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")],
        "ad_types": {"acc-1": {"SMART_PLUS": ("ad",)}},
    }
    assert request_directory(session, **args) == 1
    run = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    assert dispatch.task_name == "reporting.plan_due"
    assert dispatch_queue(dispatch.task_name) == "control"
    run.status, run.completed_at = "COMPLETE", datetime.now(UTC)
    session.flush()
    # 同一秒再次启停也必须读取新的平台状态；完成的旧结果不作为新操作回读。
    assert request_directory(session, **args) == 1


def test_recent_success_does_not_hide_other_directory_partitions(session, sync_seed):
    # 账户阶段只判断在途；真正新鲜度在周期请求入口按完整分区核对。
    session.add(
        AdDirectoryRun(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-2",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            connection_id=sync_seed.connection.id,
            frozen_route=_route(sync_seed),
            partition_key="a" * 64,
            query={"include_deleted": True},
            status="COMPLETE",
            claim_generation=1,
            completed_at=datetime.now(UTC),
            kind="campaign",
            ad_type="SMART_PLUS",
        )
    )
    session.flush()
    assert directory_unblocked_accounts(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
        advertiser_ids=["acc-1", "acc-2"],
    ) == ["acc-1", "acc-2"]


@pytest.mark.parametrize(
    "status", ["QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE"]
)
def test_pending_full_directory_excludes_overlapping_accounts(
    session, sync_seed, monkeypatch, status
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "advertiser_ids": ["acc-1"],
    }
    request_directory(session, **args, scope="directory")
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    for run in runs:
        run.status = status
    session.flush()
    args["advertiser_ids"] = ["acc-1", "acc-2"]
    assert directory_unblocked_accounts(session, **args) == ["acc-2"]
    # 冻结授权变化后的新任务不能被旧授权积压遮挡。
    args["frozen_route"] = {**args["frozen_route"], "binding_revision": 1}
    assert directory_unblocked_accounts(session, **args) == ["acc-1", "acc-2"]


@pytest.mark.parametrize(
    "scope,status",
    [
        ("targeted", "RUNNING"),
        ("directory", "COMPLETE"),
        ("directory", "FAILED"),
        ("directory", "CANCELLED"),
        ("directory", "STALE"),
    ],
)
def test_targeted_and_terminal_directory_do_not_block_active_refresh(
    session, sync_seed, monkeypatch, scope, status
):
    from app.integrations.tiktok.contracts.ads import EntityRef

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "advertiser_ids": ["acc-1"],
    }
    request_directory(
        session,
        **args,
        scope=scope,
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "ad-1")]
        if scope == "targeted"
        else (),
    )
    for run in session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all():
        run.status = status
    session.flush()
    assert directory_unblocked_accounts(session, **args) == ["acc-1"]


@pytest.mark.parametrize("active", [[], ["acc-1"]])
def test_periodic_ads_already_cover_creatives_without_second_full_scan(
    session, sync_seed, active
):
    """广告整页读取自带创意补齐，周期只投系列、组、广告；显式完整请求仍可含创意。"""
    session.add(_ad_object(sync_seed, "acc-1", "SMART_PLUS"))
    session.flush()
    types = directory_ad_types(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1"],
        active_ids=active,
    )
    assert types["acc-1"]["SMART_PLUS"] == ("campaign", "adgroup", "ad")


@pytest.mark.parametrize(
    "kind,parent_kind", [("ad", "adgroup"), ("adgroup", "campaign")]
)
def test_explicit_directory_ids_are_not_intersected_with_other_refresh_parents(
    session, sync_seed, monkeypatch, kind, parent_kind
):
    """分层刷新时父子候选不同；明确的 101 个对象不能被另一批父级裁掉。"""
    from app.integrations.tiktok.adapters.ads_read import compile_query
    from app.integrations.tiktok.contracts.ads import DirectoryQuery, EntityRef

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    expected = {f"object-{i}" for i in range(101)}
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="targeted",
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", kind, x) for x in expected]
        + [EntityRef(sync_seed.tenant_id, "acc-1", parent_kind, "other-parent")],
        ad_types={"acc-1": {"SMART_PLUS": (kind,)}},
    )
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(runs) == 2
    assert {x for r in runs for x in r.query["ids"]} == expected
    assert all(not r.query["parent_ids"] for r in runs)
    for run in runs:
        data = run.query | {"ids": tuple(run.query["ids"]), "parent_ids": ()}
        # 持久化调度目的与 Worker 一样先剥离，平台过滤语义不变。
        data.pop("directory_service_v1", None)
        _, args = compile_query(DirectoryQuery(**data))
        assert f"{parent_kind}_ids" not in args["filtering"]


@pytest.mark.parametrize(
    "kind,parent_kind", [("ad", "adgroup"), ("adgroup", "campaign")]
)
def test_parent_only_directory_request_still_reads_children(
    session, sync_seed, monkeypatch, kind, parent_kind
):
    from app.integrations.tiktok.contracts.ads import EntityRef

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="targeted",
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", parent_kind, "parent-1")],
        ad_types={"acc-1": {"SMART_PLUS": (kind,)}},
    )
    run = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).one()
    assert run.query["ids"] == []
    assert run.query["parent_ids"] == ["parent-1"]


@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad", "creative"])
def test_type_presence_includes_deleted_objects_in_every_layer(
    session, sync_seed, kind
):
    obj = _ad_object(sync_seed, "acc-1", "LEGACY_SMART_PLUS")
    obj.kind = kind
    obj.operation_status = "DELETED"
    session.add(obj)
    # 即使没有上级记录或对象已经删除，实际类型仍须刷新所有周期层级。
    result = directory_ad_types(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1", "acc-2"],
        active_ids=[],
    )
    assert result["acc-1"]["LEGACY_SMART_PLUS"] == ("campaign", "adgroup", "ad")
    assert result["acc-2"] == PROBES


def test_unknown_historical_type_does_not_mean_empty_active_account(session, sync_seed):
    session.add(_ad_object(sync_seed, "acc-1", "UNKNOWN_HISTORICAL"))
    result = directory_ad_types(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1"],
        active_ids=["acc-1"],
    )
    assert result == {"acc-1": PROBES}
