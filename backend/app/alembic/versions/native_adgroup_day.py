"""Add Adgroup selected view without rewriting any legacy money."""

from alembic import op

revision = "native_adgroup_day"
down_revision = "native_campaign_day"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE parent_report_day ADD COLUMN campaign_id text, ADD COLUMN campaign_name text"
    )
    op.execute(
        "CREATE VIEW adgroup_selected_day AS\nSELECT tenant_id,advertiser_id,stat_date,remote_id AS adgroup_id,object_name AS adgroup_name,campaign_id,campaign_name,currency,timezone,fetched_at,sync_job_id,spend,impressions,clicks,ad_impression_events,revenue_d0,revenue_d3,revenue_d7,revenue_d14,revenue_d30,revenue_total,spend IS NULL AS spend_has_null,impressions IS NULL AS impressions_has_null,clicks IS NULL AS clicks_has_null,ad_impression_events IS NULL AS ad_impression_events_has_null,revenue_d0 IS NULL AS revenue_d0_has_null,revenue_d3 IS NULL AS revenue_d3_has_null,revenue_d7 IS NULL AS revenue_d7_has_null,revenue_d14 IS NULL AS revenue_d14_has_null,revenue_d30 IS NULL AS revenue_d30_has_null,revenue_total IS NULL AS revenue_total_has_null,0::bigint AS source_revision FROM parent_report_day WHERE level='adgroup'\nUNION ALL\nSELECT d.tenant_id,d.advertiser_id,d.stat_date,d.adgroup_id,d.adgroup_name,d.campaign_id,d.campaign_name,d.currency,d.timezone,d.fetched_at,NULL::uuid AS sync_job_id,d.spend,d.impressions,d.clicks,d.ad_impression_events,d.revenue_d0,d.revenue_d3,d.revenue_d7,d.revenue_d14,d.revenue_d30,d.revenue_total,d.spend_has_null,d.impressions_has_null,d.clicks_has_null,d.ad_impression_events_has_null,d.revenue_d0_has_null,d.revenue_d3_has_null,d.revenue_d7_has_null,d.revenue_d14_has_null,d.revenue_d30_has_null,d.revenue_total_has_null,d.source_revision FROM adgroup_report_day d\nWHERE NOT EXISTS (SELECT 1 FROM report_sync_state s WHERE s.tenant_id=d.tenant_id AND s.advertiser_id=d.advertiser_id AND s.stat_date=d.stat_date AND s.report_kind='adgroup_day' AND s.last_success_at IS NOT NULL)"
    )


def downgrade():
    raise RuntimeError("Direct money requires a reviewed compatible reader")
