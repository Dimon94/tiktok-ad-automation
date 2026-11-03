"""目录候选窄页发现及单个候选短锁复验。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import NamedTuple, cast
from uuid import UUID

from redis import Redis
from sqlalchemy import text
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.directory_pending_age import (
    CURRENT_DIRECTORY_EXECUTION_SQL,
)
from app.modules.sync_control.models import SyncSchedule


class CandidateKey(NamedTuple):
    scope_id: UUID
    work_class: str
    id: UUID


DISCOVERY_START = datetime.min.replace(tzinfo=UTC)


class DiscoveryCursor(NamedTuple):
    at: datetime
    scope_id: UUID
    until: datetime
    floor_at: datetime
    floor_scope_id: UUID

    @classmethod
    def start(cls, now: datetime) -> DiscoveryCursor:
        return cls(DISCOVERY_START, UUID(int=0), now, DISCOVERY_START, UUID(int=0))

    def encode(self) -> str:
        return json.dumps(
            [
                self.at.isoformat(),
                str(self.scope_id),
                self.until.isoformat(),
                self.floor_at.isoformat(),
                str(self.floor_scope_id),
            ]
        )

    def after_empty_page(
        self, at: datetime, scope_id: UUID, *, cacheable: bool
    ) -> DiscoveryCursor:
        # 只有真正无资格的前缀才能缓存。临时锁跳过的候选仍未服务，不能归入该前缀。
        verified = cacheable and (self.at, self.scope_id) == (
            self.floor_at,
            self.floor_scope_id,
        )
        return self._replace(
            at=at,
            scope_id=scope_id,
            floor_at=at if verified else self.floor_at,
            floor_scope_id=scope_id if verified else self.floor_scope_id,
        )

    def restart_qualified(self) -> DiscoveryCursor:
        return self._replace(at=self.floor_at, scope_id=self.floor_scope_id)


_CANDIDATE_TEMPLATE = """
WITH heads AS MATERIALIZED (
 SELECT h.scope_id,h.work_class,h.tenant_id,s.advertiser_id,s.owner_epoch,
        h.last_dispatched_at,h.oldest_pending_at,
        {discovery_age} discovery_at
 FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
 WHERE h.tenant_id=:tenant_id AND h.work_class=:work_class AND h.active IS TRUE
 AND (CAST(:endpoint AS text) IS NULL OR h.physical_endpoint=:endpoint)
 {extra_heads}
 AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
 AND {discovery_age}<=:scan_until
 AND ({discovery_age},h.scope_id)
     >(:scan_at,:scan_cursor) AND s.resource LIKE 'directory:%'
 ORDER BY {discovery_age},h.scope_id LIMIT 64
)
SELECT h.scope_id,h.work_class,h.last_dispatched_at,h.oldest_pending_at,h.discovery_at,
       picked.id,picked.created_at FROM heads h
LEFT JOIN LATERAL (
 SELECT r.id,r.created_at FROM sync_execution_scope e
 JOIN sync_requirement q ON q.id=e.requirement_id
 CROSS JOIN LATERAL (SELECT * FROM ad_directory_run WHERE id=e.directory_run_id OFFSET 0) r
 WHERE e.scope_id=h.scope_id AND {current_execution}
 {extra_execution}
 AND r.id <> ALL(CAST(:excluded AS uuid[]))
 AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
 AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
 AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
 AND (NOT :independent_only OR (r.workload='live' AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0
 OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0))
 OR EXISTS(SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE))
 ORDER BY r.created_at,r.id LIMIT 1
) picked ON TRUE ORDER BY h.discovery_at,h.scope_id
"""

_DISCOVERY_EXECUTION = CURRENT_DIRECTORY_EXECUTION_SQL.replace(
    "s.owner_epoch", "h.owner_epoch"
).replace("s.advertiser_id", "h.advertiser_id")
PAGED_CANDIDATES = _CANDIDATE_TEMPLATE.format(
    discovery_age="coalesce(h.last_dispatched_at,h.oldest_pending_at,'epoch'::timestamptz)",
    current_execution=_DISCOVERY_EXECUTION,
    extra_heads="",
    extra_execution="",
)
COMPLETION_CANDIDATES = _CANDIDATE_TEMPLATE.format(
    discovery_age="h.oldest_pending_at",
    current_execution=_DISCOVERY_EXECUTION,
    extra_heads="""AND h.oldest_pending_at IS NOT NULL
        AND (CAST(:excluded_account AS text) IS NULL OR s.advertiser_id<>:excluded_account)""",
    extra_execution="""AND NOT (r.workload='live' AND (
        coalesce(jsonb_array_length(r.query->'ids'),0)>0
        OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0))
        AND NOT EXISTS(SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE)""",
)


def _candidate(
    session: Session,
    *,
    tenant_id: UUID,
    work_class: str,
    now: datetime,
    independent_only: bool,
    discover_only: bool = False,
    selected: tuple[UUID, UUID] | None = None,
    redis: Redis | None = None,
    scan_positions: dict[str, tuple[str, DiscoveryCursor, DiscoveryCursor]]
    | None = None,
    excluded_run_ids: set[UUID] | None = None,
    endpoint: str | None = None,
) -> tuple[SyncSchedule, AdDirectoryRun] | None:
    # 发现阶段不加锁：LATERAL内的行锁会在外层排序/LIMIT前锁住整个候选集。
    # 选出ID后再按主键锁定并重新校验所有围栏；冲突回滚小保存点，继续下一候选。
    session.flush()
    excluded: list[UUID] = list(excluded_run_ids or ())
    params = {
        "tenant_id": tenant_id,
        "work_class": work_class,
        "now": now,
        "independent_only": independent_only,
        "excluded": excluded,
        "endpoint": endpoint,
    }
    # 先按真实服务时间取64头窄页，再检查执行资格。UUID只消除同时间的歧义，
    # 不能先按随机UUID分区后仅做页内年龄排序，否则整页外的数日旧头会被越过。
    # 两种模式独立保存(服务时间,scope)发现位置；SQL回滚不会丢失无资格页进度。
    # 扫描上界跨调用冻结，避免热点头服务时间不断更新使游标永远追赶now而不回绕。
    # 未获公平工作类/后台槽仍保留完整原位置/上界，真实领取继续复验所有围栏。
    mode = "independent" if independent_only else "all"
    scan_key = f"sync:directory-discovery-age:v3:{tenant_id}:{endpoint or 'all'}:{work_class}:{mode}"
    scan_cursor = DiscoveryCursor.start(now)
    if redis is not None:
        value = redis.get(scan_key)
        if value:
            stamp, identity, until, floor_at, floor_id = json.loads(
                cast(bytes | str, value)
            )
            scan_cursor = DiscoveryCursor(
                datetime.fromisoformat(stamp),
                UUID(identity),
                datetime.fromisoformat(until),
                datetime.fromisoformat(floor_at),
                UUID(floor_id),
            )
    wrapped = False
    row: CandidateKey | None
    for _ in range(32):
        if selected:
            row = CandidateKey(
                scope_id=selected[0], work_class=work_class, id=selected[1]
            )
        else:
            rows = (
                session.connection()
                .execute(
                    text(PAGED_CANDIDATES),
                    {
                        **params,
                        "scan_at": scan_cursor.at,
                        "scan_cursor": scan_cursor.scope_id,
                        "scan_until": scan_cursor.until,
                    },
                )
                .all()
            )
            if not rows:
                if wrapped or (
                    scan_cursor.at == DISCOVERY_START and scan_cursor.until >= now
                ):
                    return None
                # 已保存的空轮次也必须在后续调用重开，否则新任务会被旧上界永久挡住。
                scan_cursor, wrapped = (
                    DiscoveryCursor.start(now),
                    True,
                )
                if redis is not None:
                    redis.set(scan_key, scan_cursor.encode(), ex=86400)
                continue
            # 页间与页内使用同一服务时间顺序；资格过滤仍只探测这64个头。
            found = min(
                (r for r in rows if r.id is not None),
                key=lambda r: (
                    r.discovery_at,
                    r.scope_id,
                ),
                default=None,
            )
            row = (
                CandidateKey(found.scope_id, found.work_class, found.id)
                if found
                else None
            )
            if row is None:
                scan_cursor = scan_cursor.after_empty_page(
                    rows[-1].discovery_at, rows[-1].scope_id, cacheable=not excluded
                )
                if redis is not None:
                    redis.set(scan_key, scan_cursor.encode(), ex=86400)
                continue
            if scan_positions is not None:
                assert found is not None
                scan_positions[work_class] = (
                    scan_key,
                    scan_cursor,
                    scan_cursor._replace(
                        at=found.discovery_at, scope_id=found.scope_id
                    ),
                )
        if row is None:
            return None
        if discover_only:
            head = session.get(
                SyncSchedule, (row.scope_id, row.work_class), populate_existing=True
            )
            run = session.get(AdDirectoryRun, row.id, populate_existing=True)
            assert head is not None and run is not None
            return head, run
        with session.begin_nested() as attempt:
            claimed = (
                session.connection()
                .execute(
                    text(
                        """
SELECT h.scope_id,h.work_class,r.id
FROM sync_schedule h
JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
JOIN sync_execution_scope e ON e.scope_id=h.scope_id AND e.tenant_id=h.tenant_id AND e.status='PENDING' AND e.owner_epoch=s.owner_epoch
JOIN sync_requirement q ON q.id=e.requirement_id AND q.source=h.work_class
JOIN ad_directory_run r ON r.id=e.directory_run_id AND r.tenant_id=h.tenant_id AND r.advertiser_id=s.advertiser_id AND r.owner_epoch=s.owner_epoch
WHERE h.scope_id=:scope_id AND r.id=:run_id AND h.tenant_id=:tenant_id AND h.work_class=:work_class AND h.active IS TRUE
AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
AND {current_execution}
AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
AND (NOT :independent_only OR (r.workload='live' AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0 OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0)) OR EXISTS(SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE))
ORDER BY coalesce(h.last_dispatched_at,h.oldest_pending_at,r.created_at),h.oldest_pending_at,r.created_at,r.id LIMIT 1 FOR UPDATE OF h,s,e,q,r SKIP LOCKED
""".replace("{current_execution}", CURRENT_DIRECTORY_EXECUTION_SQL)
                    ),
                    {
                        **params,
                        "scope_id": row.scope_id,
                        "run_id": row.id,
                    },
                )
                .first()
            )
            if claimed is None:
                attempt.rollback()
                if selected:
                    return None
                excluded.append(row.id)
                continue
            head = session.get(
                SyncSchedule, (row.scope_id, row.work_class), populate_existing=True
            )
            run = session.get(AdDirectoryRun, row.id, populate_existing=True)
            assert head is not None and run is not None
            return head, run
    return None


def completion_candidate(
    session: Session,
    *,
    tenant_id: UUID,
    endpoint: str,
    now: datetime,
    policy: dict[str, float],
    excluded_run_ids: set[UUID],
) -> tuple[SyncSchedule, AdDirectoryRun] | None:
    from app.modules.sync_control.directory_policy import (
        completion_scan,
        last_completion_account,
        reset_scan,
        save_scan,
    )

    scan = completion_scan(policy, now)
    excluded_account = last_completion_account(policy)
    include_last = bool(policy.get("scan:include_last", 0))
    # 每次最多256头；未找到不证明没有其他账户，跨调用保存位置而非永久空前缀。
    for _ in range(4):
        rows = (
            session.connection()
            .execute(
                text(COMPLETION_CANDIDATES),
                {
                    "tenant_id": tenant_id,
                    "endpoint": endpoint,
                    "work_class": "directory",
                    "now": now,
                    "scan_at": scan.at,
                    "scan_cursor": scan.scope_id,
                    "scan_until": scan.until,
                    "independent_only": False,
                    "excluded": list(excluded_run_ids),
                    "excluded_account": None if include_last else excluded_account,
                },
            )
            .all()
        )
        if not rows:
            reset_scan(policy)
            if excluded_account is not None and not include_last:
                # 只有完整扫描证明本轮没有其他账户，才允许同账户继续；预算耗尽不算证明。
                include_last = True
                scan = DiscoveryCursor.start(now)
                save_scan(policy, scan, include_last=True)
                continue
            return None  # 全范围回绕后，下次重查临时退避及新到其他账户。
        found = next((r for r in rows if r.id is not None), None)
        if found is not None:
            head = session.get(
                SyncSchedule, (found.scope_id, found.work_class), populate_existing=True
            )
            run = session.get(AdDirectoryRun, found.id, populate_existing=True)
            assert head is not None and run is not None
            return head, run
        scan = scan._replace(at=rows[-1].discovery_at, scope_id=rows[-1].scope_id)
        save_scan(policy, scan, include_last=include_last)
    return None
