"""JB-6: actual admin rebuild -> frozen PG evidence -> publication -> CSV."""

import csv
import io
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlmodel import select

from app.modules.business.evidence import auto_is_current
from app.modules.business.exports import process_pending_export
from app.modules.business.models import BusinessDay, BusinessRun, BusinessScope
from app.modules.business.projection import advance_run
from app.modules.operation_logs.models import OperationDayEvidence
from app.modules.reporting.store_models import CampaignReportDay, ReportSyncState
from tests.modules.operation_logs.test_daily_evidence import anchors
from tests.modules.operation_logs.test_historical_evidence import ANCHOR, DAY, saved_day
from tests.modules.operation_logs.test_parser import export


def drain(session, run):
    for _ in range(100):
        if advance_run(session, run.id):
            break
    assert run.status == "COMPLETE"


def test_admin_rebuild_publishes_new_evidence_preserves_old_csv_and_retries(
    session, business_case, business_client, monkeypatch
):
    case = business_case
    anchors(session, case, ANCHOR)
    saved_day(session, case, DAY)
    session.add(
        ReportSyncState(
            tenant_id=case.context.tenant_id,
            advertiser_id=case.advertiser_id,
            report_kind="ad_day",
            stat_date=DAY,
            status="COMPLETE",
            publication_version=1,
            observed_at=datetime(2026, 10, 2, tzinfo=UTC),
        )
    )
    session.add(
        CampaignReportDay(
            tenant_id=case.context.tenant_id,
            advertiser_id=case.advertiser_id,
            campaign_id="campaign-test",
            campaign_name="Synthetic JB6",
            stat_date=DAY,
            currency="USD",
            timezone="UTC",
            spend=Decimal(100),
            revenue_total=Decimal(60),
            revenue_d0=Decimal(60),
            ad_impression_events=Decimal(300),
            fetched_at=datetime(2026, 10, 2, tzinfo=UTC),
            sync_job_id=uuid4(),
        )
    )
    session.flush()
    base = f"/api/tenants/{case.context.tenant_id}/business"
    filters = {"bc_id": case.bc_id, "start_date": str(DAY), "end_date": str(DAY)}

    def request():
        response = business_client.post(base + "/rebuild", json=filters)
        assert response.status_code == 202, response.text
        assert len(response.json()["run_ids"]) == 1
        return session.get(BusinessRun, UUID(response.json()["run_ids"][0]))

    def query():
        response = business_client.post(base + "/query", json=filters)
        assert response.status_code == 200, response.text
        return response.json()

    def download(page):
        response = business_client.post(
            base + "/exports", json={**filters, "run_ids": page["run_ids"]}
        )
        assert response.status_code == 202, response.text
        export_id = response.json()["id"]
        for _ in range(4):
            process_pending_export(session, batch_size=1)
        response = business_client.get(base + f"/exports/{export_id}/download")
        assert response.status_code == 200, response.text
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert len(rows) == 1
        assert str(page["run_ids"][0]) in rows[0]["计算版本"]
        return export_id, response.content, rows[0]

    assert (
        business_client.post(
            base + "/rebuild", json={**filters, "bc_id": "unauthorized-jb6"}
        ).status_code
        == 403
    )
    old = request()
    drain(session, old)
    old_page = query()
    assert old_page["totals"]["compensation"] is None
    old_export, old_bytes, old_csv = download(old_page)
    assert old_csv["预计赔付"] == ""
    old_day = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == old.id)
    ).one()
    old_auto = session.get(
        OperationDayEvidence, UUID(old_day.compensation_source.split(":")[1])
    )
    old_values = deepcopy(
        (old.model_dump(), old_day.model_dump(), old_auto.model_dump())
    )
    assert auto_is_current(session, old_auto)
    # Same-source COMPLETE reuse remains the original public API contract.
    assert request().id == old.id
    early_id = saved_day(session, case, ANCHOR.date())
    assert not auto_is_current(session, old_auto)
    candidate = request()
    assert candidate.id != old.id
    assert str(early_id) in candidate.inputs["operation_logs"]["exports"]
    assert query()["run_ids"] == old_page["run_ids"]
    # Inject exactly one computation exception at the existing worker boundary.
    from app.modules.business import tasks

    @contextmanager
    def local_session(*_args):
        yield session

    def fail_once(*_args, **_kwargs):
        raise RuntimeError("synthetic JB6 calculation interruption")

    with monkeypatch.context() as patch:
        patch.setattr(tasks, "_work_session", local_session)
        patch.setattr(tasks, "Session", local_session)
        patch.setattr(tasks, "advance_run", fail_once)
        tasks.work_business()
    session.refresh(candidate)
    assert candidate.status == "FAILED"
    assert query()["run_ids"] == old_page["run_ids"]
    assert (
        business_client.get(base + f"/exports/{old_export}/download").content
        == old_bytes
    )
    failed_values = deepcopy(candidate.model_dump())
    retry = request()
    assert retry.id not in {old.id, candidate.id}
    # Change early evidence after BASE but before final publication: must invalidate before publication.
    for _ in range(100):
        if retry.phase == "VERIFY":
            break
        assert not advance_run(session, retry.id)
    assert retry.phase == "VERIFY"
    before_signature = retry.source_signature
    saved_day(
        session,
        case,
        ANCHOR.date(),
        content=export(
            records=[
                (
                    "2026-09-30 13:00",
                    "Campaign",
                    "campaign-test",
                    [
                        {
                            "action": "Change",
                            "name": "Name",
                            "before_after": [{"before": "a", "after": "b"}],
                        }
                    ],
                )
            ]
        ),
        observed=datetime(2026, 11, 12, 2, tzinfo=UTC),
    )
    assert advance_run(session, retry.id)
    assert retry.status == "SUPERSEDED"
    assert retry.source_signature == before_signature
    assert query()["run_ids"] == old_page["run_ids"]
    superseded = retry
    retry = request()
    assert retry.id != superseded.id
    assert retry.source_signature != before_signature
    drain(session, retry)
    page = query()
    assert page["run_ids"] == [str(retry.id)]
    assert Decimal(page["totals"]["compensation"]) == 50
    current = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == retry.id)
    ).one()
    auto = session.get(
        OperationDayEvidence, UUID(current.compensation_source.split(":")[1])
    )
    assert auto.data["bid"] == "1.2" and auto.data["status"] == "ESTIMATED_PENDING"
    assert auto.data["amount"] == "50"
    assert auto.data["source_start"] == auto.data["source_end"] == str(DAY)
    assert "COMPENSATION_QUALIFICATION_PENDING" in current.issues
    assert auto_is_current(session, auto)
    _, _, new_csv = download(page)
    assert new_csv["预计赔付"] == "50.00"
    assert "COMPENSATION_QUALIFICATION_PENDING" in new_csv["数据说明"]
    assert (
        business_client.get(base + f"/exports/{old_export}/download").content
        == old_bytes
    )
    for row in (old, old_day, old_auto, candidate):
        session.refresh(row)
    assert (old.model_dump(), old_day.model_dump(), old_auto.model_dump()) == old_values
    assert candidate.model_dump() == failed_values
    scope = session.get(BusinessScope, (case.context.tenant_id, case.bc_id))
    assert scope.published_runs[str(DAY)] == str(retry.id)
    assert request().id == retry.id
