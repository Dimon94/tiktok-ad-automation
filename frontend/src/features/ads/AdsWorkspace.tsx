import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { useCallback, useEffect, useRef, useState } from "react"
import {
  AdsReportingService,
  type FrozenSelection,
  type ReportRow,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  type ManagementAction,
  ManagementPreviewSheet,
  managementActionKey,
} from "@/features/ad-management/ManagementPreviewSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import useAuth from "@/hooks/useAuth"
import { AdsFilters } from "./AdsFilters"
import { rowDisplayName } from "./AdsNameCell"
import {
  AdsScopeChips,
  type ScopeKind,
  type ScopeLabels,
} from "./AdsScopeChips"
import { AdsSummary } from "./AdsSummary"
import { AdsTable } from "./AdsTable"
import { AdsTableToolbar } from "./AdsTableToolbar"
import {
  applyColumnPrefs,
  type ColumnPrefs,
  loadColumnPrefs,
  saveColumnPrefs,
} from "./columnPrefs"
import {
  AdsDetailSheet,
  type DetailActionScope,
  type DetailEntry,
} from "./details/AdsDetailSheet"
import { columnsFor, useMetricCatalog } from "./metricCatalog"
import { ReportQueryStatus } from "./ReportQueryStatus"
import { ReportStatusBar } from "./ReportStatusBar"
import {
  type AdsSearch,
  childDimension,
  DIMENSIONS,
  defaultAdsSearch,
  dramaCampaignRefs,
  MAX_LINKED_IDS,
  type ParentLink,
  rowAdvertiserId,
  rowObjectId,
  toReportingFilter,
} from "./search"
import { useAdsQuery } from "./useAdsQuery"
import { useReportExport } from "./useReportExport"
export function AdsWorkspace() {
  const { tenantId, bc, scope, bcDirectory } = useTenantScope()
  const { user } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState<AdsSearch>(() => defaultAdsSearch())
  // 详情抽屉的访问栈：底层是表格里点开的行，之后每点一个关联对象压一层，
  // “返回”逐层弹出（如 剧 → 系列 → 账户），关闭抽屉清空整个栈。
  const [detailStack, setDetailStack] = useState<DetailEntry[]>([])
  const detail = detailStack[detailStack.length - 1] ?? null
  const openDetail = (row: ReportRow, dimension: AdsSearch["dimension"]) =>
    setDetailStack([{ source: "table", row, dimension }])
  const closeDetail = useCallback(() => setDetailStack([]), [])
  const [applied, setApplied] = useState<AdsSearch>(search)
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([])
  const [queryRevision, setQueryRevision] = useState(0)
  const lastSnapshotRequestAt = useRef(Date.now())
  const [pageVisible, setPageVisible] = useState(!document.hidden)
  useEffect(() => {
    const changed = () => setPageVisible(!document.hidden)
    document.addEventListener("visibilitychange", changed)
    return () => document.removeEventListener("visibilitychange", changed)
  }, [])
  // biome-ignore lint/correctness/useExhaustiveDependencies: 主动刷新或切换查询范围重新起算自动刷新间隔。
  useEffect(() => {
    lastSnapshotRequestAt.current = Date.now()
  }, [queryRevision, applied.dimension, bc?.bc_id])
  const currentBcId = bc?.bc_id
  const [appliedBcId, setAppliedBcId] = useState<string | null>(null)
  const requestSearch =
    appliedBcId === currentBcId
      ? applied
      : { ...applied, page: 1, cursor: undefined, snapshot_id: undefined }
  const ads = useAdsQuery(requestSearch, queryRevision)
  const items = ads.query.data?.items ?? []
  const catalog = useMetricCatalog()
  const [submittedTask, setSubmittedTask] = useState<{
    task_id: string
    bc_id: string
    submittedAt: number
  } | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [frozenSelection, setFrozenSelection] =
    useState<FrozenSelection | null>(null)
  const [managementSubmitting, setManagementSubmitting] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [managementAction, setManagementAction] =
    useState<ManagementAction | null>(null)
  // 发起动作时的维度随动作一起记下，预览面板据此说明实际修改的层级。
  const [managementDimension, setManagementDimension] = useState<
    AdsSearch["dimension"]
  >(applied.dimension)
  const scopeRef = useRef(`${tenantId ?? ""}:${currentBcId ?? ""}`)
  const selectionRequestScopeRef = useRef<string | null>(null)

  // 列偏好状态：仅在 tenantId 和 user.id 都就绪后读写
  const prefsReady = Boolean(tenantId && user?.id)
  const prefsScope = prefsReady ? `${tenantId}:${user!.id}` : null
  const [columnPrefs, setColumnPrefs] = useState<ColumnPrefs | null>(null)

  // 当 scope 就绪时加载偏好
  useEffect(() => {
    if (prefsScope) {
      setColumnPrefs(loadColumnPrefs(prefsScope, applied.dimension))
    }
  }, [prefsScope, applied.dimension])

  const handlePrefsChange = (prefs: ColumnPrefs) => {
    setColumnPrefs(prefs)
    if (prefsScope) {
      saveColumnPrefs(prefsScope, applied.dimension, prefs)
    }
  }
  const allColumns = columnsFor(catalog.data ?? [], applied.dimension)
  const visibleColumns = applyColumnPrefs(
    allColumns,
    prefsReady ? columnPrefs : null,
  )

  const clearManagementPreview = useCallback(() => {
    selectionRequestScopeRef.current = null
    setFrozenSelection(null)
    setPreviewOpen(false)
    setManagementAction(null)
  }, [])
  const [syncNotice, setSyncNotice] = useState<string | null>(null)
  const syncStatus = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads-sync-status",
      currentBcId,
      applied.start_date,
      applied.end_date,
    ],
    enabled: Boolean(tenantId && currentBcId),
    queryFn: async () =>
      (
        await AdsReportingService.getAdSyncStatus({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: currentBcId!,
            start_date: applied.start_date,
            end_date: applied.end_date,
          },
        })
      ).data,
    // 用户主动操作后短时加快本地版本观察；不在浏览器反复触发平台刷新任务。
    refetchIntervalInBackground: false,
    refetchInterval: () =>
      !pageVisible
        ? false
        : submittedTask && Date.now() - submittedTask.submittedAt < 5 * 60_000
          ? 2000
          : 30_000,
  })
  const publicationRef = useRef<{ scope: string; version: string } | null>(null)
  const publicationScope = `${tenantId}:${currentBcId}:${applied.start_date}:${applied.end_date}`
  const clearSelection = ads.selection.clear
  const pendingRefreshScope = useRef<string | null>(null)
  const [newDataAvailable, setNewDataAvailable] = useState(false)
  const loadLatest = useCallback(() => {
    lastSnapshotRequestAt.current = Date.now()
    pendingRefreshScope.current = null
    setNewDataAvailable(false)
    clearSelection()
    clearManagementPreview()
    closeDetail()
    setCursorHistory([])
    setSearch((current) => ({
      ...current,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }))
    setApplied((current) => ({
      ...current,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }))
    setQueryRevision((current) => current + 1)
    setSyncNotice("报表数据已更新")
  }, [clearSelection, clearManagementPreview, closeDetail])
  const refetchRows = ads.query.refetch
  const refreshCurrentPage = useCallback(() => {
    void refetchRows()
    // 已打开的详情/广告内素材也读取同一轮目录回读，不留着操作前的缓存。
    void queryClient.invalidateQueries({
      queryKey: ["tenant", tenantId, "ads"],
      predicate: (query) =>
        ["detail", "ad-materials", "linked"].includes(
          String(query.queryKey[3]),
        ) && query.queryKey[4] === currentBcId,
    })
  }, [refetchRows, queryClient, tenantId, currentBcId])
  const syncMutation = useMutation({
    mutationFn: async () => {
      // 手动刷新按当前筛选账户插队，不从当前页推导范围，也不触发历史回补。
      const requestScope = scopeRef.current
      const result = (
        await AdsReportingService.requestAdSyncRefresh({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: { advertiser_ids: applied.advertiser_ids ?? [] },
        })
      ).data
      return { result, requestScope }
    },
    onSuccess: ({ result, requestScope }) => {
      if (scopeRef.current !== requestScope) return
      if (result.status === "QUEUED")
        pendingRefreshScope.current = publicationScope
      setSyncNotice(
        result.status === "SYNC_DISABLED"
          ? "报表同步未开启"
          : result.status === "API_BINDING_REQUIRED"
            ? "该 BC 未接入 API，无法同步报表"
            : "已加入刷新队列",
      )
      void syncStatus.refetch()
    },
  })
  const reportExport = useReportExport(tenantId, currentBcId)
  const viewMutation = useMutation({
    mutationFn: async () =>
      (
        await AdsReportingService.createReportView({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: {
            name: "广告报表当前筛选",
            filters: toReportingFilter(applied),
            columns: ["name", "status", ...visibleColumns.map((c) => c.key)],
          },
        })
      ).data,
  })
  const selectionMutation = useMutation({
    // single 用于详情里的单对象操作：只冻结这一行，不受表格勾选影响。关联对象的行
    // 来自它自己的查询快照，因此冻结时用该行所属的快照，而不是表格当前快照。
    mutationFn: async (
      single?: { rowKey: string; snapshotId: string } | undefined,
    ) => {
      if (!single && !ads.snapshot) throw new Error("当前报表没有可操作的快照")
      return (
        await AdsReportingService.freezeAdSelection({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: single
            ? {
                snapshot_id: single.snapshotId,
                mode: "EXPLICIT",
                row_keys: [single.rowKey],
                excluded_row_keys: [],
              }
            : {
                snapshot_id: ads.snapshot!.snapshot_id,
                mode: ads.selection.allMatching ? "ALL_MATCHING" : "EXPLICIT",
                row_keys: [...ads.selection.selected],
                excluded_row_keys: [...ads.selection.excluded],
              },
        })
      ).data
    },
    onSuccess: (selection) => {
      if (selectionRequestScopeRef.current !== scopeRef.current) return
      setFrozenSelection(selection)
      setPreviewOpen(true)
    },
  })
  useEffect(() => {
    if (!bc && tenantId && bcDirectory?.items[0]?.bc_id) {
      void navigate({
        to: "/tenants/$tenantId/ads",
        params: { tenantId },
        search: { bc_id: bcDirectory.items[0].bc_id },
        replace: true,
      })
    }
  }, [bc, bcDirectory, navigate, tenantId])
  useEffect(() => {
    if (!currentBcId) return
    scopeRef.current = `${tenantId ?? ""}:${currentBcId}`
    clearManagementPreview()
    setSyncNotice(null)
    setActionError(null)
    setSubmittedTask(null)
    setAppliedBcId(currentBcId)
    setCursorHistory([])
    closeDetail()
    setSearch((current) => ({
      ...current,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }))
    setApplied((current) => ({
      ...current,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }))
  }, [clearManagementPreview, closeDetail, currentBcId, tenantId])
  // 层级筛选标签的显示名；重置或取消筛选时一并清空。
  const [scopeLabels, setScopeLabels] = useState<ScopeLabels>({})
  const update = (next: Partial<AdsSearch>) =>
    setSearch((current) => ({ ...current, ...next, page: next.page ?? 1 }))
  const apply = () => {
    ads.selection.clear()
    clearManagementPreview()
    setCursorHistory([])
    closeDetail()
    setQueryRevision((current) => current + 1)
    setApplied({
      ...search,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    })
  }
  const reset = () => {
    const next = defaultAdsSearch()
    setScopeLabels({})
    ads.selection.clear()
    clearManagementPreview()
    setCursorHistory([])
    closeDetail()
    setQueryRevision((current) => current + 1)
    setSearch(next)
    setApplied(next)
  }
  const sort = (sortBy: string) => {
    // 同一列再次点击切换方向；新列中名称默认升序，数值指标默认降序（大的在前）。
    const isName = sortBy.endsWith("_name") || sortBy === "name"
    const direction =
      applied.sort_by === sortBy
        ? applied.sort_direction === "asc"
          ? "desc"
          : "asc"
        : isName
          ? "asc"
          : "desc"
    const next = {
      ...applied,
      sort_by: sortBy,
      sort_direction: direction as "asc" | "desc",
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }
    ads.selection.clear()
    clearManagementPreview()
    setCursorHistory([])
    closeDetail()
    setQueryRevision((current) => current + 1)
    setSearch(next)
    setApplied(next)
  }
  // 按新的层级筛选立即查询；下钻、点上级路径和取消筛选标签共用同一套状态重置。
  const applyScope = (scope: Partial<AdsSearch>) => {
    const next: AdsSearch = {
      ...applied,
      ...scope,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }
    ads.selection.clear()
    clearManagementPreview()
    closeDetail()
    setCursorHistory([])
    setActionError(null)
    setSearch(next)
    setApplied(next)
  }
  // 切换到某个层级并按父级 ID 筛选，同时记下名称供筛选标签显示。
  const filterTo = (
    dimension: AdsSearch["dimension"],
    advertiserId: string,
    parent: { kind: ScopeKind; id: string; name: string },
    accountName?: string,
  ) => {
    setScopeLabels({ accountName, parent })
    applyScope({ dimension, advertiser_ids: [advertiserId], ids: [parent.id] })
  }
  const clearScopeAccount = () => {
    setScopeLabels({})
    applyScope({ advertiser_ids: [], ids: [] })
  }
  // 只取消下级对象，保留账户筛选，回到该账户下的全部数据。
  const clearScopeParent = () => {
    setScopeLabels((current) => ({ accountName: current.accountName }))
    applyScope({ ids: [] })
  }
  // 剧 → 该剧关联的系列：系列可能分布在多个账户，只按系列 ID 筛选，不限定账户。
  const navigateToDramaCampaigns = (row: ReportRow) => {
    const refs = dramaCampaignRefs(row)
    if (!refs.length) {
      setActionError("这部剧暂无关联系列")
      return
    }
    setScopeLabels({
      parent: {
        kind: "drama",
        id: refs[0].remote_id,
        name: rowDisplayName(row, "drama"),
      },
    })
    applyScope({
      dimension: "campaign",
      advertiser_ids: [],
      ids: refs.slice(0, MAX_LINKED_IDS).map((ref) => ref.remote_id),
    })
    if (refs.length > MAX_LINKED_IDS)
      setActionError(
        `这部剧关联 ${refs.length} 个系列，列表只显示前 ${MAX_LINKED_IDS} 个`,
      )
  }
  const navigateToChild = (row: ReportRow) => {
    const dimension = childDimension(applied.dimension)
    if (!dimension) {
      openDetail(row, applied.dimension)
      return
    }
    if (applied.dimension === "drama") {
      navigateToDramaCampaigns(row)
      return
    }
    const parentId = rowObjectId(row, applied.dimension)
    const advertiserId = rowAdvertiserId(row)
    if (!parentId || !advertiserId) {
      setActionError("当前行缺少层级 ID，暂时无法跳转下级报表")
      return
    }
    const parentKind = applied.dimension as ScopeKind
    filterTo(
      dimension,
      advertiserId,
      {
        kind: parentKind,
        id: parentId,
        name: rowDisplayName(row, applied.dimension),
      },
      row.display?.account_name ?? undefined,
    )
  }
  const filterByParent = (link: ParentLink, advertiserId: string) => {
    if (!advertiserId) {
      setActionError("当前行缺少账户 ID，暂时无法按上级筛选")
      return
    }
    // 点了哪个上级，就看它的直接下级：账户 → 系列，系列 → 广告组，广告组 → 广告。
    const dimension =
      link.kind === "account"
        ? "campaign"
        : link.kind === "campaign"
          ? "adgroup"
          : "ad"
    const accountName =
      link.kind === "account"
        ? link.name
        : items.find((row) => rowAdvertiserId(row) === advertiserId)?.display
            ?.account_name
    filterTo(dimension, advertiserId, link, accountName ?? undefined)
  }
  const runAction = (action: () => void) => {
    setActionError(null)
    action()
  }
  const roleReadonly = scope?.role === "viewer"
  // 工具栏动作：先记下动作与发起时的 BC 范围，再冻结选择；冻结成功后才打开预览。
  // 每次发起都换一个预览会话号，使预览面板重新挂载，旧的方式/数值/排除项不会被带入。
  const [previewSession, setPreviewSession] = useState(0)
  const startManagementAction = (
    next: ManagementAction,
    single?: { row: ReportRow; scope: DetailActionScope },
  ) => {
    if (
      roleReadonly ||
      managementSubmitting ||
      selectionMutation.isPending ||
      adMaterialSelection.isPending
    )
      return
    if (single && !single.scope.snapshotId) {
      setActionError("该对象没有可操作的报表快照，请刷新后重试")
      return
    }
    runAction(() => {
      setManagementAction(next)
      // 详情里的操作按该对象自身的维度说明修改层级（可能不同于表格当前维度）。
      setManagementDimension(single?.scope.dimension ?? applied.dimension)
      setPreviewSession((current) => current + 1)
      // 按表格改名的目标来自粘贴的推广系列 ID，不冻结列表勾选，直接打开面板。
      if (next.field === "name" && next.entry === "paste") {
        selectionRequestScopeRef.current = null
        setFrozenSelection(null)
        setPreviewOpen(true)
        return
      }
      selectionRequestScopeRef.current = scopeRef.current
      selectionMutation.mutate(
        single
          ? { rowKey: single.row.row_key, snapshotId: single.scope.snapshotId! }
          : undefined,
        {
          onError: (error) => setActionError(String(error)),
        },
      )
    })
  }
  // 广告详情里的素材启停：只冻结这条广告的指定素材，预览与提交复用广告管理流程。
  const adMaterialSelection = useMutation({
    mutationFn: async (target: {
      advertiserId: string
      adId: string
      adMaterialIds: string[]
    }) =>
      (
        await AdsReportingService.freezeAdMaterialSelection({
          path: { tenant_id: tenantId!, remote_id: target.adId },
          query: { advertiser_id: target.advertiserId, bc_id: bc!.bc_id },
          body: { ad_material_ids: target.adMaterialIds },
        })
      ).data,
    onSuccess: (selection) => {
      if (selectionRequestScopeRef.current !== scopeRef.current) return
      setFrozenSelection(selection)
      setPreviewOpen(true)
    },
  })
  const startAdMaterialAction = (
    advertiserId: string,
    adId: string,
    adMaterialIds: string[],
    value: "ENABLE" | "DISABLE",
  ) => {
    if (
      roleReadonly ||
      managementSubmitting ||
      selectionMutation.isPending ||
      adMaterialSelection.isPending
    )
      return
    runAction(() => {
      setManagementAction({ field: "status", value })
      setManagementDimension("material")
      setPreviewSession((current) => current + 1)
      selectionRequestScopeRef.current = scopeRef.current
      adMaterialSelection.mutate(
        { advertiserId, adId, adMaterialIds },
        { onError: (error) => setActionError(String(error)) },
      )
    })
  }
  const canAutoLoadLatest =
    pageVisible &&
    !ads.selection.selected.size &&
    !ads.selection.allMatching &&
    !previewOpen &&
    !detail &&
    !selectionMutation.isPending &&
    !adMaterialSelection.isPending &&
    !ads.query.isFetching &&
    applied.page === 1
  useEffect(() => {
    if (!syncStatus.data) return
    const version = syncStatus.data.publication_version
    const previous = publicationRef.current
    publicationRef.current = { scope: publicationScope, version }
    const followOwnOperation =
      submittedTask?.bc_id === currentBcId &&
      !!ads.snapshot &&
      Date.parse(ads.snapshot.expires_at) > Date.now()
    if (!previous || previous.scope !== publicationScope) {
      pendingRefreshScope.current = null
      setNewDataAvailable(false)
      // 早于首次同步状态响应完成的提交，也要读取当前页的真实状态。
      if (followOwnOperation) void refreshCurrentPage()
      return
    }
    // 延迟的新版本在操作结束后仍可自动加载；同一版本只加载一次，避免刷新循环。
    if (previous.version === version && !newDataAvailable) return
    // 只有空闲且当前请求结束时换快照，避免打断选择、详情、预览或跨页操作。
    // 用户明确点击刷新可重建；后台发布必须服从空闲状态和60秒间隔。
    if (pendingRefreshScope.current === publicationScope && pageVisible)
      loadLatest()
    else if (canAutoLoadLatest) {
      const remaining = 60_000 - (Date.now() - lastSnapshotRequestAt.current)
      if (remaining <= 0) loadLatest()
      else {
        setNewDataAvailable(true)
        const timer = window.setTimeout(loadLatest, remaining)
        return () => window.clearTimeout(timer)
      }
    } else if (previous.version !== version && followOwnOperation) {
      // 本人操作的回读结果自动补全原快照；不换成员、不重排，也不清掉后续勾选或预览。
      void refreshCurrentPage()
      setNewDataAvailable(true)
    } else setNewDataAvailable(true)
  }, [
    syncStatus.data,
    publicationScope,
    loadLatest,
    submittedTask,
    currentBcId,
    ads.snapshot,
    refreshCurrentPage,
    canAutoLoadLatest,
    newDataAvailable,
    pageVisible,
  ])
  const nextPage = () => {
    const nextCursor = ads.query.data?.next_cursor
    if (!nextCursor) return
    setCursorHistory((history) => [...history, applied.cursor ?? null])
    setApplied((current) => ({
      ...current,
      cursor: nextCursor,
      snapshot_id: ads.snapshot?.snapshot_id,
      page: current.page + 1,
    }))
  }
  const previousPage = () => {
    setCursorHistory((history) => {
      const next = [...history]
      const cursor = next.pop() ?? null
      setApplied((current) => ({
        ...current,
        cursor: cursor ?? undefined,
        page: Math.max(1, current.page - 1),
      }))
      return next
    })
  }
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {submittedTask && submittedTask.bc_id === currentBcId && (
        <Alert>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
            <span>操作已提交。回读完成后本页自动更新状态，可继续操作。</span>
            <Link
              to="/tenants/$tenantId/ad-management-tasks/$taskId"
              params={{ tenantId: tenantId!, taskId: submittedTask.task_id }}
              search={{ bc_id: submittedTask.bc_id }}
              className="underline underline-offset-4"
            >
              查看操作记录
            </Link>
          </AlertDescription>
        </Alert>
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <WorkspacePageTitle>广告报表</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            {bc?.name ?? "当前 BC"} · 六维广告效果与配置
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={syncMutation.isPending || roleReadonly}
            onClick={() =>
              runAction(() =>
                syncMutation.mutate(undefined, {
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
          >
            {syncMutation.isPending ? "刷新中…" : "刷新"}
          </Button>
          <Button
            variant="outline"
            disabled={
              !ads.snapshot ||
              reportExport.state.phase === "running" ||
              roleReadonly
            }
            title="导出当前维度、筛选和日期下的全部行（CSV），不只当前页"
            onClick={() => {
              if (!ads.snapshot) return
              const label =
                DIMENSIONS.find((d) => d.value === applied.dimension)?.label ??
                applied.dimension
              void reportExport.start(
                ads.snapshot.snapshot_id,
                `广告报表-${label}-${applied.start_date}_${applied.end_date}.csv`,
              )
            }}
          >
            {reportExport.state.phase === "running" ? "导出中…" : "导出"}
          </Button>
        </div>
      </div>
      <ReportStatusBar
        status={syncStatus.data}
        selectedAdvertiserIds={applied.advertiser_ids}
        startDate={applied.start_date}
        coverage={ads.query.data?.coverage}
        notice={syncNotice}
        onLoadLatest={newDataAvailable ? loadLatest : undefined}
      />
      {/* 卡片自带纵向内边距，这里改为只用内容区的紧凑内边距，给列表留出空间。 */}
      <Card className="py-0 lg:py-0">
        <CardContent className="px-4 py-3">
          <AdsFilters
            loading={ads.query.isFetching}
            search={search}
            onChange={update}
            onApply={apply}
            onReset={reset}
            onSaveView={() =>
              runAction(() =>
                viewMutation.mutate(undefined, {
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
            savingView={viewMutation.isPending}
            disableSaveView={roleReadonly}
          />
        </CardContent>
      </Card>
      {catalog.isError && (
        <div className="flex items-center gap-2 text-sm text-destructive">
          <p>指标定义读取失败</p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void catalog.refetch()}
          >
            重试
          </Button>
        </div>
      )}
      <ReportQueryStatus
        loading={ads.query.isFetching}
        hasPreviousData={!!ads.query.data}
        snapshotAt={ads.snapshot?.snapshot_at}
        metricsObservedAt={ads.query.data?.metrics_observed_at}
        projectionStatus={String(
          ads.query.data?.metrics_coverage?.status ?? "",
        )}
      />
      <AdsSummary
        summary={ads.query.data?.summary}
        catalog={catalog.data ?? []}
      />
      <Card className="py-0 lg:py-0">
        <CardContent className="space-y-3 p-4">
          <Tabs
            value={applied.dimension}
            onValueChange={(value) => {
              const dimension = value as AdsSearch["dimension"]
              ads.selection.clear()
              clearManagementPreview()
              setCursorHistory([])
              closeDetail()
              setApplied((current) => ({
                ...current,
                dimension,
                page: 1,
                cursor: undefined,
                snapshot_id: undefined,
              }))
              setSearch((current) => ({
                ...current,
                dimension,
                page: 1,
                cursor: undefined,
                snapshot_id: undefined,
              }))
              // 加载新维度的列偏好
              if (prefsScope) {
                setColumnPrefs(loadColumnPrefs(prefsScope, dimension))
              }
            }}
          >
            <div className="flex flex-wrap items-center gap-3">
              <TabsList>
                {DIMENSIONS.map((dimension) => (
                  <TabsTrigger key={dimension.value} value={dimension.value}>
                    {dimension.label}
                  </TabsTrigger>
                ))}
              </TabsList>
              <AdsScopeChips
                advertiserIds={applied.advertiser_ids}
                ids={applied.ids}
                labels={scopeLabels}
                onClearAccount={clearScopeAccount}
                onClearParent={clearScopeParent}
              />
            </div>
            {DIMENSIONS.map((dimension) => (
              <TabsContent
                key={dimension.value}
                value={dimension.value}
                className="mt-3 space-y-3"
              >
                <AdsTableToolbar
                  columns={allColumns}
                  prefs={columnPrefs}
                  onPrefsChange={handlePrefsChange}
                  selection={{
                    selectedCount: ads.selection.selected.size,
                    allMatching: ads.selection.allMatching,
                    total: ads.query.data?.total ?? 0,
                    dimension: dimension.value,
                    dataOnly: !applied.include_empty,
                    readonly: roleReadonly,
                    busy:
                      managementSubmitting ||
                      selectionMutation.isPending ||
                      !ads.snapshot,
                    onAction: (action) => startManagementAction(action),
                    onSelectAllMatching: ads.selection.selectAllMatching,
                    onClear: ads.selection.clear,
                  }}
                />
                <AdsTable
                  rows={items}
                  dimension={dimension.value}
                  columns={visibleColumns}
                  endDate={applied.end_date}
                  selected={ads.selection.selected}
                  allMatching={ads.selection.allMatching}
                  total={ads.query.data?.total ?? 0}
                  onToggle={ads.selection.toggleRow}
                  onToggleAllOnPage={(checked) =>
                    ads.selection.toggleRows(items, checked)
                  }
                  onOpen={(row) => openDetail(row, dimension.value)}
                  onDrill={navigateToChild}
                  onFilterParent={filterByParent}
                  nextCursor={ads.query.data?.next_cursor}
                  page={cursorHistory.length + 1}
                  pageSize={applied.limit}
                  // 更改条数后回到第一页，并清除与旧快照关联的游标和冻结选择。
                  onPageSizeChange={(limit) => applyScope({ limit })}
                  onNext={nextPage}
                  onPrevious={previousPage}
                  sortBy={applied.sort_by ?? "spend"}
                  sortDirection={applied.sort_direction ?? "desc"}
                  onSort={sort}
                  loading={ads.query.isFetching}
                  emptyMessage={
                    ads.query.data?.metrics_coverage?.status === "BUILDING"
                      ? "素材数据正在整理，完成后显示查询结果。"
                      : undefined
                  }
                />
              </TabsContent>
            ))}
          </Tabs>
        </CardContent>
      </Card>
      {reportExport.state.phase === "failed" && (
        <p role="alert" className="text-sm text-destructive">
          导出失败：{reportExport.state.message}
        </p>
      )}
      {actionError && (
        <p role="alert" className="text-sm text-destructive">
          操作失败：{actionError}
        </p>
      )}
      {ads.query.isError && (
        <p role="alert" className="text-sm text-destructive">
          广告报表读取失败，请刷新重试。
        </p>
      )}
      <AdsDetailSheet
        entry={detail}
        // 表格来源的行换成当前表格里的最新数据（操作回读后状态会更新）。
        tableRow={
          detail?.source === "table"
            ? detail.dimension === applied.dimension
              ? (items.find((row) => row.row_key === detail.row.row_key) ??
                detail.row)
              : detail.row
            : null
        }
        canGoBack={detailStack.length > 1}
        startDate={applied.start_date}
        endDate={applied.end_date}
        readonly={roleReadonly}
        // 表格行属于表格快照；维度已切换时旧行不再在当前快照里，不能据此冻结。
        snapshotId={
          detail?.source === "table" && detail.dimension === applied.dimension
            ? ads.snapshot?.snapshot_id
            : null
        }
        onBack={() => setDetailStack((stack) => stack.slice(0, -1))}
        onClose={closeDetail}
        onAction={(row, action, scope) => {
          // 单对象操作只冻结该行；先关闭详情，避免两个抽屉叠在一起。
          closeDetail()
          startManagementAction(action, { row, scope })
        }}
        onAdMaterialAction={(advertiserId, adId, adMaterialIds, value) => {
          closeDetail()
          startAdMaterialAction(advertiserId, adId, adMaterialIds, value)
        }}
        onOpenLinked={(target) =>
          setDetailStack((stack) => [...stack, { source: "linked", target }])
        }
      />
      {tenantId && bc?.bc_id && (
        <ManagementPreviewSheet
          key={`${previewSession}:${frozenSelection?.selection_id ?? ""}:${managementActionKey(managementAction)}`}
          tenantId={tenantId}
          bcId={bc.bc_id}
          selection={frozenSelection}
          action={managementAction}
          dimension={managementDimension}
          open={previewOpen}
          onOpenChange={setPreviewOpen}
          onSubmittingChange={setManagementSubmitting}
          onSubmitted={(task) => {
            if (scopeRef.current !== `${tenantId}:${task.bc_id}`) return
            setSubmittedTask({ ...task, submittedAt: Date.now() })
            ads.selection.clear()
            clearManagementPreview()
          }}
        />
      )}
    </div>
  )
}
