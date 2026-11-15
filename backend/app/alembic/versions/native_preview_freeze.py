"""Expand provider-reference guard for native Series frozen previews."""

from alembic import op
from sqlalchemy import text

revision = "native_preview_freeze"
down_revision = "native_material_readiness"
branch_labels = None
depends_on = None


def upgrade():
    definition = (
        op.get_bind()
        .execute(
            text(
                "SELECT pg_get_functiondef('check_copy_source_references()'::regprocedure)"
            )
        )
        .scalar_one()
    )
    marker = "IF COALESCE(is_copy,false) THEN"
    replacement = """IF EXISTS (SELECT 1 FROM build_preview p WHERE p.tenant_id=NEW.tenant_id AND p.id=NEW.preview_id
            AND p.config->'native_request'->>'kind'='NATIVE_SERIES_DIRECT_VIDEO') THEN
            IF NEW.provider_drama_id IS NOT NULL OR NEW.link_id IS NOT NULL THEN
              RAISE EXCEPTION 'native preview cannot invent provider references';
            END IF;
          ELSIF COALESCE(is_copy,false) THEN"""
    if marker not in definition:
        raise RuntimeError("preview source guard changed; review expansion")
    op.execute(definition.replace(marker, replacement, 1))


def downgrade():
    raise RuntimeError(
        "native frozen previews are retained; downgrade requires reviewed contract migration"
    )
