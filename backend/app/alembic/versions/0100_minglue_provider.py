"""Allow the Minglue copyright provider kind."""

import sqlalchemy as sa
from alembic import op

revision = "0100_minglue_provider"
down_revision = "0099_transition_replan"
branch_labels = None
depends_on = None

_OLD_KINDS = "kind IN ('wangyan','jiashu','duiba','gangganhao','rongliang','other')"
_NEW_KINDS = "kind IN ('wangyan','jiashu','duiba','gangganhao','rongliang','minglue','other')"


def upgrade() -> None:
    op.drop_constraint(
        "ck_provider_connection_kind", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_kind", "provider_connection", _NEW_KINDS
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM provider_connection WHERE kind = 'minglue')"
        )
    ).scalar():
        raise RuntimeError(
            "Minglue connections exist; downgrade would invalidate them"
        )
    op.drop_constraint(
        "ck_provider_connection_kind", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_kind", "provider_connection", _OLD_KINDS
    )
