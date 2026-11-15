from logging import getLogger
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
assert config.config_file_name is not None
# 同进程迁移不能关闭业务脱敏诊断；仅保留该安全 logger 的原状态，
# 不全局启用 existing loggers，以免重新开启 SDK 的原始请求/令牌日志。
transport_logger = getLogger("app.tiktok.transport")
transport_logging_disabled = transport_logger.disabled
fileConfig(config.config_file_name)
transport_logger.disabled = transport_logging_disabled

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
# target_metadata = None

from app.models import SQLModel  # noqa
from app.jobs import models as jobs_models  # noqa: E402, F401
from app.modules.tenants import models as tenants_models  # noqa: E402, F401
from app.modules.material_production import models as material_production_models  # noqa: E402, F401
from app.modules.accounts import models as accounts_models  # noqa: E402, F401
from app.modules.accounts import connection_models  # noqa: E402, F401
from app.modules.accounts import capability_models  # noqa: E402, F401
from app.modules.accounts import directory_models  # noqa: E402, F401
from app.modules.accounts import discovery_models  # noqa: E402, F401
from app.modules.builds import execution_models  # noqa: E402, F401
from app.modules.builds import correction_models  # noqa: E402, F401
from app.modules.builds import route_models  # noqa: E402, F401
from app.modules.builds import recovery_models  # noqa: E402, F401
from app.modules.builds import recovery_routes  # noqa: E402, F401
from app.modules.builds import mini_targets  # noqa: E402, F401
from app.modules.builds import scene_job_models  # noqa: E402, F401
from app.modules.materials import models as materials_models  # noqa: E402, F401
from app.modules.materials import seed_models  # noqa: E402, F401
from app.modules.materials import batch_models  # noqa: E402, F401
from app.modules.materials import cover_models  # noqa: E402, F401
from app.modules.materials import ingest_models  # noqa: E402, F401
from app.modules.materials import push_models  # noqa: E402, F401
from app.modules.providers import models as provider_models  # noqa: E402, F401
from app.modules.strategies import models as strategy_models  # noqa: E402, F401
from app.modules.builds import models as builds_models  # noqa: E402, F401
from app.modules.builds import scene_models  # noqa: E402, F401
from app.modules.builds import preview_models  # noqa: E402, F401
from app.modules.builds import mutation_models  # noqa: E402, F401
from app.modules.ads import models as ads_models  # noqa: E402, F401
from app.modules.ads import creative_models  # noqa: E402, F401
from app.modules.ads import sync_models as ads_sync_models  # noqa: E402, F401
from app.modules.reporting import models as reporting_models  # noqa: E402, F401
from app.modules.reporting import query_models as reporting_query_models  # noqa: E402, F401
from app.modules.ad_management import models as ad_management_models  # noqa: E402, F401
from app.modules.campaign_copies import models as campaign_copy_models  # noqa: E402, F401
from app.modules.automation_rules import models as automation_rules_models  # noqa: E402, F401
from app.core.config import settings  # noqa

target_metadata = SQLModel.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_url():
    return str(settings.DATABASE_URL)


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_url()
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section)
    assert configuration is not None
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # 分区子表由报表分区管理器维护，ORM 只映射父表；不能自动生成删除子表的迁移。
        report_partitions = set(
            connection.execute(
                text("""
            SELECT child.relname FROM pg_inherits i
            JOIN pg_class child ON child.oid=i.inhrelid
            JOIN pg_class parent ON parent.oid=i.inhparent
            WHERE parent.relname IN (
                'ad_report_day','creative_report_day',
                'entity_metric_day','business_day'
            )
        """)
            ).scalars()
        )
        connection.commit()

        def include_name(name, type_, parent_names):
            # 0116 刻意保全旧 CID，ORM 已退休该字段；后续专项删列前不生成删除操作。
            if (
                parent_names.get("table_name") == "ad_material_reference"
                and (type_, name)
                in {
                    ("column", "creative_ids"),
                    ("check_constraint", "ck_ad_material_creatives"),
                }
            ):
                return False
            return type_ != "table" or name not in report_partitions

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_name=include_name,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
