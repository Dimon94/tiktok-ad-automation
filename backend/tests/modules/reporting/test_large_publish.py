"""单遍输入必须边读边暂存；最终事实仍只在完整验证后原子替换。"""

from decimal import Decimal

import pytest
from sqlalchemy import event, func
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.reporting.publish import publish_ad_day
from app.modules.reporting.store_models import AdReportDay
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_publish_watermark import D, args


def test_publisher_consumes_single_pass_rows_in_bounded_database_batches(
    session, sync_seed
):
    written = 0
    sizes = []
    connection = session.connection()

    def before(_conn, _cursor, sql, parameters, _context, many):
        nonlocal written
        if sql.lstrip().upper().startswith("INSERT"):
            size = len(parameters) if many else 1
            sizes.append(size)
            written += size

    def rows():
        for i in range(1501):
            if i == 1000:
                assert written >= 500, (
                    "publisher accumulated all rows before database staging"
                )
            yield ad_row(str(i), D, spend="0.01")

    event.listen(connection, "before_cursor_execute", before)
    try:
        assert publish_ad_day(session, **args(sync_seed.tenant_id), rows=rows()) == 1501
    finally:
        event.remove(connection, "before_cursor_execute", before)
    assert max(sizes) <= 500
    assert session.exec(
        select(func.sum(AdReportDay.spend)).where(
            AdReportDay.tenant_id == sync_seed.tenant_id
        )
    ).one() == Decimal("15.01")


def test_late_invalid_stream_keeps_previous_complete_facts(session, sync_seed):
    tenant = sync_seed.tenant_id
    publish_ad_day(session, **args(tenant), rows=[ad_row("old", D, spend="7")])

    def rows():
        for i in range(1200):
            yield ad_row(str(i), D)
        yield ad_row("invalid", D, adv="unauthorized")

    with pytest.raises(DomainError), session.begin_nested():
        publish_ad_day(session, **args(tenant), rows=rows())
    facts = session.exec(
        select(AdReportDay).where(AdReportDay.tenant_id == tenant)
    ).all()
    assert [(row.ad_id, row.spend) for row in facts] == [("old", Decimal("7"))]


def test_cross_batch_duplicate_identity_rolls_back_entire_replacement(
    session, sync_seed
):
    from sqlalchemy.exc import IntegrityError

    tenant = sync_seed.tenant_id
    publish_ad_day(session, **args(tenant), rows=[ad_row("old", D, spend="7")])

    def rows():
        for i in range(1200):
            yield ad_row(str(i), D)
        yield ad_row("1", D, spend="999")

    with pytest.raises(IntegrityError), session.begin_nested():
        publish_ad_day(session, **args(tenant), rows=rows())
    facts = session.exec(
        select(AdReportDay).where(AdReportDay.tenant_id == tenant)
    ).all()
    assert [(row.ad_id, row.spend) for row in facts] == [("old", Decimal("7"))]


def test_material_stream_replacement_keeps_type_and_null_money(session, sync_seed):
    from app.modules.reporting.publish import publish_material_day
    from app.modules.reporting.store_models import MaterialReportDay
    from tests.modules.reporting.test_publish_watermark import material_row

    tenant = sync_seed.tenant_id

    def rows():
        for i in range(1001):
            row = material_row(spend="-" if i == 1000 else "0.01")
            row["dimensions"]["main_material_id"] = str(i)
            row["dimensions"]["main_material_type"] = "IMAGE" if i % 2 else "VIDEO"
            yield row

    assert publish_material_day(session, **args(tenant), rows=rows()) == 1001
    facts = session.exec(
        select(MaterialReportDay).where(MaterialReportDay.tenant_id == tenant)
    ).all()
    assert sum(row.spend or 0 for row in facts) == Decimal("10")
    assert next(row for row in facts if row.main_material_id == "1000").spend is None
    assert {row.main_material_type for row in facts} == {"VIDEO", "IMAGE"}
    assert publish_material_day(session, **args(tenant), rows=iter(())) == 0
    assert (
        session.exec(
            select(MaterialReportDay).where(MaterialReportDay.tenant_id == tenant)
        ).first()
        is None
    )
