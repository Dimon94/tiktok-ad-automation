"""Real committed jobs, official SDK transport doubles, no live API."""

import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts import capabilities
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from app.modules.providers.models import PromotionLink
from tests.modules.accounts.capabilities.test_service import page as role_page
from tests.modules.accounts.capabilities.test_service import run as run_capability
from tests.modules.builds.scene.test_scene_reads import page
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_policy_regions import region_response
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.fixture
def job_env(scene_env, monkeypatch):
    assert importlib.util.find_spec("app.modules.builds.scene_jobs"), (
        "Shared scene orchestration is missing"
    )
    from app.modules.builds import scene_jobs

    monkeypatch.setattr(scene_jobs, "_require_bounded_worker", lambda: None)
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    return scene_env


def ensure(env, link_id=None):
    from app.modules.builds.scene_jobs import ensure_scene_preparation

    with Session(engine) as session, session.begin():
        return ensure_scene_preparation(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=link_id or env["link_id"],
            route=env["route"],
        )


def run(env, redis_client, job_id):
    from app.modules.builds.scene_jobs import process_scene_job

    with Session(engine) as session, session.begin():
        job = session.get(SceneJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
        revision = job.revision
    process_scene_job(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload={"job_id": str(job_id), "revision": revision},
    )
    with Session(engine) as session:
        return session.get(SceneJob, job_id)


def responses(env):
    return [
        page(
            [
                {
                    "identity_id": "fixture-identity",
                    "identity_type": "BC_AUTH_TT",
                    "identity_authorized_bc_id": env["bc_id"],
                    "available_status": "AVAILABLE",
                    "can_push_video": True,
                    "is_gpppa": False,
                }
            ],
            key="identity_list",
        ),
        page(
            [
                {
                    "minis_id": "fixture-minis",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US", "CA"],
                }
            ]
        ),
        {
            "recommend_assets": [
                {"asset_ids": ["cta-watch"], "asset_content": "Watch now"}
            ]
        },
        {"vo_min_roas": "QUALIFIED"},
        region_response()["data"],
    ]


def complete(env, wire, redis_client):
    receipt = ensure(env)
    job = run(env, redis_client, receipt.job_id)
    assert job.capability_job_id
    wire[1].append(role_page(["actual-account"]))
    cap_env = {**env, "request_id": uuid4()}
    run_capability(cap_env, redis_client, job.capability_job_id)
    run_capability(cap_env, redis_client, job.capability_job_id)
    wire[1].extend(responses(env))
    for _ in range(8):
        job = run(env, redis_client, job.id)
        if job.status != "PENDING":
            break
    assert job.status == "COMPLETE", job.error_code
    return job


@pytest.mark.parametrize("exhaust_budget", [False, True])
def test_scene_turn_continues_ready_reads_with_a_shared_time_budget(
    job_env, wire, redis_client, monkeypatch, exhaust_budget
):
    from app.modules.builds import scene_jobs

    env = job_env
    receipt = ensure(env)
    job = run(env, redis_client, receipt.job_id)
    wire[1].append(role_page(["actual-account"]))
    cap_env = {**env, "request_id": uuid4()}
    run_capability(cap_env, redis_client, job.capability_job_id)
    run_capability(cap_env, redis_client, job.capability_job_id)
    wire[1].extend(responses(env))
    with Session(engine) as db, db.begin():
        current = db.get(SceneJob, job.id)
        current.due_at = datetime.now(UTC) - timedelta(seconds=1)
        payload = {"job_id": str(job.id), "revision": current.revision}
    moments = iter([0.0, 9.0] if exhaust_budget else [0.0] * 10)
    monkeypatch.setattr(scene_jobs, "monotonic", lambda: next(moments), raising=False)
    scene_jobs.process_scene_turn(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload=payload,
    )
    with Session(engine) as db:
        current = db.get(SceneJob, job.id)
        assert current.status == ("PENDING" if exhaust_budget else "COMPLETE")
        assert current.resource == ("minis" if exhaust_budget else "done")
        assert current.dispatch_id is not None
    assert len(wire[0]) == (2 if exhaust_budget else 6)
    # 已消费的旧代消息不重读平台，也不能偷偷推进后续代数。
    monkeypatch.setattr(scene_jobs, "monotonic", lambda: 0.0)
    scene_jobs.process_scene_turn(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload=payload,
    )
    assert len(wire[0]) == (2 if exhaust_budget else 6)


def test_confirmed_same_mini_links_share_scene_and_bc_proof(
    job_env, wire, redis_client
):
    from app.modules.builds.scene import read_scene_context

    env = job_env
    with Session(engine) as session, session.begin():
        original = session.get(PromotionLink, env["link_id"])
        clone = PromotionLink(
            **(
                original.model_dump()
                | {
                    "id": uuid4(),
                    "reuse_key": "b" * 64,
                    "is_current": False,
                    "url": "https://example.com/another-drama",
                }
            )
        )
        session.add(clone)
        session.flush()
        other_link = clone.id
        from app.modules.builds.mini_targets import remember_target

        remember_target(
            session,
            context=env["context"],
            url=clone.url,
            minis_id="fixture-minis",
            source="USER",
        )
    first = ensure(env)
    second = ensure(env, other_link)
    assert first.job_id == second.job_id and first.state == second.state == "queued"
    job = complete(env, wire, redis_client)
    assert len(wire[0]) == 6  # 一次角色读取和五类必要场景，不查历史广告组。
    assert not any("smart_plus/adgroup/get" in str(call) for call in wire[0])
    assert all(call[0] == "GET" for call in wire[0])
    assert ensure(env, other_link).state == "ready"
    with Session(engine) as session:
        result = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=other_link,
            route=env["route"],
        )
        assert result.supported, result.reason_codes
        assert result.adgroup_fields["targeting_spec"]["location_ids"] == (
            "6251999",
            "6252001",
        )
        assert result.adgroup_fields["placement_type"] == "PLACEMENT_TYPE_NORMAL"
        assert result.adgroup_fields["placements"] == ("PLACEMENT_TIKTOK",)
        assert result.copy_length_limit == 100
        assert result.field_constraints["platform_copy_length"] is None
        assert (
            len(
                session.exec(
                    select(SceneJobPage).where(SceneJobPage.job_id == job.id)
                ).all()
            )
            == 5
        )


def test_changed_mini_reuses_fresh_mini_independent_scene_facts(
    job_env, wire, redis_client
):
    from app.modules.builds.mini_targets import remember_target

    env = job_env
    original = complete(env, wire, redis_client)
    with Session(engine) as session, session.begin():
        page_row = session.exec(
            select(SceneJobPage).where(
                SceneJobPage.job_id == original.id,
                SceneJobPage.resource == "minis",
            )
        ).one()
        page_row.facts = {
            **page_row.facts,
            "options": [
                *page_row.facts["options"],
                {
                    "minis_id": "alternate-minis",
                    "name": "Alternate",
                    "status": "ACTIVE",
                    "type": "MINI_SERIES",
                    "regions": ["US", "CA"],
                },
            ],
        }
        link = session.get(PromotionLink, env["link_id"])
        remember_target(
            session,
            context=env["context"],
            url=link.url,
            minis_id="alternate-minis",
            source="USER",
        )

    prepared = ensure(env)
    assert prepared.state == "ready"
    assert prepared.job_id != original.id
    with Session(engine) as session:
        reused = session.get(SceneJob, prepared.job_id)
        assert reused.status == "COMPLETE" and reused.resource == "done"
        assert reused.facts["reused_scene_job_id"] == str(original.id)
        assert set(reused.facts) >= {
            "identity",
            "minis",
            "cta",
            "vbo",
            "regions",
        }
    assert len(wire[0]) == 6


@pytest.mark.parametrize("expired", [False, True])
def test_new_mini_reuses_only_fresh_account_facts(job_env, wire, redis_client, expired):
    from app.modules.builds.mini_targets import remember_target
    from app.modules.builds.scene import read_scene_context

    env = job_env
    original = complete(env, wire, redis_client)
    with Session(engine) as session, session.begin():
        link = session.get(PromotionLink, env["link_id"])
        remember_target(
            session,
            context=env["context"],
            url=link.url,
            minis_id="new-minis",
            source="USER",
        )
        if expired:
            source = session.get(SceneJob, original.id)
            source.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    prepared = ensure(env)
    assert prepared.state == "queued"
    with Session(engine) as session:
        job = session.get(SceneJob, prepared.job_id)
        if expired:
            assert "reused_scene_job_id" not in job.facts
            assert job.resource == "capabilities"
            return
        assert job.resource == "minis"
        assert job.facts["reused_scene_job_id"] == str(original.id)
        assert job.expires_at == original.expires_at
        assert "minis" not in job.facts
    # 新小程序不在旧目录；仍须真实读取并确认，不能用旧 Mini 的资格代替。
    wire[1].append(
        page(
            [
                {
                    "minis_id": "new-minis",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US", "CA"],
                }
            ]
        )
    )
    job = run(env, redis_client, prepared.job_id)
    assert job.status == "COMPLETE"
    assert job.expires_at == original.expires_at
    assert len(wire[0]) == 7  # 初次六次 + 一次新 Mini；其余四类不重复读取。
    with Session(engine) as session:
        result = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=env["link_id"],
            route=env["route"],
        )
        assert result.supported


def test_complete_scene_reader_is_readonly_no_credentials_or_network(
    job_env, wire, redis_client, monkeypatch
):
    from app.modules.builds import scene, scene_jobs

    env = job_env
    complete(env, wire, redis_client)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pure scene read performed a side effect")

    for module, name in [
        (scene_jobs, "open_tiktok_gateway"),
        (scene_jobs, "enqueue_after_commit"),
    ]:
        monkeypatch.setattr(module, name, forbidden)
    statements = []

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement.lstrip().split()[0])

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = scene.read_scene_context(
                session,
                context=env["context"],
                bc_id=env["bc_id"],
                advertiser_id="actual-account",
                link_id=env["link_id"],
                route=env["route"],
            )
            assert result.supported
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert set(statements) == {"SELECT", "SET"}


def test_partial_scene_never_reports_ready(job_env, wire):
    from app.modules.builds.scene import read_scene_context

    env = job_env
    receipt = ensure(env)
    with Session(engine) as session:
        result = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=env["link_id"],
            route=env["route"],
        )
        assert not result.supported and "scene_evidence_missing" in result.reason_codes
    assert receipt.state == "queued" and not wire[0]


def test_missing_provider_mapping_still_discovers_named_minis(
    job_env, wire, redis_client
):
    from app.modules.providers.models import ProviderApplication

    with Session(engine) as session, session.begin():
        link = session.get(PromotionLink, job_env["link_id"])
        app = session.exec(
            select(ProviderApplication).where(
                ProviderApplication.tenant_id == link.tenant_id,
                ProviderApplication.connection_id == link.connection_id,
                ProviderApplication.external_id == link.application_id,
            )
        ).one()
        app.tiktok_minis_id = None
        from app.modules.builds.mini_targets import MiniTarget, url_key

        session.delete(session.get(MiniTarget, (link.tenant_id, url_key(link.url))))
    prepared = ensure(job_env)
    assert prepared.state == "queued"
    job = run(job_env, redis_client, prepared.job_id)
    wire[1].append(role_page(["actual-account"]))
    run_capability(job_env, redis_client, job.capability_job_id)
    run_capability(job_env, redis_client, job.capability_job_id)
    wire[1].append(
        page(
            [
                {
                    "minis_id": "real-mini",
                    "minis_name": "LemonShow",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US"],
                }
            ]
        )
    )
    job = run(job_env, redis_client, prepared.job_id)
    assert job.status == "COMPLETE", job.error_code
    with Session(engine) as session:
        stored = session.exec(
            select(SceneJobPage).where(SceneJobPage.job_id == job.id)
        ).one()
        assert stored.facts["options"][0]["name"] == "LemonShow"
    assert ensure(job_env).reason_code == "minis_selection_required"
    assert len(wire[0]) == 2  # 角色与 Mini 目录；不提前读取全部创建资源。
    # 明确路径必须与真实目录中的 ID 完全一致才可自动匹配；随后复用目录。
    with Session(engine) as db, db.begin():
        db.get(
            PromotionLink, job_env["link_id"]
        ).url = "https://www.tiktok.com/minis/real-mini"
    selected = ensure(job_env)
    assert selected.state == "queued" and selected.job_id != prepared.job_id
    with Session(engine) as db:
        selected_job = db.get(SceneJob, selected.job_id)
        assert selected_job.minis_id == "real-mini"
        assert selected_job.facts["minis_catalog_job_id"] == str(job.id)
        assert selected_job.expires_at == job.expires_at
    wire[1].append(responses(job_env)[0])
    selected_job = run(job_env, redis_client, selected.job_id)
    assert selected_job.resource == "cta"  # 无第二次 Mini 目录读取。
    wire[1].extend(responses(job_env)[2:])
    for _ in range(4):
        selected_job = run(job_env, redis_client, selected.job_id)
    assert selected_job.status == "COMPLETE", selected_job.error_code
    assert ensure(job_env).state == "ready"
    assert len(wire[0]) == 6
