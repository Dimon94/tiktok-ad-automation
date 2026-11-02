"""页内素材映射必须完整包含历史帖子，同时不加载无关身份。"""

import json
from datetime import UTC, datetime

from app.modules.reporting.store_models import MaterialPostLink
from tests.modules.reporting.test_material_query import D, material_row, publish


def test_page_identity_is_bounded_and_keeps_historical_posts(session, sync_seed):
    from app.modules.reporting.read_models.material_identity import (
        hydrate_material_identities,
    )

    posts = ["old-post", "new-post", "other-post", "ambiguous-post", "wrong-type"]
    publish(session, sync_seed, "acc-1", [material_row(p, "5") for p in posts])
    for post, video, ad, kind in [
        ("old-post", "wanted", "deleted-ad", "VIDEO_SPARK_ADS"),
        ("new-post", "wanted", "current-ad", "VIDEO_SPARK_ADS"),
        ("other-post", "unrelated", "other-ad", "VIDEO_SPARK_ADS"),
        ("ambiguous-post", "wanted", "ambiguous-1", "VIDEO_SPARK_ADS"),
        ("ambiguous-post", "aaa", "ambiguous-2", "VIDEO_SPARK_ADS"),
        ("wrong-type", "wanted", "wrong-ad", "CAROUSEL_SPARK_ADS"),
    ]:
        session.add(
            MaterialPostLink(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id=post,
                main_material_type=kind,
                ad_remote_id=ad,
                platform_material_id=video,
                observed_at=datetime.now(UTC),
            )
        )
    session.flush()
    mapping = hydrate_material_identities(
        session,
        tenant_id=sync_seed.tenant_id,
        advertisers=["acc-1"],
        row_keys=[json.dumps(["material", "wanted"])],
        start=D,
        end=D,
    )
    assert mapping == {
        ("acc-1", post, "VIDEO_SPARK_ADS"): "wanted"
        for post in ["old-post", "new-post"]
    }


def test_page_identity_never_crosses_authorized_accounts(session, sync_seed):
    from app.modules.reporting.read_models.material_identity import (
        hydrate_material_identities,
    )

    for advertiser in ["acc-1", "acc-2"]:
        publish(session, sync_seed, advertiser, [material_row("same-post", "7")])
        session.add(
            MaterialPostLink(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=advertiser,
                main_material_id="same-post",
                main_material_type="VIDEO_SPARK_ADS",
                ad_remote_id="ad",
                platform_material_id="video",
                observed_at=datetime.now(UTC),
            )
        )
    session.flush()
    result = hydrate_material_identities(
        session,
        tenant_id=sync_seed.tenant_id,
        advertisers=["acc-1"],
        row_keys=[json.dumps(["material", "video"])],
        start=D,
        end=D,
    )
    assert set(result) == {("acc-1", "same-post", "VIDEO_SPARK_ADS")}


def test_campaign_query_does_not_read_wide_directory_or_name_history(report_case):
    from sqlalchemy import event

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
    assert not any(
        "from ad_object" in sql or "from campaign_name_projection" in sql
        for sql in statements
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
        SnapshotMember(json.dumps(["material", f"video-{i}"]), (), ())
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
