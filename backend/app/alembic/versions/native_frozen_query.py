"""Add immutable selected results; legacy snapshots remain incompatible."""

from alembic import op

revision = "native_frozen_query"
down_revision = "native_publication_coordinates"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE query_snapshot ADD COLUMN result_contract jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE query_snapshot ADD COLUMN publication_map jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE query_snapshot_row ADD COLUMN frozen_result jsonb NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade():
    raise RuntimeError(
        "Drain readers/writers and expire snapshots before a reviewed rollback; retain direct facts"
    )
