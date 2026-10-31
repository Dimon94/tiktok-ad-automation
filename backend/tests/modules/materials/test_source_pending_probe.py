"""来源已有当前待投递消息时，恢复探测不能随该来源历史长度增加。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import repair_material_dispatches
from app.modules.materials.models import MaterialAssetOperation
from tests.modules.materials.test_source_repair_backlog import _message
from tests.modules.materials.test_source_uploads import seed_operation
from tests.modules.materials.test_source_uploads import source_env as source_env


def test_pending_source_probe_does_not_visit_published_history(source_env):
    operation = seed_operation(
        source_env, status="result_unknown", evidence={"revision": 2}
    )
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        # 大部分历史身份不同；当前身份却有大量旧消息。模拟线上平均估算很小、
        # 单个长期核查身份很大的分布，而不是只验证 SQL 文本是否写了某个 CTE。
        db.connection().execute(
            text("""
            INSERT INTO pending_dispatch
              (id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
            SELECT gen_random_uuid(),:tenant,:actor,'materials.verify_original',
              'probe-history:'||gen_random_uuid()::text,
              jsonb_build_object('operation_id',CASE WHEN n<=6000 THEN :operation
                ELSE gen_random_uuid()::text END,'revision',1),
              :old,:old,0 FROM generate_series(1,12000) n
            """),
            {
                "tenant": source_env["context"].tenant_id,
                "actor": source_env["context"].actor_id,
                "operation": str(operation),
                "old": now - timedelta(minutes=5),
            },
        )
        pending = _message(
            source_env, operation, published=None, due=now + timedelta(hours=1)
        )
        db.add(pending)
        for _ in range(300):
            db.add(
                PendingDispatch(
                    tenant_id=source_env["context"].tenant_id,
                    actor_id=source_env["context"].actor_id,
                    task_name="reporting.plan_due",
                    task_key=f"unrelated-pending:{uuid4()}",
                    payload={},
                )
            )
        db.flush()
        db.connection().execute(text("ANALYZE pending_dispatch"))
        # 线上千万行表走索引；小夹具禁用顺序扫描以复现同一访问方式，
        # 比较的是实际过滤历史行数，不依赖本机毫秒耗时或特定索引名称。
        db.connection().execute(text("SET LOCAL enable_seqscan=off"))
        queries = []

        def capture(_connection, _cursor, statement, parameters, *_rest):
            if "repair_source_identities" in statement:
                queries.append((statement, parameters))

        connection = db.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            assert repair_material_dispatches(db) == 0
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert len(queries) == 1
        sql, parameters = queries[0]
        plan = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, parameters
        ).scalar_one()[0]["Plan"]

        def inspected(node):
            own = 0
            if node.get("Relation Name") == "pending_dispatch":
                own = (
                    node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                ) * node.get("Actual Loops", 0)
            return own + sum(inspected(child) for child in node.get("Plans", []))

        # 允许读当前全部待投递集合，但不能为一个待投递来源读 6000 条历史。
        assert inspected(plan) <= 350
        db.refresh(pending)
        assert pending.published_at is None
        assert pending.available_at == now + timedelta(hours=1)


@pytest.mark.parametrize(
    "scheduled_scope,repaired",
    [
        ("current_claim", 0),
        ("old_claim", 1),
        ("other_tenant", 1),
        ("other_task", 1),
        ("superseded", 1),
        ("retired", 1),
    ],
)
def test_pending_source_keeps_claim_tenant_and_task_scope(
    source_env, scheduled_scope, repaired
):
    operation = seed_operation(
        source_env, status="result_unknown", evidence={"revision": 2}
    )
    now, claim = datetime.now(UTC), uuid4()
    with Session(engine) as db, db.begin():
        owned = db.get(MaterialAssetOperation, operation)
        assert owned is not None
        owned.attempt_token, owned.claimed_until = claim, now - timedelta(seconds=1)
        old = _message(
            source_env, operation, published=now - timedelta(minutes=5), due=now
        )
        pending = _message(
            source_env, operation, published=None, due=now + timedelta(hours=1)
        )
        old.payload = pending.payload = {
            "operation_id": str(operation),
            "claim_id": str(claim),
        }
        if scheduled_scope == "old_claim":
            pending.payload = {**pending.payload, "claim_id": str(uuid4())}
        elif scheduled_scope == "other_tenant":
            pending.tenant_id = uuid4()
        elif scheduled_scope == "other_task":
            pending.task_name = "reporting.plan_due"
        elif scheduled_scope == "superseded":
            pending.superseded_at = now
        elif scheduled_scope == "retired":
            pending.dispatch_contract_version = 2
            pending.issued_at = now - timedelta(hours=1)
            pending.valid_until = now + timedelta(hours=1)
            pending.retired_at = now
        db.add_all([old, pending])
        db.flush()
        assert repair_material_dispatches(db, limit=1) == repaired
        db.refresh(old)
        db.refresh(pending)
        assert (old.published_at is None) == bool(repaired)
        assert old.payload["claim_id"] == str(claim)
        assert pending.available_at == now + timedelta(hours=1)
        if scheduled_scope == "other_tenant":
            db.delete(pending)


def test_source_repair_keeps_completed_operation_terminal(source_env):
    operation = seed_operation(
        source_env, status="result_unknown", evidence={"revision": 2}
    )
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        owned = db.get(MaterialAssetOperation, operation)
        assert owned is not None
        owned.status = "succeeded"
        old = _message(
            source_env,
            operation,
            published=now - timedelta(minutes=5),
            due=now - timedelta(minutes=5),
        )
        db.add(old)
        db.flush()
        assert repair_material_dispatches(db) == 0
        db.refresh(old)
        assert old.published_at is not None
