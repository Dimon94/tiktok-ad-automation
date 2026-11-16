"""紧凑快照在操作/导出时展开原版本，并拒绝错绑素材证据。"""

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.modules.reporting.read_models.evidence import ReportEvidenceVersion
from app.modules.reporting.snapshot_members import members_from_data
from tests.modules.reporting.test_queries import filters_for, freeze, query


@pytest.mark.parametrize("dimension", ["campaign", "drama"])
def test_selection_and_export_expand_original_name_version(report_case, dimension):
    from sqlmodel import select

    from app.modules.ads.directory import append_campaign_name_projection
    from app.modules.reporting.exports import create_export
    from app.modules.reporting.query_models import (
        FrozenSelectionRecord,
        QuerySnapshot,
        QuerySnapshotRow,
        ReportExport,
    )
    from app.modules.reporting.snapshot_members import snapshot_members, snapshot_names

    case = report_case
    ref = case.seed_campaign("嘉书-旧剧-备注", "report-account", 4, 8)
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="嘉书-旧剧-备注", parser_revision=1
    )
    page = query(case, filters=filters_for(dimension))
    assert (
        case.session.execute(
            text(
                "SELECT count(*) FROM query_snapshot_name_version WHERE snapshot_id=:s"
            ),
            {"s": page.snapshot.snapshot_id},
        ).scalar_one()
        == 0
    )
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="嘉书-新剧-备注", parser_revision=1
    )
    selected = freeze(case, page.snapshot.snapshot_id)
    record = case.session.get(FrozenSelectionRecord, selected.selection_id)
    assert record.naming_versions == {
        "directory": 1,
        f"report-account:{ref.remote_id}": 1,
    }
    exported = create_export(
        case.session,
        context=case.context,
        bc_id=case.bc_id,
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key=f"old-name-{dimension}",
    )
    export = case.session.get(ReportExport, exported.id)
    snapshot = case.session.get(QuerySnapshot, page.snapshot.snapshot_id)
    assert export.naming_versions == snapshot.naming_versions
    rows = case.session.exec(
        select(QuerySnapshotRow).where(QuerySnapshotRow.snapshot_id == snapshot.id)
    ).all()
    members = snapshot_members(case.session, snapshot=snapshot, rows=rows)
    assert (
        snapshot_names(case.session, snapshot=snapshot, rows=rows, members=members)
        == record.naming_versions
    )
    assert "旧剧" in str(export.frozen_rows[0]["display"])
    assert export.frozen_rows[0]["refs"][0]["remote_id"] == ref.remote_id


def test_material_resolver_rejects_other_vid_even_when_count_matches(report_case):
    case = report_case
    ref = {
        "tenant_id": str(case.context.tenant_id),
        "advertiser_id": "report-account",
        "kind": "ad",
        "remote_id": "ad-1",
    }
    version = ReportEvidenceVersion(
        tenant_id=case.context.tenant_id,
        advertiser_id="report-account",
        kind="material_ad",
        refs=[ref],
        material_uses=[
            {
                "ad_ref": ref,
                "platform_material_id": "actual-vid",
                "ad_material_id": "m1",
                "material_type": "VIDEO",
            }
        ],
    )
    case.session.add(version)
    case.session.flush()
    with pytest.raises(HTTPException) as error:
        members_from_data(
            case.session,
            tenant_id=case.context.tenant_id,
            advertisers=["report-account"],
            dimension="material",
            rows=[
                {
                    "row_key": '["material", "wrong-vid"]',
                    "member_count": 1,
                    "evidence_versions": [version.id],
                }
            ],
        )
    assert (error.value.status_code, error.value.detail) == (
        409,
        "query_membership_unavailable",
    )
