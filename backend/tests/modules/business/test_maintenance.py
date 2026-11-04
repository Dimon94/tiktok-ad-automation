from datetime import UTC, date, datetime
from decimal import Decimal

from app.modules.business.configuration import save_rate
from app.modules.business.exports import create_export
from app.modules.business.maintenance import cleanup_business
from app.modules.business.models import BusinessRun
from app.modules.business.queries import query_dashboard
from app.modules.business.schemas import DashboardFilter
from tests.modules.business.test_projection import build
from tests.modules.business.test_queries import prepared


def test_cleanup_preserves_current_and_unexpired_export_versions(
    session, business_case, tmp_path, monkeypatch
):
    from app.modules.business import exports

    monkeypatch.setattr(exports, "LOCAL_ROOT", tmp_path)
    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    old_id = page.run_ids[0]
    export = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    export.expires_at = datetime(2026, 11, 20, tzinfo=UTC)
    session.add(export)
    save_rate(
        session,
        context=c.context,
        kind="rebate",
        scope_id=c.bc_id,
        effective_date=date(2026, 10, 1),
        rate=Decimal("0.06"),
    )
    current = build(session, c)[0]
    cleanup_business(session, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert session.get(BusinessRun, old_id) is not None
    export.expires_at = datetime(2026, 11, 9, tzinfo=UTC)
    session.add(export)
    session.flush()
    cleanup_business(session, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert session.get(BusinessRun, old_id) is None
    assert session.get(BusinessRun, current.id).status == "COMPLETE"
