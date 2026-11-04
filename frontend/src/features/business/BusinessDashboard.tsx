import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { Download, RefreshCw, Settings } from "lucide-react"
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
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { canManage, isForbidden, ServerTable } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { EvidenceEditor } from "./EvidenceEditor"
import { MappingEditor } from "./MappingEditor"
import {
  type BusinessOptions,
  businessKey,
  formatValue,
  ISSUE_LABELS,
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
    <Field className="w-full sm:w-44">
      <FieldLabel>{label}</FieldLabel>
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
    enabled: !!tenantId && !!bc,
  })
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
  const columns = useMemo<ColumnDef<BusinessRow>[]>(
    () => [
      {
        header:
          filters.dimension === "campaign"
            ? "广告系列"
            : filters.dimension === "provider"
              ? "版权方"
              : "剧目",
        cell: ({ row: { original: r } }) => (
          <div className="flex min-w-48 max-w-72 flex-col gap-1">
            <Button
              variant="link"
              className="h-auto justify-start p-0 whitespace-normal text-left"
              onClick={() => setDetail(r)}
            >
              {r.name}
            </Button>
            {filters.dimension !== "provider" && (
              <span className="text-xs text-muted-foreground">
                {r.provider_name}
              </span>
            )}
            {manage && filters.dimension === "campaign" && (
              <Button variant="ghost" size="sm" onClick={() => setMapping(r)}>
                设置归属
              </Button>
            )}
            {manage && filters.dimension === "campaign" && (
              <Button variant="ghost" size="sm" onClick={() => setEvidence(r)}>
                赔付依据
              </Button>
            )}
          </div>
        ),
      },
      ...MONEY_COLUMNS.map(([key, title]) => ({
        header: title,
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
    [filters.dimension, manage],
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
    <>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <WorkspacePageTitle>经营看板</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            {bcName} · T+1完整日经营数据
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={busy || !!exportId || !data?.total || query.isFetching}
            onClick={() => void startExport()}
          >
            <Download data-icon="inline-start" />
            {exportId ? "正在导出…" : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            disabled={busy || !valid}
            onClick={() => void rebuild()}
          >
            <RefreshCw data-icon="inline-start" />
            重新核算
          </Button>
          {manage && (
            <Button variant="outline" onClick={() => setSettings(true)}>
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
      <Card>
        <CardHeader>
          <CardTitle>查看范围</CardTitle>
          <CardDescription>
            按账户当地日期统计，不包含当天；返点按消耗计算，版权方抽成按平台收入计算。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <Field className="w-full sm:w-40">
            <FieldLabel htmlFor="business-start">开始日期</FieldLabel>
            <Input
              id="business-start"
              type="date"
              value={filters.start_date}
              max={filters.end_date}
              onChange={(e) => update({ start_date: e.target.value })}
            />
          </Field>
          <Field className="w-full sm:w-40">
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
          <Field className="w-full sm:w-56">
            <FieldLabel htmlFor="business-search">名称搜索</FieldLabel>
            <Input
              id="business-search"
              placeholder="剧名、系列名或版权方"
              value={filters.query ?? ""}
              onChange={(e) => update({ query: e.target.value })}
            />
          </Field>
        </CardContent>
      </Card>
      {!valid && (
        <Alert variant="destructive">
          <AlertDescription>
            请选择有效的完整日日期范围，结束日期不能晚于{selectedEnd}。
          </AlertDescription>
        </Alert>
      )}
      {(data?.pending || busy) && (
        <Alert>
          <AlertTitle>后台正在核算</AlertTitle>
          <AlertDescription>
            最近区间优先生成，历史分批补齐。已有结果继续可查，完成后点击刷新查看。
          </AlertDescription>
        </Alert>
      )}
      {!!data?.issues?.length && (
        <Alert>
          <AlertTitle>数据说明</AlertTitle>
          <AlertDescription className="flex flex-col gap-1">
            {data.issues?.map((issue) => (
              <p key={issue}>{ISSUE_LABELS[issue] ?? issue}</p>
            ))}
          </AlertDescription>
        </Alert>
      )}
      <div
        data-testid="business-cards"
        className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5"
      >
        {[
          ["平台消耗", "spend"],
          ["平台收入", "revenue"],
          ["平台 ROAS", "platform_roas"],
          ["经营收入", "scenario_revenue"],
          ["经营 ROAS", "scenario_roas"],
        ].map(([label, key]) => (
          <Card key={key}>
            <CardHeader>
              <CardDescription>{label}</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold tabular-nums">
                {formatValue(totals[key], key.endsWith("roas"))}
              </p>
            </CardContent>
            <CardFooter>
              <span className="text-xs text-muted-foreground">
                {key.endsWith("roas")
                  ? "合计收入 ÷ 合计消耗"
                  : filters.currency}
                {data?.missing_dates?.length ? " · 已核算小计" : ""}
              </span>
            </CardFooter>
          </Card>
        ))}
      </div>
      <Card className="min-w-0">
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>经营明细</CardTitle>
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
          <CardDescription>
            三种新剧补贴为备选情景，不相加。列表展示所选期间的预计补贴增量；系列为按季度消耗占比分摊的预估金额。
          </CardDescription>
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
    </>
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
    ...(filters.dimension === "campaign" ||
    (filters.dimension === "drama" && !row.drama_id)
      ? { advertiser_ids: [row.advertiser_id!], campaign_id: row.campaign_id! }
      : filters.dimension === "drama"
        ? { drama_id: row.drama_id }
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
      header: title,
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
