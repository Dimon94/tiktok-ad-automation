"""真实 PG 验证大页保全按批往返，且批次不改变同 CID 的观察顺序。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from math import ceil
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session

from app.modules.ads.creatives import upsert_creatives
from app.modules.ads.models import AdObject
from scripts.preserve_legacy_creative_identities import preserve_creative_identities
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def _reference(
    db, tenant, ids, *, parent="parent", video="video", mid="mid", kind="VIDEO"
):
    db.execute(
        text("""INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,
         material_type,name,creative_ids,complete,published_version)
        VALUES (:id,:tenant,'acc-1',:parent,:video,:mid,:kind,'legacy',CAST(:ids AS jsonb),true,1)
    """),
        {
            "id": uuid4(),
            "tenant": tenant,
            "ids": json.dumps(ids),
            "parent": parent,
            "video": video,
            "mid": mid,
            "kind": kind,
        },
    )


@contextmanager
def _statements(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("count", [128, 1024])
def test_large_single_parent_page_has_bounded_identity_reads_and_writes(
    monkeypatch, count, record_property
):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db, historical=True).tenant_id
            ids = [f"creative-{i:04}" for i in range(count)]
            _reference(db, tenant, ids)
        with Session(engine) as db, db.begin(), _statements(engine) as statements:
            started = perf_counter()
            result = preserve_creative_identities(
                db, tenant_id=tenant, advertiser_id="acc-1"
            )
        reads = [
            sql for sql in statements if sql.startswith("SELECT * FROM ad_creative")
        ]
        writes = [
            sql
            for sql in statements
            if sql.startswith(("INSERT INTO ad_creative", "UPDATE ad_creative"))
        ]
        record_property("identity_reads", len(reads))
        record_property("identity_writes", len(writes))
        record_property("sql_roundtrips", len(statements))
        record_property("elapsed_seconds", perf_counter() - started)
        assert len(reads) == 1, (count, len(reads), len(statements))
        assert len(writes) <= ceil(count / 500), (count, len(writes))
        assert result["done"] and result["written"] == count
        with Session(engine) as db, db.begin():
            records = (
                db.execute(
                    text("SELECT * FROM ad_creative WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                )
                .mappings()
                .all()
            )
            assert len(records) == count
            assert all(
                row["identity_status"] == "RESOLVED"
                and row["platform_material_id"] == "video"
                for row in records
            )
            assert all(
                len(row["evidence"]["legacy_identities"]) == 1 for row in records
            )
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )


@pytest.mark.parametrize("kind,assets", [("VIDEO", ["video"]), ("IMAGE", ["b", "a"])])
def test_missing_identity_writes_once_with_exact_legacy_evidence(
    monkeypatch, kind, assets
):
    with historical_database(monkeypatch, "0115_copy_current_source") as (engine, _):
        with Session(engine) as db, db.begin():
            db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            tenant = sync_seed.__wrapped__(db, historical=True).tenant_id
            for asset in assets:
                _reference(db, tenant, ["new"], video=asset, kind=kind)
            # 数据库行级触发器计数真实写入，防止一条 SQL 内再次改写同一身份。
            db.execute(
                text("CREATE TABLE prefill_write_audit (operation text NOT NULL)")
            )
            db.execute(
                text("""CREATE FUNCTION audit_prefill_write() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                  INSERT INTO prefill_write_audit VALUES (TG_OP); RETURN NEW;
                END $$""")
            )
            db.execute(
                text("""CREATE TRIGGER audit_prefill_write
                AFTER INSERT OR UPDATE ON ad_creative FOR EACH ROW
                EXECUTE FUNCTION audit_prefill_write()""")
            )
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            row = dict(
                db.execute(text("SELECT * FROM ad_creative WHERE creative_id='new'"))
                .mappings()
                .one()
            )
            identity = {
                "ad_remote_id": "parent",
                "ad_material_id": "mid",
                "assets": [
                    {"material_type": kind, "platform_material_id": asset}
                    for asset in sorted(assets)
                ],
            }
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            assert row == {
                "tenant_id": tenant,
                "advertiser_id": "acc-1",
                "creative_id": "new",
                **identity,
                "material_type": "VIDEO" if kind == "VIDEO" else "CAROUSEL",
                "platform_material_id": "video" if kind == "VIDEO" else None,
                "name": "legacy",
                "operation_status": None,
                "deleted": False,
                "identity_status": "RESOLVED" if kind == "VIDEO" else "COMPOSITE",
                "first_observed_at": epoch,
                "observed_at": epoch,
                "published_version": 1,
                "evidence": {
                    "original_identity": identity,
                    "conflicts": [],
                    "asset_order_source": "CANONICAL_REFERENCE_SORT",
                    "legacy_identities": [
                        {
                            "identity": identity,
                            "published_version": 1,
                            "observed_at": "1970-01-01T00:00:00+00:00",
                            "asset_order_source": "CANONICAL_REFERENCE_SORT",
                        }
                    ],
                },
            }
            assert db.execute(
                text("SELECT operation FROM prefill_write_audit")
            ).scalars().all() == ["INSERT"]


def test_batch_enrichment_preserves_newer_state_and_every_conflict_proof(monkeypatch):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        now = datetime.now(UTC)
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db, historical=True).tenant_id
            ids = [f"creative-{i:03}" for i in range(129)]
            _reference(db, tenant, ids)
            rows = [
                {
                    "creative_id": cid,
                    "ad_remote_id": "parent",
                    "ad_material_id": None,
                    "name": "live",
                    "operation_status": "DISABLE",
                    "deleted": True,
                    "assets": []
                    if i % 3 == 0
                    else [
                        {
                            "material_type": "VIDEO",
                            "platform_material_id": "video" if i % 3 == 1 else "other",
                        }
                    ],
                }
                for i, cid in enumerate(ids)
            ]
            upsert_creatives(
                db,
                tenant_id=tenant,
                advertiser_id="acc-1",
                rows=rows,
                observed_at=now,
                published_version=9,
            )
        with Session(engine) as db, db.begin():
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            records = {
                r["creative_id"]: r
                for r in db.execute(
                    text("SELECT * FROM ad_creative WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                ).mappings()
            }
            for i, cid in enumerate(ids):
                row = records[cid]
                assert (
                    row["published_version"],
                    row["observed_at"],
                    row["name"],
                    row["operation_status"],
                    row["deleted"],
                ) == (9, now, "live", "DISABLE", True)
                if i % 3 == 2:
                    assert (
                        row["identity_status"] == "CONFLICT"
                        and row["platform_material_id"] == "other"
                    )
                    assert row["evidence"]["conflicts"][0]["published_version"] == 1
                    assert (
                        row["evidence"]["conflicts"][0]["identity"]["assets"][0][
                            "platform_material_id"
                        ]
                        == "video"
                    )
                else:
                    assert (
                        row["identity_status"] == "RESOLVED"
                        and row["platform_material_id"] == "video"
                    )
                    assert row["ad_material_id"] == "mid"
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )


@pytest.mark.parametrize("legacy_asset,conflict", [("a", False), ("c", True)])
def test_cache_publication_is_refreshed_before_legacy_classification(
    monkeypatch, legacy_asset, conflict
):
    with historical_database(monkeypatch, "0115_copy_current_source") as (engine, _):
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db, historical=True).tenant_id
            observed = datetime(2026, 11, 11, tzinfo=UTC)
            db.add(
                AdObject(
                    tenant_id=tenant,
                    advertiser_id="acc-1",
                    kind="ad",
                    remote_id="parent",
                    ad_type="SMART_PLUS",
                    name="cached",
                    operation_status="DISABLE",
                    observed_at=observed,
                    published_version=9,
                    configuration={
                        "creative_list": [
                            {
                                "smart_plus_creative_id": "cached",
                                "ad_material_id": "mid",
                                "material_operation_status": "DISABLE",
                                "creative_info": {
                                    "image_info": [{"image_id": "b"}, {"image_id": "a"}]
                                },
                            }
                        ]
                    },
                )
            )
            db.flush()
            _reference(db, tenant, ["cached"], video=legacy_asset, kind="IMAGE")
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            row = (
                db.execute(text("SELECT * FROM ad_creative WHERE creative_id='cached'"))
                .mappings()
                .one()
            )
            assert row["assets"] == [
                {"material_type": "IMAGE", "platform_material_id": "b"},
                {"material_type": "IMAGE", "platform_material_id": "a"},
            ]
            assert (
                row["published_version"],
                row["observed_at"],
                row["name"],
                row["operation_status"],
            ) == (9, observed, "cached", "DISABLE")
            if conflict:
                assert row["identity_status"] == "CONFLICT"
                assert (
                    row["evidence"]["conflicts"] == row["evidence"]["legacy_identities"]
                )
                proof = row["evidence"]["conflicts"][0]
                assert proof["published_version"] == 1
                assert proof["identity"]["assets"] == [
                    {"material_type": "IMAGE", "platform_material_id": "c"}
                ]
            else:
                assert row["identity_status"] == "COMPOSITE"
                assert row["evidence"]["conflicts"] == []
                assert "legacy_identities" not in row["evidence"]
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )


def test_multiple_observations_of_one_cid_keep_parent_order_and_all_proofs(monkeypatch):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db, historical=True).tenant_id
            for parent, mid in [("a", None), ("a", "one"), ("b", "two")]:
                _reference(db, tenant, ["shared", "second"], parent=parent, mid=mid)
        with Session(engine) as db, db.begin():
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            rows = (
                db.execute(
                    text("SELECT * FROM ad_creative WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                )
                .mappings()
                .all()
            )
            for row in rows:
                assert row["ad_remote_id"] == "a" and row["ad_material_id"] == "one"
                assert row["identity_status"] == "CONFLICT"
                assert (
                    row["evidence"]["conflicts"][0]["identity"]["ad_remote_id"] == "b"
                )
                assert [
                    proof["identity"]["ad_remote_id"]
                    for proof in row["evidence"]["legacy_identities"]
                ] == ["a", "b"]
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )
