"""冻结源的初始化不积累每账户运行时锁；日常维护仍保留并发围栏。"""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting.read_models.evidence_sql import evidence_statements
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def _advisory_count(connection):
    return connection.execute(
        text("""SELECT count(*) FROM pg_locks
            WHERE pid=pg_backend_pid() AND locktype='advisory'""")
    ).scalar_one()


def _members(connection, tenant, advertiser):
    return connection.execute(
        text("""SELECT v.id,v.refs,v.naming_versions FROM account_membership_current h
            JOIN account_membership_version v ON v.id=h.version_id
            WHERE h.tenant_id=:t AND h.advertiser_id=:a"""),
        {"t": tenant, "a": advertiser},
    ).one()


def _names(connection, tenant, advertiser):
    return connection.execute(
        text("""SELECT v.id,v.refs,v.material_uses,v.naming_versions
            FROM report_name_scope_current h JOIN report_evidence_version v ON v.id=h.version_id
            WHERE h.tenant_id=:t AND h.advertiser_id=:a"""),
        {"t": tenant, "a": advertiser},
    ).one()


def test_membership_bootstraps_preserve_payload_without_runtime_locks(monkeypatch):
    with historical_database(monkeypatch, "0102_business_rate_periods") as (engine, _):
        with Session(engine) as session, session.begin():
            seeds = [
                sync_seed.__wrapped__(session, historical=True),
                sync_seed.__wrapped__(session, historical=True),
            ]
            for offset, seed in enumerate(seeds):
                session.execute(
                    text("""INSERT INTO advertiser_account
                        (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
                        SELECT :t,'empty-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,32)i"""),
                    {"t": seed.tenant_id},
                )
                session.execute(
                    text("""INSERT INTO ad_object
                        (tenant_id,advertiser_id,kind,remote_id,ad_type,name,configuration,observed_at,published_version)
                        SELECT :t,'acc-1',kind,rid,'REGULAR',rid,'{}',now(),1
                        FROM (VALUES ('campaign','c-z'),('campaign','c-a'),('ad','ignore-ad')) s(kind,rid)"""),
                    {"t": seed.tenant_id},
                )
                # 名称投影可以保留已删除系列；账户成员只取真实系列，名称范围保留全部current。
                session.execute(
                    text("""INSERT INTO campaign_name_projection
                        (tenant_id,advertiser_id,campaign_remote_id,name_revision,raw_name,status,
                         parser_revision,grouping_revision,observed_at)
                        VALUES(:t,'acc-1','c-z',:revision,'named','INVALID',1,1,now()),
                              (:t,'acc-1','orphan-name',11,'deleted','INVALID',1,1,now()),
                              (:t,'acc-2','orphan-empty',13,'deleted','INVALID',1,1,now())"""),
                    {"t": seed.tenant_id, "revision": 7 + offset * 10},
                )

        migration = importlib.import_module(
            "app.alembic.versions.0103_account_report_reads"
        )
        with engine.connect() as connection, connection.begin():
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(connection))
            )
            before = _advisory_count(connection)
            migration.upgrade()
            assert _advisory_count(connection) == before
            # 与0106固定迁移使用同一模板，不提前加载并行worker尚未生成的素材bootstrap。
            for statement in evidence_statements():
                connection.execute(text(statement))
            assert _advisory_count(connection) == before

            for offset, seed in enumerate(seeds):
                member = _members(connection, seed.tenant_id, "acc-1")
                assert member.refs == [
                    {
                        "tenant_id": str(seed.tenant_id),
                        "advertiser_id": "acc-1",
                        "kind": "campaign",
                        "remote_id": remote,
                    }
                    for remote in ("c-a", "c-z")
                ]
                assert member.naming_versions == {"acc-1:c-z": 7 + offset * 10}
                names = _names(connection, seed.tenant_id, "acc-1")
                assert names.naming_versions == {
                    "acc-1:c-z": 7 + offset * 10,
                    "acc-1:orphan-name": 11,
                }
                assert names.refs == names.material_uses == []
                empty_member = _members(connection, seed.tenant_id, "acc-2")
                assert empty_member.refs == [] and empty_member.naming_versions == {}
                assert _names(connection, seed.tenant_id, "acc-2").naming_versions == {
                    "acc-2:orphan-empty": 13
                }
                for advertiser in ("empty-1", "empty-32"):
                    empty_member = _members(connection, seed.tenant_id, advertiser)
                    assert (
                        empty_member.refs == [] and empty_member.naming_versions == {}
                    )
                    assert (
                        _names(connection, seed.tenant_id, advertiser).naming_versions
                        == {}
                    )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM account_membership_current")
                ).scalar_one()
                == 68
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM report_name_scope_current")
                ).scalar_one()
                == 68
            )

            # bootstrap绕过的是调用，不是删掉锁函数或残留关闭开关。真实改名仍锁定并发布新版本。
            tenant = seeds[0].tenant_id
            old_member = _members(connection, tenant, "acc-1")
            old_names = _names(connection, tenant, "acc-1")
            connection.execute(
                text("""UPDATE campaign_name_projection SET name_revision=8
                    WHERE tenant_id=:t AND advertiser_id='acc-1' AND campaign_remote_id='c-z'"""),
                {"t": tenant},
            )
            assert _advisory_count(connection) == before + 2
            assert _members(connection, tenant, "acc-1").id != old_member.id
            assert _names(connection, tenant, "acc-1").id != old_names.id
            assert _members(connection, tenant, "acc-1").naming_versions == {
                "acc-1:c-z": 8
            }
            assert _names(connection, tenant, "acc-1").naming_versions == {
                "acc-1:c-z": 8,
                "acc-1:orphan-name": 11,
            }
