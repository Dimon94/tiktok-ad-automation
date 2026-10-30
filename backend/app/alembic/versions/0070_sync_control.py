"""Frozen demand contracts and observation-based publication fencing.

Revision ID: 0070_sync_control
Revises: 0069_management_batches

Existing observation times remain NULL: use last_success_at conservatively until
an actual new snapshot publishes. No mass backfill guesses a source timestamp.
"""

from alembic import op

revision = "0070_sync_control"
down_revision = "0069_management_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE report_sync_state ADD COLUMN observed_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE report_sync_state ADD COLUMN publication_version BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE report_sync_job ADD COLUMN claim_generation BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE report_sync_job ADD COLUMN owner_epoch BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE report_sync_job ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 2"
    )
    op.execute(
        "ALTER TABLE ad_directory_run ADD COLUMN owner_epoch BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE ad_directory_run ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 2"
    )
    op.execute(
        "ALTER TABLE report_balance_run ADD COLUMN claim_generation BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE report_balance_run ADD COLUMN owner_epoch BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE report_balance_run ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 2"
    )
    op.execute("ALTER TABLE report_balance_run ADD COLUMN claim_token UUID")
    op.execute(
        "ALTER TABLE report_balance_run ADD COLUMN claimed_until TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE report_balance_run ADD COLUMN error_code VARCHAR(128)")
    op.execute(
        "ALTER TABLE report_balance_run ADD COLUMN next_attempt_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "CREATE TABLE sync_scope (\n\ttenant_id UUID NOT NULL, \n\tadvertiser_id VARCHAR(128) NOT NULL, \n\tbc_id VARCHAR(128) NOT NULL, \n\tactor_id UUID NOT NULL, \n\tconnection_id UUID NOT NULL, \n\tchannel VARCHAR(32) NOT NULL, \n\tfrozen_route JSONB NOT NULL, \n\tid UUID NOT NULL, \n\tresource VARCHAR(64) NOT NULL, \n\ttimezone VARCHAR(128) NOT NULL, \n\tidentity_digest VARCHAR(64) NOT NULL, \n\tquery_contract JSONB NOT NULL, \n\towner_epoch INTEGER NOT NULL, \n\tcontract_version INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id, advertiser_id) REFERENCES advertiser_account (tenant_id, advertiser_id), \n\tFOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id), \n\tFOREIGN KEY(actor_id) REFERENCES \"user\" (id), \n\tFOREIGN KEY(tenant_id, connection_id, channel) REFERENCES tiktok_connection (tenant_id, id, kind), \n\tCONSTRAINT ck_sync_scope_frozen_route CHECK (frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0 AND frozen_route->>'connection_id' = connection_id::text), false)), \n\tCONSTRAINT ck_sync_scope_route_generation CHECK (frozen_route ? 'binding_revision' AND frozen_route->>'channel' = channel), \n\tCONSTRAINT uq_sync_scope_identity UNIQUE (identity_digest), \n\tCONSTRAINT uq_sync_scope_tenant UNIQUE (id, tenant_id), \n\tCONSTRAINT ck_sync_scope_fence CHECK (owner_epoch > 0 AND contract_version = 2)\n)"
    )
    op.execute(
        "CREATE TABLE sync_requirement (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tscope_id UUID NOT NULL, \n\tbucket_key VARCHAR(256) NOT NULL, \n\tbucket JSONB NOT NULL, \n\tdesired_revision INTEGER NOT NULL, \n\tsatisfied_revision INTEGER NOT NULL, \n\trequested_after TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdeadline TIMESTAMP WITH TIME ZONE, \n\toldest_pending_at TIMESTAMP WITH TIME ZONE, \n\tsource VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(scope_id, tenant_id) REFERENCES sync_scope (id, tenant_id) ON DELETE CASCADE, \n\tCONSTRAINT uq_sync_requirement_bucket UNIQUE (scope_id, bucket_key), \n\tCONSTRAINT uq_sync_requirement_scope UNIQUE (id, scope_id, tenant_id), \n\tCONSTRAINT ck_sync_requirement_revision CHECK (desired_revision > 0 AND satisfied_revision >= 0 AND satisfied_revision <= desired_revision)\n)"
    )
    op.execute(
        "CREATE INDEX ix_sync_requirement_pending ON sync_requirement (scope_id, oldest_pending_at) WHERE satisfied_revision < desired_revision"
    )
    op.execute(
        "CREATE TABLE sync_schedule (\n\tscope_id UUID NOT NULL, \n\twork_class VARCHAR(32) NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tnext_eligible_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\twait_reason VARCHAR(32), \n\tphysical_endpoint VARCHAR(256) NOT NULL, \n\tcost FLOAT NOT NULL, \n\tdeficit FLOAT NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tlease_token UUID, \n\tleased_until TIMESTAMP WITH TIME ZONE, \n\toldest_pending_at TIMESTAMP WITH TIME ZONE, \n\tlast_dispatched_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (scope_id, work_class), \n\tFOREIGN KEY(scope_id, tenant_id) REFERENCES sync_scope (id, tenant_id) ON DELETE CASCADE, \n\tCONSTRAINT ck_sync_schedule_class CHECK (work_class IN ('realtime','history','directory','maintenance')), \n\tCONSTRAINT ck_sync_schedule_numbers CHECK (generation >= 0 AND cost > 0)\n)"
    )
    op.execute(
        "CREATE INDEX ix_sync_schedule_due ON sync_schedule (physical_endpoint, next_eligible_at, tenant_id) WHERE active IS TRUE"
    )
    op.execute(
        "CREATE TABLE sync_execution_scope (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tscope_id UUID NOT NULL, \n\trequirement_id UUID NOT NULL, \n\treport_job_id UUID, \n\tdirectory_run_id UUID, \n\tbalance_run_id UUID, \n\tfrozen_revision INTEGER NOT NULL, \n\towner_epoch INTEGER NOT NULL, \n\texecution_generation INTEGER NOT NULL, \n\tobserved_at TIMESTAMP WITH TIME ZONE, \n\tpublished_version INTEGER, \n\tstatus VARCHAR(16) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(requirement_id, scope_id, tenant_id) REFERENCES sync_requirement (id, scope_id, tenant_id) ON DELETE CASCADE, \n\tFOREIGN KEY(report_job_id) REFERENCES report_sync_job (id) ON DELETE CASCADE, \n\tFOREIGN KEY(directory_run_id) REFERENCES ad_directory_run (id) ON DELETE CASCADE, \n\tFOREIGN KEY(balance_run_id) REFERENCES report_balance_run (id) ON DELETE CASCADE, \n\tCONSTRAINT ck_sync_execution_reference CHECK (num_nonnulls(report_job_id, directory_run_id, balance_run_id) = 1), \n\tCONSTRAINT ck_sync_execution_fence CHECK (frozen_revision > 0 AND owner_epoch > 0 AND execution_generation >= 0), \n\tCONSTRAINT ck_sync_execution_status CHECK (status IN ('PENDING','PUBLISHED','SUPERSEDED'))\n)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sync_execution_balance_run_id ON sync_execution_scope (balance_run_id, requirement_id) WHERE balance_run_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sync_execution_directory_run_id ON sync_execution_scope (directory_run_id, requirement_id) WHERE directory_run_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sync_execution_report_job_id ON sync_execution_scope (report_job_id, requirement_id) WHERE report_job_id IS NOT NULL"
    )


def downgrade():
    op.drop_table("sync_execution_scope")
    op.drop_table("sync_schedule")
    op.drop_table("sync_requirement")
    op.drop_table("sync_scope")
    op.drop_column("report_balance_run", "next_attempt_at")
    op.drop_column("report_balance_run", "error_code")
    op.drop_column("report_balance_run", "claimed_until")
    op.drop_column("report_balance_run", "claim_token")
    op.drop_column("report_balance_run", "contract_version")
    op.drop_column("report_balance_run", "owner_epoch")
    op.drop_column("report_balance_run", "claim_generation")
    op.drop_column("ad_directory_run", "contract_version")
    op.drop_column("ad_directory_run", "owner_epoch")
    op.drop_column("report_sync_job", "contract_version")
    op.drop_column("report_sync_job", "owner_epoch")
    op.drop_column("report_sync_job", "claim_generation")
    op.drop_column("report_sync_state", "publication_version")
    op.drop_column("report_sync_state", "observed_at")
