"""Preserve raw creative field observations without inventing historical evidence."""

from alembic import op

revision = "creative_fact_evidence"
down_revision = "native_frozen_export"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE creative_report_day ADD COLUMN evidence jsonb")
    op.execute("ALTER TABLE creative_material_entity_day ADD COLUMN evidence jsonb")


def downgrade():
    raise RuntimeError("creative evidence cannot be discarded automatically")
