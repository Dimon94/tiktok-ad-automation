"""真实PG慢触发器：仅canonical素材写可延长，成功恢复、失败原子回滚。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ads.sync import (
    _material_stage,
    _publish_staged_materials,
    _stage_material_batch,
)
from tests.modules.reporting.sync_conftest import (
    committed_sync_seed as committed_sync_seed,
)


def staged_material(session, seed):
    staged = _material_stage(session)
    run = SimpleNamespace(
        tenant_id=seed.context.tenant_id, advertiser_id=seed.ref.advertiser_id
    )
    _stage_material_batch(
        session,
        staged,
        [
            {
                "use_ref": {
                    "ad_ref": {
                        "tenant_id": str(run.tenant_id),
                        "advertiser_id": run.advertiser_id,
                        "kind": "ad",
                        "remote_id": seed.ref.remote_id,
                    },
                    "platform_material_id": "deadline-video",
                    "material_type": "VIDEO",
                },
                "complete": True,
            }
        ],
        run=run,
        version=2,
    )
    session.execute(
        text("""
        CREATE FUNCTION pg_temp.slow_material_publish() RETURNS trigger LANGUAGE plpgsql
        AS $$ BEGIN PERFORM pg_sleep(0.2); RETURN NULL; END $$
    """)
    )
    session.execute(
        text("""
        CREATE TRIGGER test_slow_material_publish BEFORE INSERT ON ad_material_reference
        FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.slow_material_publish()
    """)
    )
    return run, staged


def test_only_canonical_material_write_uses_extended_budget(session, directory_seed):
    run, staged = staged_material(session, directory_seed)
    session.execute(text("SET LOCAL statement_timeout='100ms'"))
    _publish_staged_materials(session, run, staged, statement_timeout_ms=1000)
    assert session.execute(text("SHOW statement_timeout")).scalar_one() == "100ms"
    assert (
        session.execute(
            text("SELECT count(*) FROM ad_material_reference WHERE tenant_id=:tenant"),
            {"tenant": run.tenant_id},
        ).scalar_one()
        == 1
    )
    assert (
        session.execute(
            text("SELECT count(*) FROM current_ad_material WHERE tenant_id=:tenant"),
            {"tenant": run.tenant_id},
        ).scalar_one()
        == 1
    )
    # 下一条普通查询仍受原预算约束，扩展不泄漏到后续发布或池连接。
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        session.execute(text("SELECT pg_sleep(0.2)"))
    assert caught.value.orig.sqlstate == "57014"


def test_failed_material_write_keeps_original_error_and_rolls_back_setting(
    session, directory_seed
):
    run, staged = staged_material(session, directory_seed)
    session.execute(text("SET LOCAL statement_timeout='1s'"))
    with pytest.raises(DBAPIError) as caught, session.begin_nested():
        _publish_staged_materials(session, run, staged, statement_timeout_ms=100)
    assert caught.value.orig.sqlstate == "57014"
    assert session.execute(text("SHOW statement_timeout")).scalar_one() == "1s"
    assert (
        session.execute(
            text("SELECT count(*) FROM ad_material_reference WHERE tenant_id=:tenant"),
            {"tenant": run.tenant_id},
        ).scalar_one()
        == 0
    )
    assert (
        session.execute(
            text("SELECT count(*) FROM current_ad_material WHERE tenant_id=:tenant"),
            {"tenant": run.tenant_id},
        ).scalar_one()
        == 0
    )


def test_material_statement_extension_cannot_extend_total_transaction(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    material_seed = SimpleNamespace(
        context=seed.context,
        ref=EntityRef(seed.tenant_id, "acc-1", "ad", "deadline-ad"),
    )
    with pytest.raises(DBAPIError) as caught, factory() as db:
        run, staged = staged_material(db, material_seed)
        db.execute(text("SET LOCAL transaction_timeout='100ms'"))
        _publish_staged_materials(db, run, staged, statement_timeout_ms=1000)
    assert caught.value.orig.sqlstate == "25P04"
    with factory() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM ad_material_reference WHERE tenant_id=:tenant"
                ),
                {"tenant": seed.tenant_id},
            ).scalar_one()
            == 0
        )
