"""真实 PostgreSQL 保留 UNKNOWN 证据，并约束一次授权的替代链。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from app.modules.builds.execution_models import Submission
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.migration_database import historical_database, insert_historical_row
from tests.modules.builds.test_route_migration import historical_rows
from tests.modules.materials.test_tenant_materials import mapping, material

TABLES = ("material_asset_operation", "material_distribution", "material_cover_job")


def _history(db):
    context, preview, _ = historical_rows(db, connections=0)
    submission = Submission(
        tenant_id=context.tenant_id,
        bc_id=preview.bc_id,
        preview_id=preview.id,
        draft_id=preview.draft_id,
        actor_id=context.actor_id,
        ordinal=1,
    )
    insert_historical_row(db, submission)
    file = material(db, context, "old.mp4", bc=preview.bc_id, historical=True)
    asset = mapping(db, context, file, historical=True)
    scope = {
        "tenant_id": context.tenant_id,
        "bc_id": file.bc_id,
        "material_id": file.id,
        "advertiser_id": asset.advertiser_id,
    }
    operation = MaterialAssetOperation(
        **scope,
        path="upload_original",
        status="result_unknown",
        request_digest="a" * 64,
        remote_response={"request_id": "original-unknown", "body": "preserved"},
    )
    distribution = MaterialDistribution(
        **scope,
        actor_id=context.actor_id,
        operation_id=operation.id,
        path="upload_original",
        status="result_unknown",
    )
    cover = MaterialCoverJob(
        **scope,
        actor_id=context.actor_id,
        asset_id=asset.id,
        connection_id=asset.connection_id,
        video_id=asset.video_id,
        remote_name="old-cover",
        status="UNKNOWN",
        request_armed_at=datetime.now(UTC),
    )
    rows = {}
    for row in (operation, distribution, cover):
        table = Table(row.__tablename__, MetaData(), autoload_with=db.connection())
        values = {
            k: v for k, v in row.model_dump().items() if k in table.c and v is not None
        }
        db.execute(table.insert().values(values))
        rows[row.__tablename__] = values
    db.flush()
    return {
        "tenant_id": context.tenant_id,
        "bc_id": file.bc_id,
        "actor_id": context.actor_id,
        "submission_id": submission.id,
    }, rows


def test_reissue_migration_preserves_unknown_history(monkeypatch):
    with historical_database(monkeypatch, "material_route_scope") as (engine, config):
        with Session(engine) as db, db.begin():
            _, rows = _history(db)
        with engine.connect() as db:
            before = {
                table: db.execute(
                    text(f"SELECT to_jsonb(t) FROM {table} t WHERE id=:id"),
                    {"id": rows[table]["id"]},
                ).scalar_one()
                for table in TABLES
            }
        command.upgrade(config, "material_approved_reissue")
        with engine.connect() as db:
            for table in TABLES:
                current = db.execute(
                    text(f"SELECT to_jsonb(t) FROM {table} t WHERE id=:id"),
                    {"id": rows[table]["id"]},
                ).scalar_one()
                assert "superseded_by_id" in current
                assert current.pop("superseded_by_id") is None
                assert current == before[table]


@pytest.fixture
def database(monkeypatch):
    with historical_database(monkeypatch, "material_route_scope") as (engine, config):
        with Session(engine) as db, db.begin():
            scope, rows = _history(db)
        command.upgrade(config, "material_approved_reissue")
        yield engine, config, scope, rows


def _replacement(db, scope, rows, kind, *, authorized=True):
    tables = {name: Table(name, MetaData(), autoload_with=db) for name in TABLES}
    replacements = {}
    selected = TABLES[:2] if kind == "VIDEO" else TABLES[2:]
    for name in selected:
        table, old = tables[name], rows[name]
        new = {**old, "id": uuid4()}
        if name == "material_asset_operation":
            new.update(status="pending", remote_response={})
        elif name == "material_distribution":
            new.update(status="queued", operation_id=replacements[TABLES[0]]["id"])
        else:
            new.update(status="PENDING", request_armed_at=None)
        # 新 ID 预分配，旧指针先 flush 才腾出未解决身份的唯一槽。
        db.execute(
            table.update()
            .where(table.c.id == old["id"])
            .values(superseded_by_id=new["id"])
        )
        db.execute(table.insert().values(new))
        replacements[name] = new
    authorization = dict(
        **scope,
        id=uuid4(),
        request_id=uuid4(),
        kind=kind,
        scope_digest="b" * 64,
        details={"accepted": "duplicate material"},
        accepted_duplicate_materials=True,
        created_at=datetime.now(UTC),
    )
    prefix = "distribution" if kind == "VIDEO" else "cover_job"
    name = "material_" + prefix
    authorization.update(
        {
            f"old_{prefix}_id": rows[name]["id"],
            f"new_{prefix}_id": replacements[name]["id"],
        }
    )
    if authorized:
        table = Table("material_reissue_authorization", MetaData(), autoload_with=db)
        db.execute(table.insert().values(authorization))
    return authorization, replacements


@pytest.mark.parametrize("kind", ["VIDEO", "COVER"])
def test_authorized_replacement_keeps_unknown_and_receipts_and_is_immutable(
    database, kind
):
    engine, config, scope, rows = database
    with engine.begin() as db:
        authorization, replacements = _replacement(db, scope, rows, kind)
    with engine.connect() as db:
        for name, replacement in replacements.items():
            value = db.execute(
                text(f"SELECT to_jsonb(t) FROM {name} t WHERE id=:id"),
                {"id": rows[name]["id"]},
            ).scalar_one()
            assert value["status"] == rows[name]["status"]
            assert value["superseded_by_id"] == str(replacement["id"])
            if name == "material_asset_operation":
                assert value["remote_response"] == {
                    "request_id": "original-unknown",
                    "body": "preserved",
                }
    for statement in (
        "UPDATE material_reissue_authorization SET details='{}'::jsonb WHERE id=:id",
        "DELETE FROM material_reissue_authorization WHERE id=:id",
    ):
        with (
            pytest.raises(DBAPIError, match="immutable material reissue"),
            engine.begin() as db,
        ):
            db.execute(text(statement), {"id": authorization["id"]})
    for name in replacements:
        with (
            pytest.raises(DBAPIError, match="immutable material replacement"),
            engine.begin() as db,
        ):
            db.execute(
                text(f"UPDATE {name} SET superseded_by_id=NULL WHERE id=:id"),
                {"id": rows[name]["id"]},
            )
    with pytest.raises(RuntimeError, match="material reissue evidence"):
        command.downgrade(config, "material_route_scope")
    with engine.connect() as db:
        assert (
            db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "material_approved_reissue"
        )


@pytest.mark.parametrize("kind", ["VIDEO", "COVER"])
def test_unauthorized_replacement_is_rejected_at_commit_and_rolled_back(database, kind):
    engine, _, scope, rows = database
    with (
        pytest.raises(DBAPIError, match="material reissue authorization"),
        engine.begin() as db,
    ):
        _replacement(db, scope, rows, kind, authorized=False)
    with engine.connect() as db:
        for name in TABLES:
            assert db.execute(text(f"SELECT count(*) FROM {name}")).scalar_one() == 1
            assert (
                db.execute(text(f"SELECT superseded_by_id FROM {name}")).scalar_one()
                is None
            )


@pytest.mark.parametrize("kind", ["VIDEO", "COVER"])
def test_authorization_cannot_cross_bc_or_point_to_unrelated_generation(database, kind):
    engine, _, scope, rows = database
    with (
        pytest.raises(DBAPIError, match="material reissue scope"),
        engine.begin() as db,
    ):
        authorization, _ = _replacement(db, scope, rows, kind, authorized=False)
        # 已存在的另一个 BC 可通过普通租户外键，但不能充当原提交的授权范围。
        db.execute(
            text(
                "INSERT INTO tenant_bc (tenant_id,bc_id,name,ownership_conflict) VALUES (:tenant_id,'other-bc','',false)"
            ),
            scope,
        )
        authorization["bc_id"] = "other-bc"
        table = Table("material_reissue_authorization", MetaData(), autoload_with=db)
        db.execute(table.insert().values(authorization))


def test_unused_migration_can_downgrade_without_changing_history(database):
    engine, config, _, rows = database
    command.downgrade(config, "material_route_scope")
    with engine.connect() as db:
        for name in TABLES:
            value = db.execute(
                text(f"SELECT to_jsonb(t) FROM {name} t WHERE id=:id"),
                {"id": rows[name]["id"]},
            ).scalar_one()
            assert "superseded_by_id" not in value
            assert value["status"] == rows[name]["status"]


@pytest.mark.parametrize("kind", ["VIDEO", "COVER"])
def test_request_and_old_identity_cannot_be_consumed_twice(database, kind):
    engine, _, scope, rows = database
    with engine.begin() as db:
        authorization, _ = _replacement(db, scope, rows, kind)
    for changed in ({"id": uuid4()}, {"id": uuid4(), "request_id": uuid4()}):
        with pytest.raises(DBAPIError, match="unique constraint"), engine.begin() as db:
            table = Table(
                "material_reissue_authorization", MetaData(), autoload_with=db
            )
            db.execute(table.insert().values({**authorization, **changed}))


@pytest.mark.parametrize("name", TABLES)
def test_replacement_pointer_cannot_reference_itself(database, name):
    engine, _, _, rows = database
    with pytest.raises(DBAPIError, match="check constraint"), engine.begin() as db:
        db.execute(
            text(f"UPDATE {name} SET superseded_by_id=id WHERE id=:id"),
            {"id": rows[name]["id"]},
        )


def test_cover_replacement_cannot_change_actual_video_scope(database):
    engine, _, scope, rows = database
    with (
        pytest.raises(DBAPIError, match="material reissue scope"),
        engine.begin() as db,
    ):
        authorization, replacements = _replacement(
            db, scope, rows, "COVER", authorized=False
        )
        db.execute(
            text(
                "UPDATE material_cover_job SET video_id='different-video' WHERE id=:id"
            ),
            {"id": replacements["material_cover_job"]["id"]},
        )
        table = Table("material_reissue_authorization", MetaData(), autoload_with=db)
        db.execute(table.insert().values(authorization))


@pytest.mark.parametrize("kind", ["VIDEO", "COVER"])
def test_authorization_requires_acceptance_and_same_tenant(database, kind):
    engine, _, scope, rows = database
    with (
        pytest.raises(DBAPIError, match="ck_material_reissue_accepted"),
        engine.begin() as db,
    ):
        authorization, _ = _replacement(db, scope, rows, kind, authorized=False)
        table = Table("material_reissue_authorization", MetaData(), autoload_with=db)
        db.execute(
            table.insert().values(
                {**authorization, "accepted_duplicate_materials": False}
            )
        )
    with (
        pytest.raises(DBAPIError, match="foreign key constraint"),
        engine.begin() as db,
    ):
        authorization, _ = _replacement(db, scope, rows, kind, authorized=False)
        table = Table("material_reissue_authorization", MetaData(), autoload_with=db)
        db.execute(table.insert().values({**authorization, "tenant_id": uuid4()}))


@pytest.mark.parametrize("name", TABLES)
def test_supersession_cannot_fabricate_unknown_status_in_same_update(database, name):
    engine, _, _, rows = database
    with engine.begin() as db:
        status = (
            "BLOCKED"
            if name == "material_cover_job"
            else "failed"
            if name == "material_asset_operation"
            else "blocked"
        )
        db.execute(
            text(f"UPDATE {name} SET status=:status WHERE id=:id"),
            {"id": rows[name]["id"], "status": status},
        )
        with pytest.raises(DBAPIError, match="UNKNOWN evidence"):
            db.execute(
                text(
                    f"UPDATE {name} SET status=:status, superseded_by_id=:replacement WHERE id=:id"
                ),
                {
                    "id": rows[name]["id"],
                    "status": rows[name]["status"],
                    "replacement": uuid4(),
                },
            )
