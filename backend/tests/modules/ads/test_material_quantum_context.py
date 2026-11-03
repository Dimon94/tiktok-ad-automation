"""真实SDK/PG：同一次量子不得逐页反序列化整个冻结素材桥。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.ads import tasks
from app.modules.ads.material_directory_children import MATERIAL_CONTEXT_PAGE
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from tests.integrations.tiktok.gateway_support import business_calls
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.integrations.tiktok.test_ads_read_adapters import page, replies
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.ads.test_material_directory_children import (
    durable_material_case as durable_material_case,
)


@pytest.fixture
def material_case(
    request,
    database_engine,
    redis_client,
    durable_material_case,
    gateway_wire,
    monkeypatch,
):
    total = getattr(request, "param", 4002)
    context, route, advertiser = durable_material_case
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_: None)
    primary = [
        {
            "advertiser_id": advertiser,
            "smart_plus_ad_id": ad,
            "adgroup_id": "group",
            "creative_list": [
                {
                    "smart_plus_creative_id": creative,
                    "ad_material_id": material,
                    "creative_info": {"video_info": {"video_id": video}},
                }
            ],
        }
        for ad, creative, material, video in [
            ("ad-a", "creative-0", "material-0", "video-0"),
            ("ad-b", "creative-b", "material-b", "video-b"),
        ]
    ]
    creatives = [
        {
            "advertiser_id": advertiser,
            "smart_plus_ad_id": "ad-a",
            "adgroup_id": "group",
            "ad_id": f"creative-{i}",
            "video_id": f"video-{i}",
        }
        for i in range(total - 1)
    ]
    creatives.append(
        {
            "advertiser_id": advertiser,
            "smart_plus_ad_id": "ad-b",
            "adgroup_id": "group",
            "ad_id": "creative-b",
            "video_id": "video-b",
        }
    )
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            "smart_plus_ad_get": [page(primary, size=10)],
            "ad_get": [
                page(
                    creatives[i : i + 1000],
                    number=i // 1000 + 1,
                    size=1000,
                    total=total,
                )
                for i in range(0, total, 1000)
            ],
        },
    )
    with Session(database_engine) as db, db.begin():
        source = AdDirectoryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            advertiser_id=advertiser,
            bc_id=route.bc_id,
            connection_id=route.connection_id,
            channel=route.channel,
            frozen_route=route.model_dump(mode="json"),
            workload="background",
            kind="ad",
            ad_type="SMART_PLUS",
            partition_key=uuid4().hex * 2,
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
        db.add(source)
        db.flush()
        source_id = source.id
        register_directory_run(db, source, new_execution=True)

    def claim(identity):
        with Session(database_engine) as db, db.begin():
            run = db.get(AdDirectoryRun, identity)
            previous = run.claim_generation
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=40,
                )
                >= 1
            )
            db.refresh(run)
            assert run.claim_generation > previous
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

    generation = claim(source_id)
    assert run(source_id, generation) == "CONTINUE"
    assert run(source_id, generation, publish_only=True) == "READY"
    with Session(database_engine) as db:
        child = db.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == context.tenant_id,
                AdDirectoryRun.kind == "creative",
            )
        ).one()
        child_id = child.id
    return SimpleNamespace(
        engine=database_engine,
        context=context,
        child_id=child_id,
        claim=claim,
        run=run,
        wire=gateway_wire,
        total=total,
    )


def _context_reads():
    seen = []

    def before(_connection, _cursor, statement, params, _context, _many):
        values = params.values() if isinstance(params, dict) else params
        if "ad_directory_page" in statement and MATERIAL_CONTEXT_PAGE in values:
            seen.append(statement)

    return seen, before


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_five_page_quantum_only_loads_frozen_context_at_entry(material_case):
    case = material_case
    generation = case.claim(case.child_id)
    seen, before = _context_reads()
    event.listen(case.engine, "before_cursor_execute", before)
    try:
        assert case.run(case.child_id, generation) == "CONTINUE"
    finally:
        event.remove(case.engine, "before_cursor_execute", before)
    assert len(seen) == 3  # 首次streaming核验2次、完整载入1次，后四页复用。
    assert len(business_calls(case.wire, "OFFICIAL_API")) == 6
    with Session(case.engine) as db:
        child = db.get(AdDirectoryRun, case.child_id)
        assert child.next_page == 6 and child.claimed_until is None
        assert db.get(AdDirectoryPage, (child.id, 5)).complete


def _after_first_child(case, action):
    original = case.wire["before"]["callback"]

    def callback():
        if len(business_calls(case.wire, "OFFICIAL_API")) == 1:
            action()
        original()

    case.wire["before"]["callback"] = callback


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("owners", ["all", "one", "missing"])
def test_owner_freshness_is_rechecked_between_cached_pages(material_case, owners):
    case = material_case

    def update():
        with Session(case.engine) as db, db.begin():
            ads = db.exec(
                select(AdObject).where(AdObject.tenant_id == case.context.tenant_id)
            ).all()
            for ad in ads:
                if owners == "all" or ad.remote_id == "ad-a":
                    if owners == "missing":
                        db.delete(ad)
                    else:
                        ad.observed_at = datetime.now(UTC) + timedelta(seconds=1)
                        db.add(ad)

    _after_first_child(case, update)
    generation = case.claim(case.child_id)
    case.run(case.child_id, generation)
    calls = len(business_calls(case.wire, "OFFICIAL_API"))
    with Session(case.engine) as db:
        child = db.get(AdDirectoryRun, case.child_id)
        if owners == "all":
            assert calls == 2 and child.status == "STALE"
            assert db.get(AdDirectoryPage, (child.id, 2)) is None
        else:
            assert calls == 6 and child.next_page == 6


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("field", ["marker", "kind", "ad_type", "route", "query"])
def test_changed_frozen_input_cannot_reuse_context(material_case, field, monkeypatch):
    case = material_case

    def change():
        with Session(case.engine) as db, db.begin():
            child = db.get(AdDirectoryRun, case.child_id)
            if field == "marker":
                child.query = {
                    k: v for k, v in child.query.items() if k != "material_child_v1"
                }
            elif field == "route":
                child.frozen_route = {**child.frozen_route, "binding_revision": 999}
            elif field == "query":
                child.query = {**child.query, "parent_ids": ["changed-group"]}
            else:
                setattr(child, field, "ad" if field == "kind" else "REGULAR")
            db.add(child)

    original = tasks._directory_session
    changed = False

    @contextmanager
    def after_committed_page(*args, **kwargs):
        nonlocal changed
        with original(*args, **kwargs) as db:
            yield db
        if not changed:
            with Session(case.engine) as db:
                committed = db.get(AdDirectoryPage, (case.child_id, 1)) is not None
            if committed:
                changed = True
                change()

    monkeypatch.setattr(tasks, "_directory_session", after_committed_page)
    generation = case.claim(case.child_id)
    with pytest.raises(DomainError) as caught:
        case.run(case.child_id, generation)
    assert caught.value.code == "directory_scope_mismatch"
    assert changed
    assert len(business_calls(case.wire, "OFFICIAL_API")) == 2


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("material_case", [6002], indirect=True)
def test_new_quantum_reloads_context(material_case):
    case = material_case
    first = case.claim(case.child_id)
    assert case.run(case.child_id, first) == "CONTINUE"
    second = case.claim(case.child_id)
    seen, before = _context_reads()
    event.listen(case.engine, "before_cursor_execute", before)
    try:
        assert case.run(case.child_id, second) == "CONTINUE"
    finally:
        event.remove(case.engine, "before_cursor_execute", before)
    assert len(seen) == 3
    assert len(business_calls(case.wire, "OFFICIAL_API")) == 8


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_initially_superseded_child_does_not_eagerly_load_bridge(
    material_case, monkeypatch
):
    case = material_case
    with Session(case.engine) as db, db.begin():
        for ad in db.exec(
            select(AdObject).where(AdObject.tenant_id == case.context.tenant_id)
        ).all():
            ad.observed_at = datetime.now(UTC) + timedelta(seconds=1)
            db.add(ad)

    def unexpected(*_args, **_kwargs):
        pytest.fail("stale entry must not eagerly load material scope")

    monkeypatch.setattr(tasks, "material_child_scope", unexpected)
    generation = case.claim(case.child_id)
    case.run(case.child_id, generation)
    assert len(business_calls(case.wire, "OFFICIAL_API")) == 1
    with Session(case.engine) as db:
        assert db.get(AdDirectoryRun, case.child_id).status == "STALE"
