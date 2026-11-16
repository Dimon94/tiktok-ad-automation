"""旧表先退休，旧列保全后退休；两个阶段都保持事务原子性。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.store_models import AdSyncBc
from scripts.preserve_legacy_creative_identities import preserve_creative_identities
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed

TABLES = {"material_post_link", "material_report_day", "material_link_scan"}


def _seed(db):
    seed = sync_seed.__wrapped__(db, historical=True)
    now = datetime.now(UTC)
    db.add(
        AdSyncBc(
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            frozen_route={},
            actor_id=seed.actor_id,
            next_hot_at=now,
            next_material_at=now,
            next_directory_at=now,
            next_daily_at=now,
        )
    )
    db.flush()
    db.execute(
        text("""INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type,
         name,creative_ids,complete,published_version)
        VALUES (:id,:tenant,'acc-1','parent','video','VIDEO','legacy','["missing"]',true,1)
    """),
        {"id": uuid4(), "tenant": seed.tenant_id},
    )
    return seed.tenant_id


def _progress(connection):
    return connection.execute(
        text("SELECT screening_progress FROM ad_sync_bc")
    ).scalar_one()


def test_table_retirement_keeps_unpreserved_column_and_current_orm_writable(
    monkeypatch,
):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            tenant = _seed(db)
        command.upgrade(config, "0117_directory_status_version")
        assert not TABLES.intersection(inspect(engine).get_table_names())
        with Session(engine) as db, db.begin():
            assert db.execute(
                text("SELECT creative_ids FROM ad_material_reference")
            ).scalar_one() == ["missing"]
            assert (
                db.execute(text("SELECT count(*) FROM ad_creative")).scalar_one() == 0
            )
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname LIKE 'qrm_material_post_link_%' OR proname LIKE 'qrm_material_report_day_%'"
                    )
                ).scalar_one()
                == 0
            )
            progress = _progress(db)
            assert (
                progress["creative_material_table_retirement"]["revision"]
                == "0116_drop_legacy_material"
            )
            assert "creative_material_column_retirement" not in progress
            # 新 ORM 已不写旧列，数据库默认值只作用新行，旧关系必须原样保留。
            db.add(
                AdMaterialReference(
                    tenant_id=tenant,
                    advertiser_id="acc-1",
                    ad_remote_id="new-parent",
                    platform_material_id="new-video",
                    material_type="VIDEO",
                    published_version=2,
                )
            )
            db.flush()
            assert (
                db.execute(
                    text(
                        "SELECT creative_ids FROM ad_material_reference WHERE ad_remote_id='new-parent'"
                    )
                ).scalar_one()
                == []
            )
            assert db.execute(
                text(
                    "SELECT creative_ids FROM ad_material_reference WHERE ad_remote_id='parent'"
                )
            ).scalar_one() == ["missing"]
            # 表退休之后前置 CLI 仍可保全剩余关系。
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")


def test_table_drop_dependency_failure_rolls_back_entire_first_stage(monkeypatch):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            _seed(db)
            db.execute(
                text(
                    "CREATE VIEW retirement_dependency AS SELECT tenant_id FROM material_link_scan"
                )
            )
        with pytest.raises(DBAPIError):
            command.upgrade(config, "0116_drop_legacy_material")
        assert TABLES <= set(inspect(engine).get_table_names())
        assert (
            next(
                c
                for c in inspect(engine).get_columns("ad_material_reference")
                if c["name"] == "creative_ids"
            )["default"]
            is None
        )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0115_copy_current_source"
            )
            assert connection.execute(
                text("SELECT creative_ids FROM ad_material_reference")
            ).scalar_one() == ["missing"]
            assert "creative_material_table_retirement" not in _progress(connection)
