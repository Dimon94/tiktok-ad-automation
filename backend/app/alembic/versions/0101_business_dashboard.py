"""T+1 business accounting, immutable publications and rate revisions."""

from alembic import op

revision = "0101_business_dashboard"
down_revision = "0100_minglue_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE business_evidence (
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            advertiser_id VARCHAR(128) NOT NULL,
            campaign_id VARCHAR(128) NOT NULL,
            stat_date DATE NOT NULL,
            bid NUMERIC,
            eligible BOOLEAN,
            source VARCHAR(1000) NOT NULL,
            actor_id UUID NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (tenant_id, bc_id, advertiser_id, campaign_id, stat_date),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            FOREIGN KEY(tenant_id, advertiser_id) REFERENCES advertiser_account (tenant_id, advertiser_id),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
    """)
    op.execute("""
        CREATE TABLE business_export (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            actor_id UUID NOT NULL,
            filters JSONB NOT NULL,
            run_ids JSONB NOT NULL,
            advertiser_ids JSONB NOT NULL,
            status VARCHAR(16) NOT NULL,
            object_key VARCHAR,
            frozen BOOLEAN NOT NULL,
            issues JSONB NOT NULL,
            cursor INTEGER NOT NULL,
            file_offset INTEGER NOT NULL,
            rows_count INTEGER NOT NULL,
            error VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            CONSTRAINT uq_business_export_scope UNIQUE (tenant_id, bc_id, id),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
    """)
    op.execute("""
        CREATE TABLE business_member (
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            advertiser_id VARCHAR(128) NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE,
            PRIMARY KEY (tenant_id, bc_id, advertiser_id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            FOREIGN KEY(tenant_id, advertiser_id) REFERENCES advertiser_account (tenant_id, advertiser_id),
            CONSTRAINT ck_business_member_dates CHECK (end_date IS NULL OR end_date >= start_date)
        )
    """)
    op.execute("""
        CREATE TABLE business_rate (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            kind VARCHAR(16) NOT NULL,
            scope_id VARCHAR(128) NOT NULL,
            provider_id UUID,
            bc_id VARCHAR(128),
            effective_date DATE NOT NULL,
            revision INTEGER NOT NULL,
            rate NUMERIC NOT NULL,
            actor_id UUID NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(tenant_id, provider_id) REFERENCES provider_connection (tenant_id, id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            CONSTRAINT ck_business_rate_value CHECK (rate >= 0 AND rate <= 1),
            CONSTRAINT ck_business_rate_scope CHECK ((kind='provider' AND provider_id IS NOT NULL AND bc_id IS NULL) OR (kind='rebate' AND bc_id IS NOT NULL AND provider_id IS NULL)),
            CONSTRAINT uq_business_rate_revision UNIQUE (tenant_id, kind, scope_id, revision),
            FOREIGN KEY(tenant_id) REFERENCES tenant (id),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
    """)
    op.execute("""
        CREATE TABLE business_run (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            actor_id UUID NOT NULL,
            quarter DATE NOT NULL,
            end_date DATE NOT NULL,
            scope_revision INTEGER NOT NULL,
            phase VARCHAR(16) NOT NULL,
            cursor DATE NOT NULL,
            status VARCHAR(16) NOT NULL,
            inputs JSONB NOT NULL,
            coverage JSONB NOT NULL,
            source_signature VARCHAR(64) NOT NULL,
            rows_count INTEGER NOT NULL,
            base_after JSONB,
            error VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            finished_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            CONSTRAINT uq_business_run_scope UNIQUE (tenant_id, bc_id, id),
            CONSTRAINT ck_business_run_status CHECK (status IN ('QUEUED','RUNNING','COMPLETE','SUPERSEDED','FAILED')),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
    """)
    op.execute("""
        CREATE TABLE business_scope (
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            actor_id UUID NOT NULL,
            start_date DATE NOT NULL,
            revision INTEGER NOT NULL,
            last_daily DATE,
            next_check_at TIMESTAMP WITH TIME ZONE NOT NULL,
            history_cursor DATE,
            repair_cursor VARCHAR(512),
            published_revision INTEGER,
            published_runs JSONB NOT NULL,
            initialized_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (tenant_id, bc_id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
    """)
    op.execute("""
        CREATE TABLE business_day (
            spend NUMERIC,
            revenue NUMERIC,
            provider_share NUMERIC,
            net_revenue NUMERIC,
            rebate NUMERIC,
            business_revenue NUMERIC,
            compensation NUMERIC,
            subsidy_comic NUMERIC,
            subsidy_other NUMERIC,
            subsidy_existing NUMERIC,
            issues JSONB NOT NULL,
            run_id UUID NOT NULL,
            stat_date DATE NOT NULL,
            advertiser_id VARCHAR(128) NOT NULL,
            campaign_id VARCHAR(128) NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            drama_key VARCHAR(255) NOT NULL,
            drama_id UUID,
            drama_name VARCHAR NOT NULL,
            campaign_name VARCHAR NOT NULL,
            provider_id UUID,
            provider_name VARCHAR NOT NULL,
            currency VARCHAR(16) NOT NULL,
            timezone VARCHAR(64) NOT NULL,
            revenue_d0 NUMERIC,
            iaa_events NUMERIC,
            provider_rate NUMERIC,
            rebate_rate NUMERIC NOT NULL,
            compensation_bid NUMERIC,
            compensation_eligible BOOLEAN,
            compensation_source VARCHAR,
            PRIMARY KEY (run_id, stat_date, advertiser_id, campaign_id),
            FOREIGN KEY(tenant_id, bc_id, run_id) REFERENCES business_run (tenant_id, bc_id, id)
        )
         PARTITION BY RANGE (stat_date)
    """)
    op.execute("""
        CREATE TABLE business_export_row (
            export_id UUID NOT NULL,
            sequence INTEGER NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            row_key VARCHAR NOT NULL,
            rounding_group VARCHAR NOT NULL,
            payload JSONB NOT NULL,
            adjustments JSONB NOT NULL,
            PRIMARY KEY (export_id, sequence),
            FOREIGN KEY(tenant_id, bc_id, export_id) REFERENCES business_export (tenant_id, bc_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE business_publication (
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            quarter DATE NOT NULL,
            run_id UUID NOT NULL,
            PRIMARY KEY (tenant_id, bc_id, quarter),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            FOREIGN KEY(tenant_id, bc_id, run_id) REFERENCES business_run (tenant_id, bc_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE business_summary_day (
            spend NUMERIC,
            revenue NUMERIC,
            provider_share NUMERIC,
            net_revenue NUMERIC,
            rebate NUMERIC,
            business_revenue NUMERIC,
            compensation NUMERIC,
            subsidy_comic NUMERIC,
            subsidy_other NUMERIC,
            subsidy_existing NUMERIC,
            issues JSONB NOT NULL,
            run_id UUID NOT NULL,
            stat_date DATE NOT NULL,
            advertiser_id VARCHAR(128) NOT NULL,
            kind VARCHAR(16) NOT NULL,
            drama_key VARCHAR(255) NOT NULL,
            currency VARCHAR(16) NOT NULL,
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            drama_id UUID,
            provider_id UUID,
            drama_name VARCHAR NOT NULL,
            provider_name VARCHAR NOT NULL,
            campaign_name VARCHAR NOT NULL,
            campaign_id VARCHAR NOT NULL,
            PRIMARY KEY (run_id, stat_date, advertiser_id, kind, drama_key, currency),
            FOREIGN KEY(tenant_id, bc_id, run_id) REFERENCES business_run (tenant_id, bc_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE business_mapping (
            tenant_id UUID NOT NULL,
            bc_id VARCHAR(128) NOT NULL,
            advertiser_id VARCHAR(128) NOT NULL,
            campaign_id VARCHAR(128) NOT NULL,
            drama_id UUID NOT NULL,
            actor_id UUID NOT NULL,
            evidence VARCHAR(255) NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (tenant_id, bc_id, advertiser_id, campaign_id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
            FOREIGN KEY(tenant_id, advertiser_id) REFERENCES advertiser_account (tenant_id, advertiser_id),
            FOREIGN KEY(tenant_id, drama_id) REFERENCES provider_drama (tenant_id, id),
            FOREIGN KEY(actor_id) REFERENCES "user" (id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_business_export_expiry ON business_export (expires_at)
    """)
    op.execute("""
        CREATE INDEX ix_business_rate_lookup ON business_rate (tenant_id, kind, scope_id, effective_date, revision)
    """)
    op.execute("""
        CREATE INDEX ix_business_run_pending ON business_run (status, created_at)
    """)
    op.execute("""
        CREATE INDEX ix_business_run_scope ON business_run (tenant_id, bc_id, quarter, source_signature, end_date, status)
    """)
    op.execute("""
        CREATE INDEX ix_business_day_group ON business_day (tenant_id, bc_id, run_id, stat_date, drama_key)
    """)
    op.execute("""
        CREATE INDEX ix_business_day_provider ON business_day (tenant_id, bc_id, provider_id, stat_date)
    """)
    op.execute("""
        CREATE INDEX ix_business_export_rounding ON business_export_row (export_id, rounding_group, row_key)
    """)
    op.execute("""
        CREATE INDEX ix_business_summary_query ON business_summary_day (tenant_id, bc_id, run_id, kind, stat_date)
    """)
    op.execute("""
        CREATE TABLE business_day_202601 PARTITION OF business_day FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202602 PARTITION OF business_day FOR VALUES FROM ('2026-02-01') TO ('2026-03-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202603 PARTITION OF business_day FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202604 PARTITION OF business_day FOR VALUES FROM ('2026-04-01') TO ('2026-05-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202605 PARTITION OF business_day FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202606 PARTITION OF business_day FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202607 PARTITION OF business_day FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202608 PARTITION OF business_day FOR VALUES FROM ('2026-08-01') TO ('2026-09-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202609 PARTITION OF business_day FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202610 PARTITION OF business_day FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202611 PARTITION OF business_day FOR VALUES FROM ('2026-11-01') TO ('2026-12-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202612 PARTITION OF business_day FOR VALUES FROM ('2026-12-01') TO ('2027-01-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202701 PARTITION OF business_day FOR VALUES FROM ('2027-01-01') TO ('2027-02-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202702 PARTITION OF business_day FOR VALUES FROM ('2027-02-01') TO ('2027-03-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202703 PARTITION OF business_day FOR VALUES FROM ('2027-03-01') TO ('2027-04-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202704 PARTITION OF business_day FOR VALUES FROM ('2027-04-01') TO ('2027-05-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202705 PARTITION OF business_day FOR VALUES FROM ('2027-05-01') TO ('2027-06-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202706 PARTITION OF business_day FOR VALUES FROM ('2027-06-01') TO ('2027-07-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202707 PARTITION OF business_day FOR VALUES FROM ('2027-07-01') TO ('2027-08-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202708 PARTITION OF business_day FOR VALUES FROM ('2027-08-01') TO ('2027-09-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202709 PARTITION OF business_day FOR VALUES FROM ('2027-09-01') TO ('2027-10-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202710 PARTITION OF business_day FOR VALUES FROM ('2027-10-01') TO ('2027-11-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202711 PARTITION OF business_day FOR VALUES FROM ('2027-11-01') TO ('2027-12-01')
    """)
    op.execute("""
        CREATE TABLE business_day_202712 PARTITION OF business_day FOR VALUES FROM ('2027-12-01') TO ('2028-01-01')
    """)
    op.execute("""
        CREATE TABLE business_day_default PARTITION OF business_day DEFAULT
    """)


def downgrade() -> None:
    op.drop_table("business_mapping")
    op.drop_table("business_summary_day")
    op.drop_table("business_publication")
    op.drop_table("business_export_row")
    op.drop_table("business_day")
    op.drop_table("business_scope")
    op.drop_table("business_run")
    op.drop_table("business_rate")
    op.drop_table("business_member")
    op.drop_table("business_export")
    op.drop_table("business_evidence")
