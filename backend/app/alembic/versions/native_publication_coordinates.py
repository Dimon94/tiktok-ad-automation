"""Persist published empty-bucket coordinates without rewriting history."""

from alembic import op

revision = "native_publication_coordinates"
down_revision = "native_adgroup_day"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE report_sync_state ADD COLUMN publication_coordinates jsonb")


def downgrade():
    op.execute("ALTER TABLE report_sync_state DROP COLUMN publication_coordinates")
