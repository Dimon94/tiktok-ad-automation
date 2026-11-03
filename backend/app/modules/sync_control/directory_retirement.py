"""被真实冻结需求水位完整覆盖的旧目录有界收尾，不伪造发布或删除事实。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.directory_demand import directory_outcome


def retire_covered_directories(
    session: Session,
    *,
    now: datetime,
    cursor: list[str] | None,
    scan_limit: int = 1000,
    batch_limit: int = 100,
) -> tuple[int, list[str] | None]:
    position = [UUID(v) for v in cursor] if cursor else [UUID(int=0)] * 3
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT scope_id,requirement_id,id,directory_run_id FROM sync_execution_scope
        WHERE status='PENDING' AND scope_id>=:scope
          AND (scope_id,requirement_id,id)>(:scope,:requirement,:reference)
        ORDER BY scope_id,requirement_id,id LIMIT :limit
    """),
            {
                "scope": position[0],
                "requirement": position[1],
                "reference": position[2],
                "limit": scan_limit,
            },
        )
        .all()
    )
    if not rows:
        return 0, None
    identities = list(
        {r.directory_run_id for r in rows if r.directory_run_id is not None}
    )
    # 先在窄页ID内批量证明覆盖，不能对每个仍有欠账的目录逐条持锁/往返。
    identities = (
        list(
            session.connection()
            .execute(
                text("""
        SELECT r.id FROM ad_directory_run r
        WHERE r.id=ANY(CAST(:ids AS uuid[]))
          AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
          AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
          AND EXISTS(SELECT 1 FROM sync_execution_scope e WHERE e.directory_run_id=r.id AND e.status='PENDING')
          AND NOT EXISTS(SELECT 1 FROM sync_schedule h WHERE h.lease_execution_id=r.id AND h.leased_until>:now)
          AND NOT EXISTS(
              SELECT 1 FROM sync_execution_scope e
              JOIN sync_scope s ON s.id=e.scope_id JOIN sync_requirement q ON q.id=e.requirement_id
              WHERE e.directory_run_id=r.id AND (s.owner_epoch<>e.owner_epoch
                OR s.owner_epoch<>r.owner_epoch OR e.tenant_id<>r.tenant_id OR s.tenant_id<>r.tenant_id
                OR (e.status<>'PUBLISHED' AND q.satisfied_revision<e.frozen_revision)))
        ORDER BY r.id LIMIT :limit
    """),
                {"ids": identities, "now": now, "limit": batch_limit},
            )
            .scalars()
        )
        if identities
        else []
    )
    count = 0
    for identity in identities:
        if count >= batch_limit:
            break
        with session.begin_nested() as attempt:
            # 不等待正在运行的worker、刚获grant或其他维护者；只领取本页确定的父执行。
            run = (
                session.connection()
                .execute(
                    text("""
                SELECT id FROM ad_directory_run r WHERE id=:id
                  AND status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
                  AND (claimed_until IS NULL OR claimed_until<=:now)
                  AND NOT EXISTS(SELECT 1 FROM sync_schedule h
                      WHERE h.lease_execution_id=r.id AND h.leased_until>:now)
                FOR UPDATE OF r SKIP LOCKED
            """),
                    {"id": identity, "now": now},
                )
                .first()
            )
            if run is None:
                continue
            head_params = {"id": identity}
            head_where = """EXISTS(SELECT 1 FROM sync_execution_scope e
                WHERE e.scope_id=h.scope_id AND e.directory_run_id=:id)"""
            locked_heads = (
                session.connection()
                .execute(
                    text(
                        "SELECT h.scope_id,h.work_class FROM sync_schedule h WHERE "
                        + head_where
                        + " ORDER BY h.scope_id,h.work_class FOR UPDATE OF h SKIP LOCKED"
                    ),
                    head_params,
                )
                .all()
            )
            total_heads = (
                session.connection()
                .execute(
                    text("SELECT count(*) FROM sync_schedule h WHERE " + head_where),
                    head_params,
                )
                .scalar_one()
            )
            if len(locked_heads) != total_heads:
                attempt.rollback()
                continue  # outcome不能再等待另一维护/领取事务持有的头。
            refs = (
                session.connection()
                .execute(
                    text("""
                SELECT e.id,s.owner_epoch,e.owner_epoch execution_owner,r.owner_epoch run_owner,
                       e.status,q.satisfied_revision,e.frozen_revision
                FROM sync_execution_scope e JOIN sync_scope s ON s.id=e.scope_id
                JOIN sync_requirement q ON q.id=e.requirement_id
                JOIN ad_directory_run r ON r.id=e.directory_run_id
                WHERE r.id=:id FOR UPDATE OF s,e,q SKIP LOCKED
            """),
                    {"id": identity},
                )
                .all()
            )
            total = (
                session.connection()
                .execute(
                    text(
                        "SELECT count(*) FROM sync_execution_scope WHERE directory_run_id=:id"
                    ),
                    {"id": identity},
                )
                .scalar_one()
            )
            if (
                not refs
                or len(refs) != total
                or not any(r.status == "PENDING" for r in refs)
                or any(
                    r.owner_epoch != r.execution_owner
                    or r.owner_epoch != r.run_owner
                    or (
                        r.status != "PUBLISHED"
                        and r.satisfied_revision < r.frozen_revision
                    )
                    for r in refs
                )
            ):
                attempt.rollback()
                continue
            model = session.get(AdDirectoryRun, identity, populate_existing=True)
            assert model is not None
            model.status, model.error_code, model.completed_at = (
                "CANCELLED",
                "coverage_superseded",
                now,
            )
            model.claim_token = model.claimed_until = None
            directory_outcome(session, model)
            count += 1
    next_cursor = cursor if count == batch_limit else [str(v) for v in rows[-1][:3]]
    return count, next_cursor
