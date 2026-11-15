"""Recognize native batch manifests in the existing provider-reference guard."""

from alembic import op
from sqlalchemy import text

revision = "native_batch_preview"
down_revision = "native_preview_freeze"
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
    marker = "p.config->'native_request'->>'kind'='NATIVE_SERIES_DIRECT_VIDEO'"
    if marker not in definition:
        raise RuntimeError("preview source guard changed; review expansion")
    op.execute(
        definition.replace(
            marker,
            "("
            + marker
            + " OR p.config->'native_batch'->>'revision'='native-series-batch-v1')",
            1,
        )
    )


def downgrade():
    raise RuntimeError("native batch previews are retained; review contract migration")
