"""两侧已有数据库可汇合，保留原业务行并获得两侧结构。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.migration_database import historical_database


@pytest.mark.parametrize(
    "revision", ["creative_fact_evidence", "0130_ai_review_reading"]
)
def test_upgrade_each_previous_head_preserves_data(monkeypatch, revision):
    with historical_database(monkeypatch, revision) as (engine, config):
        tenant_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tenant (id,name,active) VALUES (:id,'existing tenant',true)"
                ),
                {"id": tenant_id},
            )
        command.upgrade(config, "0131_native_ai_merge")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT name,active FROM tenant WHERE id=:id"), {"id": tenant_id}
            ).one() == ("existing tenant", True)
            inspector = inspect(connection)
            assert inspector.has_table("ai_optimizer")
            assert "scene_selection" in {
                column["name"] for column in inspector.get_columns("build_draft")
            }
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0131_native_ai_merge"]


@pytest.mark.parametrize("revision", ["0131_native_ai_merge", "0131_ai_review_archive"])
def test_upgrade_archive_and_native_heads_preserves_data(monkeypatch, revision):
    with historical_database(monkeypatch, revision) as (engine, config):
        tenant_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tenant (id,name,active) VALUES (:id,'archive merge tenant',true)"
                ),
                {"id": tenant_id},
            )
        command.upgrade(config, "0132_native_ai_archive_merge")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT name,active FROM tenant WHERE id=:id"), {"id": tenant_id}
            ).one() == ("archive merge tenant", True)
            inspector = inspect(connection)
            assert all(
                inspector.has_table(table)
                for table in (
                    "ai_review_source",
                    "ai_review_receipt",
                    "ai_review_artifact",
                    "parent_report_day",
                )
            )
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0132_native_ai_archive_merge"]
