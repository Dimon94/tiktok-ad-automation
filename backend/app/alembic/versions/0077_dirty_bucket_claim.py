"""Index dirty bucket claim order instead of sorting the entire backlog.

Revision ID: 0077_dirty_bucket_claim
Revises: 0076_projection_transitions
"""

import sqlalchemy as sa
from alembic import op

revision = "0077_dirty_bucket_claim"
down_revision = "0076_projection_transitions"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_read_bucket_dirty", table_name="read_bucket_state")
    op.create_index(
        "ix_read_bucket_dirty",
        "read_bucket_state",
        ["observed_at", "tenant_id", "advertiser_id", "kind", "bucket"],
        postgresql_where=sa.text(
            "source_revision > derived_revision AND kind IN ('ad_day','material_day')"
        ),
    )


def downgrade():
    op.drop_index("ix_read_bucket_dirty", table_name="read_bucket_state")
    op.create_index(
        "ix_read_bucket_dirty",
        "read_bucket_state",
        ["kind", "observed_at"],
        postgresql_where=sa.text("source_revision > derived_revision"),
    )
