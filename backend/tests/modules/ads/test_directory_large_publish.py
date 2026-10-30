"""真实 PostgreSQL 大目录发布：保持全量原子可见，记录数据库与Python成本。"""

import json
import os
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import event, func, text
from sqlmodel import select

from app.integrations.tiktok.contracts.ads import (
    AdMaterialUsage,
    CallEvidence,
    DirectoryPage,
    EntityRef,
    MaterialUseRef,
)
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from tests.modules.ads.test_directory_sync import _authority, _page, _run


def test_large_full_directory_publish_preserves_every_object_and_material(
    session, directory_seed, record_property
):
    _authority(session, directory_seed)
    trace_path = os.environ.get("TK_ADA_PUBLISH_TRACE")
    if trace_path:
        Path(trace_path).write_text("")
    record_property(
        "schema_revision",
        session.connection()
        .execute(text("SELECT version_num FROM alembic_version"))
        .scalar_one(),
    )
    background_rows = int(os.environ.get("TK_ADA_PUBLISH_BACKGROUND_ROWS", "0"))
    if background_rows:
        background_started = monotonic()
        _seed_material_background(session, directory_seed, background_rows)
        record_property("background_rows", background_rows)
        record_property("background_seed_seconds", monotonic() - background_started)
    object_count = int(os.environ.get("TK_ADA_PUBLISH_OBJECTS", "12000"))
    material_count = int(os.environ.get("TK_ADA_PUBLISH_MATERIALS", "40000"))
    shared_video = os.environ.get("TK_ADA_PUBLISH_SHARED_VIDEO") == "1"
    record_property(
        "dataset",
        {
            "objects": object_count,
            "materials": material_count,
            "shared_video": shared_video,
        },
    )
    template = _page(directory_seed).items[0]
    # 一个真实大账户会由广告页带出大量创意和素材引用；不能只测千条广告。
    items = tuple(
        replace(
            template,
            ref=EntityRef(
                directory_seed.context.tenant_id, "account-a", "ad", f"large-{i}"
            ),
            configuration={"creative": "x" * 1024},
        )
        for i in range(object_count)
    )
    materials = tuple(
        AdMaterialUsage(
            use_ref=MaterialUseRef(
                items[i % len(items)].ref,
                "shared-video" if shared_video else f"video-{i}",
                str(i),
                "VIDEO",
            ),
            local_material_id=None,
            operation_status="ENABLE",
            complete=True,
            name=f"material-{i}",
            creative_ids=(f"creative-{i}",),
        )
        for i in range(material_count)
    )
    run = _run(session, directory_seed, kind="ad")
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage(items, materials, None, True, CallEvidence()),
        claim_generation=1,
    )
    stats = {
        "sql_count": 0,
        "sql_seconds": 0.0,
        "material_return_rows": 0,
        "wide_object_reads": 0,
        "statements": {},
    }
    connection = session.connection()

    def before(_conn, _cursor, _sql, _params, context, _many):
        context.test_started = monotonic()

    def after(_conn, cursor, sql, _params, context, _many):
        stats["sql_count"] += 1
        elapsed = monotonic() - context.test_started
        stats["sql_seconds"] += elapsed
        key = " ".join(sql.split())[:100]
        entry = stats["statements"].setdefault(key, [0, 0.0])
        entry[0] += 1
        entry[1] += elapsed
        if trace_path:
            with Path(trace_path).open("a") as trace:
                trace.write(json.dumps({"statement": key, "seconds": elapsed}) + "\n")
        if "INSERT INTO ad_material_reference" in sql and "RETURNING" in sql:
            stats["material_return_rows"] += cursor.rowcount
        if sql.startswith("SELECT") and "ad_object.configuration" in sql:
            stats["wide_object_reads"] += 1

    event.listen(connection, "before_cursor_execute", before)
    event.listen(connection, "after_cursor_execute", after)
    first_plan = os.environ.get("TK_ADA_PUBLISH_FIRST_PLAN")
    if first_plan:
        record_property("first_plan_instrumented", True)
    # 失败路径也保留原连接已缓存的首次计划；此诊断模式的时间不作SLO验收。
    with _capture_plans(session, first_plan) if first_plan else nullcontext():
        started = monotonic()
        try:
            version = publish_directory(session, run_id=run.id, claim_generation=1)
        finally:
            event.remove(connection, "before_cursor_execute", before)
            event.remove(connection, "after_cursor_execute", after)
        stats["total_seconds"] = monotonic() - started
    assert run.status == "COMPLETE"
    assert (
        session.exec(
            select(func.count())
            .select_from(AdObject)
            .where(
                AdObject.tenant_id == run.tenant_id,
                AdObject.published_version == version,
                AdObject.remote_id.startswith("large-"),
            )
        ).one()
        == object_count
    )
    assert (
        session.exec(
            select(func.count())
            .select_from(AdMaterialReference)
            .where(
                AdMaterialReference.tenant_id == run.tenant_id,
                AdMaterialReference.published_version == version,
            )
        ).one()
        == material_count
    )
    record_property("large_directory_publish", deepcopy(stats))
    assert stats["total_seconds"] < 45

    # 第二次走冲突更新和所有现有索引维护，不能只以首次插入证明大账户能发布。
    loaded = session.exec(
        select(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == run.tenant_id,
            AdMaterialReference.published_version == version,
        )
        .limit(1)
    ).one()
    repeat = _run(session, directory_seed, kind="ad")
    stage_directory_page(
        session,
        run_id=repeat.id,
        page=DirectoryPage(items, materials, None, True, CallEvidence()),
        claim_generation=1,
    )
    stats["statements"] = {}
    event.listen(connection, "before_cursor_execute", before)
    event.listen(connection, "after_cursor_execute", after)
    started = monotonic()
    try:
        repeated_version = publish_directory(
            session, run_id=repeat.id, claim_generation=1
        )
    finally:
        event.remove(connection, "before_cursor_execute", before)
        event.remove(connection, "after_cursor_execute", after)
    repeat_seconds = monotonic() - started
    record_property("large_directory_repeat_statements", stats["statements"])
    assert repeat.status == "COMPLETE"
    assert loaded.published_version == repeated_version
    assert (
        session.exec(
            select(func.count())
            .select_from(AdMaterialReference)
            .where(
                AdMaterialReference.tenant_id == repeat.tenant_id,
                AdMaterialReference.published_version == repeated_version,
            )
        ).one()
        == material_count
    )
    record_property("large_directory_repeat_seconds", repeat_seconds)
    assert (
        session.connection()
        .execute(
            text(
                "SELECT count(*) FROM current_ad_material WHERE tenant_id=:tenant AND published_version=:version"
            ),
            {"tenant": run.tenant_id, "version": repeated_version},
        )
        .scalar_one()
        == material_count
    )
    if background_rows:
        for table in ("ad_material_reference", "current_ad_material"):
            assert (
                session.connection()
                .execute(
                    text(f"""
                SELECT count(*) FROM {table} WHERE tenant_id=:tenant AND advertiser_id='account-a'
                    AND ad_remote_id LIKE 'background-ad-%'
            """),
                    {"tenant": run.tenant_id},
                )
                .scalar_one()
                == background_rows
            )
    plan_path = os.environ.get("TK_ADA_PUBLISH_PLANS")
    if plan_path:
        plan_rows = int(os.environ.get("TK_ADA_PUBLISH_PLAN_ROWS", "500"))
        _capture_cached_publish_plans(
            session,
            repeat,
            items[:plan_rows],
            materials[:plan_rows],
            repeated_version,
            plan_path,
        )
    assert repeat_seconds < 45
    # 发布后的父链与系列名审计不需要再次传输上万条完整配置。
    assert stats["wide_object_reads"] == 0


def _background_progress(**values):
    path = os.environ.get("TK_ADA_PUBLISH_TRACE")
    if path:
        with Path(path).open("a") as output:
            output.write(json.dumps({"monotonic": monotonic(), **values}) + "\n")


def _seed_material_background(session, seed, rows):
    """夹具从canonical写入真实无关身份；统计仅在准备阶段更新，发布不碰共享ANALYZE。"""
    _background_progress(stage="background_seed", event="start", rows=rows)
    started = monotonic()
    version = (
        session.connection()
        .execute(text("SELECT nextval('ad_directory_published_version_seq')"))
        .scalar_one()
    )
    session.connection().execute(
        text("""
        INSERT INTO ad_material_reference (
            id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,
            material_type,name,creative_ids,local_material_id,operation_status,complete,published_version)
        SELECT md5(CAST(:tenant AS text)||':background:'||i)::uuid,:tenant,'account-a',
            'background-ad-'||i,'background-video-'||i,'background-material-'||i,
            'VIDEO','unrelated background '||i,'[]'::jsonb,NULL,'ENABLE',true,:version
        FROM generate_series(1,:rows) i
    """),
        {"tenant": seed.context.tenant_id, "rows": rows, "version": version},
    )
    _background_progress(
        stage="background_insert",
        event="complete",
        rows=rows,
        seconds=monotonic() - started,
    )
    for table in ("ad_material_reference", "current_ad_material"):
        analyzed = monotonic()
        session.connection().execute(text(f"ANALYZE {table}"))
        _background_progress(
            stage="background_analyze", table=table, seconds=monotonic() - analyzed
        )
        count = (
            session.connection()
            .execute(
                text(f"""
            SELECT count(*) FROM {table} WHERE tenant_id=:tenant AND advertiser_id='account-a'
                AND ad_remote_id LIKE 'background-ad-%'
        """),
                {"tenant": seed.context.tenant_id},
            )
            .scalar_one()
        )
        assert count == rows


@contextmanager
def _capture_plans(session, path):
    raw = session.connection().connection.driver_connection
    Path(path).write_text("")

    def notice(message):
        with Path(path).open("a") as output:
            output.write(message.message_primary + "\n")

    raw.add_notice_handler(notice)
    try:
        session.connection().execute(text("LOAD 'auto_explain'"))
        for setting in (
            "SET LOCAL auto_explain.log_min_duration=1",
            "SET LOCAL auto_explain.log_analyze=on",
            "SET LOCAL auto_explain.log_buffers=on",
            "SET LOCAL auto_explain.log_nested_statements=on",
            "SET LOCAL auto_explain.log_format=json",
            "SET LOCAL client_min_messages=log",
        ):
            session.connection().execute(text(setting))
        yield
    finally:
        raw.remove_notice_handler(notice)


def _capture_cached_publish_plans(session, run, entities, materials, version, path):
    """计时完成后在回滚savepoint内重放指定规模，抓实际PL/pgSQL缓存计划。"""
    from app.modules.ads.sync import (
        _entity_json,
        _material_json,
        _publish_entities,
        _publish_materials,
    )

    savepoint = session.begin_nested()
    try:
        with _capture_plans(session, path):
            _publish_entities(
                session,
                [_entity_json(entity) for entity in entities],
                run=run,
                version=version,
                fallback=entities[0].observed_at,
            )
            _publish_materials(
                session,
                [_material_json(material) for material in materials],
                run=run,
                version=version,
            )
    finally:
        savepoint.rollback()


def test_object_publish_refreshes_loaded_configuration_after_narrow_read(
    session, directory_seed
):
    from datetime import timedelta

    from app.modules.ads.sync import _entity_json, _publish_entities

    current = directory_seed.row
    original_version = current.published_version
    observed = current.observed_at + timedelta(seconds=1)
    incoming = replace(
        _page(directory_seed).items[0],
        ref=current.ref,
        observed_at=observed,
        name="更新后的广告",
        configuration={"creative": "新的配置"},
    )
    run = _run(session, directory_seed, kind="ad")
    published = _publish_entities(
        session,
        [_entity_json(incoming)],
        run=run,
        version=original_version + 1,
        fallback=observed,
    )
    assert published == [current]
    assert current.name == incoming.name
    assert current.configuration == incoming.configuration
    assert current.observed_at == observed
    assert current.published_version == original_version + 1


@pytest.mark.parametrize("expire_identity", [False, True])
def test_material_publish_refreshes_loaded_and_partly_expired_identity(
    session, directory_seed, expire_identity
):
    from app.modules.ads.sync import _publish_materials

    run = _run(session, directory_seed, kind="ad")
    row = AdMaterialReference(
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
        ad_remote_id="ad-loaded",
        platform_material_id="video-loaded",
        ad_material_id="non-null-id",
        material_type="VIDEO",
        name="before",
        complete=True,
        published_version=1,
    )
    session.add(row)
    session.flush()
    if expire_identity:
        session.expire(row, ["ad_material_id"])
    _publish_materials(
        session,
        [
            {
                "use_ref": {
                    "ad_ref": {
                        "tenant_id": str(run.tenant_id),
                        "advertiser_id": run.advertiser_id,
                        "kind": "ad",
                        "remote_id": "ad-loaded",
                    },
                    "platform_material_id": "video-loaded",
                    "ad_material_id": "non-null-id",
                    "material_type": "VIDEO",
                },
                "name": "after",
                "complete": True,
                "creative_ids": ["creative-new"],
            }
        ],
        run=run,
        version=2,
    )
    assert row.name == "after"
    assert row.published_version == 2
    assert row.ad_material_id == "non-null-id"
    assert row.creative_ids == ["creative-new"]


def test_multi_page_publish_passes_only_bounded_entity_batches(
    session, directory_seed, monkeypatch
):
    from app.modules.ads import sync

    _authority(session, directory_seed)
    run = _run(session, directory_seed)
    template = _page(directory_seed).items[0]
    for number in range(1, 5):
        stage_directory_page(
            session,
            run_id=run.id,
            claim_generation=1,
            page=DirectoryPage(
                tuple(
                    replace(
                        template,
                        ref=replace(template.ref, remote_id=f"batch-{number}-{i}"),
                        configuration={"wide": "x" * 4096},
                    )
                    for i in range(400)
                ),
                (),
                number + 1 if number < 4 else None,
                number == 4,
                CallEvidence(),
                page=number,
            ),
        )
    original = sync._entity_values
    validated = written = 0
    sizes = []
    connection = session.connection()

    def staged(_connection, _cursor, _statement, parameters, _context, _many):
        nonlocal written
        if isinstance(parameters, dict) and isinstance(parameters.get("rows"), str):
            size = len(json.loads(parameters["rows"]))
            sizes.append(size)
            written += size

    def normalize(data, **kwargs):
        nonlocal validated
        validated += 1
        if validated == 1001:
            assert written >= 500, (
                "directory publisher accumulated all page bodies before staging"
            )
        return original(data, **kwargs)

    monkeypatch.setattr(sync, "_entity_values", normalize)
    event.listen(connection, "after_cursor_execute", staged)
    try:
        publish_directory(session, run_id=run.id, claim_generation=1)
    finally:
        event.remove(connection, "after_cursor_execute", staged)
    assert validated == 1600
    assert max(sizes) <= 500


def test_set_based_directory_publish_expires_preloaded_wide_configuration(
    session, directory_seed
):
    from datetime import timedelta

    _authority(session, directory_seed)
    current = directory_seed.row
    incoming = replace(
        _page(directory_seed).items[0],
        ref=current.ref,
        observed_at=current.observed_at + timedelta(seconds=1),
        name="set based updated",
        configuration={"creative": "new complete configuration"},
    )
    run = _run(session, directory_seed, kind=current.kind)
    stage_directory_page(
        session,
        run_id=run.id,
        claim_generation=1,
        page=replace(_page(directory_seed), items=(incoming,)),
    )
    version = publish_directory(session, run_id=run.id, claim_generation=1)
    assert current.name == incoming.name
    assert current.configuration == incoming.configuration
    assert current.observed_at == incoming.observed_at
    assert current.published_version == version


def test_multi_page_materials_publish_one_complete_set_statement(
    session, directory_seed
):
    _authority(session, directory_seed)
    run = _run(session, directory_seed, kind="ad")
    owner = replace(
        _page(directory_seed).items[0],
        ref=EntityRef(directory_seed.context.tenant_id, "account-a", "ad", "one-owner"),
    )
    for number in range(1, 5):
        materials = tuple(
            AdMaterialUsage(
                MaterialUseRef(owner.ref, f"video-{number}-{i}", None, "VIDEO"),
                None,
                "ENABLE",
                True,
                "name",
                (),
            )
            for i in range(400)
        )
        stage_directory_page(
            session,
            run_id=run.id,
            claim_generation=1,
            page=DirectoryPage(
                (owner,),
                materials,
                number + 1 if number < 4 else None,
                number == 4,
                CallEvidence(),
                page=number,
            ),
        )
    writes = []
    connection = session.connection()

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO ad_material_reference"):
            writes.append(statement)

    event.listen(connection, "after_cursor_execute", record)
    try:
        version = publish_directory(session, run_id=run.id, claim_generation=1)
    finally:
        event.remove(connection, "after_cursor_execute", record)
    assert len(writes) == 1, (
        "each batch retriggers a full current-material candidate rebuild"
    )
    assert (
        session.exec(
            select(func.count())
            .select_from(AdMaterialReference)
            .where(
                AdMaterialReference.tenant_id == run.tenant_id,
                AdMaterialReference.published_version == version,
            )
        ).one()
        == 1600
    )


def test_material_stage_deduplicates_nullable_identity_across_batches_and_keeps_types(
    session, directory_seed
):
    _authority(session, directory_seed)
    run = _run(session, directory_seed, kind="ad")
    owner = replace(
        _page(directory_seed).items[0],
        ref=EntityRef(directory_seed.context.tenant_id, "account-a", "ad", "dup-owner"),
    )

    def usage(video, *, complete=True, name="filler", kind="VIDEO"):
        return AdMaterialUsage(
            MaterialUseRef(owner.ref, video, None, kind),
            None,
            "ENABLE",
            complete,
            name,
            (),
        )

    pages = (
        (
            usage("shared", complete=False, name="partial-first"),
            *(usage(f"filler-{i}") for i in range(600)),
        ),
        (
            usage("shared", name="complete-final"),
            usage("shared", complete=False, name="partial-later"),
            usage("shared", name="image-kept", kind="IMAGE"),
        ),
    )
    for number, materials in enumerate(pages, 1):
        stage_directory_page(
            session,
            run_id=run.id,
            claim_generation=1,
            page=DirectoryPage(
                (owner,),
                materials,
                2 if number == 1 else None,
                number == 2,
                CallEvidence(),
                page=number,
            ),
        )
    publish_directory(session, run_id=run.id, claim_generation=1)
    rows = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == run.tenant_id,
            AdMaterialReference.platform_material_id == "shared",
        )
    ).all()
    assert {
        (row.material_type, row.name, row.complete, row.ad_material_id) for row in rows
    } == {("VIDEO", "complete-final", True, None), ("IMAGE", "image-kept", True, None)}


def test_material_projection_local_windows_keep_history_ties_and_type_boundaries(
    session, directory_seed
):
    from app.modules.reporting.read_models.models import CurrentAdMaterial

    tenant = directory_seed.context.tenant_id

    def reference(inner, version, *, complete=True, kind="VIDEO"):
        return AdMaterialReference(
            tenant_id=tenant,
            advertiser_id="account-a",
            ad_remote_id="choice-owner",
            platform_material_id="shared-video",
            ad_material_id=inner,
            material_type=kind,
            complete=complete,
            published_version=version,
        )

    fallback = reference(None, 5)
    older = reference("old-precise", 1)
    first, second = reference("tie-a", 2), reference("tie-b", 2)
    partial = reference("partial-newer", 99, complete=False)
    image = reference("image", 10, kind="IMAGE")
    session.add_all([fallback, older, first, second, partial, image])
    session.flush()

    def projected():
        return set(
            session.exec(
                select(
                    CurrentAdMaterial.ad_material_id,
                    CurrentAdMaterial.material_type,
                    CurrentAdMaterial.published_version,
                ).where(
                    CurrentAdMaterial.tenant_id == tenant,
                    CurrentAdMaterial.ad_remote_id == "choice-owner",
                )
            ).all()
        )

    assert projected() == {
        ("tie-a", "VIDEO", 2),
        ("tie-b", "VIDEO", 2),
        ("image", "IMAGE", 10),
    }
    session.delete(first)
    session.delete(second)
    session.flush()
    assert projected() == {("old-precise", "VIDEO", 1), ("image", "IMAGE", 10)}
    older.complete = False
    session.flush()
    assert projected() == {(None, "VIDEO", 5), ("image", "IMAGE", 10)}
    session.delete(fallback)
    session.flush()
    assert projected() == {("image", "IMAGE", 10)}
    image.material_type = "VIDEO"
    session.flush()
    assert projected() == {("image", "VIDEO", 10)}
