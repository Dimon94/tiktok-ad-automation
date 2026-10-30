"""目录派发先读一次活动键及本代消息，公平排序不再反复读取业务大行。"""

from sqlalchemy import text


def directory_candidate_parameters(
    now,
    *,
    background_window,
    live_window,
    batch_limit,
    directory_lost_after,
    publish_lost_after,
    targeted_lost_after,
):
    """同一参数入口供实际派发和只读 EXPLAIN 使用，时间保持带时区绑定。"""
    from app.modules.ads.directory_services import SERVICE_KEY
    from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY

    return {
        "material_child_key": MATERIAL_CHILD_KEY,
        "service_key": SERVICE_KEY,
        "now": now,
        "directory_cutoff": now - directory_lost_after,
        "publish_cutoff": now - publish_lost_after,
        "targeted_cutoff": now - targeted_lost_after,
        "background_window": background_window,
        "live_window": live_window,
        "batch_limit": batch_limit,
    }


def directory_candidate_sql(*, lock=True):
    """lock=False 是相同读取/排名路径的只读探针；实际领取必须锁住原运行。"""
    locking = "FOR UPDATE OF r SKIP LOCKED" if lock else ""
    # active 只投影分类与领取需要的标量。query 不带入后续排序；当前消息
    # 按唯一键最多读取一行，避免把历史 outbox 搬进哈希表或重复做 EXISTS。
    prefix = """
WITH active AS MATERIALIZED (
  SELECT r.id,r.tenant_id,r.created_at,r.claim_generation,r.next_page,
    r.claimed_until,r.next_attempt_at,r.channel,r.request_sequence,
    (r.workload='live' AND flags.has_filters) AS targeted,
    (r.workload<>'live' AND flags.has_filters) AS dependency,
    (r.workload='live' AND flags.has_filters AND NOT r.query ? :material_child_key
      AND coalesce(r.query->>:service_key='status',false)) AS status_service,
    CASE WHEN flags.legacy_creative THEN 1 ELSE 0 END AS creative_last,
    CASE WHEN flags.legacy_creative THEN jsonb_build_array(
      r.tenant_id,r.advertiser_id,r.bc_id,r.actor_id,r.frozen_route,r.partition_key)
    END AS legacy_scope
  FROM ad_directory_run r CROSS JOIN LATERAL (
    SELECT (coalesce(jsonb_array_length(r.query->'ids'),0)>0
      OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0) AS has_filters,
      (r.kind='creative' AND NOT r.query ? :material_child_key) AS legacy_creative
    OFFSET 0
  ) flags
  WHERE r.status IN ('QUEUED','RUNNING','WAITING_REMOTE')
), delivery AS MATERIALIZED (
  SELECT a.*, p.id IS NOT NULL AS enqueued, p.published_at,
    coalesce(p.published_at < CASE
      WHEN p.task_name='ads.publish_step' THEN CAST(:publish_cutoff AS timestamptz)
      WHEN a.targeted THEN CAST(:targeted_cutoff AS timestamptz)
      ELSE CAST(:directory_cutoff AS timestamptz) END,false) AS lost
  FROM active a
  LEFT JOIN LATERAL (
    SELECT p.id,p.published_at,p.task_name FROM pending_dispatch p
    WHERE p.tenant_id=a.tenant_id AND p.task_key=(
      CASE WHEN a.targeted THEN 'ads.targeted_step:' ELSE 'ads.sync_step:' END
      ||a.id::text||':'||a.claim_generation::text||':page:'||a.next_page::text)
    LIMIT 1
  ) p ON true
), inflight AS (
  SELECT count(*) FILTER (WHERE NOT targeted AND channel='OFFICIAL_API' AND
      ((enqueued AND NOT lost AND claimed_until IS NULL)
        OR claimed_until>CAST(:now AS timestamptz))) AS background,
    count(*) FILTER (WHERE dependency AND channel='OFFICIAL_API' AND
      ((enqueued AND NOT lost AND claimed_until IS NULL)
        OR claimed_until>CAST(:now AS timestamptz))) AS dependencies,
    count(*) FILTER (WHERE targeted AND status_service AND
      ((enqueued AND (published_at IS NULL OR published_at>CAST(:targeted_cutoff AS timestamptz)))
        OR claimed_until>CAST(:now AS timestamptz))) AS live_status,
    count(*) FILTER (WHERE targeted AND
      ((enqueued AND (published_at IS NULL OR published_at>CAST(:targeted_cutoff AS timestamptz)))
        OR claimed_until>CAST(:now AS timestamptz))) AS live
  FROM delivery
), capacity AS MATERIALIZED (
  SELECT greatest(0,:background_window-background)::int AS background_available,
    greatest(0,:live_window-live)::int AS live_available,
    greatest(0,:live_window/2-live_status)::int AS status_reserved,
    greatest(0,:live_window-:live_window/2-(live-live_status))::int AS identity_reserved,
    greatest(0,:background_window/2-dependencies)::int AS dependency_reserved,
    greatest(0,:background_window-:background_window/2-(background-dependencies))::int AS full_reserved
  FROM inflight
), latest_creative AS MATERIALIZED (
  SELECT legacy_scope,max(request_sequence) AS request_sequence
  FROM active WHERE creative_last=1 GROUP BY legacy_scope
), eligible AS (
  SELECT d.id,d.tenant_id,d.created_at,d.claim_generation,d.next_page,d.claimed_until,
    d.targeted,d.dependency,d.status_service,d.creative_last,c.last_dispatched_at,
    CASE WHEN d.targeted THEN cap.live_available ELSE cap.background_available END AS lane_limit,
    row_number() OVER (PARTITION BY d.tenant_id,d.targeted,d.dependency,d.status_service ORDER BY
      d.creative_last,d.created_at,d.id) AS lane_rn
  FROM delivery d CROSS JOIN capacity cap
  LEFT JOIN directory_dispatch_cursor c ON c.tenant_id=d.tenant_id
  LEFT JOIN latest_creative newer ON newer.legacy_scope=d.legacy_scope
  WHERE d.channel='OFFICIAL_API' AND ((d.targeted AND cap.live_available>0) OR (NOT d.targeted AND cap.background_available>0))
    AND NOT (NOT d.targeted AND d.creative_last=1 AND newer.request_sequence>d.request_sequence)
    AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=CAST(:now AS timestamptz))
    AND ((d.claimed_until IS NULL AND (NOT d.enqueued OR d.lost))
      OR d.claimed_until<=CAST(:now AS timestamptz))
), tenant_ranked AS (
  -- 租户轮次只累计各服务窗口内的前缀，防止大量实时任务把同租户后台推到队尾。
  -- 超出前缀的行保留在最后，只有前排被其他事务锁住时才用来补足空槽。
  SELECT id,tenant_id,created_at,claim_generation,next_page,claimed_until,
    targeted,dependency,status_service,creative_last,last_dispatched_at,
    lane_rn>lane_limit AS overflow,
    sum(CASE WHEN lane_rn<=lane_limit THEN 1 ELSE 0 END) OVER (
      PARTITION BY tenant_id ORDER BY targeted DESC,creative_last,created_at,id
      ROWS UNBOUNDED PRECEDING) AS rn
  FROM eligible
), service_ranked AS MATERIALIZED (
  SELECT e.*,row_number() OVER (PARTITION BY targeted,dependency,status_service ORDER BY
    overflow,rn,last_dispatched_at ASC NULLS FIRST,creative_last,created_at,id) AS service_rn
  FROM tenant_ranked e
), locked AS MATERIALIZED (
  SELECT picked.* FROM capacity cap CROSS JOIN LATERAL (
"""
    # 分别取每种服务可竞争的少量行，再计算保底：锁住队首不会让预先
    # 截断的排名占掉空槽；实时状态/身份、后台依赖/全量各自保底。
    branches = []
    for condition, available in (
        ("s.targeted AND s.status_service", "live_available"),
        ("s.targeted AND NOT s.status_service", "live_available"),
        ("NOT s.targeted AND s.dependency", "background_available"),
        ("NOT s.targeted AND NOT s.dependency", "background_available"),
    ):
        branches.append(f"""
    SELECT picked_service.* FROM LATERAL (
      SELECT s.* FROM (
        SELECT s.* FROM service_ranked s WHERE {condition}
        ORDER BY s.service_rn OFFSET 0
      ) s CROSS JOIN LATERAL (
        SELECT r.id FROM ad_directory_run r WHERE r.id=s.id
          AND r.status IN ('QUEUED','RUNNING','WAITING_REMOTE')
          AND r.claim_generation=s.claim_generation AND r.next_page=s.next_page
          AND (NOT s.targeted OR (NOT r.query ? :material_child_key
            AND coalesce(r.query->>:service_key='status',false))=s.status_service)
          AND r.claimed_until IS NOT DISTINCT FROM s.claimed_until
          AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=CAST(:now AS timestamptz))
        LIMIT 1
        {locking}
      ) held
      ORDER BY s.service_rn
      LIMIT least(cap.{available},:batch_limit)
    ) picked_service
""")
    return (
        prefix
        + " UNION ALL ".join(branches)
        + """
  ) picked
), available_services AS (
  SELECT l.*,row_number() OVER (PARTITION BY targeted,dependency,status_service ORDER BY service_rn) AS available_rn
  FROM locked l
), reserved_services AS (
  -- 保底先于最终批次截断，batch=1 时也不能被更老的借槽任务抢走。
  SELECT a.*,
    CASE WHEN targeted THEN
      available_rn<=CASE WHEN status_service THEN status_reserved ELSE identity_reserved END
        OR :live_window<2
    ELSE available_rn<=CASE WHEN dependency THEN dependency_reserved ELSE full_reserved END
        OR :background_window<2 END AS reserved
  FROM available_services a CROSS JOIN capacity
), pool_ranked AS (
  SELECT a.*,row_number() OVER (PARTITION BY targeted ORDER BY
    reserved DESC,
    service_rn,last_dispatched_at ASC NULLS FIRST,creative_last,created_at,id) AS pool_rn
  FROM reserved_services a
), bounded_pools AS (
  SELECT p.* FROM pool_ranked p CROSS JOIN capacity
  WHERE pool_rn<=least(:batch_limit,CASE WHEN targeted THEN live_available ELSE background_available END)
), final_tenant_turns AS (
  -- 先在各池保留本批可服务的保底前缀，再按租户轮转；保底不能让同租户
  -- 的实时、后台两条记录一起越过另一个租户的首轮。
  SELECT p.*,row_number() OVER (PARTITION BY tenant_id ORDER BY reserved DESC,rn,
    last_dispatched_at ASC NULLS FIRST,targeted DESC,creative_last,created_at,id) AS final_rn
  FROM bounded_pools p
), chosen AS (
  SELECT * FROM final_tenant_turns
  ORDER BY final_rn,last_dispatched_at ASC NULLS FIRST,targeted DESC,creative_last,created_at,id
  LIMIT :batch_limit
)
SELECT chosen.id,capacity.background_available,capacity.live_available
FROM capacity LEFT JOIN chosen ON true
ORDER BY chosen.rn,chosen.last_dispatched_at ASC NULLS FIRST,
  chosen.targeted DESC,chosen.creative_last,chosen.created_at,chosen.id
"""
    )


def directory_candidates(session, now, **parameters):
    return (
        session.connection()
        .execute(
            text(directory_candidate_sql()),
            directory_candidate_parameters(now, **parameters),
        )
        .mappings()
        .all()
    )
