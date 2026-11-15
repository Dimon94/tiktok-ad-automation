"""Historical Mini draft/preview bytes survive the additive native migration."""

from alembic import command
from sqlalchemy import text
from sqlmodel import Session

from tests.migration_database import historical_database
from tests.modules.builds.test_route_migration import historical_rows


def test_native_migration_preserves_mini_draft_and_frozen_preview(monkeypatch):
    with historical_database(monkeypatch, "mcp_build_recovery") as (engine, config):
        with Session(engine) as session, session.begin():
            historical_rows(session, connections=0)
        command.upgrade(config, "0118_sync_schedule_service_age")
        with engine.begin() as db:
            db.execute(
                text(
                    "UPDATE build_draft SET identity_id='history-owner', identity_type='TT_USER'"
                )
            )
        with engine.connect() as db:
            drafts = db.execute(
                text("SELECT to_jsonb(d) FROM build_draft d ORDER BY id")
            ).all()
            previews = db.execute(
                text("SELECT to_jsonb(p) FROM build_preview p ORDER BY id")
            ).all()
        command.upgrade(config, "native_series_identity")
        with engine.connect() as db:
            assert (
                db.execute(
                    text(
                        "SELECT (to_jsonb(d) - 'scene_selection') || jsonb_build_object('identity_id',scene_selection->'creative_identity'->>'identity_id','identity_type',scene_selection->'creative_identity'->>'identity_type','identity_authorized_bc_id',scene_selection->'creative_identity'->>'identity_authorized_bc_id') FROM build_draft d ORDER BY id"
                    )
                ).all()
                == drafts
            )
            assert (
                db.execute(
                    text("SELECT to_jsonb(p) FROM build_preview p ORDER BY id")
                ).all()
                == previews
            )
        command.downgrade(config, "0118_sync_schedule_service_age")
        with engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT to_jsonb(d) FROM build_draft d ORDER BY id")
                ).all()
                == drafts
            )
