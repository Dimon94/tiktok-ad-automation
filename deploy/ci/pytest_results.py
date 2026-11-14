"""pytest 插件：逐条写出 nodeid、阶段、结果和耗时，供并行分组统计与重跑。"""

import json
import os

import pytest


def pytest_runtest_logreport(report):
    path = os.environ.get("CI_RESULTS_FILE")
    if not path:
        return
    record = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "duration": report.duration,
    }
    if report.failed:
        record["longrepr"] = report.longreprtext
    if report.user_properties:
        record["properties"] = report.user_properties
    with open(path, "a") as stream:
        stream.write(json.dumps(record) + "\n")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    yield
    if (
        os.environ.get("CI_ISOLATE_TEST_FILES") != "1"
        or nextitem is None
        or item.path == nextitem.path
    ):
        return
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url

    from app.core.config import settings
    from app.core.db import engine
    from tests.database import require_test_database
    from tests.database_template import _head_template, create_head_database

    source = make_url(str(settings.DATABASE_URL))
    require_test_database(str(settings.DATABASE_URL))
    # 多连接测试会真实提交；文件结束后复用干净模板，避免后续 Worker 处理前例积压。
    # 模板由既有 helper 独立迁移，不能复制当前已含测试数据的数据库。
    _head_template(source)
    engine.dispose()
    admin_url = source.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(source.database))
        )
    create_head_database(str(settings.DATABASE_URL), source.database)
