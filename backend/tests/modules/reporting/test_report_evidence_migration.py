"""真实PG往返：紧凑证据保留旧身份、顺序、名称以及租户/BC快照边界。"""

import json
from uuid import uuid4

import pytest
from alembic import command
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotRow,
    read_snapshot,
)
from app.modules.reporting.snapshot_members import snapshot_members, snapshot_names
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed
from tests.modules.reporting.test_queries import filters_for


def _ref(tenant, advertiser, kind, remote):
    return {
        "tenant_id": str(tenant),
        "advertiser_id": advertiser,
        "kind": kind,
        "remote_id": remote,
    }


def _seed_snapshot(session, seed, bc, dimension, advertisers, rows, names):
    snapshot = QuerySnapshot(
        id=uuid4(),
        tenant_id=seed.tenant_id,
        bc_id=bc,
        actor_id=seed.actor_id,
        advertiser_ids=advertisers,
        filters=filters_for(dimension).model_dump(mode="json"),
        filter_digest="e" * 64,
        total=len(rows),
        naming_versions=names,
        summary={"migration": "frozen-summary"},
        coverage={"migration": "frozen-coverage"},
    )
    session.add(snapshot)
    session.flush()
    for sequence, (row_key, refs, uses) in enumerate(rows):
        version = None
        if dimension == "account":
            version = session.execute(
                text("""SELECT version_id FROM account_membership_current
                    WHERE tenant_id=:t AND advertiser_id=:a"""),
                {"t": seed.tenant_id, "a": advertisers[sequence]},
            ).scalar_one()
        # 显式旧列播种；不让今日ORM的新member字段渗入0105。
        session.execute(
            text("""INSERT INTO query_snapshot_row
                (snapshot_id,row_key,stable_sequence,dimension,display,refs,material_uses,
                 metric_buckets,capabilities,directory_versions,membership_digest,
                 account_membership_version_id)
                VALUES(:s,:key,:seq,:dim,'{}',CAST(:refs AS jsonb),CAST(:uses AS jsonb),
                    '[]','{}','{}','old-digest',:version)"""),
            {
                "s": snapshot.id,
                "key": row_key,
                "seq": sequence,
                "dim": dimension,
                "refs": json.dumps(refs),
                "uses": json.dumps(uses),
                "version": version,
            },
        )
    return snapshot.id


def _stored(connection):
    headers = connection.execute(
        text("SELECT id,to_jsonb(s) FROM query_snapshot s ORDER BY id")
    ).all()
    rows = connection.execute(
        text("""SELECT snapshot_id,row_key,to_jsonb(q) FROM query_snapshot_row q
            ORDER BY snapshot_id,stable_sequence""")
    ).all()
    return dict(headers), {(s, key): value for s, key, value in rows}


def test_report_evidence_migration_round_trip_preserves_scope_and_payload(monkeypatch):
    with historical_database(monkeypatch, "0105_entity_metric_days") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            first = sync_seed.__wrapped__(session)
            other = sync_seed.__wrapped__(session)
            cases = []
            # 两租户共享远端账户/系列/素材ID；同租户第二BC只授权acc-2。
            for seed, bc, advertisers in (
                (first, "bc-a", ["acc-1", "acc-2"]),
                (first, "bc-b", ["acc-2"]),
                (other, "bc-a", ["acc-1", "acc-2"]),
            ):
                tenant = seed.tenant_id
                adv = advertisers[0]
                for dimension in ("campaign", "adgroup", "ad"):
                    remote = "deleted:old-identity"
                    refs = [_ref(tenant, adv, dimension, remote)]
                    names = {"directory": 1}
                    if dimension == "campaign":
                        names[f"{adv}:{remote}"] = 7 if seed is first else 19
                    identity = _seed_snapshot(
                        session,
                        seed,
                        bc,
                        dimension,
                        advertisers,
                        [(f"{adv}:{dimension}:{remote}", refs, [])],
                        names,
                    )
                    cases.append((identity, seed, bc))
                # 剧顺序故意不按账户排序，紧凑二元组必须保留原数组序号。
                drama_refs = [
                    _ref(tenant, account, "campaign", "old-drama")
                    for account in reversed(advertisers)
                ]
                names = {
                    "directory": 1,
                    **{
                        f"{account}:old-drama": index + (11 if seed is first else 31)
                        for index, account in enumerate(advertisers)
                    },
                }
                identity = _seed_snapshot(
                    session,
                    seed,
                    bc,
                    "drama",
                    advertisers,
                    [
                        (f'{bc}:drama:["旧剧"]', drama_refs, []),
                        (f'{bc}:drama:["空剧"]', [], []),
                    ],
                    names,
                )
                cases.append((identity, seed, bc))
                refs = [
                    _ref(tenant, account, "ad", remote)
                    for account in advertisers
                    for remote in ("old-a", "old-b")
                ]
                uses = [
                    {
                        "ad_ref": ref,
                        "platform_material_id": "shared-vid",
                        "ad_material_id": None if index % 2 == 0 else "precise-old",
                        "material_type": "VIDEO",
                    }
                    for index, ref in enumerate(refs)
                ]
                identity = _seed_snapshot(
                    session,
                    seed,
                    bc,
                    "material",
                    advertisers,
                    [
                        ('["material", "shared-vid"]', refs, uses),
                        ('["material", "no-members"]', [], []),
                    ],
                    {"directory": 1},
                )
                cases.append((identity, seed, bc))
                identity = _seed_snapshot(
                    session,
                    seed,
                    bc,
                    "account",
                    advertisers,
                    [
                        (f"{account}:account:{account}", [], [])
                        for account in advertisers
                    ],
                    {"directory": 1},
                )
                cases.append((identity, seed, bc))
            # 零结果快照同样可逆，不能被当成缺失证据。
            _seed_snapshot(
                session, first, "bc-a", "campaign", ["acc-1"], [], {"directory": 1}
            )
        with engine.connect() as connection:
            before_headers, before_rows = _stored(connection)

        for _cycle in range(2):
            command.upgrade(config, "0106_report_snapshot_evidence")
            with engine.connect() as connection:
                after_headers, after_rows = _stored(connection)
                for (snapshot_id, key), old in before_rows.items():
                    compact = after_rows[snapshot_id, key]
                    assert compact["refs"] == []
                    assert compact["material_uses"] == []
                    dimension = old["dimension"]
                    if dimension == "account":
                        assert (
                            compact["account_membership_version_id"]
                            == old["account_membership_version_id"]
                        )
                    elif dimension == "drama":
                        assert compact["member_keys"] == [
                            [ref["advertiser_id"], ref["remote_id"]]
                            for ref in old["refs"]
                        ]
                        assert compact["member_count"] == len(old["refs"])
                    elif dimension == "material":
                        evidence = (
                            connection.execute(
                                text("""SELECT e.* FROM query_snapshot_evidence l
                                JOIN report_evidence_version e ON e.id=l.version_id
                                WHERE l.snapshot_id=:s AND l.row_key=:key ORDER BY e.advertiser_id COLLATE "C"
                            """),
                                {"s": snapshot_id, "key": key},
                            )
                            .mappings()
                            .all()
                        )
                        assert [ref for e in evidence for ref in e["refs"]] == old[
                            "refs"
                        ]
                        assert [
                            use for e in evidence for use in e["material_uses"]
                        ] == old["material_uses"]
                        assert all(
                            str(e["tenant_id"])
                            == before_headers[snapshot_id]["tenant_id"]
                            for e in evidence
                        )
                        assert all(
                            e["advertiser_id"]
                            in before_headers[snapshot_id]["advertiser_ids"]
                            for e in evidence
                        )
                        assert all(e["retired_at"] is not None for e in evidence)
                        assert compact["member_count"] == len(old["material_uses"])
                    else:
                        assert compact["member_count"] == 1
                for identity, old in before_headers.items():
                    if old["filters"]["dimension"] not in ("campaign", "drama"):
                        assert after_headers[identity] == old
                        continue
                    evidence = (
                        connection.execute(
                            text("""SELECT e.* FROM query_snapshot_name_version l
                            JOIN report_evidence_version e ON e.id=l.version_id WHERE l.snapshot_id=:s"""),
                            {"s": identity},
                        )
                        .mappings()
                        .all()
                    )
                    names = {"directory": 1}
                    for item in evidence:
                        assert str(item["tenant_id"]) == old["tenant_id"]
                        assert item["advertiser_id"] in old["advertiser_ids"]
                        assert item["retired_at"] is not None
                        names.update(item["naming_versions"])
                    assert names == old["naming_versions"]
                    current_ids = set(
                        connection.execute(
                            text("SELECT version_id FROM report_name_scope_current")
                        ).scalars()
                    )
                    assert not current_ids & {item["id"] for item in evidence}
            command.downgrade(config, "0105_entity_metric_days")
            with engine.connect() as connection:
                # 全部旧列逐值比较：数组顺序、冻结名称、展示/指标及权限字段均不能漂移。
                assert _stored(connection) == (before_headers, before_rows)

        # 上面独立核对0106历史结构；今日解析器在当前head运行，不能用新增列读取旧schema。
        command.upgrade(config, "head")
        with Session(engine) as session:
            for identity, seed, bc in cases:
                snapshot = session.get(QuerySnapshot, identity)
                if snapshot.filters["dimension"] == "material":
                    # 旧素材快照保留审计证据，但新查询合同必须拒绝重用旧补链身份。
                    with pytest.raises(HTTPException) as retired:
                        read_snapshot(
                            session,
                            context=seed.context,
                            bc_id=bc,
                            snapshot_id=identity,
                        )
                    assert retired.value.status_code == 409
                    assert retired.value.detail == "query_snapshot_contract_changed"
                else:
                    snapshot = read_snapshot(
                        session,
                        context=seed.context,
                        bc_id=bc,
                        snapshot_id=identity,
                    )
                assert snapshot.id == identity
                rows = session.exec(
                    select(QuerySnapshotRow).where(
                        QuerySnapshotRow.snapshot_id == identity
                    )
                ).all()
                if snapshot.filters["dimension"] == "material":
                    with pytest.raises(HTTPException) as retired_members:
                        snapshot_members(session, snapshot=snapshot, rows=rows)
                    assert retired_members.value.status_code == 409
                else:
                    # 迁移可逆还不够：真实分页/选择/导出解析器必须立即接受旧证据。
                    members = snapshot_members(session, snapshot=snapshot, rows=rows)
                    for key, member in members.items():
                        assert list(member.refs) == before_rows[identity, key]["refs"]
                        assert (
                            list(member.material_uses)
                            == before_rows[identity, key]["material_uses"]
                        )
                    assert (
                        snapshot_names(
                            session, snapshot=snapshot, rows=rows, members=members
                        )
                        == before_headers[identity]["naming_versions"]
                    )
                with pytest.raises(HTTPException) as denied_bc:
                    read_snapshot(
                        session,
                        context=seed.context,
                        bc_id="bc-b" if bc == "bc-a" else "bc-a",
                        snapshot_id=identity,
                    )
                assert denied_bc.value.status_code == 404
                wrong_tenant = other if seed is first else first
                with pytest.raises(HTTPException) as denied_tenant:
                    read_snapshot(
                        session,
                        context=wrong_tenant.context,
                        bc_id=bc,
                        snapshot_id=identity,
                    )
                assert denied_tenant.value.status_code == 404


def test_report_evidence_migration_rejects_mismatched_scalar_atomically(monkeypatch):
    with historical_database(monkeypatch, "0105_entity_metric_days") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session)
            _seed_snapshot(
                session,
                seed,
                "bc-a",
                "ad",
                ["acc-1"],
                [("acc-1:ad:old", [_ref(seed.tenant_id, "acc-2", "ad", "old")], [])],
                {"directory": 1},
            )
        with engine.connect() as connection:
            before = _stored(connection)
        # 不允许把异常旧身份静默改写成row_key身份；失败应回滚整次DDL和数据迁移。
        with pytest.raises(DBAPIError, match="scalar snapshot identity mismatch"):
            command.upgrade(config, "0106_report_snapshot_evidence")
        with engine.connect() as connection:
            assert _stored(connection) == before
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0105_entity_metric_days"
            )
            assert (
                connection.execute(
                    text("SELECT to_regclass('report_evidence_version')")
                ).scalar_one()
                is None
            )
