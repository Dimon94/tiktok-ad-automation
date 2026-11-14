"""Campaign direct facts; no historical amounts or derived writers changed."""

from alembic import op

revision = "native_campaign_day"
down_revision = "actor_authorization_stamp"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE ad_report_day ADD COLUMN evidence jsonb")
    op.execute(
        "ALTER TABLE report_sync_state ADD COLUMN active_contract varchar(128), ADD COLUMN publication_digest varchar(64)"
    )
    op.execute("""CREATE TABLE parent_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL,
        level varchar(16) NOT NULL CHECK (level IN ('campaign','adgroup')),
        remote_id varchar(128) NOT NULL, stat_date date NOT NULL,
        object_name text, currency varchar(16) NOT NULL, timezone varchar(64) NOT NULL,
        fetched_at timestamptz NOT NULL, sync_job_id uuid NOT NULL,
        evidence jsonb NOT NULL,
        spend numeric, impressions numeric, clicks numeric, ad_impression_events numeric, revenue_d0 numeric, revenue_d3 numeric, revenue_d7 numeric, revenue_d14 numeric, revenue_d30 numeric, revenue_total numeric,
        PRIMARY KEY(tenant_id,advertiser_id,level,remote_id,stat_date))""")
    op.execute(
        "CREATE INDEX ix_parent_report_period ON parent_report_day (tenant_id,advertiser_id,level,stat_date)"
    )
    op.execute("""CREATE VIEW campaign_selected_day AS
SELECT tenant_id,advertiser_id,stat_date,remote_id AS campaign_id,object_name AS campaign_name,currency,timezone,fetched_at,sync_job_id,spend,impressions,clicks,ad_impression_events,revenue_d0,revenue_d3,revenue_d7,revenue_d14,revenue_d30,revenue_total FROM parent_report_day WHERE level='campaign'
UNION ALL
SELECT d.tenant_id,d.advertiser_id,d.stat_date,d.campaign_id,d.campaign_name,d.currency,d.timezone,d.fetched_at,d.sync_job_id,d.spend,d.impressions,d.clicks,d.ad_impression_events,d.revenue_d0,d.revenue_d3,d.revenue_d7,d.revenue_d14,d.revenue_d30,d.revenue_total FROM campaign_report_day d
WHERE NOT EXISTS (SELECT 1 FROM report_sync_state s WHERE s.tenant_id=d.tenant_id AND s.advertiser_id=d.advertiser_id AND s.stat_date=d.stat_date AND s.report_kind='campaign_day' AND s.last_success_at IS NOT NULL)""")


def downgrade():
    raise RuntimeError(
        "Direct money must be preserved; review a compatible reader before rollback"
    )
