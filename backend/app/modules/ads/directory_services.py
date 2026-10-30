"""调度目的与平台查询分离：状态保底不能被历史身份补录及其子页占满。"""

import json
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun

SERVICE_KEY = "directory_service_v1"
STATUS_SERVICE = "status"
IDENTITY_SERVICE = "identity"


def directory_service(run: AdDirectoryRun) -> str | None:
    # 子页只补素材身份；即使源广告服务当前状态，也不继承它的保底槽。
    from app.modules.ads.material_directory_children import is_material_child

    if is_material_child(run):
        return IDENTITY_SERVICE
    value = run.query.get(SERVICE_KEY)
    return value if value in (STATUS_SERVICE, IDENTITY_SERVICE) else None


def promote_status_requests(session: Session, runs: Iterable[AdDirectoryRun]) -> int:
    """已证明覆盖当前状态需求的旧请求沿原身份晋升，不重写平台查询、年龄或代数。

    调用方必须先按冻结范围和所需对象取交集。这里再做范围/查询 CAS，
    批量锁住未开始且从未投递的行；避免大 BC 每对象往返数据库。
    无标记只表示来源未知，晋升依据本次真实需求，不猜测历史请求目的。
    """
    candidates = {
        run.id: run
        for run in runs
        if run.workload == "live"
        and directory_service(run) != STATUS_SERVICE
        and "material_child_v1" not in run.query
        and (run.query.get("ids") or run.query.get("parent_ids"))
        and run.status == "QUEUED"
        and run.next_page == 1
        and run.claim_token is None
        and run.claimed_until is None
        and run.published_version is None
    }
    if not candidates:
        return 0
    session.flush()
    values = list(candidates.values())
    changed = 0
    for offset in range(0, len(values), 500):
        expected = [
            {
                "id": str(run.id),
                "tenant": str(run.tenant_id),
                "advertiser": run.advertiser_id,
                "bc": run.bc_id,
                "actor": str(run.actor_id),
                "route": run.frozen_route,
                "generation": run.claim_generation,
                "partition": run.partition_key,
                "query": run.query,
            }
            for run in values[offset : offset + 500]
        ]
        rows = (
            session.connection()
            .execute(
                text("""
            WITH eligible AS MATERIALIZED (
              SELECT r.id FROM jsonb_to_recordset(CAST(:expected AS jsonb)) AS e(
                id uuid, tenant uuid, advertiser text, bc text, actor uuid, route jsonb,
                generation integer, partition text, query jsonb)
              JOIN ad_directory_run r ON r.id=e.id
              WHERE r.tenant_id=e.tenant AND r.advertiser_id=e.advertiser
                AND r.bc_id=e.bc AND r.actor_id=e.actor
                AND r.frozen_route=e.route AND r.partition_key=e.partition
                AND r.claim_generation=e.generation AND r.query=e.query
                AND r.workload='live' AND r.status='QUEUED' AND r.next_page=1
                AND r.claim_token IS NULL AND r.claimed_until IS NULL
                AND r.published_version IS NULL
                AND NOT EXISTS (SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id)
                AND NOT EXISTS (SELECT 1 FROM pending_dispatch p
                  WHERE p.tenant_id=r.tenant_id
                    AND p.task_key>=('ads.targeted_step:'||r.id::text||':')
                    AND p.task_key<('ads.targeted_step:'||r.id::text||';'))
              ORDER BY r.id FOR UPDATE OF r SKIP LOCKED
            )
            UPDATE ad_directory_run r
            SET query=r.query||jsonb_build_object(CAST(:key AS text),'status')
            FROM eligible e WHERE r.id=e.id RETURNING r.id,r.query
        """),
                {"expected": json.dumps(expected), "key": SERVICE_KEY},
            )
            .all()
        )
        for identity, query in rows:
            set_committed_value(candidates[identity], "query", query)
        changed += len(rows)
    return changed
