"""JB34 独立预期：creative40，父Ad100；源刷新不能改同次对账。"""

import csv
import io
import json
from decimal import Decimal

from app.modules.reporting.exports import (
    create_export,
    download_export,
    generate_export,
)
from app.modules.reporting.query_models import ReportExport
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


def test_material_result_is_frozen_before_export(session, sync_seed):
    publish(session, sync_seed, "acc-1", [material_row("cr1", "40")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="cr1",
    )
    from sqlalchemy import text

    session.execute(
        text(
            "UPDATE ad_report_day SET ad_id='ad1', spend=100 WHERE tenant_id=:t AND advertiser_id='acc-1' AND ad_id='cr1'"
        ),
        {"t": sync_seed.tenant_id},
    )
    # 第二租户使用相同remote ID；金额不能穿过现有tenant事实筛选。
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    foreign = seed_fixture.__wrapped__(session)
    publish(session, foreign, "acc-1", [material_row("cr1", "700")])
    use(
        session,
        foreign,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="cr1",
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        ids=("vid1",),
    )
    import time

    started = time.perf_counter()
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    print(f"JB34 single material query={(time.perf_counter() - started) * 1000:.2f}ms")  # noqa: T201
    assert (
        page.snapshot.result_contract.get("revision") == "material-instance-result-v1"
    )
    assert len(page.items) == 1
    assert len(page.items[0].material_instances) == 1
    instance = page.items[0].material_instances[0]
    assert instance.creative_ids == ("cr1",)
    assert instance.key["tenant_id"] == str(sync_seed.tenant_id)
    assert instance.key["bc_id"] == "bc-a"
    assert instance.key["connection_id"] == str(sync_seed.connection.id)
    assert len(instance.observations) == 1
    assert instance.observations[0]["creative_id"] == "cr1"
    assert instance.metric_buckets[0].values["spend"] == Decimal("40")
    assert instance.metric_buckets[0].reasons.get("spend") != "CONTRACT_MISMATCH"
    assert page.items[0].parent_controls[0].metric_buckets[0].values[
        "spend"
    ] == Decimal("100")
    original = page.items[0].model_dump(mode="json")
    assert page.items[0].metric_buckets[0].values["spend"] == Decimal("40")
    public = create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="jb34-frozen",
    )
    export = session.get(ReportExport, public.id)
    publish(session, sync_seed, "acc-1", [material_row("cr1", "90")])
    refreshed = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot_id=page.snapshot.snapshot_id,
    )
    assert refreshed.items[0].model_dump(mode="json") == original
    assert refreshed.metrics_coverage == page.metrics_coverage
    generate_export(session, export)
    content = download_export(
        session, context=sync_seed.context, bc_id="bc-a", export_id=export.id
    )
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    assert [r["row_kind"] for r in rows] == [
        "INSTANCE",
        "PARENT_CONTROL",
        "RECONCILIATION_DIFFERENCE",
    ]
    assert [r["消耗"] for r in rows] == ["40", "100", ""]
    assert rows[0]["evidence_digest"] == instance.evidence_digest
    assert rows[0]["result_digest"] == page.items[0].evidence_digest
    import json

    assert json.loads(rows[0]["identity"]) == instance.key
    assert json.loads(rows[0]["observations"]) == list(instance.observations)
    assert page.items[0].coverage["status"] != "BUILDING"
    # 目录改名/删除只能影响新查询；旧展开直接消费冻结载荷。
    session.execute(
        text("DELETE FROM ad_material_reference WHERE tenant_id=:t"),
        {"t": sync_seed.tenant_id},
    )
    same = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot_id=page.snapshot.snapshot_id,
    )
    assert same.items[0].model_dump(mode="json") == original
    newer = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert newer.snapshot.snapshot_id != page.snapshot.snapshot_id
    assert newer.items[0].evidence_digest != page.items[0].evidence_digest
    import pytest
    from fastapi import HTTPException

    from app.modules.reporting.query_models import QuerySnapshot

    old = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    old.publication_versions = old.publication_versions | {"material_query_contract": 2}
    session.add(old)
    session.flush()
    with pytest.raises(HTTPException) as changed:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=filters,
            snapshot_id=old.id,
        )
    assert changed.value.status_code == 409
    # 已生成原文件无需按新合同重新解释；未生成旧任务明确失败。
    export.result_contract = {}
    assert (
        download_export(
            session, context=sync_seed.context, bc_id="bc-a", export_id=export.id
        )
        == content
    )
    with pytest.raises(HTTPException) as incompatible:
        generate_export(session, export)
    assert incompatible.value.detail == "report_export_contract_incompatible_reexport"
    with pytest.raises(HTTPException) as forbidden:
        download_export(
            session, context=sync_seed.context, bc_id="bc-b", export_id=export.id
        )
    assert forbidden.value.status_code in {403, 404}


def test_material_workspace_sdk_worker_csv(
    committed_sync_seed,
    monkeypatch,
    redis_client,
    app_config,  # noqa: ARG001
):
    """同现有live harness：只替换官方SDK transport，真实HTTP/Worker/PG/Redis。"""
    import json
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlsplit

    from urllib3.response import HTTPResponse

    from app.core.config import settings
    from app.core.credentials import encrypt_credentials
    from app.core.db import engine
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess, TenantBC, TikTokConnection
    from app.modules.reporting.job_tasks import run_job_step
    from app.modules.reporting.jobs import enqueue_job
    from tests.modules.reporting.claiming import claim_jobs
    from tests.modules.reporting.sync_conftest import ad_row
    from tests.modules.reporting.test_creative_reporting import creative_row
    from tests.modules.reporting.test_jobs import _route
    from tests.modules.reporting.test_native_campaign import (
        TEST_BC,
        live_campaign_workspace,
    )
    from tests.modules.reporting.test_native_campaign import D as live_day

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)

    def transport(_pool, _method, url, **kwargs):
        args = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        args.update(dict(kwargs.get("fields") or []))
        creative = '"ad_id"' in args.get("dimensions", "")
        row = (
            creative_row("cr1", "40", "4")
            if creative
            else ad_row("ad1", live_day, spend="100")
        )
        row["dimensions"]["stat_time_day"] = str(live_day)
        return HTTPResponse(
            status=200,
            body=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "list": (
                            [
                                dict(
                                    creative_row(
                                        c,
                                        amount,
                                        "1"
                                        if c == "cr1"
                                        else "8"
                                        if c == "cr2"
                                        else "4",
                                    ),
                                    dimensions={
                                        "ad_id": c,
                                        "stat_time_day": str(live_day),
                                    },
                                )
                                for c, amount in [
                                    ("cr1", "10"),
                                    ("cr2", "30"),
                                    ("carousel", "11"),
                                    ("conflict", "13"),
                                    ("unmapped", "17"),
                                ]
                            ]
                            if creative
                            else [row]
                        ),
                        "page_info": {
                            "page": 1,
                            "page_size": 1000,
                            "total_page": 1,
                            "total_number": 5 if creative else 1,
                        },
                    },
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    with factory() as s:
        s.add(
            TenantBC(tenant_id=seed.tenant_id, bc_id=TEST_BC, name="Material Test BC")
        )
        s.flush()
        grant = s.get(
            BCAccountAccess, (seed.tenant_id, "bc-a", "acc-1", seed.connection.id)
        )
        s.add(BCAccountAccess(**(grant.model_dump() | {"bc_id": TEST_BC})))
        binding = s.get(
            BCConnectionBinding, (seed.tenant_id, "bc-a", seed.connection.id)
        )
        s.add(BCConnectionBinding(**(binding.model_dump() | {"bc_id": TEST_BC})))
        connection = s.get(TikTokConnection, seed.connection.id)
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=seed.tenant_id, value={"access_token": "synthetic-offline-jb34"}
        )
        s.add(connection)
        use(
            s, seed, adv="acc-1", ad="ad1", video="vid1", ad_material="mid1", post="cr1"
        )
        from app.modules.ads.creative_models import AdCreative
        from app.modules.ads.models import AdObject

        parent_object = s.get(AdObject, (seed.tenant_id, "acc-1", "ad", "ad1"))
        # 已同步的creative可导航；cr2只有事实/身份，保留未同步原因。
        s.add(
            AdObject(
                **(
                    parent_object.model_dump()
                    | {
                        "kind": "creative",
                        "remote_id": "cr1",
                        "parent_kind": "ad",
                        "parent_remote_id": "ad1",
                        "name": "Creative cr1",
                    }
                )
            )
        )
        original = s.get(AdCreative, (seed.tenant_id, "acc-1", "cr1")).model_dump()
        for c, status, kind, vid in [
            ("cr2", "RESOLVED", "VIDEO", "vid1"),
            ("carousel", "COMPOSITE", "CAROUSEL", None),
            ("conflict", "CONFLICT", None, None),
        ]:
            s.add(
                AdCreative(
                    **(
                        original
                        | {
                            "creative_id": c,
                            "identity_status": status,
                            "material_type": kind,
                            "platform_material_id": vid,
                            "assets": [
                                {
                                    "material_type": "IMAGE",
                                    "platform_material_id": "image1",
                                },
                                {
                                    "material_type": "IMAGE",
                                    "platform_material_id": "image2",
                                },
                            ],
                        }
                    )
                )
            )
        for kind in ("ad_day", "creative_day"):
            enqueue_job(
                s,
                tenant_id=seed.tenant_id,
                bc_id=TEST_BC,
                actor_id=seed.actor_id,
                frozen_route=_route(seed) | {"bc_id": TEST_BC},
                kind=kind,
                tier="manual",
                advertiser_ids=["acc-1"],
                start=live_day,
                end=live_day,
            )
        s.commit()
        jobs = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=4)
        s.commit()
    jobs = [
        j
        for j in jobs
        if j.tenant_id == seed.tenant_id and j.kind in {"ad_day", "creative_day"}
    ]
    assert {j.kind for j in jobs} == {"ad_day", "creative_day"}
    for job in jobs:
        outcome = run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        from app.modules.reporting.store_models import ReportSyncJob

        with factory() as check:
            recorded = check.get(ReportSyncJob, job.id)
            assert outcome == "COMPLETE", (
                job.kind,
                recorded.error_code,
                recorded.progress,
            )
    from tests.modules.reporting.query_helpers import finish_projections

    with factory() as s:
        finish_projections(s)
        s.commit()
    from uuid import UUID

    from sqlalchemy import text

    from app.modules.reporting.query_models import QuerySnapshot

    trigger = "jb37_" + seed.tenant_id.hex

    def lifecycle(command):
        with factory() as s:
            action = command["action"]
            if action in {"expire", "contract"}:
                snapshot = s.get(QuerySnapshot, UUID(command["snapshot_id"]))
                assert snapshot.tenant_id == seed.tenant_id
                if action == "expire":
                    snapshot.created_at = datetime.now(UTC) - timedelta(minutes=16)
                    snapshot.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    snapshot.result_contract = {"revision": "old-material"}
                s.add(snapshot)
            elif action == "fault":
                s.execute(
                    text(f"""CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.tenant_id='{seed.tenant_id}'::uuid AND NEW.filters->>'dimension'='material'
                THEN RAISE EXCEPTION 'JB37 local storage fault'; END IF; RETURN NEW; END $$""")
                )
                s.execute(
                    text(
                        f"CREATE TRIGGER {trigger} BEFORE INSERT ON query_snapshot FOR EACH ROW EXECUTE FUNCTION {trigger}()"
                    )
                )
            elif action == "repair":
                s.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON query_snapshot"))
                s.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))
            s.commit()
        return {"action": action}

    try:
        live_campaign_workspace(
            seed,
            factory,
            level="material",
            label="素材",
            name="vid1.mp4",
            material_extended=True,
            material_lifecycle=True,
            snapshot_refresh=lifecycle,
        )
    finally:
        lifecycle({"action": "repair"})


def test_multi_creative_five_states_and_special_identity(session, sync_seed):
    """Spec: 10/null/dash/absent/legacy each once, no parent allocation."""
    from sqlalchemy import text

    rows = [material_row(f"c{i}", "10" if i == 0 else "-") for i in range(5)]
    rows[1]["metrics"]["spend"] = None
    del rows[3]["metrics"]["spend"]
    rows[4]["metrics"]["spend"] = "7"
    publish(session, sync_seed, "acc-1", [*rows, rows[0]])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="c0",
    )
    from app.modules.ads.creative_models import AdCreative

    original = session.get(AdCreative, (sync_seed.tenant_id, "acc-1", "c0"))
    for i in range(1, 5):
        session.add(AdCreative(**(original.model_dump() | {"creative_id": f"c{i}"})))
    session.flush()
    session.execute(
        text(
            "UPDATE creative_report_day SET evidence=NULL WHERE tenant_id=:t AND creative_id='c4'"
        ),
        {"t": sync_seed.tenant_id},
    )
    # 模拟真实旧事实：有金额但没有publisher证据，不能事后补造。
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    instance = next(
        i
        for r in page.items
        for i in r.material_instances
        if i.key["platform_material_id"] == "vid1"
    )
    assert instance.creative_ids == ("c0", "c1", "c2", "c3", "c4")
    assert len(instance.fact_keys) == 5
    assert instance.metric_buckets[0].values["spend"] == Decimal("17")
    assert instance.metric_buckets[0].availability["spend"] == "PARTIAL"
    assert instance.field_state_counts["spend"] == {
        "VALUE": 1,
        "NULL": 1,
        "DASH": 1,
        "ABSENT": 1,
        "LEGACY_UNKNOWN": 1,
    }
    assert [o["field_states"]["spend"] for o in instance.observations] == [
        "VALUE",
        "NULL",
        "DASH",
        "ABSENT",
        "LEGACY_UNKNOWN",
    ]
    assert instance.observations[0]["publisher"]["scope"]["kind"] == "ACCOUNT_DAY"
    assert instance.observations[4]["publisher"] is None
    assert [o["values"]["spend"] for o in instance.observations] == [
        "10",
        None,
        None,
        None,
        "7",
    ]


def test_special_buckets_and_known_mid_identity(session, sync_seed):
    from app.modules.ads.creative_models import AdCreative
    from tests.modules.reporting.query_helpers import finish_projections

    publish(
        session,
        sync_seed,
        "acc-1",
        [
            material_row(c, v)
            for c, v in [
                ("exact", "2"),
                ("other", "8"),
                ("carousel", "11"),
                ("conflict", "13"),
                ("missing", "17"),
                ("midonly", "19"),
            ]
        ],
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="exact",
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid2",
        post="other",
    )
    template = session.get(
        AdCreative, (sync_seed.tenant_id, "acc-1", "exact")
    ).model_dump()
    for creative, status, kind, vid, mid in [
        ("carousel", "COMPOSITE", "CAROUSEL", None, "carousel-mid"),
        ("conflict", "CONFLICT", None, None, None),
        ("midonly", "UNRESOLVED", None, None, "historical-mid"),
    ]:
        session.add(
            AdCreative(
                **(
                    template
                    | {
                        "creative_id": creative,
                        "identity_status": status,
                        "material_type": kind,
                        "platform_material_id": vid,
                        "ad_material_id": mid,
                        "assets": [
                            {
                                "material_type": "IMAGE",
                                "platform_material_id": "image1",
                            },
                            {
                                "material_type": "IMAGE",
                                "platform_material_id": "image2",
                            },
                        ],
                        "evidence": {"reason": status, "candidates": ["a", "b"]},
                    }
                )
            )
        )
    finish_projections(session)
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    instances = [i for r in page.items for i in r.material_instances]
    ordinary = [i for i in instances if i.row_kind == "INSTANCE"]
    assert {i.key["ad_material_id"] for i in ordinary} == {"mid1", "mid2"}
    assert sum(i.metric_buckets[0].values["spend"] for i in ordinary) == Decimal("10")
    special = {i.creative_ids[0]: i for i in instances if i.row_kind != "INSTANCE"}
    assert {c: i.row_kind for c, i in special.items()} == {
        "carousel": "COMPOSITE",
        "conflict": "UNMAPPED_CREATIVE",
        "missing": "UNMAPPED_CREATIVE",
        "midonly": "UNMAPPED_CREATIVE",
    }
    assert special["conflict"].identity_reason == "CONFLICT"
    assert special["midonly"].key["ad_material_id"] == "historical-mid"
    assert (
        special["carousel"].observations[0]["identity"]["assets"][1][
            "platform_material_id"
        ]
        == "image2"
    )
    public = create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="special",
    )
    export = session.get(ReportExport, public.id)
    generate_export(session, export)
    rows = list(
        csv.DictReader(
            io.StringIO(
                download_export(
                    session,
                    context=sync_seed.context,
                    bc_id="bc-a",
                    export_id=export.id,
                ).decode()
            )
        )
    )
    assert sum(
        Decimal(r["消耗"]) for r in rows if r["row_kind"] == "INSTANCE"
    ) == Decimal("10")
    assert len([r for r in rows if r["row_kind"] == "COMPOSITE"]) == 1
    assert len([r for r in rows if r["row_kind"] == "UNMAPPED_CREATIVE"]) == 3


def test_creative_target_preserves_own_observation(session, sync_seed):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.reporting.publish import AccountMeta, publish_creative_day
    from app.modules.reporting.store_models import CreativeReportDay, ReportSyncState
    from tests.modules.reporting.test_jobs import _route

    t0 = datetime.now(UTC) - timedelta(minutes=10)
    t1 = t0 + timedelta(minutes=1)
    args = dict(  # noqa: C408
        tenant_id=sync_seed.tenant_id,
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        route=FrozenTikTokRoute(**_route(sync_seed)),
    )
    rows = [material_row("c1", "10"), material_row("c2", "20")]
    for row in rows:
        row["metrics"]["advertiser_id"] = "acc-1"
    rows[1]["metrics"]["campaign_id"] = "other-campaign"
    publish_creative_day(session, **args, job_id=uuid4(), requested_at=t0, rows=rows)
    session.flush()
    state = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "creative_day", D)
    )
    version = state.publication_version
    old = session.get(
        CreativeReportDay, (sync_seed.tenant_id, "acc-1", D, "c2")
    ).evidence
    row = material_row("c1", "30")
    row["metrics"]["advertiser_id"] = "acc-1"
    publish_creative_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t1,
        rows=[row],
        campaign_ids=(row["metrics"]["campaign_id"],),
    )
    session.expire_all()
    c1 = session.get(CreativeReportDay, (sync_seed.tenant_id, "acc-1", D, "c1"))
    c2 = session.get(CreativeReportDay, (sync_seed.tenant_id, "acc-1", D, "c2"))
    assert c1.evidence["publication"]["observed_at"] == t1.isoformat()
    assert c1.evidence["publication"]["publication_version"] == 0
    assert c1.evidence["publication"]["scope"]["kind"] == "CAMPAIGNS"
    assert c2.evidence == old and c2.spend == Decimal("20")
    state = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "creative_day", D)
    )
    assert state.publication_version == version and state.observed_at == t0


def test_bc_connection_collision_is_explained(session, sync_seed):
    from datetime import UTC, datetime

    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        ConnectionAuthorization,
    )
    from app.modules.accounts.models import (
        BCAccountAccess,
        TikTokConnection,
    )
    from app.modules.reporting.store_models import CreativeReportDay
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("c1", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="c1",
    )
    second = TikTokConnection(
        tenant_id=sync_seed.tenant_id,
        status="ACTIVE",
        kind="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
    )
    session.add(second)
    session.flush()
    session.add(
        ConnectionAuthorization(
            tenant_id=sync_seed.tenant_id,
            connection_id=second.id,
            authorization_revision=0,
            source="OFFLINE_TEST",
            verified_at=datetime.now(UTC),
            permission_summary={"read_authorized": True},
        )
    )
    for bc in ("bc-a", "bc-b"):
        binding = session.get(
            BCConnectionBinding, (sync_seed.tenant_id, bc, sync_seed.connection.id)
        )
        grant = session.get(
            BCAccountAccess, (sync_seed.tenant_id, bc, "acc-1", sync_seed.connection.id)
        )
        if grant is None:
            grant = session.get(
                BCAccountAccess,
                (sync_seed.tenant_id, "bc-a", "acc-1", sync_seed.connection.id),
            )
            session.add(BCAccountAccess(**(grant.model_dump() | {"bc_id": bc})))
        session.add(
            BCConnectionBinding(**(binding.model_dump() | {"connection_id": second.id}))
        )
        session.add(
            BCAccountAccess(
                **(grant.model_dump() | {"connection_id": second.id, "bc_id": bc})
            )
        )
    finish_projections(session)
    filters = ReportingFilter(
        dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    for bc in ("bc-a", "bc-b"):
        page = query_ads(session, context=sync_seed.context, bc_id=bc, filters=filters)
        i = next(i for r in page.items for i in r.material_instances)
        assert (
            i.row_kind == "UNMAPPED_CREATIVE"
            and i.identity_reason == "CONNECTION_AMBIGUOUS"
        )
        assert i.key["connection_id"] is None and i.key["bc_id"] == bc
        assert i.metric_buckets[0].values["spend"] == Decimal("10")
    fact = session.get(CreativeReportDay, (sync_seed.tenant_id, "acc-1", D, "c1"))
    fact.evidence = fact.evidence | {
        "source_connection_id": str(second.id),
        "source_bc_id": "bc-a",
    }
    session.add(fact)
    finish_projections(session)
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
    )
    i = next(i for r in page.items for i in r.material_instances)
    assert i.row_kind == "INSTANCE" and i.key["connection_id"] == str(second.id)
    assert len(i.fact_keys) == 1 and i.metric_buckets[0].values["spend"] == Decimal(
        "10"
    )
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-b",
        filters=filters,
    )
    i = next(i for r in page.items for i in r.material_instances)
    assert (
        i.row_kind == "UNMAPPED_CREATIVE"
        and i.identity_reason == "SOURCE_SCOPE_MISMATCH"
    )


def test_stale_deleted_creative_preserves_projected_observation(session, sync_seed):
    from sqlalchemy import text

    from app.modules.reporting.queries import query_ads as production_query
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("gone", "40")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="gone",
    )
    finish_projections(session)
    original = session.execute(
        text(
            "SELECT evidence FROM creative_report_day WHERE tenant_id=:t AND creative_id='gone'"
        ),
        {"t": sync_seed.tenant_id},
    ).scalar_one()
    session.execute(
        text(
            "DELETE FROM creative_report_day WHERE tenant_id=:t AND creative_id='gone'"
        ),
        {"t": sync_seed.tenant_id},
    )
    # 不推进异步投影：真实删除后旧投影窗口必须仍能解释原有观测。
    page = production_query(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            ids=("vid1",),
        ),
    )
    instance = page.items[0].material_instances[0]
    assert instance.metric_buckets[0].values["spend"] == 40
    assert (
        instance.observations[0]["observed_at"]
        == original["publication"]["observed_at"]
    )
    assert page.coverage["projection"]["pending_bucket_count"] > 0


def test_adgroup_residual_csv_retains_signed_coordinates(session, sync_seed):
    from sqlalchemy import text

    publish(session, sync_seed, "acc-1", [material_row("cr1", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="cr1",
    )
    session.execute(
        text("UPDATE ad_report_day SET spend=3 WHERE tenant_id=:t"),
        {"t": sync_seed.tenant_id},
    )
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    public = create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="residual-columns",
    )
    export = session.get(ReportExport, public.id)
    generate_export(session, export)
    rows = list(
        csv.DictReader(
            io.StringIO(
                download_export(
                    session,
                    context=sync_seed.context,
                    bc_id="bc-a",
                    export_id=export.id,
                ).decode("utf-8-sig")
            )
        )
    )
    residual = next(
        r
        for r in rows
        if json.loads(r["row_coverage"]).get("scope") == "ADGROUP_RESIDUAL"
    )
    assert (residual["消耗"], residual["currency"], residual["timezone"]) == (
        "-7",
        "USD",
        "Asia/Hong_Kong",
    )
    assert json.loads(residual["row_coverage"])["status"] == "NOT_COMPARABLE"
    assert sum(Decimal(v["values"]["spend"]) for v in page.summary["buckets"]) == 10


def test_review_freeze_preserves_mapped_instance_totals_and_archived_observations(
    session, sync_seed
):
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.modules.ai_workbench.review_projection import report_for_reading
    from app.modules.ai_workbench.review_reporting import freeze_report
    from app.modules.reporting.read_models.models import ReadBucketState
    from app.modules.reporting.store_models import ReportSyncState
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("cr1", "40")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="cr1",
    )
    finish_projections(session)
    filters = ReportingFilter(
        dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    states = {
        "material": session.exec(
            select(ReadBucketState).where(
                ReadBucketState.tenant_id == sync_seed.tenant_id,
                ReadBucketState.advertiser_id == "acc-1",
                ReadBucketState.kind == "creative_material",
            )
        ).all(),
        "publications": session.exec(
            select(ReportSyncState).where(
                ReportSyncState.tenant_id == sync_seed.tenant_id,
                ReportSyncState.advertiser_id == "acc-1",
            )
        ).all(),
    }
    report = freeze_report(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        cutoff=datetime.now(UTC),
        states=states,
    )
    assert report["summary"] == page.summary
    assert report["summary"]["scope"] == "SELECTED_MATERIALS"
    assert report["summary"]["buckets"][0]["values"]["spend"] == "40"
    instance = report["rows"][0]["material_instances"][0]
    assert instance["row_kind"] == "INSTANCE" and instance["observations"]
    visible = report_for_reading(report)
    assert visible["rows"][0]["material_instances"][0]["observations_archived"]
    assert "observations" not in visible["rows"][0]["material_instances"][0]
    for row in visible["rows"]:
        assert not row.get("refs") and not row.get("material_uses")
        assert all("observations" not in r for r in row["reconciliation_rows"])
    assert instance["observations"]  # 交付投影不能就地删除归档正文。
