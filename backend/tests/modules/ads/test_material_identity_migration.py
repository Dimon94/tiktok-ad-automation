"""目录空字段结构往返不触碰素材日报/帖子关联中的同名业务身份。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, insert, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.ads.models import AdMaterialReference
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def test_drop_directory_main_identity_preserves_report_chain_and_reverses_schema(
    monkeypatch,
):
    with historical_database(monkeypatch, "0041_rule_frequency") as (engine, config):
        now = datetime.now(UTC)
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            table = Table(
                "ad_material_reference", MetaData(), autoload_with=session.connection()
            )
            usage = AdMaterialReference(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-2",
                ad_remote_id="ad",
                platform_material_id="video",
                ad_material_id="inner",
                material_type="VIDEO",
                name="素材",
                creative_ids=["creative"],
                complete=True,
                published_version=1,
            )
            session.execute(
                insert(table),
                usage.model_dump()
                | {
                    "creative_ids": ["creative"],
                    "main_material_id": None,
                    "main_material_type": None,
                },
            )
            session.execute(
                Table(
                    "material_report_day",
                    MetaData(),
                    autoload_with=session.connection(),
                )
                .insert()
                .values(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-2",
                    stat_date=now.date(),
                    main_material_id="post",
                    main_material_type="VIDEO_SPARK_ADS",
                    currency="USD",
                    timezone="UTC",
                    fetched_at=now,
                    sync_job_id=uuid4(),
                    spend=10,
                )
            )
            session.execute(
                Table(
                    "material_post_link", MetaData(), autoload_with=session.connection()
                )
                .insert()
                .values(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-2",
                    ad_remote_id="ad",
                    main_material_id="post",
                    main_material_type="VIDEO_SPARK_ADS",
                    ad_material_id="inner",
                    platform_material_id="video",
                    observed_at=now,
                )
            )
        command.upgrade(config, "0042_drop_ad_material_main")
        names = {
            c["name"] for c in inspect(engine).get_columns("ad_material_reference")
        }
        assert not {"main_material_id", "main_material_type"} & names
        assert "ck_ad_material_main_identity" not in {
            c["name"]
            for c in inspect(engine).get_check_constraints("ad_material_reference")
        }
        for table_name in ("material_report_day", "material_post_link"):
            assert {"main_material_id", "main_material_type"} <= {
                c["name"] for c in inspect(engine).get_columns(table_name)
            }
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        f"SELECT main_material_id,main_material_type FROM {table_name}"
                    )
                ).one() == ("post", "VIDEO_SPARK_ADS")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT platform_material_id,ad_material_id,creative_ids FROM ad_material_reference"
                )
            ).one() == ("video", "inner", ["creative"])
        command.downgrade(config, "0041_rule_frequency")
        columns = {
            c["name"]: c for c in inspect(engine).get_columns("ad_material_reference")
        }
        assert columns["main_material_id"]["type"].length == 128
        assert columns["main_material_type"]["type"].length == 64
        assert (
            columns["main_material_id"]["nullable"]
            and columns["main_material_type"]["nullable"]
        )
        with engine.begin() as connection:
            assert connection.execute(
                text(
                    "SELECT main_material_id,main_material_type FROM ad_material_reference"
                )
            ).one() == (None, None)
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text("UPDATE ad_material_reference SET main_material_id='untyped'")
                )
        from scripts.preserve_legacy_creative_identities import (
            preserve_creative_identities,
        )

        command.upgrade(config, "0114_retire_material")
        with Session(engine) as session, session.begin():
            preserve_creative_identities(
                session, tenant_id=seed.tenant_id, advertiser_id="acc-2"
            )
        command.upgrade(config, "head")
        command.check(config)
