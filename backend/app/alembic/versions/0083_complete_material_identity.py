"""Index complete material identities for skewed videos shared by many ads.

Revision ID: 0083_complete_material_identity
Revises: 0082_material_projection_probe
"""

import sqlalchemy as sa
from alembic import op

revision = "0083_complete_material_identity"
down_revision = "0082_material_projection_probe"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_ad_material_complete_identity",
        "ad_material_reference",
        [
            "tenant_id",
            "advertiser_id",
            "ad_remote_id",
            "platform_material_id",
            "material_type",
        ],
        postgresql_where=sa.text("complete"),
    )


def downgrade():
    op.drop_index(
        "ix_ad_material_complete_identity", table_name="ad_material_reference"
    )
