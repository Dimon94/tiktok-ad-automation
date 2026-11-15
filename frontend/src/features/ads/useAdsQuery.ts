import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  type AdsQueryPage,
  AdsReportingService,
  type ReportRow,
} from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"
import useAuth from "@/hooks/useAuth"
import { type AdsSearch, toAdsQuery } from "./search"
export function useAdsQuery(search: AdsSearch, queryRevision = 0) {
  const { tenantId, bc, scope } = useTenantScope()
  const { user } = useAuth()
  const actorId = user?.id ?? null
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [allMatching, setAllMatching] = useState(false)
  const [excluded, setExcluded] = useState<Set<string>>(new Set())
  const requestedRefresh = useRef(0)
  const completedRefresh = useRef(0)
  const forceNextQuery = useCallback(() => {
    requestedRefresh.current += 1
  }, [])
  const bcId = bc?.bc_id ?? null
  const queryKey = useMemo(
    () =>
      [
        "tenant",
        tenantId,
        "ads",
        bcId,
        queryRevision,
        search,
        actorId,
      ] as const,
    [tenantId, bcId, queryRevision, search, actorId],
  )
  const query = useQuery<AdsQueryPage>({
    queryKey,
    enabled: !!tenantId && !!bcId && !!actorId,
    // 只在同一维度内（翻页、排序、筛选）沿用上一页数据避免闪烁；切换维度必须清空，
    // 否则新维度加载期间表格仍是上个维度的行（如素材页签显示剧名），看起来像数据错了。
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === tenantId &&
      previousQuery?.queryKey[3] === bcId &&
      previousQuery?.queryKey[6] === actorId &&
      (previousQuery?.queryKey[5] as AdsSearch | undefined)?.dimension ===
        search.dimension
        ? previous
        : undefined,
    // 查询失败由用户明确重查，避免自动重试反复等待同一失效快照。
    retry: false,
    staleTime: 15_000,
    gcTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
    queryFn: async ({ signal }) => {
      const refresh = requestedRefresh.current
      const forceRefresh = refresh > completedRefresh.current
      // 首屏重新进入走一分钟缓存；选择或分页中的操作仍固定原快照。
      const heldSnapshot =
        selected.size || allMatching || (search.page ?? 1) > 1
          ? queryClient.getQueryData<AdsQueryPage>(queryKey)?.snapshot
              .snapshot_id
          : undefined
      const result = await AdsReportingService.queryAds({
        path: { tenant_id: tenantId! },
        query: {
          bc_id: bcId!,
          ...toAdsQuery(search, search.snapshot_id ?? heldSnapshot),
          force_refresh: forceRefresh ? true : undefined,
        },
        signal,
      })
      if (forceRefresh && refresh === requestedRefresh.current)
        completedRefresh.current = refresh
      return result.data
    },
  })
  const selectionKey = useMemo(() => {
    const {
      page: _page,
      limit: _limit,
      cursor: _cursor,
      snapshot_id: _snapshot,
      ...filters
    } = search
    return JSON.stringify({ tenantId, bcId, actorId, filters })
  }, [tenantId, bcId, actorId, search])
  // 筛选/维度/BC 变化必须清空跨页冻结选择，避免 row_key 复用。
  useEffect(() => {
    if (!selectionKey) return
    setSelected(new Set())
    setExcluded(new Set())
    setAllMatching(false)
  }, [selectionKey])
  // 整页勾选要一次性写入，避免逐行 setState 造成多次渲染和中间态。
  const toggleRows = (rows: ReportRow[], checked: boolean) => {
    const keys = rows.map((row) => row.row_key)
    setSelected((current) => {
      const next = new Set(current)
      for (const key of keys) {
        if (checked) next.add(key)
        else next.delete(key)
      }
      return next
    })
    // 全部匹配模式下取消勾选记为排除，重新勾选则撤销排除。
    if (allMatching)
      setExcluded((current) => {
        const next = new Set(current)
        for (const key of keys) {
          if (checked) next.delete(key)
          else next.add(key)
        }
        return next
      })
  }
  const toggleRow = (row: ReportRow, checked: boolean) =>
    toggleRows([row], checked)
  const selectAllMatching = () => {
    setAllMatching(true)
    setSelected(new Set(query.data?.items?.map((row) => row.row_key) ?? []))
    setExcluded(new Set())
  }
  const clear = useCallback(() => {
    setSelected(new Set())
    setExcluded(new Set())
    setAllMatching(false)
  }, [])
  return {
    query: { ...query, data: query.isError ? undefined : query.data },
    forceNextQuery,
    snapshot: query.isError ? null : query.data?.snapshot ?? null,
    selection: {
      selected,
      excluded,
      allMatching,
      toggleRow,
      toggleRows,
      selectAllMatching,
      clear,
    },
    role: scope?.role,
  }
}
