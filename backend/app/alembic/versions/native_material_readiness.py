"""Preserve ordered native material preparation evidence per draft revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "native_material_readiness"
down_revision = "native_series_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "draft_scene_preparation",
        sa.Column("native_materials", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM draft_scene_preparation WHERE native_materials IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "native material evidence exists; downgrade would lose preparation facts"
        )
    op.drop_column("draft_scene_preparation", "native_materials")
