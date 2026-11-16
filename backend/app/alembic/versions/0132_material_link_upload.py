"""Persist tenant-scoped VID link batches and fenced account operations."""

from alembic import op

revision = "0132_material_link_upload"
down_revision = "0131_ai_review_archive"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE material_link_batch (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            actor_id UUID NOT NULL,
            request_id UUID NOT NULL,
            request_digest VARCHAR(64) NOT NULL,
            naming_date VARCHAR(8) NOT NULL,
            input_count INTEGER NOT NULL,
            unique_count INTEGER NOT NULL,
            account_count INTEGER NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_material_link_request UNIQUE (tenant_id, request_id),
            CONSTRAINT uq_material_link_batch_scope UNIQUE (tenant_id, bc_id, id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_material_link_batch_created ON material_link_batch (tenant_id, bc_id, created_at, id)
        """
    )
    op.execute(
        """
        CREATE TABLE material_link_item (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            batch_id UUID NOT NULL,
            connection_id UUID NOT NULL,
            advertiser_id VARCHAR(128) NOT NULL,
            frozen_route JSONB NOT NULL,
            line_no INTEGER NOT NULL,
            source_video_id VARCHAR(128) NOT NULL,
            source_url VARCHAR(512) NOT NULL,
            requested_file_name VARCHAR(100) NOT NULL,
            actual_file_name VARCHAR(1000),
            status VARCHAR(32) NOT NULL,
            sent_at TIMESTAMP WITH TIME ZONE,
            video_id VARCHAR(128),
            mid VARCHAR(128),
            material_id UUID,
            duplicate_of UUID,
            error_code VARCHAR(128),
            error_message VARCHAR(1000),
            request_id VARCHAR(128),
            poll_attempts INTEGER NOT NULL,
            search_candidate JSONB NOT NULL,
            search_page INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            claim_id UUID,
            lease_until TIMESTAMP WITH TIME ZONE,
            next_run_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(tenant_id, bc_id, batch_id) REFERENCES material_link_batch (tenant_id, bc_id, id),
            CONSTRAINT uq_material_link_item_target UNIQUE (batch_id, line_no, advertiser_id),
            CONSTRAINT uq_material_link_item_scope UNIQUE (tenant_id, bc_id, advertiser_id, id),
            FOREIGN KEY(tenant_id, bc_id, advertiser_id, duplicate_of) REFERENCES material_link_item (tenant_id, bc_id, advertiser_id, id),
            FOREIGN KEY(tenant_id, bc_id, advertiser_id, connection_id) REFERENCES bc_account_access (tenant_id, bc_id, advertiser_id, connection_id),
            FOREIGN KEY(tenant_id, material_id) REFERENCES material_file (tenant_id, id),
            CONSTRAINT ck_material_link_item_frozen_route CHECK (frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0 AND frozen_route->>'connection_id' = connection_id::text), false)),
            CONSTRAINT ck_material_link_item_status CHECK (status IN ('pending','sending','submitted','verifying','unknown','needs_review','success','failed','blocked')),
            CONSTRAINT ck_material_link_item_numbers CHECK (line_no > 0 AND revision >= 0 AND poll_attempts >= 0 AND search_page >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_material_link_item_batch ON material_link_item (tenant_id, batch_id, line_no, id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_material_link_item_due ON material_link_item (next_run_at, id) WHERE status IN ('pending','sending','submitted','verifying','unknown')
        """
    )


def downgrade():
    op.drop_table("material_link_item")
    op.drop_table("material_link_batch")
