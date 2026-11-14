"""JB34 独立预期：creative40，父Ad100；源刷新不能改同次对账。"""

import csv
import io
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
    assert [r["row_kind"] for r in rows] == ["INSTANCE", "PARENT_CONTROL"]
    assert [r["消耗"] for r in rows] == ["40", "100"]
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
                        "list": [row],
                        "page_info": {
                            "page": 1,
                            "page_size": 1000,
                            "total_page": 1,
                            "total_number": 1,
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
    live_campaign_workspace(
        seed, factory, level="material", label="素材", name="vid1.mp4"
    )
