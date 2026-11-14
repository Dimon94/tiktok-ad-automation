"""Copy query contracts into the existing export owner; preserve legacy files."""

from alembic import op

revision = "native_frozen_export"
down_revision = "native_frozen_query"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE report_export ADD COLUMN result_contract jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE report_export ADD COLUMN publication_map jsonb NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade():
    raise RuntimeError(
        "Drain exports and use a reviewed compatible reader; retain direct facts"
    )
