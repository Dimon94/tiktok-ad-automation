"""旧表、旧路由和旧派发不再参与运行；保留新的报表与目录入口。"""

from pathlib import Path

from sqlalchemy import inspect
from sqlmodel import SQLModel

from app.jobs.celery_app import celery_app
from app.main import app


def test_legacy_tables_are_removed_by_migration(session):
    old = {
        "report_fact",
        "report_observation",
        "report_coverage",
        "report_staged_page",
        "report_sync_run",
    }
    assert not old.intersection(inspect(session.connection()).get_table_names())
    assert not old.intersection(SQLModel.metadata.tables)
    # sync_schedule旧名被新规范需求头复用，校验新结构而非禁止合法表名。
    columns = {
        column["name"]
        for column in inspect(session.connection()).get_columns("sync_schedule")
    }
    assert {
        "scope_id",
        "physical_endpoint",
        "work_class",
        "lease_execution_generation",
    } <= columns
    assert {
        "ad_report_day",
        "material_report_day",
        "report_sync_job",
        "account_balance_observation",
    } <= set(inspect(session.connection()).get_table_names())


def test_only_new_reporting_endpoints_and_dispatch_remain():
    paths = app.openapi()["paths"]
    assert "/api/tenants/{tenant_id}/reports/trend" not in paths
    assert "/api/tenants/{tenant_id}/ad-sync-runs" not in paths
    assert "/api/tenants/{tenant_id}/ad-sync-refresh" in paths
    assert "app.modules.reporting.tasks" not in celery_app.conf.imports
    assert "reporting.sync_step" not in celery_app.conf.task_routes
    assert celery_app.conf.task_routes["reporting.job_step"]["queue"] == "ads-reporting"


def test_legacy_source_modules_are_deleted():
    root = Path(__file__).resolve().parents[3] / "app"
    paths = [
        *(
            f"modules/reporting/{name}.py"
            for name in (
                "contracts",
                "facts",
                "sync",
                "scheduling",
                "tasks",
                "aggregation",
                "trends",
                "sync_models",
            )
        ),
        "integrations/tiktok/adapters/sdk_reporting.py",
        "integrations/tiktok/adapters/mcp_reporting.py",
        "integrations/tiktok/contracts/reporting.py",
    ]
    assert not [p for p in paths if (root / p).exists()]
