"""JB30: exports retain the query observation beyond snapshot cleanup."""

import csv
import io

from sqlmodel import select

from app.modules.reporting.exports import (
    create_export,
    download_export,
    generate_export,
)
from app.modules.reporting.queries import query_ads
from app.modules.reporting.query_models import QuerySnapshot, ReportExport
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.evidence_helpers import assert_csv_evidence
from tests.modules.reporting.evidence_helpers import records as frozen_records
from tests.modules.reporting.test_native_scope_roas import D, publish


def test_export_copies_observation_before_source_refresh_and_cleanup(
    session, sync_seed
):
    publish(session, sync_seed, ("0.100000000000000001", "0"), ("0.3", "-"))
    filters = ReportingFilter(
        dimension="campaign", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    public = create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="frozen",
    )
    export = session.get(ReportExport, public.id)
    expected_sources = [
        frozen_records(session, sync_seed, row.metric_buckets[0]) for row in page.items
    ]
    for frozen, row, source in zip(
        export.frozen_rows, page.items, expected_sources, strict=True
    ):
        assert {
            k: v for k, v in frozen.items() if k != "source_evidence"
        } == row.model_dump(mode="json", exclude={"source_evidence"})
        assert frozen["source_evidence"] == source
    snap = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    assert export.result_contract == snap.result_contract
    assert export.publication_map == snap.publication_map
    publish(session, sync_seed, ("100", "200"), ("900", "900"))
    session.delete(snap)
    session.flush()
    generate_export(session, export)
    content = download_export(
        session, context=sync_seed.context, bc_id="bc-a", export_id=export.id
    )
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    assert [r["消耗"] for r in rows] == ["0.100000000000000001", "0"]
    assert [r["Native Growth 总展示价值（平台原字段）"] for r in rows] == ["0.3", ""]
    import json

    assert_csv_evidence(rows[0], page.items[0].metric_buckets[0], expected_sources[0])
    assert json.loads(rows[0]["publication_map"]) == export.publication_map
    from datetime import timedelta

    from app.modules.reporting.exports import cleanup_expired_exports

    cleanup_expired_exports(session, now=export.expires_at + timedelta(seconds=1))
    assert export.status == "EXPIRED" and not export.frozen_rows
    assert not export.publication_map and not export.result_contract
    assert (
        len(
            session.exec(
                select(ReportExport).where(
                    ReportExport.tenant_id == sync_seed.tenant_id
                )
            ).all()
        )
        == 1
    )


def test_export_scope_revocation_and_idempotent_retry(session, sync_seed):
    import pytest
    from fastapi import HTTPException

    from app.modules.accounts.models import BCAccountAccess

    publish(session, sync_seed, ("0",), ("0",))
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="campaign", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    args = dict(  # noqa: C408
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="same",
    )
    first = create_export(session, **args)
    assert create_export(session, **args).id == first.id
    export = session.get(ReportExport, first.id)
    generate_export(session, export)
    original = download_export(
        session, context=sync_seed.context, bc_id="bc-a", export_id=first.id
    )
    publish(session, sync_seed, ("10",), ("20",))
    generate_export(session, export)
    assert (
        download_export(
            session, context=sync_seed.context, bc_id="bc-a", export_id=first.id
        )
        == original
    )
    with pytest.raises(HTTPException) as error:
        download_export(
            session, context=sync_seed.context, bc_id="bc-b", export_id=first.id
        )
    assert error.value.status_code == 404
    access = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == sync_seed.tenant_id,
            BCAccountAccess.bc_id == "bc-a",
        )
    ).first()
    access.authorized = False
    session.add(access)
    session.flush()
    for action in (
        lambda: create_export(session, **args),
        lambda: create_export(session, **(args | {"idempotency_key": "new"})),
        lambda: download_export(
            session, context=sync_seed.context, bc_id="bc-a", export_id=first.id
        ),
    ):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 403


def test_legacy_worker_fails_without_reinterpreting_but_completed_file_survives(
    committed_sync_seed,
):
    from app.modules.reporting.export_tasks import run_export

    seed, factory = committed_sync_seed
    with factory() as session:
        publish(session, seed, ("10",), ("20",))
        page = query_ads(
            session,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension="campaign",
                start_date=D,
                end_date=D,
                advertiser_ids=("acc-1",),
            ),
        )
        ids = []
        for key in ("complete", "pending"):
            public = create_export(
                session,
                context=seed.context,
                bc_id="bc-a",
                snapshot_id=page.snapshot.snapshot_id,
                idempotency_key=key,
            )
            export = session.get(ReportExport, public.id)
            if key == "complete":
                generate_export(session, export)
                original = download_export(
                    session, context=seed.context, bc_id="bc-a", export_id=export.id
                )
            export.result_contract = {}
            session.add(export)
            ids.append(export.id)
        session.commit()
    for export_id in ids:
        run_export(export_id)
    with factory() as session:
        complete, pending = [session.get(ReportExport, export_id) for export_id in ids]
        assert complete.status == "COMPLETE"
        assert (
            download_export(
                session, context=seed.context, bc_id="bc-a", export_id=complete.id
            )
            == original
        )
        assert pending.status == "FAILED" and pending.object_key is None
        assert (
            pending.coverage["export_error"]
            == "report_export_contract_incompatible_reexport"
        )
