"""Derived rows follow account deletion; canonical facts and controls stay unchanged.

Revision ID: 0081_derived_account_lifecycle
Revises: 0080_scope_execution_indexes
"""

from alembic import op

revision = "0081_derived_account_lifecycle"
down_revision = "0080_scope_execution_indexes"
branch_labels = None
depends_on = None
TABLES = (
    "read_bucket_state",
    "ad_entity_read",
    "campaign_name_current",
    "current_ad_material",
    "adgroup_report_day",
    "material_entity_day",
)


def _replace(ondelete):
    for table in TABLES:
        name = f"{table}_tenant_id_advertiser_id_fkey"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            "advertiser_account",
            ["tenant_id", "advertiser_id"],
            ["tenant_id", "advertiser_id"],
            ondelete=ondelete,
        )


def upgrade():
    _replace("CASCADE")


def downgrade():
    _replace(None)
