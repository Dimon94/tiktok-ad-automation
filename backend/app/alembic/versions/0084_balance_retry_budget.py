"""Persist the balance retry budget across dispatch generations.

Revision ID: 0084_balance_retry_budget
Revises: 0083_complete_material_identity
"""

import sqlalchemy as sa
from alembic import op

revision = "0084_balance_retry_budget"
down_revision = "0083_complete_material_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "report_balance_run",
        sa.Column(
            "provider_failures", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_balance_provider_failures", "report_balance_run", "provider_failures >= 0"
    )
    op.alter_column("report_balance_run", "provider_failures", server_default=None)


def downgrade():
    op.drop_constraint(
        "ck_balance_provider_failures", "report_balance_run", type_="check"
    )
    op.drop_column("report_balance_run", "provider_failures")
