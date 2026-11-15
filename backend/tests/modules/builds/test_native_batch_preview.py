"""JB-15 contract checks through real preparation and preview owners."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.ads.models import AdObject
from app.modules.builds import previews
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.scene_jobs.test_native_series import job_env as job_env
from tests.modules.builds.scene_jobs.test_native_series import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_native_series import source_env as source_env
from tests.modules.builds.scene_jobs.test_native_series import wire as wire
from tests.modules.builds.test_native_plan_set import two_account_workspace


def test_two_prepared_plans_freeze_exact_budget(job_env, wire, redis_client):
    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    from uuid import UUID

    pid = UUID(response.json()["preview_id"])
    response = client.get(
        f"/tenants/{job_env['context'].tenant_id}/build-previews/{pid}"
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["daily_budget_sum"]) == Decimal("200.50")
    with Session(native.engine) as s, s.begin():
        result = previews.get_preview_summary(
            s, context=job_env["context"], preview_id=pid
        )
        batch = result.native_request
        assert batch["revision"] == "native-series-batch-v1"
        assert [p["plan_key"] for p in batch["plans"]] == [
            p["plan_key"] for p in value["plans"]
        ]
        assert [p["status"] for p in batch["plans"]] == ["INCLUDED", "INCLUDED"]
        assert result.daily_budget_sum == Decimal("200.50")
        assert (result.campaign_count, result.adgroup_count, result.ad_count) == (
            2,
            2,
            4,
        )
        rows = s.exec(select(BuildUnit).where(BuildUnit.preview_id == pid)).all()
        assert len(rows) == 2
        listed = previews.get_preview_units(
            s, context=job_env["context"], preview_id=pid
        )
        assert [u.budget for u in listed.items] == [
            Decimal("100.25"),
            Decimal("100.25"),
        ]
        for unit in rows:
            frozen_unit = previews.load_frozen_unit(
                s, context=job_env["context"], unit_id=unit.id
            )
            assert frozen_unit.budget == Decimal("100.25")
            assert frozen_unit.native_request == unit.scene_snapshot["native_request"]
        persisted = s.get(BuildPreview, pid).config["native_batch"]
        assert all("request" not in p for p in persisted["plans"])
        for item in batch["plans"]:
            unit = next(u for u in rows if str(u.id) == item["unit_id"])
            assert item["request"] == unit.scene_snapshot["native_request"]
            assert len(item["request"]["ads"]) == 2
            assert item["request"]["campaign"]["budget"] == "100.25"
            assert item["request"]["selection"] == next(
                p["selection"]
                for p in value["plans"]
                if p["plan_key"] == item["plan_key"]
            )


def test_conflict_matrix():
    from copy import deepcopy
    from uuid import uuid4

    from app.modules.builds.native_preview import conflicting_plan_keys
    from app.modules.builds.selection_schemas import NativeSeriesPlan
    from tests.modules.builds.scene_jobs.test_native_series import selection

    a = NativeSeriesPlan(plan_key=uuid4(), selection=selection())
    for account, series, regions, conflicts in [
        ("actual-account", "exact-series", ["US"], True),
        ("actual-account", "exact-series", ["CA"], False),
        ("second-account", "exact-series", ["US"], False),
        ("actual-account", "other-series", ["US"], False),
    ]:
        b = deepcopy(a)
        b.plan_key = uuid4()
        b.selection.advertiser_id = account
        b.selection.native_series_id = series
        b.selection.regions = regions
        assert conflicting_plan_keys([a, b]) == (
            {a.plan_key, b.plan_key} if conflicts else set()
        )


def test_manifest_digest_binds_every_field():
    from copy import deepcopy

    from app.modules.builds.frozen_requests import native_request_digest

    manifest = {
        "plans": [
            {"plan_key": "a", "status": "INCLUDED", "request_digest": "full-a"},
            {
                "plan_key": "b",
                "status": "EXCLUDED",
                "reason_codes": ["blocked"],
                "occupancy": {"coverage": "UNKNOWN", "scope_limits": ["account only"]},
            },
        ]
    }
    baseline = native_request_digest(manifest)
    for mutate in [
        lambda x: x["plans"].reverse(),
        lambda x: x["plans"][0].update(request_digest="changed"),
        lambda x: x["plans"][1].update(reason_codes=["different"]),
        lambda x: x["plans"][1]["occupancy"].update(coverage="PARTIAL"),
        lambda x: x["plans"][1]["occupancy"].update(scope_limits=["different"]),
    ]:
        changed = deepcopy(manifest)
        mutate(changed)
        assert native_request_digest(changed) != baseline


@pytest.mark.parametrize(
    "source_env", [{"bc_id": "90071992547409931234"}], indirect=True
)
def test_batch_browser(job_env, wire, redis_client):
    from copy import deepcopy
    from uuid import UUID, uuid4

    from tests.modules.builds.native_browser_http import submit_in_browser
    from tests.modules.builds.test_native_plan_set import drive_plan_jobs

    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )
    blocked = deepcopy(value["plans"][0])
    blocked["plan_key"] = str(uuid4())
    blocked["selection"]["advertiser_id"] = "not-authorized"
    value["plans"].append(blocked)
    response = client.patch(
        url, json={"expected_revision": revision, "scene_selection": value}
    )
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    response = client.post(url + "/prepare", json={"request_id": str(uuid4())})
    assert response.status_code == 202, response.text
    drive_plan_jobs(job_env, wire, redis_client, UUID(response.json()["task_id"]))
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    submit_in_browser(
        native.engine,
        job_env["context"],
        job_env["bc_id"],
        UUID(response.json()["preview_id"]),
        mode="batch",
    )


@pytest.mark.parametrize("active", [False, True])
def test_visible_occupancy_only_excludes_affected_plan(
    job_env, wire, redis_client, active
):
    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )
    with Session(native.engine) as s, s.begin():
        s.add(
            AdObject(
                tenant_id=job_env["context"].tenant_id,
                advertiser_id="actual-account",
                kind="adgroup",
                remote_id="conflict-1",
                ad_type="SMART_PLUS",
                configuration={
                    "native_series_id": "exact-series",
                    "location_ids": ["6252001"],
                },
                operation_status="ENABLE",
                delivery_status="ACTIVE" if active else None,
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    response = client.get(
        f"/tenants/{job_env['context'].tenant_id}/build-previews/{response.json()['preview_id']}"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    first, second = result["native_request"]["plans"]
    assert first["status"] == ("EXCLUDED" if active else "INCLUDED")
    assert second["status"] == "INCLUDED"
    assert first["occupancy"]["visible_conflicts"][0]["state"] == (
        "ACTIVE" if active else "UNKNOWN"
    )
    assert second["occupancy"]["scope_limits"]
    assert Decimal(result["daily_budget_sum"]) == Decimal(
        "100.25" if active else "200.50"
    )
    if active:
        assert (
            "request" not in first
            and "request_digest" not in first
            and "unit_id" not in first
        )


def test_corrupt_snapshot_rolls_back_entire_freeze(job_env, wire, redis_client):
    from sqlalchemy import text

    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )
    with Session(native.engine) as s, s.begin():
        # 真 PG 写入边界故障；不替换 preview service。
        s.execute(
            text("""CREATE FUNCTION pg_temp.corrupt_native_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.scene_snapshot = jsonb_set(NEW.scene_snapshot, '{native_request,campaign,budget}', '\"999\"'); RETURN NEW; END $$""")
        )
        s.execute(
            text(
                "CREATE TRIGGER jb15_corrupt BEFORE INSERT ON build_unit FOR EACH ROW EXECUTE FUNCTION pg_temp.corrupt_native_snapshot()"
            )
        )
        try:
            with pytest.raises(DomainError, match="摘要不一致"):
                previews.generate_preview(
                    s,
                    context=job_env["context"],
                    draft_id=draft_id,
                    expected_revision=revision,
                )
            assert not s.exec(
                select(BuildPreview).where(BuildPreview.draft_id == draft_id)
            ).all()
            assert not s.exec(select(BuildUnit)).all()
        finally:
            s.execute(text("DROP TRIGGER jb15_corrupt ON build_unit"))


def test_reorder_preserves_allocation_and_all_conflicts_exclude(
    job_env, wire, redis_client
):
    from copy import deepcopy
    from uuid import UUID

    from app.modules.builds.models import BuildDraft
    from app.modules.builds.native_preview import expand_plan
    from app.modules.builds.selection_schemas import NativeSeriesPlanSet
    from tests.modules.builds.test_native_plan_set import drive_plan_jobs

    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )
    with Session(native.engine) as s, s.begin():
        draft = s.get(BuildDraft, draft_id)
        plans = NativeSeriesPlanSet.model_validate(draft.scene_selection).plans

        def expand(order):
            return {
                str(p.plan_key): expand_plan(
                    s,
                    context=job_env["context"],
                    draft=draft,
                    route=job_env["route"],
                    plan=p,
                )[0]
                for p in order
            }

        assert expand(plans) == expand(list(reversed(plans)))
    value["plans"][1]["selection"] = deepcopy(value["plans"][0]["selection"])
    response = client.patch(
        url, json={"expected_revision": revision, "scene_selection": value}
    )
    assert response.status_code == 200
    revision = response.json()["revision"]
    response = client.post(
        url + "/prepare", json={"request_id": str(__import__("uuid").uuid4())}
    )
    assert response.status_code == 202
    drive_plan_jobs(job_env, wire, redis_client, UUID(response.json()["task_id"]))
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 409
    assert "native_batch_region_conflict" in response.text
    assert all(p["plan_key"] in response.text for p in value["plans"])
    with Session(native.engine) as s:
        assert not s.exec(
            select(BuildPreview).where(BuildPreview.draft_id == draft_id)
        ).all()


@pytest.mark.parametrize("coverage", ["PARTIAL", "STALE", "UNKNOWN"])
def test_partial_coverage_and_exact_decimal_sum(job_env, wire, redis_client, coverage):
    from datetime import timedelta

    from app.modules.ads.sync_models import AdDirectoryRun

    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env,
        wire,
        redis_client,
        config_changes={"budget": Decimal("100.10")},
    )
    if coverage != "UNKNOWN":
        with Session(native.engine) as s, s.begin():
            for advertiser in ["actual-account", "second-account"]:
                for kind in ("campaign", "adgroup"):
                    s.add(
                        AdDirectoryRun(
                            tenant_id=job_env["context"].tenant_id,
                            bc_id=job_env["bc_id"],
                            actor_id=job_env["context"].actor_id,
                            connection_id=job_env["route"].connection_id,
                            frozen_route=job_env["route"].model_dump(mode="json"),
                            advertiser_id=advertiser,
                            partition_key="a" * 64,
                            query={},
                            status="COMPLETE",
                            observed_at=datetime.now(UTC)
                            - timedelta(days=2 if coverage == "STALE" else 0),
                            coverage="PARTIAL",
                            published_version=1,
                            kind=kind,
                            ad_type="SMART_PLUS",
                        )
                    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    response = client.get(
        f"/tenants/{job_env['context'].tenant_id}/build-previews/{response.json()['preview_id']}"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert Decimal(result["daily_budget_sum"]) == Decimal("200.20")
    for item in result["native_request"]["plans"]:
        assert item["status"] == "INCLUDED"
        assert item["occupancy"]["coverage"] == coverage
        assert item["occupancy"]["scope_limits"]
        assert Decimal(item["request"]["campaign"]["budget"]) == Decimal("100.10")


@pytest.mark.parametrize("newest_kind", ["campaign", "adgroup"])
@pytest.mark.parametrize("older_state", ["STALE", "PARTIAL", "UNKNOWN"])
def test_directory_kinds_keep_independent_frozen_coverage(
    job_env, wire, redis_client, newest_kind, older_state
):
    from datetime import timedelta

    from app.modules.ads.sync_models import AdDirectoryRun

    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    older_kind = "adgroup" if newest_kind == "campaign" else "campaign"
    now = datetime.now(UTC)
    old = now - timedelta(days=2 if older_state == "STALE" else 0)
    with Session(native.engine) as session, session.begin():
        for kind, observed, version in [(newest_kind, now, 9), (older_kind, old, 3)]:
            if kind == older_kind and older_state == "UNKNOWN":
                continue
            session.add(
                AdDirectoryRun(
                    tenant_id=job_env["context"].tenant_id,
                    bc_id=job_env["bc_id"],
                    actor_id=job_env["context"].actor_id,
                    connection_id=job_env["route"].connection_id,
                    frozen_route=job_env["route"].model_dump(mode="json"),
                    advertiser_id="actual-account",
                    partition_key=kind[0] * 64,
                    query={"ids": [f"visible-{kind}"], "ad_type": "SMART_PLUS"},
                    status="COMPLETE",
                    observed_at=observed,
                    coverage="COMPLETE_MATERIALS" if kind == newest_kind else "PARTIAL",
                    published_version=version,
                    kind=kind,
                    ad_type="SMART_PLUS",
                )
            )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    result = client.get(
        f"/tenants/{job_env['context'].tenant_id}/build-previews/{response.json()['preview_id']}"
    )
    assert result.status_code == 200, result.text
    plans = result.json()["native_request"]["plans"]
    assert all(plan["status"] == "INCLUDED" for plan in plans)
    occupancy = next(
        p for p in plans if p["selection"]["advertiser_id"] == "actual-account"
    )["occupancy"]
    assert occupancy["coverage"] == older_state
    directories = occupancy["directories"]
    fresh = directories[newest_kind]
    assert fresh["coverage"] == "PARTIAL"
    assert fresh["directory_coverage"] == "COMPLETE_MATERIALS"
    assert fresh["directory_query"] == {
        "ids": [f"visible-{newest_kind}"],
        "ad_type": "SMART_PLUS",
    }
    assert datetime.fromisoformat(fresh["observed_at"]) == now
    assert fresh["published_version"] == 9
    older = directories[older_kind]
    assert older["coverage"] == older_state
    assert older["directory_coverage"] == (
        None if older_state == "UNKNOWN" else "PARTIAL"
    )
    assert older["directory_query"] == (
        None
        if older_state == "UNKNOWN"
        else {"ids": [f"visible-{older_kind}"], "ad_type": "SMART_PLUS"}
    )
    assert older["published_version"] == (None if older_state == "UNKNOWN" else 3)
    assert (
        older["observed_at"] is None
        if older_state == "UNKNOWN"
        else datetime.fromisoformat(older["observed_at"]) == old
    )
    assert all(
        key not in occupancy
        for key in (
            "observed_at",
            "published_version",
            "directory_kind",
            "directory_query",
            "directory_coverage",
        )
    )


def test_budget_pure_decimal_precision():
    from app.modules.builds.frozen_requests import native_batch_budget

    assert native_batch_budget(
        [
            {"campaign": {"budget": "100.100000000001"}, "ads": [1, 2, 3]},
            {"campaign": {"budget": "100.200000000002"}, "ads": [1]},
        ]
    ) == Decimal("200.300000000003")


def test_concurrent_batch_freeze_one_manifest(job_env, wire, redis_client):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Barrier

    from app.modules.builds.frozen_requests import native_request_digest

    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )
    barrier = Barrier(2)

    def freeze():
        barrier.wait(timeout=10)
        with Session(native.engine) as s, s.begin():
            return previews.generate_preview(
                s,
                context=job_env["context"],
                draft_id=draft_id,
                expected_revision=revision,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(lambda _: freeze(), range(2)))
    assert identities[0] == identities[1]
    with Session(native.engine) as s:
        result = previews.get_preview_summary(
            s, context=job_env["context"], preview_id=identities[0]
        )
        assert result.campaign_count == 2
        assert (
            len(
                s.exec(
                    select(BuildPreview).where(BuildPreview.draft_id == draft_id)
                ).all()
            )
            == 1
        )
        item = result.native_request["plans"][0]
        for path in [
            ("selection", "series_identity", "identity_id"),
            ("selection", "creative_identity", "identity_id"),
            ("selection", "regions"),
            ("materials",),
            ("config",),
            ("ads",),
            ("campaign", "budget"),
            ("occupancy", "scope_limits"),
        ]:
            from app.modules.builds.frozen_requests import native_request_digest

            changed = deepcopy(item["request"])
            node = changed
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = "changed"
            assert native_request_digest(changed) != item["request_digest"]
