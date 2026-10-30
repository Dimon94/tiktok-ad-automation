"""Persist generation-scoped physical request cost receipts."""

from alembic import op

revision = "0086_sync_cost_ledger"
down_revision = "0085_dispatch_supersession"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "\nCREATE TABLE sync_cost_ledger (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\treport_job_id UUID, \n\tdirectory_run_id UUID, \n\tbalance_run_id UUID, \n\tgeneration INTEGER NOT NULL, \n\tphysical_endpoint VARCHAR(256) NOT NULL, \n\twork_class VARCHAR(32) NOT NULL, \n\teligible_weights JSONB NOT NULL, \n\tendpoint_weights JSONB NOT NULL, \n\treserved_units INTEGER NOT NULL, \n\tobserved_units INTEGER NOT NULL, \n\tcharged_units INTEGER NOT NULL, \n\tproducers JSONB NOT NULL, \n\tclosed BOOLEAN NOT NULL, \n\treceipt_version INTEGER NOT NULL, \n\tsettled_version INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE CASCADE, \n\tFOREIGN KEY(report_job_id) REFERENCES report_sync_job (id) ON DELETE CASCADE, \n\tFOREIGN KEY(directory_run_id) REFERENCES ad_directory_run (id) ON DELETE CASCADE, \n\tFOREIGN KEY(balance_run_id) REFERENCES report_balance_run (id) ON DELETE CASCADE, \n\tCONSTRAINT ck_sync_cost_execution CHECK (num_nonnulls(report_job_id, directory_run_id, balance_run_id) = 1), \n\tCONSTRAINT ck_sync_cost_numbers CHECK (generation > 0 AND reserved_units >= 0 AND observed_units >= 0 AND charged_units >= 0 AND receipt_version >= 0 AND settled_version >= 0 AND settled_version <= receipt_version), \n\tCONSTRAINT ck_sync_cost_contract CHECK (work_class IN ('realtime','history','directory','maintenance') AND jsonb_typeof(eligible_weights)='object' AND jsonb_typeof(endpoint_weights)='object' AND jsonb_typeof(producers)='object')\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_sync_cost_open ON sync_cost_ledger (tenant_id, physical_endpoint, work_class, lease_until) WHERE closed IS FALSE"
    )
    op.execute(
        "CREATE INDEX ix_sync_cost_retention ON sync_cost_ledger (updated_at, id)"
    )
    op.execute(
        "CREATE INDEX ix_sync_cost_unsettled ON sync_cost_ledger (tenant_id, physical_endpoint, id) WHERE receipt_version > settled_version"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sync_cost_balance_run_id ON sync_cost_ledger (balance_run_id, generation, physical_endpoint) WHERE balance_run_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sync_cost_directory_run_id ON sync_cost_ledger (directory_run_id, generation, physical_endpoint) WHERE directory_run_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sync_cost_report_job_id ON sync_cost_ledger (report_job_id, generation, physical_endpoint) WHERE report_job_id IS NOT NULL"
    )


def downgrade():
    op.drop_table("sync_cost_ledger")
