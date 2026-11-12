"""Public workspace prepare -> persistent SceneJob -> official SDK wire."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.builds.scene_job_models import SceneJob
from app.modules.builds.scene_jobs import ensure_scene_preparation, native_checkpoint
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene.test_scene_reads import wire as wire
from tests.modules.builds.scene_jobs import test_draft_bootstrap as bootstrap
from tests.modules.builds.scene_jobs.test_policy_regions import region_response
from tests.modules.builds.scene_jobs.test_service import (
    job_env as job_env,
)
from tests.modules.builds.scene_jobs.test_service import (
    role_page,
    run,
    run_capability,
)


@pytest.fixture
def source_env(monkeypatch, redis_client, isolated_strategy_database, request):
    import sys

    monkeypatch.setattr(sys.modules[__name__], "engine", isolated_strategy_database[0])
    yield from bootstrap.source_env.__wrapped__(
        monkeypatch, redis_client, isolated_strategy_database, request
    )


def selection():
    return {
        "kind": "NATIVE_SERIES_DIRECT_VIDEO",
        "advertiser_id": "actual-account",
        "native_series_id": "exact-series",
        "video_sources": [],
        "series_identity": {
            "identity_type": "TT_USER",
            "identity_id": "owner",
            "identity_authorized_bc_id": None,
        },
        "creative_identity": {
            "identity_type": "TT_USER",
            "identity_id": "owner",
            "identity_authorized_bc_id": None,
        },
        "regions": ["US"],
    }


def identities(rows, page=1, total=None):
    total = len(rows) if total is None else total
    return {
        "identity_list": rows,
        "page_info": {
            "page": page,
            "page_size": 50,
            "total_number": total,
            "total_page": (total + 49) // 50,
        },
    }


@pytest.mark.parametrize("bad_cursor", [False, True])
def test_native_job_exact_asset_complete_streams_and_unverified_combination(
    job_env, wire, redis_client, bad_cursor
):
    env = job_env
    chosen = selection()
    with Session(engine, expire_on_commit=False) as session, session.begin():
        receipt = ensure_scene_preparation(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=env["route"],
            native_selection=chosen,
        )
    job = run(env, redis_client, receipt.job_id)
    wire[1].append(role_page(["actual-account"]))
    cap_env = {**env, "request_id": uuid4()}
    run_capability(cap_env, redis_client, job.capability_job_id)
    run_capability(cap_env, redis_client, job.capability_job_id)
    owner = {
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "available_status": "AVAILABLE",
        "can_use_series": True,
        "can_push_video": True,
        "is_gpppa": False,
    }
    rows = [owner] + [owner | {"identity_id": f"other-{i}"} for i in range(49)]
    wire[1].extend(
        [
            identities(rows, total=51),
            identities([owner | {"identity_id": "last"}], page=2, total=51),
            identities([]),
            {
                "list": [
                    {
                        "native_series_id": "wrong-series",
                        "native_series_name": "Same name",
                    }
                ],
                "cursor": 0 if bad_cursor else 17,
                "has_more": True,
            },
        ]
    )
    if not bad_cursor:
        wire[1].extend(
            [
                {
                    "list": [
                        {
                            "native_series_id": "exact-series",
                            "video_sources": [],
                            "native_series_name": "Same name",
                            "native_series_cover_url": "https://example.invalid/cover",
                        }
                    ],
                    "cursor": 17,
                    "has_more": False,
                },
                region_response()["data"],
            ]
        )
    for _ in range(8):
        job = run(env, redis_client, job.id)
        if job.status != "PENDING":
            break
    if bad_cursor:
        assert job.status == "FAILED" and job.error_code == "scene_response_unverified"
        return
    assert job.status == "COMPLETE", job.error_code
    with Session(engine, expire_on_commit=False) as session:
        draft = SimpleNamespace(scene_selection=chosen, bc_id=env["bc_id"], revision=7)
        result = native_checkpoint(
            session, context=env["context"], draft=draft, route=env["route"]
        )
        assert result.state == "UNVERIFIED"
        assert result.reason_codes == ["native_series_identity_combination_unverified"]
        assert result.materials_ready is False and result.draft_revision == 7
        from app.modules.builds.scene import read_scene_context

        scene = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=env["route"],
            native_selection=chosen,
            draft_revision=7,
        )
        assert scene.supported is False
        assert scene.adgroup_fields["native_series_id"] == "exact-series"
        assert scene.field_constraints["native_checkpoint"]["draft_revision"] == 7
        assert scene.field_constraints["native_checkpoint"]["route"] == env[
            "route"
        ].model_dump(mode="json")

        assert len(result.evidence_ids) == 6
        assert len(result.facts["native_tt_identity"]["items"]) == 51
        assert (
            result.facts["native_series"]["items"][1]["native_series_id"]
            == "exact-series"
        )
        changed = selection() | {"native_series_id": "wrong-series"}
        draft.scene_selection = changed
        assert (
            native_checkpoint(
                session, context=env["context"], draft=draft, route=env["route"]
            ).state
            == "PENDING"
        )
    series_calls = [
        dict(call[2]["fields"]) for call in wire[0] if "native_series/get" in call[1]
    ]
    assert [call["cursor"] for call in series_calls] == [0, 17]
    assert all(call["count"] == 20 and "page_size" not in call for call in series_calls)
    identity_calls = [
        dict(call[2]["fields"]) for call in wire[0] if "/identity/get" in call[1]
    ]
    assert [(call["identity_type"], call["page"]) for call in identity_calls] == [
        ("TT_USER", 1),
        ("TT_USER", 2),
        ("BC_AUTH_TT", 1),
    ]


def test_workspace_prepare_drives_native_job_without_provider(
    job_env, wire, redis_client
):
    from app.modules.builds import drafts
    from app.modules.builds.catalog import draft_summary
    from app.modules.strategies.models import StrategyVersion
    from app.modules.strategies.service import create_strategy
    from tests.modules.strategies.test_versions import config

    env = job_env
    with Session(engine, expire_on_commit=False) as session, session.begin():
        strategy_id = create_strategy(
            session, context=env["context"], name="Native workspace", config=config()
        )
        version = session.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        ).one()
        draft_id = drafts.create_draft(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            strategy_version_id=version.id,
            provider_connection_id=None,
            application_id=None,
            drama_lines=[],
            account_lines=["actual-account"],
            link_config={},
            scene_selection=selection(),
        )
        prep_id = drafts.prepare_draft(
            session, context=env["context"], draft_id=draft_id, request_id=uuid4()
        )
    # Drive the existing workspace continuation. Capability reads use the same SDK wire.
    for _ in range(8):
        with Session(engine, expire_on_commit=False) as session, session.begin():
            drafts.continue_draft(session, context=env["context"], task_id=prep_id)
            from app.modules.accounts.capability_models import CapabilityJob

            pending = session.exec(
                select(CapabilityJob).where(
                    CapabilityJob.tenant_id == env["context"].tenant_id,
                    CapabilityJob.status == "PENDING",
                )
            ).first()
            native_job = session.exec(
                select(SceneJob).where(
                    SceneJob.tenant_id == env["context"].tenant_id,
                    SceneJob.native_selection.is_not(None),
                )
            ).first()
        if pending:
            wire[1].append(role_page(["actual-account"]))
            run_capability({**env, "request_id": uuid4()}, redis_client, pending.id)
            run_capability({**env, "request_id": uuid4()}, redis_client, pending.id)
        if native_job:
            break
    assert native_job is not None
    owner = {
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "available_status": "AVAILABLE",
        "can_use_series": True,
        "can_push_video": True,
        "is_gpppa": False,
    }
    wire[1].extend(
        [
            identities([owner]),
            identities([]),
            {
                "list": [{"native_series_id": "exact-series"}],
                "cursor": 0,
                "has_more": False,
            },
            region_response()["data"],
        ]
    )
    for _ in range(6):
        native_job = run(env, redis_client, native_job.id)
        if native_job.status != "PENDING":
            break
    assert native_job.status == "COMPLETE", native_job.error_code
    with Session(engine, expire_on_commit=False) as session, session.begin():
        assert drafts.continue_draft(session, context=env["context"], task_id=prep_id)
        result = draft_summary(session, context=env["context"], draft_id=draft_id)
        assert result.status == "BLOCKED"
        assert result.error_code == "native_series_identity_combination_unverified"
        assert result.provider_task_id is None
        assert (
            result.scene_selection.series_identity
            == result.scene_selection.creative_identity
        )
    assert not any("minis/get" in call[1] for call in wire[0])


@pytest.mark.parametrize(
    "payload,valid",
    [
        ({"list": [], "cursor": 0, "has_more": False}, True),
        (
            {
                "list": [{"native_series_id": "same"}, {"native_series_id": "same"}],
                "cursor": 0,
                "has_more": False,
            },
            False,
        ),
        ({"list": [], "cursor": 0, "has_more": True}, False),
        ({"list": [], "cursor": 17}, False),
    ],
)
def test_native_series_sdk_empty_duplicate_and_incomplete_pages(wire, payload, valid):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    import business_api_client as sdk

    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.accounts import RuntimeReadContext
    from app.integrations.tiktok.official.scenes import OfficialScenesGateway

    @contextmanager
    def scope(_advertiser_id, _operation, _deadline):
        yield "offline-token"

    client = sdk.ApiClient()
    client.default_headers["Access-Token"] = "offline-token"
    wire[1].append(payload)
    gateway = OfficialScenesGateway(
        client,
        context=RuntimeReadContext(bc_id="bc-test"),
        request_scope=scope,
        deadline=datetime.now(UTC) + timedelta(seconds=20),
    )
    try:
        if valid:
            page = gateway.read_page(
                resource="native_series",
                advertiser_id="actual-account",
                page=1,
                minis_id=None,
                native_selection=selection(),
            )
            assert page.last is True and page.facts.items == ()
        else:
            with pytest.raises(DomainError) as error:
                gateway.read_page(
                    resource="native_series",
                    advertiser_id="actual-account",
                    page=1,
                    minis_id=None,
                    native_selection=selection(),
                )
            assert error.value.code == "scene_response_unverified"
    finally:
        client.rest_client.pool_manager.clear()


def test_native_pending_selection_discovers_identities_without_fake_series(
    job_env, wire, redis_client
):
    env = job_env
    pending = selection() | {
        "native_series_id": None,
        "series_identity": None,
        "creative_identity": None,
    }
    with Session(engine) as session, session.begin():
        receipt = ensure_scene_preparation(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=env["route"],
            native_selection=pending,
        )
    job = run(env, redis_client, receipt.job_id)
    wire[1].append(role_page(["actual-account"]))
    run_capability({**env, "request_id": uuid4()}, redis_client, job.capability_job_id)
    run_capability({**env, "request_id": uuid4()}, redis_client, job.capability_job_id)
    wire[1].extend(
        [
            identities(
                [
                    {
                        "identity_type": "TT_USER",
                        "identity_id": "actual-owner",
                        "can_use_series": True,
                    }
                ]
            ),
            identities([]),
            region_response()["data"],
        ]
    )
    for _ in range(4):
        job = run(env, redis_client, job.id)
        if job.status != "PENDING":
            break
    assert job.status == "COMPLETE", job.error_code
    with Session(engine) as session:
        checkpoint = native_checkpoint(
            session,
            context=env["context"],
            draft=SimpleNamespace(
                scene_selection=pending, bc_id=env["bc_id"], revision=1
            ),
            route=env["route"],
        )
        assert checkpoint.state == "BLOCKED"
        assert "native_series_identity_selection_required" in checkpoint.reason_codes
        assert (
            checkpoint.facts["native_tt_identity"]["items"][0]["identity_id"]
            == "actual-owner"
        )
    assert not any("native_series/get" in call[1] for call in wire[0])
