from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.models import User
from app.modules.accounts.models import BCAccountAccess, TenantBC
from app.modules.ads.directory import append_campaign_name_projection
from app.modules.ads.models import AdObject
from app.modules.reporting.dimension_query import (
    aggregate_metrics,
    merge_metric_buckets,
    metric_vector,
)
from app.modules.reporting.query_models import QuerySnapshot
from app.modules.reporting.schemas import ReportingFilter, SelectionRequest
from app.modules.tenants.models import TenantMembership


@pytest.fixture
def session():
    """服务接收已有事务时必须具有一致读；生产 Engine 会自行开启短事务。"""
    from app.core.db import engine, init_db

    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ"
    ) as connection:
        transaction = connection.begin()
        try:
            with Session(
                bind=connection, join_transaction_mode="create_savepoint"
            ) as value:
                init_db(value)
                yield value
        finally:
            transaction.rollback()


def filters_for(dimension="campaign", **changes):
    return ReportingFilter(
        dimension=dimension,
        start_date=date(2026, 9, 30),
        end_date=date(2026, 9, 30),
        **changes,
    )


def query(case, **kwargs):
    from app.modules.reporting.queries import query_ads

    return query_ads(case.session, context=case.context, bc_id=case.bc_id, **kwargs)


def freeze(case, snapshot_id, **kwargs):
    from app.modules.reporting.selection import freeze_selection

    return freeze_selection(
        case.session,
        context=case.context,
        bc_id=case.bc_id,
        request=SelectionRequest(
            snapshot_id=snapshot_id, mode="ALL_MATCHING", **kwargs
        ),
    )


def test_all_matching_freezes_snapshot(report_case):
    case = report_case
    for index in range(51):
        case.seed_campaign(f"Provider-{index:03d}-Drama", "report-account", 1, 2)
    case.seed_campaign("Other-Drama", "report-account", 1, 2)
    filters = filters_for(query="Provider", sort_by="spend", sort_direction="desc")
    first = query(case, filters=filters, limit=50)
    second = query(
        case,
        filters=filters,
        snapshot_id=first.snapshot.snapshot_id,
        cursor=first.next_cursor,
        limit=50,
    )
    assert (first.total, len(first.items), len(second.items)) == (51, 50, 1)
    assert second.next_cursor is None
    assert len({row.row_key for row in first.items + second.items}) == 51
    assert first.summary["buckets"][0]["values"]["spend"] == "51"
    new_ref = case.seed_campaign("Provider-new-Drama", "report-account", 1, 2)
    selected = freeze(case, first.snapshot.snapshot_id)
    assert len(selected.refs) == 51
    assert new_ref.remote_id not in {ref.remote_id for ref in selected.refs}
    reread = query(
        case, filters=filters, snapshot_id=first.snapshot.snapshot_id, limit=100
    )
    # 快照冻结成员、顺序与汇总；行内容按当前数据补全（如覆盖的同步时间）。
    assert reread.total == first.total
    assert reread.summary == first.summary
    assert [row.row_key for row in reread.items] == [
        row.row_key for row in first.items + second.items
    ]


def test_account_selection_captures_campaigns_at_query_time(report_case):
    case = report_case
    old = case.seed_campaign("P-Old", "report-account", 1, 2)
    page = query(case, filters=filters_for("account"))
    case.seed_campaign("P-New", "report-account", 1, 2)
    assert freeze(case, page.snapshot.snapshot_id).refs == (old,)
    assert page.items[0].refs == ()  # 账户行列表响应不返回引用


def test_account_listing_refs_are_empty_but_freeze_expands_campaigns(report_case):
    """账户维度列表响应不返回引用，避免每行带回上百条系列；冻结选择展开快照内引用。"""
    case = report_case
    c1 = case.seed_campaign("P-Drama-A", "report-account", 1, 2)
    c2 = case.seed_campaign("P-Drama-B", "report-account", 1, 2)
    page = query(case, filters=filters_for("account"))
    assert len(page.items) == 1
    assert page.items[0].refs == ()
    selected = freeze(case, page.snapshot.snapshot_id)
    assert set(selected.refs) == {c1, c2}


def test_snapshot_preserves_coverage_and_versions(report_case):
    case = report_case
    ref = case.seed_campaign("P-Old", "report-account", 1, 2)
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="P-Old", parser_revision=1
    )
    page = query(case, filters=filters_for())
    assert page.coverage["status"] == "COMPLETE"
    assert page.items[0].coverage["status"] == "COMPLETE"
    snapshot = case.session.get(QuerySnapshot, page.snapshot.snapshot_id)
    assert snapshot.publication_versions
    assert snapshot.naming_versions


def test_naming_versions_handle_more_campaigns_than_bind_parameter_limit(report_case):
    """历史系列超过 PostgreSQL 65535 绑定参数上限时，快照名称版本仍按最新修订读取。"""
    from app.modules.reporting.queries import snapshot_naming_versions

    case = report_case
    ref = case.seed_campaign("P-Old", "report-account", 1, 2)
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="P-Old", parser_revision=1
    )
    latest = append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="P-New", parser_revision=1
    )
    case.session.flush()
    # 生产事故规模：788 个账户与 69,228 个系列逐项绑定共 70,016 个参数。
    pairs = [(f"adv-{i % 788}", f"missing-{i}") for i in range(69_228)]
    pairs.append((ref.advertiser_id, ref.remote_id))
    versions = snapshot_naming_versions(
        case.session, tenant_id=case.context.tenant_id, campaigns=tuple(pairs)
    )
    assert versions == {
        "directory": 1,
        f"{ref.advertiser_id}:{ref.remote_id}": latest.name_revision,
    }


def test_page_objects_handle_more_ids_than_bind_parameter_limit(report_case):
    """剧名等维度一页可涉及数万个系列；分页读取目录对象不能因参数上限失败。"""
    from app.modules.reporting.dimension_query import _page_objects

    case = report_case
    ref = case.seed_campaign("P-Drama", "report-account", 1, 2)
    ids = [f"missing-{i}" for i in range(70_000)] + [ref.remote_id]
    objects = _page_objects(
        case.session,
        tenant_id=case.context.tenant_id,
        kind="campaign",
        parents={},
        identities=[(ref.advertiser_id, remote_id) for remote_id in ids],
    )
    assert set(objects) == {(ref.advertiser_id, "campaign", ref.remote_id)}


def test_page_objects_only_read_requested_account_object_pairs(report_case):
    from app.modules.reporting.dimension_query import _page_objects

    case = report_case
    first = case.seed_campaign("P-First", "report-account", 1, 2)
    second = case.seed_campaign("P-Second", "report-account-2", 3, 4)
    objects = _page_objects(
        case.session,
        tenant_id=case.context.tenant_id,
        kind="campaign",
        parents={},
        identities=[(first.advertiser_id, second.remote_id)],
    )
    assert objects == {}


def test_rename_keeps_old_snapshot_and_new_query_regroups(report_case):
    case = report_case
    ref = case.seed_campaign("P-Old", "report-account", 1, 2)
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="P-Old", parser_revision=1
    )
    filters = filters_for("drama")
    old = query(case, filters=filters)
    obj = case.session.get(
        AdObject, (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
    )
    obj.name = "P-New"
    case.session.add(obj)
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="P-New", parser_revision=1
    )
    case.session.flush()
    reread = query(case, filters=filters, snapshot_id=old.snapshot.snapshot_id)
    new = query(case, filters=filters)
    # 旧快照保留原归组（成员与顺序不变），展示名称按当前目录补全；新查询重新归组。
    assert [row.row_key for row in reread.items] == [row.row_key for row in old.items]
    assert reread.items[0].refs == old.items[0].refs
    assert old.items[0].display["name"] == "Old"
    assert new.items[0].display["name"] == "New"
    assert new.items[0].row_key != old.items[0].row_key
    assert freeze(case, old.snapshot.snapshot_id).refs == (ref,)


@pytest.mark.parametrize("limit", [0, 101])
def test_query_limit_is_bounded(report_case, limit):
    with pytest.raises(HTTPException) as exc:
        query(report_case, filters=filters_for(), limit=limit)
    assert exc.value.status_code == 422


def test_snapshot_owner_scope_and_expiry(report_case):
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.selection import freeze_selection

    case = report_case
    case.seed_campaign("P-A", "report-account", 1, 2)
    case.seed_campaign("P-B", "report-account", 1, 2)
    filters = filters_for()
    page = query(case, filters=filters, limit=1)
    actor = User(
        username=f"viewer-{uuid4().hex}", hashed_password="test", is_active=True
    )
    case.session.add(actor)
    case.session.flush()
    case.session.add(
        TenantMembership(
            tenant_id=case.context.tenant_id,
            user_id=actor.id,
            role="viewer",
            active=True,
        )
    )
    case.session.flush()
    viewer = TenantContext(case.context.tenant_id, actor.id, "viewer")
    other_bc = TenantBC(tenant_id=case.context.tenant_id, bc_id="other-bc")
    case.session.add(other_bc)
    case.session.flush()
    for request_context, request_bc in (
        (viewer, case.bc_id),
        (case.other_context, "bc-other"),
        (case.context, "other-bc"),
    ):

        def calls(context=request_context, bc_id=request_bc):
            return (
                lambda: query_ads(
                    case.session,
                    context=context,
                    bc_id=bc_id,
                    filters=filters,
                    snapshot_id=page.snapshot.snapshot_id,
                    cursor=page.next_cursor,
                ),
                lambda: freeze_selection(
                    case.session,
                    context=context,
                    bc_id=bc_id,
                    request=SelectionRequest(
                        snapshot_id=page.snapshot.snapshot_id, mode="ALL_MATCHING"
                    ),
                ),
            )

        for call in calls():
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 404
    snapshot = case.session.get(QuerySnapshot, page.snapshot.snapshot_id)
    snapshot.created_at = datetime.now(UTC) - timedelta(minutes=16)
    snapshot.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    case.session.add(snapshot)
    case.session.flush()
    with pytest.raises(HTTPException) as exc:
        query(case, filters=filters, snapshot_id=snapshot.id)
    assert (exc.value.status_code, exc.value.detail) == (409, "query_snapshot_expired")


def test_cursor_tampering_and_filter_mismatch(report_case):
    import base64
    import json

    case = report_case
    for name in ("P-A", "P-B"):
        case.seed_campaign(name, "report-account", 1, 2)
    filters = filters_for()
    page = query(case, filters=filters, limit=1)
    for cursor in ("invalid", page.next_cursor[:-2] + "xx"):
        with pytest.raises(HTTPException) as exc:
            query(
                case,
                filters=filters,
                snapshot_id=page.snapshot.snapshot_id,
                cursor=cursor,
            )
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        query(
            case,
            filters=filters_for(query="A"),
            snapshot_id=page.snapshot.snapshot_id,
            cursor=page.next_cursor,
        )
    # Even if the attacker knows the payload format, editing actor/BC/sequence
    # cannot produce a valid continuation token.
    payload = {"snapshot_id": str(page.snapshot.snapshot_id), "sequence": 42}
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(HTTPException) as exc:
        query(
            case, filters=filters, snapshot_id=page.snapshot.snapshot_id, cursor=forged
        )
    assert exc.value.status_code == 404


def test_current_account_revocation_rejects_snapshot_and_selection(report_case):
    from app.modules.reporting.selection import get_frozen_selection

    case = report_case
    case.seed_campaign("P-A", "report-account", 1, 2)
    page = query(case, filters=filters_for())
    selected = freeze(case, page.snapshot.snapshot_id)
    grant = case.session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == case.context.tenant_id
        )
    ).first()
    grant.authorized = False
    case.session.add(grant)
    case.session.flush()
    with pytest.raises(HTTPException) as exc:
        query(case, filters=filters_for(), snapshot_id=page.snapshot.snapshot_id)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        get_frozen_selection(
            case.session,
            context=case.context,
            bc_id=case.bc_id,
            selection_id=selected.selection_id,
        )
    assert exc.value.status_code == 403


def _vec(spend, revenue, *, unsupported=()):
    return metric_vector(
        {"spend": spend, "revenue_total": revenue},
        currency="USD",
        timezone="UTC",
        unsupported=unsupported,
    )


def test_aggregate_sums_available_members_and_marks_partial():
    total = aggregate_metrics([_vec(Decimal("10"), Decimal("5")), _vec(None, None)])
    assert total.values["spend"] == Decimal("10")
    assert total.availability["spend"] == "PARTIAL"
    assert total.values["roas_total"] == Decimal("0.5")
    assert total.availability["roas_total"] == "PARTIAL"


def test_aggregate_all_present_and_complete_stays_available():
    total = aggregate_metrics(
        [_vec(Decimal("10"), Decimal("5")), _vec(Decimal("2"), Decimal("1"))]
    )
    assert total.values["spend"] == Decimal("12")
    assert total.availability["spend"] == "AVAILABLE"
    assert total.availability["roas_total"] == "AVAILABLE"


def test_aggregate_marks_present_columns_partial_when_contributor_incomplete():
    total = aggregate_metrics([_vec(Decimal("10"), None)], incomplete=True)
    assert total.availability["spend"] == "PARTIAL"
    assert total.availability["revenue_total"] == "MISSING"


def test_aggregate_all_missing_stays_empty():
    total = aggregate_metrics([_vec(None, None), _vec(None, None)], incomplete=True)
    assert total.values["spend"] is None
    assert total.availability["spend"] == "MISSING"


def test_aggregate_propagates_partial_members():
    partial_member = aggregate_metrics(
        [_vec(Decimal("10"), Decimal("5")), _vec(None, None)]
    )
    total = aggregate_metrics([partial_member, _vec(Decimal("2"), Decimal("1"))])
    assert total.values["spend"] == Decimal("12")
    assert total.availability["spend"] == "PARTIAL"


def test_unsupported_only_when_all_members_unsupported():
    both = aggregate_metrics(
        [
            _vec(Decimal("1"), Decimal("1"), unsupported={"revenue_d3"}),
            _vec(Decimal("1"), Decimal("1"), unsupported={"revenue_d3"}),
        ]
    )
    assert both.availability["revenue_d3"] == "UNSUPPORTED"
    assert both.availability["roas_d3"] == "UNSUPPORTED"


def test_partially_unsupported_column_is_treated_as_missing():
    total = aggregate_metrics(
        [
            _vec(Decimal("1"), Decimal("1"), unsupported={"revenue_total"}),
            _vec(Decimal("4"), Decimal("2")),
        ]
    )
    assert total.values["revenue_total"] == Decimal("2")
    assert total.availability["revenue_total"] == "PARTIAL"
    assert total.availability["roas_total"] == "PARTIAL"


def test_merge_buckets_marks_only_buckets_with_incomplete_contributors():
    eur = metric_vector({"spend": Decimal("3")}, currency="EUR", timezone="UTC")
    merged = {
        v.currency: v
        for v in merge_metric_buckets(
            [((_vec(Decimal("1"), None),), True), ((eur,), False)]
        )
    }
    assert merged["USD"].availability["spend"] == "PARTIAL"
    assert merged["EUR"].availability["spend"] == "AVAILABLE"


def test_same_snapshot_keeps_frozen_status_without_refiltering_or_reordering(
    report_case,
):
    case = report_case
    ref = case.seed_campaign("P-Status", "report-account", 1, 2)
    obj = case.session.get(
        AdObject, (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
    )
    obj.operation_status = "DISABLE"
    case.session.add(obj)
    case.session.flush()
    filters = filters_for(operation_statuses=("DISABLE",))
    old = query(case, filters=filters)
    assert old.items[0].display["operation_status"] == "DISABLE"
    obj.operation_status = "ENABLE"
    case.session.add(obj)
    case.session.flush()
    current = query(case, filters=filters, snapshot_id=old.snapshot.snapshot_id)
    assert current.snapshot.snapshot_id == old.snapshot.snapshot_id
    assert [r.row_key for r in current.items] == [r.row_key for r in old.items]
    assert current.items[0].display["operation_status"] == "DISABLE"
    assert query(case, filters=filters).items == ()
