import { CircleAlert, Info } from "lucide-react"
import type { ReportRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Pager } from "@/features/tenants/shared"
import { AdsNameCell, rowDisplayName } from "./AdsNameCell"
import { type ColumnDef, formatMetric, isRecoveryOpen } from "./metricCatalog"
import { SortableHead } from "./SortableHead"
import {
  type AdsDimension,
  DIMENSIONS,
  metricOf,
  nameKeyFor,
  type ParentLink,
  rowOptimizationGoal,
  rowTargetRoas,
  unavailableReason,
} from "./search"
import { statusLabel } from "./statusLabel"

// 固定列必须是不透明背景，否则横向滚动时后面的列会透出来；
// 悬停色用 color-mix 混合出与整行 hover:bg-muted/50 一致的不透明颜色。
const STICKY_BG =
  "bg-background group-hover:bg-[color-mix(in_oklab,var(--muted)_50%,var(--background))] group-data-[state=selected]:bg-muted"
const STICKY_SELECT = `sticky left-0 z-20 w-10 min-w-10 max-w-10 ${STICKY_BG}`
const STICKY_NAME = `sticky left-10 z-20 min-w-64 max-w-80 whitespace-normal shadow-[inset_-1px_0_0_var(--border)] ${STICKY_BG}`
const HEAD = "sticky top-0 z-10 bg-background text-foreground"

const PARTIAL_TEXT = "部分账户数据未同步完整"

type ConfigColumn = {
  key: string
  label: string
  sortBy?: string
  render: (row: ReportRow) => { text: string; title?: string }
}

function budgetOf(row: ReportRow) {
  const display = row.display ?? {}
  const value =
    display.budget ?? display.campaign_budget ?? display.adgroup_budget
  return value
    ? { text: formatMetric(value, "money") }
    : { text: "—", title: "未设置预算或目录尚未同步" }
}

const TARGET_ROAS: ConfigColumn = {
  key: "target_roas",
  label: "目标 ROAS",
  sortBy: "target_roas",
  render: (row) => {
    const value = rowTargetRoas(row)
    return value
      ? { text: formatMetric(value, "multiple") }
      : { text: "—", title: "未设置目标 ROAS 或目录尚未同步" }
  },
}
const OPTIMIZATION_GOAL: ConfigColumn = {
  key: "optimization_goal",
  label: "优化目标",
  render: (row) => ({ text: rowOptimizationGoal(row) ?? "—" }),
}

/** 配置列不属于报表事实：系列看预算，广告组看预算与出价，广告看所属广告组的出价。 */
function configColumns(dimension: AdsDimension): ConfigColumn[] {
  switch (dimension) {
    case "campaign":
      return [
        {
          key: "budget",
          label: "预算",
          sortBy: "campaign_budget",
          render: budgetOf,
        },
      ]
    case "adgroup":
      return [
        {
          key: "budget",
          label: "预算",
          sortBy: "adgroup_budget",
          render: budgetOf,
        },
        TARGET_ROAS,
        OPTIMIZATION_GOAL,
      ]
    case "ad":
      return [TARGET_ROAS, OPTIMIZATION_GOAL]
    default:
      return []
  }
}

function BalanceCell({ row }: { row: ReportRow }) {
  const display = row.display ?? {}
  return (
    <TableCell
      className="text-xs"
      title={display.balance_updated_at ?? undefined}
    >
      {display.balance != null
        ? `${display.balance_currency} ${formatMetric(display.balance, "money")}${display.balance_scope === "PORTFOLIO" ? "（共享资金池）" : ""}`
        : "—"}
    </TableCell>
  )
}

function MetricValue({
  value,
  availability,
  column,
  currency,
}: {
  value: string | null
  availability: string
  column: ColumnDef
  currency?: string
}) {
  // PARTIAL 表示部分成员账户的值缺失，已同步部分照常显示，只加小标记。
  const shown =
    (availability === "AVAILABLE" || availability === "PARTIAL") &&
    value !== null
  const prefix = currency ? `${currency} ` : ""
  if (!shown)
    return (
      <span title={currency ? unavailableReason(availability) : undefined}>
        {prefix}—
      </span>
    )
  return (
    <span className="inline-flex items-center gap-1">
      {prefix}
      {formatMetric(value, column.unit)}
      {availability === "PARTIAL" && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              role="img"
              aria-label={PARTIAL_TEXT}
              className="inline-flex text-amber-500"
            >
              <CircleAlert className="size-3" aria-hidden="true" />
            </span>
          </TooltipTrigger>
          <TooltipContent>{PARTIAL_TEXT}</TooltipContent>
        </Tooltip>
      )}
    </span>
  )
}

function MetricCell({
  row,
  column,
  dimension,
}: {
  row: ReportRow
  column: ColumnDef
  dimension: AdsDimension
}) {
  const buckets = row.metric_buckets ?? []
  const reconciliation =
    column.key === "spend" &&
    dimension === "account" &&
    row.display?.reconciliation === "DIFF"
      ? `账户报表：${buckets[0]?.currency ?? ""} ${formatMetric(row.display?.account_report_spend, "money")}`
      : null
  const single = buckets.length <= 1 ? metricOf(row, column.key) : null
  const singleShown =
    single &&
    (single.availability === "AVAILABLE" ||
      single.availability === "PARTIAL") &&
    single.value !== null
  return (
    <TableCell
      data-metric={column.key}
      className="text-xs"
      title={
        single && !singleShown
          ? unavailableReason(single.availability)
          : undefined
      }
    >
      {single ? (
        <MetricValue
          value={single.value}
          availability={single.availability}
          column={column}
        />
      ) : (
        // 多币种不能合并，每个币种独立一行。
        <div className="flex flex-col">
          {buckets.map((bucket, index) => {
            const metric = metricOf(row, column.key, index)
            return (
              <MetricValue
                key={`${bucket.currency}-${bucket.timezone}`}
                value={metric.value}
                availability={metric.availability}
                column={column}
                currency={metric.currency}
              />
            )
          })}
        </div>
      )}
      {reconciliation && (
        <span
          className="ml-1 inline-flex"
          role="img"
          title={reconciliation}
          aria-label={reconciliation}
        >
          <Info className="size-3.5" aria-hidden="true" />
        </span>
      )}
    </TableCell>
  )
}

function browserTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

export function AdsTable({
  rows,
  dimension,
  columns,
  endDate,
  selected,
  allMatching,
  total,
  onToggle,
  onToggleAllOnPage,
  onOpen,
  onDrill,
  onFilterParent,
  nextCursor,
  page,
  pageSize,
  onPageSizeChange,
  onNext,
  onPrevious,
  sortBy,
  sortDirection,
  onSort,
  loading,
  emptyMessage,
}: {
  rows: ReportRow[]
  dimension: AdsDimension
  columns: ColumnDef[]
  endDate: string
  selected: Set<string>
  allMatching: boolean
  total: number
  onToggle: (row: ReportRow, checked: boolean) => void
  onToggleAllOnPage: (checked: boolean) => void
  onOpen: (row: ReportRow) => void
  onDrill: (row: ReportRow) => void
  onFilterParent: (link: ParentLink, advertiserId: string) => void
  nextCursor?: string | null
  page: number
  pageSize: number
  onPageSizeChange: (limit: number) => void
  onNext: () => void
  onPrevious: () => void
  sortBy: string
  sortDirection: "asc" | "desc"
  onSort: (sortBy: string) => void
  loading: boolean
  emptyMessage?: string
}) {
  const config = configColumns(dimension)
  const pageSelected =
    rows.length > 0 && rows.every((row) => selected.has(row.row_key))
  // 回收窗口按各账户本地日判断；当前页没有单一时区可用时退回浏览器时区。
  const zones = [
    ...new Set(
      rows.flatMap((row) =>
        (row.metric_buckets ?? [])
          .map((bucket) => bucket.timezone)
          .filter((zone): zone is string => Boolean(zone) && zone !== "MIXED"),
      ),
    ),
  ]
  const timezones = zones.length ? zones : [browserTimezone()]
  const dimensionLabel =
    DIMENSIONS.find((item) => item.value === dimension)?.label ?? "名称"
  return (
    <Card className="gap-0 py-0 lg:gap-0 lg:py-0">
      <CardContent className="p-0" aria-busy={loading}>
        {allMatching && (
          <div className="border-b bg-muted/40 px-4 py-2 text-sm">
            已选择全部 {total} 条匹配结果，可取消单行。
          </div>
        )}
        <div
          data-slot="table-container"
          className="max-h-[calc(100vh-12rem)] min-h-[28rem] overflow-auto"
        >
          <table data-slot="table" className="w-full caption-bottom text-sm">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead
                  data-sticky="select"
                  className={`${HEAD} left-0 z-30 w-10 min-w-10 max-w-10`}
                >
                  <Checkbox
                    checked={pageSelected}
                    onCheckedChange={(checked) =>
                      onToggleAllOnPage(checked === true)
                    }
                    aria-label="选择当前页"
                  />
                </TableHead>
                <TableHead
                  data-sticky="name"
                  className={`${HEAD} left-10 z-30 min-w-64 max-w-80 shadow-[inset_-1px_0_0_var(--border)]`}
                >
                  <SortableHead
                    label={dimensionLabel}
                    sortBy={nameKeyFor(dimension)}
                    activeSortBy={sortBy}
                    direction={sortDirection}
                    onSort={onSort}
                  />
                </TableHead>
                <TableHead className={HEAD}>状态</TableHead>
                {dimension === "account" && (
                  <TableHead className={HEAD}>
                    <SortableHead
                      label="余额"
                      sortBy="balance"
                      activeSortBy={sortBy}
                      direction={sortDirection}
                      onSort={onSort}
                    />
                  </TableHead>
                )}
                {config.map((column) => (
                  <TableHead key={column.key} className={HEAD}>
                    {column.sortBy ? (
                      <SortableHead
                        label={column.label}
                        sortBy={column.sortBy}
                        activeSortBy={sortBy}
                        direction={sortDirection}
                        onSort={onSort}
                      />
                    ) : (
                      column.label
                    )}
                  </TableHead>
                ))}
                {columns.map((column) => {
                  // 第 N 天指标在窗口结束前仍会增长：表头标灰提示，数值照常显示。
                  const open =
                    column.recoveryDay !== null &&
                    isRecoveryOpen(column.recoveryDay, endDate, timezones)
                  return (
                    <TableHead
                      key={column.key}
                      data-metric={column.key}
                      data-recovery-open={open ? "true" : undefined}
                      className={`${HEAD} ${open ? "text-muted-foreground" : ""}`}
                    >
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="inline-flex">
                            <SortableHead
                              label={column.label}
                              sortBy={column.key}
                              activeSortBy={sortBy}
                              direction={sortDirection}
                              onSort={onSort}
                            />
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>
                          <p>{column.fullName}</p>
                          {open && <p>回收窗口未结束，数值仍会增长</p>}
                        </TooltipContent>
                      </Tooltip>
                    </TableHead>
                  )
                })}
              </TableRow>
            </TableHeader>
            <TableBody>
              {/* 空状态属于列表内容，紧跟表头；不能放在有最小高度的滚动区外。 */}
              {!rows.length && (
                <TableRow className="hover:bg-transparent">
                  <TableCell
                    colSpan={
                      3 +
                      (dimension === "account" ? 1 : 0) +
                      config.length +
                      columns.length
                    }
                    className="h-32 px-8 text-muted-foreground"
                  >
                    <span role="status">
                      {loading
                        ? "正在加载当前报表…"
                        : (emptyMessage ?? "没有匹配的广告")}
                    </span>
                  </TableCell>
                </TableRow>
              )}
              {rows.map((row) => {
                const checked = selected.has(row.row_key)
                const status = statusLabel(row.display ?? {})
                return (
                  <TableRow
                    key={row.row_key}
                    className="group"
                    data-state={checked ? "selected" : undefined}
                  >
                    <TableCell data-sticky="select" className={STICKY_SELECT}>
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(value) =>
                          onToggle(row, value === true)
                        }
                        aria-label={`选择 ${rowDisplayName(row, dimension)}`}
                      />
                    </TableCell>
                    <TableCell data-sticky="name" className={STICKY_NAME}>
                      <AdsNameCell
                        row={row}
                        dimension={dimension}
                        onOpen={onOpen}
                        onDrill={onDrill}
                        onFilterParent={onFilterParent}
                      />
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={status.tone}
                        className="text-[10px]"
                        title={status.raw}
                      >
                        {status.label}
                      </Badge>
                    </TableCell>
                    {dimension === "account" && <BalanceCell row={row} />}
                    {config.map((column) => {
                      const cell = column.render(row)
                      return (
                        <TableCell
                          key={column.key}
                          className="text-xs"
                          title={cell.title}
                        >
                          {cell.text}
                        </TableCell>
                      )
                    })}
                    {columns.map((column) => (
                      <MetricCell
                        key={column.key}
                        row={row}
                        column={column}
                        dimension={dimension}
                      />
                    ))}
                  </TableRow>
                )
              })}
            </TableBody>
          </table>
        </div>
        <div className="border-t px-4 py-3">
          <Pager
            paging={{
              page,
              limit: pageSize,
              setLimit: onPageSizeChange,
              previous: onPrevious,
              next: onNext,
            }}
            total={total}
            nextCursor={nextCursor}
            busy={loading}
          />
        </div>
      </CardContent>
    </Card>
  )
}
