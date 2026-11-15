"""Fresh historical schemas; never downgrade a shared/current application DB."""

import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from tests.database import require_test_database


def insert_historical_row(session, row):
    """只写迁移前已有列；nullable JSON 的None保持SQL NULL。"""
    from sqlalchemy import null
    from sqlalchemy.types import JSON

    table = Table(row.__tablename__, MetaData(), autoload_with=session.connection())
    session.execute(
        table.insert().values(
            **{
                key: null()
                if value is None
                and table.c[key].nullable
                and isinstance(table.c[key].type, JSON)
                else value
                for key, value in row.model_dump().items()
                if key in table.c
            }
        )
    )
    return row


def insert_historical_asset(session, row):
    """旧映射按当时列播种，主图类型与授权代数不渗入旧 schema。"""
    table = Table("account_material", MetaData(), autoload_with=session.connection())
    session.execute(
        table.insert().values(
            **{key: value for key, value in row.model_dump().items() if key in table.c}
        )
    )
    return row


def insert_historical_material(session, row):
    """历史素材只播种当时的列，新媒体类型不能渗入旧 schema。"""
    table = Table("material_file", MetaData(), autoload_with=session.connection())
    session.execute(
        table.insert().values(
            **{key: value for key, value in row.model_dump().items() if key in table.c}
        )
    )
    return row


def insert_historical_bc(session, *, tenant_id, bc_id):
    """播种旧版四列 BC，避免当前 ORM 将新主账户字段写入历史 schema。"""
    session.execute(
        text(
            "INSERT INTO tenant_bc (tenant_id,bc_id,name,ownership_conflict) "
            "VALUES (:tenant_id,:bc_id,'',false)"
        ),
        {"tenant_id": tenant_id, "bc_id": bc_id},
    )


def insert_historical_account(
    session,
    *,
    tenant_id,
    advertiser_id,
    name="",
    currency="USD",
    timezone="UTC",
    remote_status="ENABLE",
):
    """旧版账户显式列播种，避免今日 ORM 的远端创建时间列渗入历史 schema。"""
    session.execute(
        text(
            "INSERT INTO advertiser_account "
            "(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict) "
            "VALUES (:tenant_id,:advertiser_id,:name,:currency,:timezone,:remote_status,false)"
        ),
        {
            "tenant_id": tenant_id,
            "advertiser_id": advertiser_id,
            "name": name,
            "currency": currency,
            "timezone": timezone,
            "remote_status": remote_status,
        },
    )


def insert_historical_application(
    session,
    *,
    tenant_id,
    connection_id,
    external_id,
    name,
    channel_config=None,
    tiktok_minis_id=None,
):
    """旧版推广应用显式列播种，避免今日 ORM 的 link_defaults 列渗入历史 schema。"""
    session.execute(
        text(
            "INSERT INTO provider_application "
            "(id,tenant_id,connection_id,external_id,name,channel_config,tiktok_minis_id) "
            "VALUES (:id,:tenant_id,:connection_id,:external_id,:name,"
            "CAST(:channel_config AS jsonb),:tiktok_minis_id)"
        ),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "external_id": external_id,
            "name": name,
            "channel_config": json.dumps(channel_config or {}),
            "tiktok_minis_id": tiktok_minis_id,
        },
    )


@contextmanager
def historical_database(monkeypatch, revision):
    require_test_database(str(settings.DATABASE_URL))
    source = make_url(str(settings.DATABASE_URL))
    name = f"migration_{uuid4().hex}_test"
    target = source.set(database=name)
    admin_url = source.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_engine(target)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                settings, "DATABASE_URL", target.render_as_string(hide_password=False)
            )
            backend = Path(__file__).resolve().parents[1]
            config = Config(str(backend / "alembic.ini"))
            config.set_main_option("script_location", str(backend / "app/alembic"))
            command.upgrade(config, revision)
            yield engine, config
    finally:
        engine.dispose()
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
