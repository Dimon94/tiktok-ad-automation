from datetime import date

import pytest
from fastapi import HTTPException

from app.modules.accounts.models import BCAccountAccess
from tests.modules.business.test_queries import prepared


def test_export_fixed_values_and_revocation(session, business_case):
    from app.modules.business.exports import (
        create_export,
        export_content,
        process_pending_export,
    )
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    row = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    process_pending_export(session)
    content = export_content(session, context=c.context, export_id=row.id)
    assert "新开漫剧预计" in content.decode("utf-8-sig")
    assert "800" in content.decode("utf-8-sig")
    # 用真实授权记录撤权，不模拟权限服务。
    from sqlmodel import select

    grant = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == c.context.tenant_id,
            BCAccountAccess.bc_id == c.bc_id,
        )
    ).one()
    grant.authorized = False
    session.add(grant)
    session.flush()
    with pytest.raises(HTTPException):
        export_content(session, context=c.context, export_id=row.id)


def test_export_resume_and_display_rounding(session, business_case):
    import csv
    import io
    from decimal import Decimal as D

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

    c = business_case
    prepared(session, c)
    # 三个系列分摊800美元，导出两位小数后仍需合计800。
    rows = session.exec(
        select(BusinessDay).where(BusinessDay.stat_date == date(2026, 11, 2))
    ).all()
    for row in rows:
        row.subsidy_comic = D("266.666666666666")
        session.add(row)
    clone = rows[0].model_dump()
    clone.update(campaign_id="c", subsidy_comic=D("266.666666666668"))
    session.add(BusinessDay(**clone))
    session.flush()
    filters = DashboardFilter(
        bc_id=c.bc_id,
        start_date=date(2026, 11, 2),
        end_date=c.day,
        dimension="campaign",
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    export = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    process_pending_export(session, batch_size=1)
    assert export.frozen and export.cursor == 1 and export.status == "RUNNING"
    partial = _path(f"business-exports/{export.tenant_id}/{export.id}.partial")
    with partial.open("ab") as output:
        output.write(b"uncommitted-extra-bytes")
    process_pending_export(session, batch_size=1)
    assert export.cursor == 2
    # 本地临时文件丢失后，从已经冻结的数据库行重建，不重新核算。
    partial.unlink()
    for _ in range(4):
        process_pending_export(session, batch_size=1)
        if export.status == "COMPLETE":
            break
    assert export.status == "COMPLETE"
    content = export_content(session, context=c.context, export_id=export.id).decode(
        "utf-8-sig"
    )
    values = list(csv.DictReader(io.StringIO(content)))
    assert len(values) == 3
    assert sum(D(row["新开漫剧预计"]) for row in values) == D("800.00")
    assert "uncommitted-extra-bytes" not in content


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_previous_admin_export_denied_after_role_downgrade(
    session, business_case, role
):
    from app.core.errors import DomainError
    from app.modules.business.exports import (
        create_export,
        export_content,
        process_pending_export,
    )
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter
    from app.modules.tenants.models import TenantMembership

    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    export = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    process_pending_export(session)
    assert export.status == "COMPLETE"
    member = session.get(TenantMembership, (c.context.tenant_id, c.context.actor_id))
    member.role = role
    session.add(member)
    session.flush()
    with pytest.raises(DomainError):
        export_content(session, context=c.context, export_id=export.id)
    with pytest.raises(DomainError):
        query_dashboard(session, context=c.context, filters=filters)
