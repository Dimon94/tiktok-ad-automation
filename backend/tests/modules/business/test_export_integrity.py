"""JB-2 的独立预期：50/NULL 不得产生 210/1.05。真实查询到下载。"""

import csv
import io
from datetime import date
from decimal import Decimal as D

import pytest
from sqlmodel import select

from app.modules.business.exports import (
    _path,
    create_export,
    export_content,
    process_pending_export,
)
from app.modules.business.models import BusinessDay
from app.modules.business.queries import query_dashboard
from app.modules.business.schemas import DashboardFilter
from tests.modules.business.test_queries import prepared


@pytest.mark.parametrize("dimension", ["campaign", "day"])
@pytest.mark.parametrize("scenario", ["comic", "other", "existing", "base"])
@pytest.mark.parametrize(
    "compensations", [(D(50), None), (None, None), (D(50), D(0)), (D(0), D(0))]
)
@pytest.mark.parametrize("spend", [D(100), D(0)])
def test_query_and_download_integrity(
    session, business_case, business_client, scenario, compensations, spend, dimension
):
    c = business_case
    prepared(session, c)
    facts = session.exec(
        select(BusinessDay)
        .where(BusinessDay.stat_date == date(2026, 11, 2))
        .order_by(BusinessDay.campaign_id)
    ).all()
    for fact, compensation in zip(facts, compensations, strict=True):
        fact.spend, fact.revenue = spend, D(80)
        fact.provider_share, fact.rebate = D(0), D(0)
        fact.net_revenue = fact.business_revenue = D(80)
        fact.compensation = compensation
        fact.subsidy_comic = fact.subsidy_other = fact.subsidy_existing = D(0)
        fact.issues = ["COMPENSATION_PARTIAL"] if compensation is None else []
        session.add(fact)
    session.flush()
    filters = DashboardFilter(
        bc_id=c.bc_id,
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
        dimension=dimension,
        scenario=scenario,
        limit=1,
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    partial = None in compensations
    assert page.totals["compensation"] == (
        sum(x for x in compensations if x is not None)
        if any(x is not None for x in compensations)
        else None
    )
    assert page.totals["scenario_revenue"] == (
        None
        if partial and scenario != "base"
        else D(160) + (sum(compensations) if scenario != "base" else D(0))
    )
    base = f"/api/tenants/{c.context.tenant_id}/business"
    response = business_client.post(
        base + "/exports",
        json=filters.model_copy(update={"run_ids": page.run_ids}).model_dump(
            mode="json"
        ),
    )
    assert response.status_code == 202
    from uuid import UUID

    export_id = UUID(response.json()["id"])
    for _ in range(2):
        process_pending_export(session, batch_size=1)
    response = business_client.get(base + f"/exports/{export_id}/download")
    assert response.status_code == 200
    records = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(records) == (2 if dimension == "campaign" else 1)
    assert (
        query_dashboard(
            session,
            context=c.context,
            filters=filters.model_copy(update={"sort_by": "name", "descending": False}),
        ).totals
        == page.totals
    )
    for offset, record in enumerate(records):
        current = query_dashboard(
            session,
            context=c.context,
            filters=filters.model_copy(
                update={"offset": offset, "run_ids": page.run_ids}
            ),
        )
        assert current.totals == page.totals
        for key, column in [
            ("scenario_revenue", "情景经营收入"),
            ("scenario_roas", "情景经营ROAS"),
        ]:
            value = current.rows[0].values[key]
            assert record[column] == ("" if value is None else format(value, ".4f"))
        assert ("COMPENSATION_PARTIAL" in record["数据说明"]) == partial
    expected_comp = (
        compensations if dimension == "campaign" else [page.totals["compensation"]]
    )
    assert sorted(r["预计赔付"] for r in records) == sorted(
        "" if x is None else format(x, ".2f") for x in expected_comp
    )


@pytest.mark.parametrize("contract", [None, "old-contract"])
@pytest.mark.parametrize("has_bytes", [False, True])
def test_incompatible_snapshot_fails_before_append(
    session, business_case, contract, has_bytes
):
    from fastapi import HTTPException

    from app.modules.business.export_snapshot import freeze_rows

    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id,
        start_date=date(2026, 11, 1),
        end_date=c.day,
        dimension="campaign",
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    row = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    freeze_rows(session, row, c.context)
    metadata = dict(row.filters)
    metadata.pop("_snapshot_contract", None)
    if contract is not None:
        metadata["_snapshot_contract"] = contract
    row.filters = metadata
    partial = _path(f"business-exports/{row.tenant_id}/{row.id}.partial")
    if has_bytes:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"old-contract-bytes")
        row.file_offset = len(b"old-contract-bytes")
    session.add(row)
    session.flush()
    process_pending_export(session, batch_size=1)
    assert row.status == "FAILED"
    assert (
        partial.read_bytes() == b"old-contract-bytes"
        if has_bytes
        else not partial.exists()
    )
    with pytest.raises(HTTPException):
        export_content(session, context=c.context, export_id=row.id)
    assert not process_pending_export(session)


def test_frozen_values_survive_changes_and_replay(session, business_case):
    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id,
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
        dimension="campaign",
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    row = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    assert row.status == "QUEUED" and not row.frozen
    process_pending_export(session, batch_size=1)
    assert row.status == "RUNNING"
    partial = _path(f"business-exports/{row.tenant_id}/{row.id}.partial")
    with partial.open("ab") as output:
        output.write(b"uncommitted crash tail")
    for fact in session.exec(select(BusinessDay)).all():
        fact.business_revenue = D(9999)
        fact.issues = []
        session.add(fact)
    session.flush()
    process_pending_export(session, batch_size=1)
    original = export_content(session, context=c.context, export_id=row.id)
    records = list(csv.DictReader(io.StringIO(original.decode("utf-8-sig"))))
    assert len(records) == 2
    assert [r["系列ID"] for r in records] == [r.campaign_id for r in page.rows]
    assert [D(r["情景经营收入"]) for r in records] == [
        r.values["scenario_revenue"] for r in page.rows
    ]
    assert not process_pending_export(session)
    assert export_content(session, context=c.context, export_id=row.id) == original


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_downgrade_blocks_pending_export(session, business_case, role):
    from app.core.errors import DomainError
    from app.modules.tenants.models import TenantMembership

    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id,
        start_date=date(2026, 11, 2),
        end_date=c.day,
        dimension="campaign",
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    row = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    process_pending_export(session, batch_size=1)
    partial = _path(f"business-exports/{row.tenant_id}/{row.id}.partial")
    before = partial.read_bytes()
    member = session.get(TenantMembership, (c.context.tenant_id, c.context.actor_id))
    member.role = role
    session.add(member)
    session.flush()
    process_pending_export(session)
    assert row.status == "FAILED" and partial.read_bytes() == before
    with pytest.raises(DomainError):
        export_content(session, context=c.context, export_id=row.id)
    assert not process_pending_export(session)
