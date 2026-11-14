"""在应用镜像内执行；输出只进入发布私有目录，不打印凭据。"""

import base64
import json
import sys
import time


def credentials():
    """只统计可解密的加密凭据数；候选迁移后用，避免再次对全部表计数。"""
    return {
        "credentials_decrypted": fingerprint(count_rows=False)["credentials_decrypted"]
    }


def fingerprint(count_rows=True):
    from app.core.config import settings
    from app.core.credentials import decrypt_credentials
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import NullPool

    engine = create_engine(str(settings.DATABASE_URL), poolclass=NullPool)
    with engine.connect() as connection:
        inspector = inspect(connection)
        names = inspector.get_table_names(schema="public")
        quote = engine.dialect.identifier_preparer.quote
        rows = (
            {
                n: connection.execute(
                    text("SELECT count(*) FROM public." + quote(n))
                ).scalar_one()
                for n in names
            }
            if count_rows
            else {}
        )
        decrypted = 0
        for name in names:
            columns = {c["name"] for c in inspector.get_columns(name, schema="public")}
            if {"tenant_id", "credential_ciphertext"} <= columns:
                for row in connection.execute(
                    text(
                        "SELECT tenant_id, credential_ciphertext FROM public."
                        + quote(name)
                        + " WHERE credential_ciphertext IS NOT NULL"
                    )
                ):
                    if row.credential_ciphertext:
                        decrypt_credentials(
                            tenant_id=row.tenant_id,
                            ciphertext=row.credential_ciphertext,
                        )
                        decrypted += 1
        return {
            "migration_heads": sorted(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            ),
            "table_rows": rows,
            "credentials_decrypted": decrypted,
        }


# 候选代码审阅过的历史 migration head。未知 head 不靠缺表或字符串大小猜版本。
LEGACY_CREATIVE_HEADS = frozenset(
    """
0003_provider_links
0004_materials
0004b_material_checks
0004c_object_claims
0005_strategies
0005b_build_drafts
0005c_scene_facts
0005d_build_previews
0005e_account_capabilities
0005f_draft_mutations
0006_build_execution
0006a_unit_dispatch
0007_directory_revision
0008_scene_jobs
0009_execution_dispatch_indexes
0010_submission_recovery
0011_recovery_evidence_index
0012_material_covers
0013_dispatch_expansion_index
0014_recovery_candidates
0015_username_auth
0016_provider_session_refresh
0017_preview_naming
0018_scene_budget_evidence
0019_scene_budget_page
0020_draft_delivery_mutation
0021_report_run_retry_count
0022_provider_link_defaults
0023_reporting_store
0024_drop_legacy_reporting
0025_drop_campaign_day_utc
0026_automation_rules
0027_automation_execution
0028_automation_summary
0029_material_post_link
0030_actor_user_reference
0031_cancel_stacked_dir_runs
0032_drop_management_capability
0033_management_rename_values
0034_directory_publish_version
0035_report_dispatch_cursor
0036_drop_rule_safety_settings
0037_realtime_sync
0038_sync_work_indexes
0039_sync_service_class
0040_material_production
0041_rule_frequency
0042_drop_ad_material_main
0043_material_video_index
0044_sync_probe_indexes
0045_directory_type_index
0046_image_materials
0047_image_account_assets
0048_image_reissue_routes
0049_image_composition
0050_image_composition_runs
0051_image_state_scope
0052_image_candidate_build
0053_carousel_music
0054_music_preparation
0055_carousel_presets
0056_carousel_preset_copy
0057_carousel_copy_integrity
0058_selected_music_retry
0059_video_creative_units
0062_directory_pending_index
0064_closeout_job_scope
0066_directory_status_turn
02a_tenants
02b_connections
02c_directory
1a31ce608336
20261004_general_strategy
20261005_build_delivery_config
20261005_merge_delivery_heads
20261005_merge_preview_heads
20261005_preview_ad_material
20261005_preview_copy_unique
9c0a54914c78
ad_management
ad_management_cancelled
ad_management_execution
ad_management_integrity
ad_management_task_item_audit
ad_same_group_reissue
ads_reporting_data
audience_targeting
automatic_mini_targets
build_batching
cover_claim_reissue
cover_rejected_reissue
d98dd8ec85a3
draft_identity_selection
draft_scene_phase
e2412789c190
fe56fa70289e
manual_promotion_links
mat_response_archive
material_approved_reissue
material_batch_candidate_index
material_bc_seed
material_cover_sharing
material_primary_account
material_push
material_route_scope
material_seed_generations
material_share_receipts
material_source_bc
material_target_scope
mcp01
mcp02
mcp_build_recovery
mcp_build_routes
mcp_capability_routes
mcp_cover_evidence
mcp_directory_bc_scope
mcp_directory_semantics
mcp_draft_connection
mcp_material_routes
mcp_multi_bc
mcp_multibc_runtime
mcp_stage_directory
outbox_payload_compaction
p01_dispatch
preview_display_drama_id
preview_skipped_materials
provider_display_drama_id
provider_kinds_expansion
r2_ingest_chunks
r2_part_receipts
r2_transient_ingest
reporting_account_history_start
reporting_queries
tenant_material_library
""".split()
)
V2_CREATIVE_HEADS = {
    "0060_frozen_creative_units",
    "0061_carousel_ad_readiness",
    "0063_merge_sync_carousel",
    "0065_merge_closeout_carousel",
    "0067_merge_directory_turn",
}


def creative_unit_drain(connection=None):
    """旧镜像内也可运行的只读 SQL 门禁；不导入新 ORM，不更改任务或队列。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    if connection is None:
        from app.core.config import settings

        engine = create_engine(str(settings.DATABASE_URL), poolclass=NullPool)
        try:
            with engine.connect() as conn:
                return creative_unit_drain(conn)
        finally:
            engine.dispose()
    heads = list(
        connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
    )
    if len(heads) != 1:
        raise RuntimeError("Unknown creative contract schema; review release plan")
    if heads[0] not in LEGACY_CREATIVE_HEADS | V2_CREATIVE_HEADS:
        from pathlib import Path

        import app
        from alembic.script import ScriptDirectory

        graph = ScriptDirectory(str(Path(app.__file__).resolve().parent / "alembic"))
        known = {
            revision.revision
            for revision in graph.iterate_revisions("heads", "0067_merge_directory_turn")
        }
        if heads[0] not in known:
            raise RuntimeError("Unknown creative contract schema; review release plan")
        # 已知后继仍可能保存V1冻结任务，继续检查其排空事实，不能按版本号直接放行。
    legacy = heads[0] in LEGACY_CREATIVE_HEADS
    # 0060 已给每个旧预览写入显式 descriptor；缺失不是 V1，必须单独阻断。
    join = (
        ""
        if legacy
        else " LEFT JOIN preview_contract c"
        " ON c.tenant_id=p.tenant_id AND c.preview_id=p.id"
    )
    predicate = "true" if legacy else "(c.version=1 OR c.preview_id IS NULL)"
    # 旧镜像加载自身已发布模型并复用业务闭环谓词，不依赖候选ORM或新表。
    # 原步骤和回执保持原样；已验证补建/明确创建回执的旧READBACK本来就不再执行。
    from app import models as _models  # noqa: F401
    from app.modules.builds.receipt_completion import obsolete_readback_sql

    outstanding = "NOT " + obsolete_readback_sql("e")
    if connection.execute(
        text("SELECT to_regclass('build_verified_replacement') IS NOT NULL")
    ).scalar_one():
        from app.modules.builds.corrections import resolved_sql

        outstanding += " AND NOT (" + resolved_sql("e") + ")"
    blockers = {}
    queries = {
        "preview_building": (
            f"SELECT p.id FROM build_preview p{join} WHERE {predicate}"
            " AND p.status='BUILDING'"
        ),
        "submission_runnable": (
            "SELECT s.id FROM build_submission s JOIN build_preview p"
            f" ON p.tenant_id=s.tenant_id AND p.id=s.preview_id{join}"
            f" WHERE {predicate} AND (NOT s.expanded"
            " OR s.status NOT IN ('COMPLETED','PARTIAL','FAILED'))"
        ),
        "step_runnable_or_unknown": (
            "SELECT e.id FROM execution_step e JOIN build_preview p"
            f" ON p.tenant_id=e.tenant_id AND p.id=e.preview_id{join}"
            f" WHERE {predicate} AND e.status IN"
            " ('QUEUED','PENDING','RUNNING','RETRYABLE','UNKNOWN')"
            f" AND {outstanding}"
        ),
    }
    if not legacy:
        queries["missing_descriptor"] = (
            f"SELECT p.id FROM build_preview p{join} WHERE c.preview_id IS NULL"
        )
    # 每类仅取一个阻断样本，避免读取整批正文、凭据或无界历史结果。
    for kind, query in queries.items():
        value = connection.execute(text(query + " LIMIT 1")).scalar_one_or_none()
        if value is not None:
            blockers[kind] = str(value)
    return {"migration_heads": heads, "drained": not blockers, "blockers": blockers}


def redis_logical(client, key, kind):
    """按内容规范化，避免不同 Redis 版本的 DUMP 编码造成假失败。"""

    def enc(value):
        return base64.b64encode(value).decode()

    if kind == "string":
        value = client.get(key)
        return enc(value) if value is not None else None
    if kind == "list":
        return [enc(v) for v in client.lrange(key, 0, -1)]
    if kind == "set":
        return sorted(enc(v) for v in client.smembers(key))
    if kind == "hash":
        return sorted([enc(k), enc(v)] for k, v in client.hgetall(key).items())
    if kind == "zset":
        return [
            [enc(v), score.hex()]
            for v, score in client.zrange(key, 0, -1, withscores=True)
        ]
    raise RuntimeError("Unsupported Redis key type: " + kind)


def redis_export():
    import redis
    from app.core.config import settings

    client = redis.Redis.from_url(settings.REDIS_URL)
    if client.connection_pool.connection_kwargs.get("db") != 15:
        raise RuntimeError("Source Redis must be DB 15")
    records = []

    def enc(value):
        return base64.b64encode(value).decode()

    # 应用已停写；只读取本逻辑库，不调用共享实例 SAVE/FLUSH。
    for key in sorted(set(client.scan_iter())):
        kind, value, ttl = (
            client.pipeline(transaction=True).type(key).dump(key).pttl(key).execute()
        )
        if value is None or ttl == -2:
            continue
        expires = int(time.time() * 1000) + ttl if ttl >= 0 else None
        kind = kind.decode()
        logical = redis_logical(client, key, kind)
        # 冻结窗口无写入，但短期键仍可自然过期。
        if not client.exists(key):
            continue
        records.append(
            {
                "key": enc(key),
                "dump": enc(value),
                "expires_at_ms": expires,
                "type": kind,
                "logical": logical,
            }
        )
    return records


def migrate_targeted_messages(
    client,
    *,
    token,
    manifest_sha256,
    priority_steps=(0, 3, 6, 9),
    sep="\x06\x16",
    key_prefix="",
):
    """冻结且已验证备份后迁移；WATCH 防并发，队列与幂等凭据在同一事务提交。"""
    import hashlib
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", token) or not re.fullmatch(
        r"[a-f0-9]{64}", manifest_sha256
    ):
        raise RuntimeError("Invalid broker migration identity")
    source, target = "ad-management", "ads-live-directory"
    receipt_key = key_prefix + "tkada:release:targeted-queue:" + token
    pairs = [
        (
            key_prefix + source + (sep + str(p) if p else ""),
            key_prefix + target + (sep + str(p) if p else ""),
        )
        for p in priority_steps
    ]
    keys = [key for pair in pairs for key in pair]
    unacked = [key_prefix + "unacked", key_prefix + "unacked_index"]

    def checksum(items):
        # 长度前缀消除不同消息边界产生相同拼接值的歧义。
        digest = hashlib.sha256()
        for item in items:
            digest.update(len(item).to_bytes(8, "big"))
            digest.update(item)
        return digest.hexdigest()

    def verify_receipt(receipt):
        readback = client.pipeline(transaction=True)
        for queue in receipt["queues"]:
            readback.lrange(queue["source"], 0, -1)
            readback.lrange(queue["target"], 0, -1)
        values = readback.execute()
        for index, queue in enumerate(receipt["queues"]):
            for offset, side in enumerate(("source", "target")):
                observed = values[index * 2 + offset]
                if (
                    len(observed) != queue[side + "_after"]
                    or checksum(observed) != queue[side + "_after_sha256"]
                ):
                    raise RuntimeError(
                        "Broker migration readback mismatch; keep consumers frozen"
                    )
        return dict(receipt, verified=True)

    with client.pipeline() as pipe:
        pipe.watch(receipt_key, *keys, *unacked)
        previous = pipe.get(receipt_key)
        if previous:
            receipt = json.loads(previous)
            if receipt["manifest_sha256"] != manifest_sha256:
                raise RuntimeError("Broker migration backup identity changed")
            return verify_receipt(receipt)
        if pipe.hlen(unacked[0]) or pipe.zcard(unacked[1]):
            raise RuntimeError("Broker deliveries must be fully acknowledged")
        # Kombu 优先级键必须来自当前 transport 合同，不能遗漏自定义优先级的积压。
        known = {k.encode() for k in keys}
        for queue in (source, target):
            discovered = set(client.scan_iter(match=key_prefix + queue + sep + "*"))
            if not discovered <= known:
                raise RuntimeError("Unexpected Kombu priority queue keys")
        updates, receipt = (
            [],
            {
                "token": token,
                "manifest_sha256": manifest_sha256,
                "moved": 0,
                "queues": [],
            },
        )
        for old_key, new_key in pairs:
            for key in (old_key, new_key):
                if pipe.type(key) not in (b"none", b"list") or pipe.pttl(key) >= 0:
                    raise RuntimeError("Broker queue type or expiry is unexpected")
            before, existing = pipe.lrange(old_key, 0, -1), pipe.lrange(new_key, 0, -1)
            retained, moved = [], []
            for raw in before:
                message = json.loads(raw)
                if message.get("headers", {}).get("task") != "ads.targeted_step":
                    retained.append(raw)
                    continue
                # body、ID、其余 header 原值保留；重试/拒绝重排必须继续回到新读取队列。
                delivery = message.get("properties", {}).get("delivery_info", {})
                if (
                    delivery.get("routing_key") != source
                    or delivery.get("exchange") != ""
                ):
                    raise RuntimeError("Targeted message routing does not match source")
                delivery["routing_key"] = target
                headers = message["headers"]
                if "routing_key" in headers:
                    if headers["routing_key"] != source:
                        raise RuntimeError(
                            "Targeted header routing does not match source"
                        )
                    headers["routing_key"] = target
                moved.append(
                    json.dumps(
                        message, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                )
            # Redis LPUSH/BRPOP：已有目标消息先消费，迁入消息内部与源队列原顺序相同。
            after = moved + existing
            receipt["moved"] += len(moved)
            receipt["queues"].append(
                {
                    "source": old_key,
                    "target": new_key,
                    "moved": len(moved),
                    "source_before": len(before),
                    "source_after": len(retained),
                    "target_before": len(existing),
                    "target_after": len(after),
                    "source_before_sha256": checksum(before),
                    "source_after_sha256": checksum(retained),
                    "target_before_sha256": checksum(existing),
                    "target_after_sha256": checksum(after),
                }
            )
            if moved:
                updates.append((old_key, retained, new_key, after))
        pipe.multi()
        for old_key, retained, new_key, after in updates:
            pipe.delete(old_key, new_key)
            if retained:
                pipe.rpush(old_key, *retained)
            pipe.rpush(new_key, *after)
        pipe.set(receipt_key, json.dumps(receipt))
        pipe.execute()
    # 原子凭据也写回调用方私有目录；断流后同 token 重试不会重复搬运。
    return verify_receipt(receipt)


def migrate_targeted(expected):
    import redis
    from app.core.config import settings
    from app.jobs.celery_app import celery_app

    client = redis.Redis.from_url(settings.REDIS_URL)
    if client.connection_pool.connection_kwargs.get("db") != 15:
        raise RuntimeError("Source Redis must be DB 15")
    options = celery_app.conf.broker_transport_options or {}
    if (
        options.get("unacked_key", "unacked") != "unacked"
        or options.get("unacked_index_key", "unacked_index") != "unacked_index"
    ):
        raise RuntimeError("Custom unacked keys require release review")
    return migrate_targeted_messages(
        client,
        **expected,
        priority_steps=options.get("priority_steps", (0, 3, 6, 9)),
        sep=options.get("sep", "\x06\x16"),
        key_prefix=options.get("global_keyprefix", ""),
    )


def worker_control(action, expected):
    from app.jobs.celery_app import celery_app

    inspect = celery_app.control.inspect(destination=expected, timeout=5)
    queues = inspect.active_queues() or {}
    if set(queues) != set(expected):
        raise RuntimeError("Not all expected workers replied")
    if action == "cancel":
        for worker, subscriptions in queues.items():
            for queue in subscriptions:
                replies = celery_app.control.cancel_consumer(
                    queue["name"], destination=[worker], reply=True, timeout=5
                )
                if not any(worker in r and "ok" in r[worker] for r in replies):
                    raise RuntimeError("Consumer cancellation was not acknowledged")
    result = {}
    for kind in ("active", "reserved", "scheduled", "active_queues"):
        result[kind] = getattr(inspect, kind)() or {}
    return result


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "fingerprint":
        result = fingerprint()
    elif action == "schema":
        result = fingerprint(count_rows=False)
    elif action == "creative-unit-drain":
        result = creative_unit_drain()
    elif action == "credentials":
        result = credentials()
    elif action == "redis":
        # 积压时 Redis 可达数十 MB，经 exec 标准输出回传会断流；先写入容器文件，
        # 再由发布端带重试与校验和复制，标准输出只返回条数。
        records = redis_export()
        with open(json.loads(sys.argv[2]), "w") as stream:
            json.dump(records, stream)
        result = {"records": len(records)}
    elif action == "migrate-targeted":
        result = migrate_targeted(json.loads(sys.argv[2]))
    elif action == "broker":
        import redis
        from app.core.config import settings

        client = redis.Redis.from_url(settings.REDIS_URL)
        result = {"unacked": client.hlen("unacked") + client.zcard("unacked_index")}
    else:
        result = worker_control(action, json.loads(sys.argv[2]))
    print(json.dumps(result))
