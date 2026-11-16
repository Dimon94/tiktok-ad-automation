"""页内素材映射必须完整包含历史帖子，同时不加载无关身份。"""

import json

from tests.modules.reporting.test_material_query import D, material_row, publish


def test_page_identity_is_bounded_and_keeps_historical_creatives(session, sync_seed):
    from sqlalchemy import text

    from app.modules.reporting.creative_query import read_facts
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.query_helpers import finish_projections
    from tests.modules.reporting.test_material_query import use

    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row(c, "5") for c in ("old", "new", "other")],
    )
    for c, video in [("old", "wanted"), ("new", "wanted"), ("other", "unrelated")]:
        use(session, sync_seed, adv="acc-1", ad=c, video=video, ad_material=c, post=c)
    session.execute(
        text(
            "DELETE FROM current_ad_material WHERE tenant_id=:t AND ad_remote_id='old'"
        ),
        {"t": sync_seed.tenant_id},
    )
    finish_projections(session)
    facts = read_facts(
        session,
        tenant_id=sync_seed.tenant_id,
        advertisers=["acc-1"],
        filters=ReportingFilter(dimension="material", start_date=D, end_date=D),
        row_keys=[json.dumps(["material", "VIDEO", "wanted"])],
    )
    assert {f["creative_id"] for f in facts} == {"old", "new"}


def test_page_identity_never_crosses_authorized_accounts(session, sync_seed):
    from app.modules.reporting.creative_query import read_facts
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.query_helpers import finish_projections
    from tests.modules.reporting.test_material_query import use

    for advertiser in ["acc-1", "acc-2"]:
        publish(session, sync_seed, advertiser, [material_row("same", "7")])
        use(
            session,
            sync_seed,
            adv=advertiser,
            ad="ad",
            video="video",
            ad_material="am",
            post="same",
        )
    finish_projections(session)
    facts = read_facts(
        session,
        tenant_id=sync_seed.tenant_id,
        advertisers=["acc-1"],
        filters=ReportingFilter(dimension="material", start_date=D, end_date=D),
        row_keys=[json.dumps(["material", "VIDEO", "video"])],
    )
    assert {f["advertiser_id"] for f in facts} == {"acc-1"}


def test_campaign_query_does_not_read_wide_directory_or_name_history(report_case):
    from sqlalchemy import event, text

    from app.modules.reporting.queries import query_ads
    from tests.modules.reporting.test_queries import filters_for

    case = report_case
    case.seed_campaign("Provider-Drama", "report-account", 4, 8)
    statements = []
    connection = case.session.connection()

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql.lower())

    event.listen(connection, "before_cursor_execute", capture)
    try:
        result = query_ads(
            case.session, context=case.context, bc_id=case.bc_id, filters=filters_for()
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert result.total == 1
    assert result.items[0].metric_buckets[0].values["spend"] == 4
    assert not any("from campaign_name_projection" in sql for sql in statements)
    from app.modules.reporting.row_set import _ctes

    ctes, params = _ctes(
        filters_for(),
        tenant_id=case.context.tenant_id,
        advertisers=["report-account"],
        bc_id=case.bc_id,
    )
    plan = connection.execute(
        text(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
            + ctes
            + " SELECT row_key FROM ordered"
        ),
        params,
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    directory_scans = [
        n
        for n in nodes(plan)
        if n.get("Actual Loops", 0)
        and n.get("Relation Name") in {"ad_object", "ad_entity_read"}
    ]
    assert all(
        (n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0))
        * n["Actual Loops"]
        <= 1
        for n in directory_scans
    )


def test_material_page_uses_video_bound_probe_under_cold_random_io(session, sync_seed):
    from sqlalchemy import event, text

    from app.modules.reporting.material_query import materialize_material_rows
    from app.modules.reporting.row_set import SnapshotMember
    from app.modules.reporting.schemas import ReportingFilter

    session.execute(
        text("""INSERT INTO current_ad_material
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type,name,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1','ad-'||i,'video-'||i,'VIDEO','name-'||i,true,1
        FROM generate_series(1,100000)i"""),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(
        text("""INSERT INTO current_ad_material
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type,name,complete,published_version)
        VALUES (gen_random_uuid(),:tenant,'acc-2','foreign','video-1','VIDEO','foreign',true,2),
          (gen_random_uuid(),:tenant,'acc-1','incomplete','video-1','VIDEO','incomplete',false,2),
          (gen_random_uuid(),:tenant,'acc-1','image','video-1','IMAGE','image',true,2)"""),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(text("ANALYZE current_ad_material"))
    # 冷随机I/O成本高时，仍应由当前页素材身份驱动；不能退为整个账户目录扫描。
    session.execute(text("SET LOCAL random_page_cost=100"))
    members = [
        SnapshotMember(
            json.dumps(["material", "VIDEO", f"video-{i}"]),
            (),
            (
                {
                    "ad_ref": {
                        "tenant_id": str(sync_seed.tenant_id),
                        "advertiser_id": "acc-1",
                        "kind": "ad",
                        "remote_id": f"ad-{i}",
                    },
                    "platform_material_id": f"video-{i}",
                    "ad_material_id": None,
                    "material_type": "VIDEO",
                },
            ),
        )
        for i in range(1, 51)
    ]
    statements = []
    connection = session.connection()

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "FROM current_ad_material" in statement:
            statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        rows = materialize_material_rows(
            session,
            tenant_id=sync_seed.tenant_id,
            filters=ReportingFilter(dimension="material", start_date=D, end_date=D),
            members=members,
            advertisers=["acc-1", *[f"account-{i}" for i in range(1343)]],
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(rows) == 50
    assert all(len(row.directory_versions) == 1 for row in rows)
    assert {row.display["name"] for row in rows} == {f"name-{i}" for i in range(1, 51)}
    assert len(statements) == 1
    statement, parameters = statements[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + statement, parameters
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    scanned = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        for node in nodes(plan)
        if node.get("Relation Name") == "current_ad_material"
    )
    assert scanned < 1000, json.dumps(plan)


def test_creative_page_pushes_row_key_into_fact_index(session, sync_seed):
    from sqlalchemy import event, text

    from app.modules.reporting.creative_query import read_facts
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("wanted", "5")])
    finish_projections(session)
    session.execute(
        text("""INSERT INTO creative_material_entity_day
        (tenant_id,advertiser_id,stat_date,fact_key,row_key,creative_id,campaign_id,adgroup_id,
         currency,timezone,fetched_at,source_revision,material_type,assets,attribution_status,source_status,source_info)
        SELECT :t,'acc-1',:d,'creative:spare-'||n,jsonb_build_array('unmapped_creative','acc-1','spare-'||n)::text,
            'spare-'||n,'c1','g1','USD','UTC',now(),1,'UNKNOWN','[]'::jsonb,'UNMAPPED','PARTIAL','{}'::jsonb
        FROM generate_series(1,10000)n"""),
        {"t": sync_seed.tenant_id, "d": D},
    )
    session.execute(text("ANALYZE creative_material_entity_day"))
    statements = []
    connection = session.connection()

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "WITH creative_selected" in statement:
            statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        rows = read_facts(
            session,
            tenant_id=sync_seed.tenant_id,
            advertisers=["acc-1"],
            filters=ReportingFilter(dimension="material", start_date=D, end_date=D),
            row_keys=[json.dumps(["unmapped_creative", "acc-1", "wanted"])],
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(rows) == 1
    statement, parameters = statements[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + statement, parameters
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    scanned = sum(
        (n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"]
        for n in nodes(plan)
        if n.get("Relation Name") == "creative_material_entity_day"
    )
    assert scanned < 100, json.dumps(plan)
