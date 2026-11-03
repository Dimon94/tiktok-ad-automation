"""明略类型迁移仅扩展约束；已有明略数据时拒绝破坏性降级。"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.providers.models import ProviderConnection
from tests.migration_database import historical_database
from tests.modules.conftest import create_context


def test_minglue_upgrade_and_guarded_downgrade(monkeypatch):
    with historical_database(monkeypatch, "0099_transition_replan") as (engine, config):
        with Session(engine) as session, session.begin():
            context = create_context(session)
            old = ProviderConnection(
                tenant_id=context.tenant_id,
                kind="jiashu",
                display_name="原有连接",
                encrypted_credentials="fixture",
            )
            session.add(old)
            old_id = old.id
        command.upgrade(config, "0100_minglue_provider")
        with Session(engine) as session, session.begin():
            assert session.get(ProviderConnection, old_id).display_name == "原有连接"
            row = ProviderConnection(
                tenant_id=context.tenant_id,
                kind="minglue",
                display_name="明略",
                encrypted_credentials="fixture",
            )
            session.add(row)
            identity = row.id
        with pytest.raises(RuntimeError, match="Minglue connections exist"):
            command.downgrade(config, "0099_transition_replan")
        with Session(engine) as session, session.begin():
            session.delete(session.get(ProviderConnection, identity))
        command.downgrade(config, "0099_transition_replan")
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text("SELECT display_name FROM provider_connection WHERE id=:id"),
                    {"id": old_id},
                ).scalar()
                == "原有连接"
            )
        with pytest.raises(IntegrityError):
            with Session(engine) as session, session.begin():
                session.add(
                    ProviderConnection(
                        tenant_id=context.tenant_id,
                        kind="minglue",
                        display_name="明略",
                        encrypted_credentials="fixture",
                    )
                )
        command.upgrade(config, "0100_minglue_provider")
