"""真实查询压测前先证明隔离边界和独立金额 oracle。"""

from datetime import date
from decimal import Decimal

import pytest

from tests.performance.sync_query_fixture import (
    FixtureParameters,
    budget_for_rows,
    expected_metrics,
    synthetic_fact,
    validate_database_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/production",
        "postgresql://localhost/sync_redesign_test_root",
        "postgresql://remote.example/sync_redesign_test_perf",
        "postgresql://127.0.0.1:6432/sync_redesign_test_perf",
        "postgresql://127.0.0.1/sync_redesign_test_perf?host=remote.example",
        "sqlite:///sync_redesign_test_perf",
    ],
)
def test_refuse_everything_except_explicit_local_perf_database(url):
    with pytest.raises(ValueError):
        validate_database_url(url)


def test_allow_only_dedicated_local_database():
    validate_database_url(
        "postgresql+psycopg://test@127.0.0.1:5432/sync_redesign_test_perf"
    )


def test_generation_is_deterministic_and_oracle_does_not_float_money():
    parameters = FixtureParameters(objects=100, seed=413, days=90)
    assert synthetic_fact(parameters, 17, 8) == synthetic_fact(parameters, 17, 8)
    assert synthetic_fact(parameters, 17, 8) != synthetic_fact(parameters, 18, 8)
    result = expected_metrics(parameters, object_ids=[1, 17, 99], days=7)
    assert isinstance(result["spend"], Decimal)
    rows = [
        synthetic_fact(parameters, obj, day) for obj in [1, 17, 99] for day in range(7)
    ]
    assert result["spend"] == sum((r["spend"] for r in rows), Decimal(0))
    assert result["revenue"] == sum((r["revenue"] for r in rows), Decimal(0))
    assert parameters.end_date == date(2026, 10, 30)


def test_space_budget_counts_heap_index_toast_wal_and_reserve():
    budget = budget_for_rows(
        rows=100_000,
        sample_rows=1000,
        heap_bytes=100_000,
        index_bytes=50_000,
        toast_bytes=20_000,
        wal_bytes=200_000,
        free_bytes=11 * 1024**3,
    )
    assert budget["estimated_relation_bytes"] == 17_000_000
    assert budget["estimated_wal_bytes"] == 20_000_000
    assert budget["reserve_bytes"] == 10 * 1024**3
    assert budget["allowed"]
    insufficient = budget_for_rows(
        rows=26_930_000,
        sample_rows=1000,
        heap_bytes=1_000_000,
        index_bytes=500_000,
        toast_bytes=200_000,
        wal_bytes=2_000_000,
        free_bytes=31 * 1024**3,
    )
    assert not insufficient["allowed"]


def test_known_cents_oracle_and_scope_shape():
    parameters = FixtureParameters(objects=1000, seed=413, days=90)
    assert synthetic_fact(parameters, 1, 0)["spend"] == Decimal("4.31")
    assert synthetic_fact(parameters, 1, 0)["revenue"] == Decimal("8.63")
    assert parameters.manifest()["directory_rows"] == 1300
    assert parameters.manifest()["ad_day_rows"] == 9000
    assert parameters.manifest()["full_current_scale"] is False


def test_real_seed_preserves_parent_identity_and_exact_money():
    from sqlalchemy import text
    from sqlmodel import Session

    from app.core.db import engine
    from tests.performance.sync_query_fixture import seed_fixture

    parameters = FixtureParameters(objects=30, days=1, active_fraction=1)
    manifest = seed_fixture(engine, parameters)
    with Session(engine) as db:
        missing = db.execute(
            text("""SELECT count(*) FROM ad_object child
           LEFT JOIN ad_object parent ON parent.tenant_id=child.tenant_id
             AND parent.advertiser_id=child.advertiser_id AND parent.kind=child.parent_kind
             AND parent.remote_id=child.parent_remote_id
           WHERE child.tenant_id=CAST(:tenant AS uuid) AND child.parent_kind IS NOT NULL
             AND parent.remote_id IS NULL"""),
            {"tenant": manifest["tenant_id"]},
        ).scalar_one()
        assert missing == 0
        amount = db.execute(
            text(
                "SELECT sum(spend) FROM ad_report_day WHERE tenant_id=CAST(:tenant AS uuid)"
            ),
            {"tenant": manifest["tenant_id"]},
        ).scalar_one()
        assert (
            amount
            == expected_metrics(parameters, object_ids=range(1, 31), days=1)["spend"]
        )
