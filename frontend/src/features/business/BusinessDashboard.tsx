import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { Download, MoreHorizontal, RefreshCw, Settings } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  type BusinessRow,
  BusinessService,
  type DashboardFilter,
  type DashboardPublic,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { SortableHead } from "@/features/ads/SortableHead"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { canManage, isForbidden, ServerTable } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { BusinessNotes, MetricHint } from "./BusinessHelp"
import { EvidenceEditor } from "./EvidenceEditor"
import { MappingEditor } from "./MappingEditor"
import {
  type BusinessOptions,
  businessKey,
  formatValue,
  optionsQuery,
  shiftedDay,
} from "./queries"
import { RateSettings } from "./RateSettings"

const MONEY_COLUMNS = [
  ["spend", "消耗"],
  ["revenue", "平台收入"],
  ["platform_roas", "平台 ROAS"],
  ["provider_share", "版权方分成"],
  ["net_revenue", "分成后收入"],
  ["rebate", "其他返点"],
  ["business_revenue", "经营收入"],
  ["business_roas", "经营 ROAS"],
  ["compensation", "预计赔付"],
  ["subsidy_comic", "新开漫剧预计"],
  ["subsidy_other", "新开其他剧预计"],
  ["subsidy_existing", "在投闭环预计"],
] as const
const SCENARIOS = {
  base: "基础经营",
  comic: "含预计：新开漫剧",
  other: "含预计：新开其他剧",
  existing: "含预计：在投闭环",
}

function Picker({
  label,
  value,
  choices,
  onChange,
}: {
  label: string
  value: string
  choices: { id: string; name: string }[]
  onChange: (value: string) => void
}) {
  return (
    <Field className="w-full sm:w-32">
      <FieldLabel className="text-xs">{label}</FieldLabel>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger aria-label={label}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {choices.map((c) => (
              <SelectItem key={c.id} value={c.id}>
                {c.name}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </Field>
  )
}

export function BusinessDashboard() {
  const { tenantId, bc, scope, user } = useTenantScope()
  const options = useQuery({
    ...optionsQuery(tenantId ?? "", bc?.bc_id ?? ""),
    queryKey: [...businessKey(tenantId ?? ""), "options", bc?.bc_id, user.id],
    enabled: !!tenantId && !!bc && canManage(scope?.role),
  })
  // 角色变化时立即隐藏缓存金额，直接访问地址也不发起经营查询。
  if (!canManage(scope?.role))
    return (
      <Alert variant="destructive">
        <AlertTitle>无权访问经营看板</AlertTitle>
        <AlertDescription>仅管理员可以查看经营报表。</AlertDescription>
      </Alert>
    )
  if (!tenantId || !bc)
    return (
      <WorkspaceEmpty
        title="经营看板"
        description="按剧目、广告系列与版权方查看完整日经营表现。"
        tenantId={tenantId}
      />
    )
  if (options.isError)
    return (
      <Alert variant="destructive">
        <AlertTitle>经营配置读取失败</AlertTitle>
        <AlertDescription>
          <Button variant="outline" onClick={() => void options.refetch()}>
            重试
          </Button>
        </AlertDescription>
      </Alert>
    )
  if (!options.data) return <Skeleton className="h-64 w-full" />
  if (!options.data.accounts.length)
    return (
      <Card>
        <CardHeader>
          <CardTitle>当前 BC 暂无可读取的广告账户</CardTitle>
          <CardDescription>
            请先到账户与授权页面同步账户，完成授权后即可生成经营看板。
          </CardDescription>
        </CardHeader>
      </Card>
    )
  return (
    <Dashboard
      key={`${tenantId}:${bc.bc_id}:${user.id}`}
      tenantId={tenantId}
      bcId={bc.bc_id}
      bcName={bc.name ?? bc.bc_id}
      actorId={user.id}
      manage={canManage(scope?.role)}
      options={options.data}
    />
  )
}

function Dashboard({
  tenantId,
  bcId,
  bcName,
  actorId,
  manage,
  options,
}: {
  tenantId: string
  bcId: string
  bcName: string
  actorId: string
  manage: boolean
  options: BusinessOptions
}) {
  const client = useQueryClient()
  const [filters, setFilters] = useState<DashboardFilter>({
    bc_id: bcId,
    start_date: shiftedDay(options.end_date, -6),
    end_date: options.end_date,
    dimension: "drama",
    scenario: "base",
    currency: options.accounts[0]?.currency ?? "USD",
    offset: 0,
    limit: 50,
    sort_by: "spend",
    descending: true,
  })
  const [settings, setSettings] = useState(false)
  const [mapping, setMapping] = useState<BusinessRow | null>(null)
  const [evidence, setEvidence] = useState<BusinessRow | null>(null)
  const [detail, setDetail] = useState<BusinessRow | null>(null)
  const [exportId, setExportId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const initialised = useRef(new Set<string>())
  const selectedEnd =
    filters.advertiser_ids?.length === 1
      ? (options.accounts.find((a) => a.id === filters.advertiser_ids?.[0])
          ?.end_date ?? options.end_date)
      : options.end_date
  const valid =
    !!filters.start_date &&
    !!filters.end_date &&
    filters.start_date <= filters.end_date &&
    filters.end_date <= selectedEnd
  const query = useQuery({
    queryKey: [...businessKey(tenantId), "query", actorId, filters],
    enabled: valid,
    queryFn: async ({ signal }) =>
      (
        await BusinessService.dashboard({
          path: { tenant_id: tenantId },
          body: filters,
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data?.pending || !q.state.data?.run_ids.length ? 3000 : 60_000,
  })
  const data = isForbidden(query.error) ? undefined : query.data
  const update = (patch: Partial<DashboardFilter>) => {
    setDetail(null)
    setFilters((f) => ({ ...f, ...patch, offset: 0, run_ids: undefined }))
  }
  const rebuild = useCallback(async () => {
    if (!valid || busy) return
    setBusy(true)
    setError("")
    try {
      await BusinessService.rebuild({
        path: { tenant_id: tenantId },
        body: {
          bc_id: bcId,
          start_date: filters.start_date,
          end_date: filters.end_date,
        },
      })
      setFilters((f) => ({ ...f, run_ids: undefined }))
      await client.invalidateQueries({ queryKey: businessKey(tenantId) })
    } catch {
      setError("核算任务受理失败，请检查账户授权后重试。")
    } finally {
      setBusy(false)
    }
  }, [
    valid,
    busy,
    tenantId,
    bcId,
    filters.start_date,
    filters.end_date,
    client,
  ])
  useEffect(() => {
    const key = `${filters.start_date}:${filters.end_date}`
    if (
      data &&
      !data.run_ids.length &&
      valid &&
      !initialised.current.has(key)
    ) {
      initialised.current.add(key)
      void rebuild()
    }
  }, [data, filters.start_date, filters.end_date, valid, rebuild])

  const exportQuery = useQuery({
    queryKey: [...businessKey(tenantId), "export", bcId, actorId, exportId],
    enabled: !!exportId,
    queryFn: async ({ signal }) =>
      (
        await BusinessService.exportStatus({
          path: { tenant_id: tenantId, export_id: exportId! },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data?.status === "COMPLETE" || q.state.data?.status === "FAILED"
        ? false
        : 1000,
  })
  useEffect(() => {
    if (exportQuery.isError) {
      setExportId(null)
      setError("导出状态读取失败，请检查权限后重试。")
    }
  }, [exportQuery.isError])
  useEffect(() => {
    if (exportQuery.data?.status === "FAILED") {
      setError("导出失败，可能是账户授权或数据范围发生变化，请重试。")
      setExportId(null)
      return
    }
    if (exportQuery.data?.status !== "COMPLETE" || !exportId) return
    const controller = new AbortController()
    void BusinessService.download({
      path: { tenant_id: tenantId, export_id: exportId },
      responseType: "blob",
      signal: controller.signal,
    })
      .then((response) => {
        if (controller.signal.aborted) return
        const url = URL.createObjectURL(
          response.data instanceof Blob
            ? response.data
            : new Blob([String(response.data)]),
        )
        const anchor = document.createElement("a")
        anchor.href = url
        anchor.download = "business.csv"
        anchor.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
        setExportId(null)
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setError("导出下载失败，请重试。")
          setExportId(null)
        }
      })
    return () => controller.abort()
  }, [exportQuery.data?.status, exportId, tenantId])
  const startExport = async () => {
    if (!data?.run_ids.length || exportId || busy) return
    setBusy(true)
    setError("")
    try {
      setExportId(
        (
          await BusinessService.export({
            path: { tenant_id: tenantId },
            body: { ...filters, run_ids: data.run_ids },
          })
        ).data.id,
      )
    } catch {
      setError("导出任务受理失败，请刷新数据后重试。")
    } finally {
      setBusy(false)
    }
  }
  const sort = useCallback((key: string) => {
    setFilters((f) => ({
      ...f,
      sort_by: key as DashboardFilter["sort_by"],
      descending: f.sort_by === key ? !f.descending : true,
      offset: 0,
    }))
  }, [])
  const heading = useCallback(
    (label: string, key: string, metric = false) => (
      <div className="flex items-center gap-1">
        <SortableHead
          label={label}
          sortBy={key}
          activeSortBy={filters.sort_by}
          direction={filters.descending ? "desc" : "asc"}
          onSort={sort}
        />
        {metric && <MetricHint label={label} metric={key} />}
      </div>
    ),
    [filters.sort_by, filters.descending, sort],
  )
  const columns = useMemo<ColumnDef<BusinessRow>[]>(
    () => [
      {
        id: "name",
        header: () =>
          heading(
            filters.dimension === "campaign"
              ? "广告系列"
              : filters.dimension === "provider"
                ? "版权方"
                : "剧目",
            "name",
          ),
        cell: ({ row: { original: r } }) => (
          <div className="grid min-w-48 max-w-72 grid-cols-[1fr_auto] items-center gap-x-1">
            <Button
              variant="link"
              className="h-auto justify-start p-0 whitespace-normal text-left"
              onClick={() => setDetail(r)}
            >
              {r.name}
            </Button>
            {filters.dimension !== "provider" && (
              <span className="col-start-1 text-xs text-muted-foreground">
                {r.provider_name}
              </span>
            )}
            {manage && filters.dimension === "campaign" && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`${r.name}操作`}
                    className="col-start-2 row-start-1"
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuGroup>
                    <DropdownMenuItem onSelect={() => setMapping(r)}>
                      设置归属
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => setEvidence(r)}>
                      赔付依据
                    </DropdownMenuItem>
                  </DropdownMenuGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        ),
      },
      ...MONEY_COLUMNS.map(([key, title]) => ({
        id: key,
        header: () => heading(title, key, true),
        cell: ({
          row: { original: r },
        }: {
          row: { original: BusinessRow }
        }) => (
          <span className="whitespace-nowrap tabular-nums">
            {formatValue(r.values[key], key.endsWith("roas"))}
          </span>
        ),
      })),
    ],
    [filters.dimension, manage, heading],
  )
  const totals = data?.totals ?? {}
  const page = Math.floor((filters.offset ?? 0) / (filters.limit ?? 50)) + 1
  const currencies = [
    ...new Set([
      ...options.accounts.map((a) => a.currency),
      ...(data?.currencies ?? []),
    ]),
  ]
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-col gap-1">
          <WorkspacePageTitle>经营看板</WorkspacePageTitle>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {(data?.pending || busy) && (
            <span role="status" className="text-xs text-muted-foreground">
              核算中…
            </span>
          )}
          <BusinessNotes
            issues={data?.issues ?? []}
            pending={!!data?.pending || busy}
          />
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !!exportId || !data?.total || query.isFetching}
            onClick={() => void startExport()}
          >
            <Download data-icon="inline-start" />
            {exportId ? "正在导出…" : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !valid}
            onClick={() => void rebuild()}
          >
            <RefreshCw data-icon="inline-start" />
            重新核算
          </Button>
          {manage && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setSettings(true)}
            >
              <Settings data-icon="inline-start" />
              经营设置
            </Button>
          )}
        </div>
      </div>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <Card className="gap-0 py-3 lg:py-3">
        <CardContent className="flex flex-wrap items-end gap-2 px-4 lg:px-4">
          <Field className="w-full sm:w-36">
            <FieldLabel htmlFor="business-start">开始日期</FieldLabel>
            <Input
              id="business-start"
              type="date"
              value={filters.start_date}
              max={filters.end_date}
              onChange={(e) => update({ start_date: e.target.value })}
            />
          </Field>
          <Field className="w-full sm:w-36">
            <FieldLabel htmlFor="business-end">结束日期</FieldLabel>
            <Input
              id="business-end"
              type="date"
              value={filters.end_date}
              max={selectedEnd}
              onChange={(e) => update({ end_date: e.target.value })}
            />
          </Field>
          <Picker
            label="版权方筛选"
            value={filters.provider_id ?? "all"}
            choices={[{ id: "all", name: "全部版权方" }, ...options.providers]}
            onChange={(id) =>
              update({ provider_id: id === "all" ? undefined : id })
            }
          />
          <Picker
            label="账户"
            value={filters.advertiser_ids?.[0] ?? "all"}
            choices={[{ id: "all", name: "全部账户" }, ...options.accounts]}
            onChange={(id) =>
              update({ advertiser_ids: id === "all" ? [] : [id] })
            }
          />
          <Picker
            label="币种"
            value={filters.currency ?? "USD"}
            choices={currencies.map((c) => ({ id: c, name: c }))}
            onChange={(currency) => update({ currency })}
          />
          <Picker
            label="经营口径"
            value={filters.scenario ?? "base"}
            choices={Object.entries(SCENARIOS).map(([id, name]) => ({
              id,
              name,
            }))}
            onChange={(scenario) =>
              update({ scenario: scenario as DashboardFilter["scenario"] })
            }
          />
        </CardContent>
      </Card>
      {!valid && (
        <Alert variant="destructive">
          <AlertDescription>
            请选择有效的完整日日期范围，结束日期不能晚于{selectedEnd}。
          </AlertDescription>
        </Alert>
      )}
      <div
        data-testid="business-cards"
        className="grid min-w-0 grid-cols-2 gap-2 lg:grid-cols-5"
      >
        {[
          ["平台消耗", "spend"],
          ["平台收入", "revenue"],
          ["平台 ROAS", "platform_roas"],
          ["经营收入", "scenario_revenue"],
          ["经营 ROAS", "scenario_roas"],
        ].map(([label, key]) => (
          <Card key={key} className="gap-1 py-3 lg:gap-1 lg:py-3">
            <CardHeader className="px-3 lg:px-3">
              <CardDescription className="flex items-center gap-1">
                {label}
                <MetricHint
                  metric={key}
                  label={label}
                  note={
                    data?.missing_dates?.length
                      ? "当前存在日报缺口，展示已核算小计；详见数据说明。"
                      : undefined
                  }
                />
              </CardDescription>
            </CardHeader>
            <CardContent className="px-3 lg:px-3">
              <p className="text-xl font-semibold tabular-nums">
                {formatValue(totals[key], key.endsWith("roas"))}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card
        className="min-w-0 gap-3 py-3 lg:gap-3 lg:py-3"
        data-testid="business-detail-table"
      >
        <CardHeader className="gap-1 px-4 lg:px-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>经营明细</CardTitle>
            <Field className="w-full sm:w-48">
              <FieldLabel htmlFor="business-search" className="sr-only">
                名称搜索
              </FieldLabel>
              <Input
                id="business-search"
                placeholder="剧名、系列名或版权方"
                value={filters.query ?? ""}
                onChange={(e) => update({ query: e.target.value })}
              />
            </Field>
            <ToggleGroup
              type="single"
              variant="outline"
              value={filters.dimension}
              aria-label="查看维度"
              onValueChange={(dimension) => {
                if (dimension)
                  update({
                    dimension: dimension as DashboardFilter["dimension"],
                  })
              }}
            >
              <ToggleGroupItem value="drama">剧目</ToggleGroupItem>
              <ToggleGroupItem value="campaign">广告系列</ToggleGroupItem>
              <ToggleGroupItem value="provider">版权方</ToggleGroupItem>
            </ToggleGroup>
          </div>
        </CardHeader>
        <CardContent className="min-w-0">
          <ServerTable
            rows={valid ? (data?.rows ?? []) : []}
            columns={columns}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={!!filters.query || !!filters.provider_id}
            emptyTitle="暂无已核算经营数据"
          />
        </CardContent>
        <CardFooter className="flex flex-wrap justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            截止 {data?.end_date ?? filters.end_date} ·{" "}
            {data?.updated_at
              ? `更新于 ${new Date(data.updated_at).toLocaleString()}`
              : "等待首次核算"}
          </p>
          <div className="flex items-center gap-2">
            <Badge variant="outline">
              共 {data?.total ?? 0} 项 · 第 {page} 页
            </Badge>
            <Button
              variant="outline"
              size="sm"
              disabled={!filters.offset || query.isFetching}
              onClick={() =>
                setFilters((f) => ({
                  ...f,
                  offset: Math.max(0, (f.offset ?? 0) - (f.limit ?? 50)),
                  run_ids: data?.run_ids,
                }))
              }
            >
              上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={
                !data ||
                (filters.offset ?? 0) + (filters.limit ?? 50) >= data.total ||
                query.isFetching
              }
              onClick={() =>
                setFilters((f) => ({
                  ...f,
                  offset: (f.offset ?? 0) + (f.limit ?? 50),
                  run_ids: data?.run_ids,
                }))
              }
            >
              下一页
            </Button>
          </div>
        </CardFooter>
      </Card>
      {settings && (
        <RateSettings
          tenantId={tenantId}
          kind="rebate"
          scopeId={bcId}
          name={bcName}
          onClose={() => setSettings(false)}
        />
      )}
      {mapping && (
        <MappingEditor
          tenantId={tenantId}
          bcId={bcId}
          row={mapping}
          onClose={() => setMapping(null)}
        />
      )}
      {evidence && (
        <EvidenceEditor
          tenantId={tenantId}
          bcId={bcId}
          row={evidence}
          endDate={filters.end_date}
          onClose={() => setEvidence(null)}
        />
      )}
      {detail && data && (
        <BusinessDetail
          tenantId={tenantId}
          row={detail}
          filters={{ ...filters, run_ids: data.run_ids }}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  )
}

function BusinessDetail({
  tenantId,
  row,
  filters,
  onClose,
}: {
  tenantId: string
  row: BusinessRow
  filters: DashboardFilter
  onClose: () => void
}) {
  const [offset, setOffset] = useState(0)
  const body = {
    ...filters,
    dimension: "day" as const,
    offset,
    limit: 100,
    sort_by: "name" as const,
    descending: false,
    ...(filters.dimension === "campaign"
      ? { advertiser_ids: [row.advertiser_id!], campaign_id: row.campaign_id! }
      : filters.dimension === "drama"
        ? { drama_key: row.key }
        : row.provider_id
          ? { provider_id: row.provider_id }
          : { unmapped_only: true }),
  }
  const query = useQuery<DashboardPublic>({
    queryKey: [...businessKey(tenantId), "detail", body],
    queryFn: async ({ signal }) =>
      (
        await BusinessService.dashboard({
          path: { tenant_id: tenantId },
          body,
          signal,
        })
      ).data,
  })
  const columns: ColumnDef<BusinessRow>[] = [
    { header: "当地日期", accessorKey: "name" },
    ...MONEY_COLUMNS.map(([key, title]) => ({
      id: key,
      header: () => (
        <span className="inline-flex items-center gap-1">
          {title}
          <MetricHint label={title} metric={key} />
        </span>
      ),
      cell: ({ row: { original: r } }: { row: { original: BusinessRow } }) =>
        formatValue(r.values[key], key.endsWith("roas")),
    })),
  ]
  return (
    <ManagementSheet
      title={row.name}
      description="所选期间的每日经营明细"
      dirty={false}
      wide
      onClose={onClose}
    >
      <ServerTable
        rows={query.data?.rows ?? []}
        columns={columns}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="暂无每日数据"
      />
      <div className="mt-3 flex items-center justify-end gap-2">
        <span>共 {query.data?.total ?? 0} 天</span>
        <Button
          variant="outline"
          size="sm"
          disabled={!offset || query.isFetching}
          onClick={() => setOffset(Math.max(0, offset - 100))}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={
            !query.data || offset + 100 >= query.data.total || query.isFetching
          }
          onClick={() => setOffset(offset + 100)}
        >
          下一页
        </Button>
      </div>
    </ManagementSheet>
  )
}
