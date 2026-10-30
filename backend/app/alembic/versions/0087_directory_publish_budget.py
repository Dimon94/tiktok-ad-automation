"""Keep local publication retry budget independent from page claim generations."""

import sqlalchemy as sa
from alembic import op

revision = "0087_directory_publish_budget"
down_revision = "0086_sync_cost_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ad_directory_run",
        sa.Column("publish_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_directory_publish_failures", "ad_directory_run", "publish_failures >= 0"
    )
    op.alter_column("ad_directory_run", "publish_failures", server_default=None)


def downgrade():
    op.drop_constraint(
        "ck_directory_publish_failures", "ad_directory_run", type_="check"
    )
    op.drop_column("ad_directory_run", "publish_failures")
