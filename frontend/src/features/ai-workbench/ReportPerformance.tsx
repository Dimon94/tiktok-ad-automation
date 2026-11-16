import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export type Performance = {
  captured_at?: string
  historical_reanalysis: boolean
  age_hours?: number | null
  windows: Array<{
    horizon: string
    coverage: {
      requested_accounts?: number
      available_accounts?: number
      available_accounts_percent?: string
    }
    partitions: Array<{
      currency: string
      timezone: string
      attribution?: string
      start_date: string
      end_date: string
      spend: string | null
      revenue: string | null
      roas: string | null
      revenue_minus_spend: string | null
    }>
  }>
  timelines: Array<{
    dimension: string
    currency: string
    timezone: string
    accounts?: number
    start_date: string
    end_date: string
    balanced_accounts?: number
    daily: Day[]
    balanced_daily: Day[]
  }>
}
type Day = {
  date: string
  spend: string | null
  revenue_total: string | null
  roas: string | null
  complete_accounts?: number
  observed_accounts?: number
}
const amount = (value: string | null | undefined, digits = 2) =>
  value == null
    ? "暂无数据"
    : Number(value).toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
const horizonLabel: Record<string, string> = {
  d0: "当天收入观察",
  d3: "3 天收入观察",
  d7: "7 天收入观察",
}
const levelLabel: Record<string, string> = {
  campaign: "推广系列",
  material: "素材",
}
function change(current: string | null, previous: string | null) {
  if (current == null || previous == null || Number(previous) === 0) return "—"
  const value = (Number(current) / Number(previous) - 1) * 100
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`
}

/** 全范围卡片与逐日表各自标注维度、窗口，绝不跨口径求提升或合并币种。 */
export function ReportPerformance({
  performance,
}: {
  performance?: Performance
}) {
  const [windowIndex, setWindowIndex] = useState("0")
  const [partitionIndex, setPartitionIndex] = useState("0")
  const [timelineIndex, setTimelineIndex] = useState("0")
  const [panel, setPanel] = useState("fixed")
  const window = performance?.windows[Number(windowIndex)]
  const partition = window?.partitions[Number(partitionIndex)]
  const timeline = performance?.timelines[Number(timelineIndex)]
  const daily = panel === "fixed" ? timeline?.balanced_daily : timeline?.daily
  return (
    <section className="flex flex-col gap-5" aria-label="消耗与回收表现">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-semibold">01　消耗与回收表现</h3>
        {performance?.historical_reanalysis && (
          <Badge variant="outline">历史数据重分析</Badge>
        )}
      </div>
      <div className="rounded-md border bg-muted/30 p-3 text-sm leading-6">
        {performance?.captured_at ? (
          <>
            数据冻结于{" "}
            {new Date(performance.captured_at).toLocaleString("zh-CN", {
              hour12: false,
            })}
            。
            {performance.historical_reanalysis ||
            (performance.age_hours ?? 0) > 24
              ? "这是一份历史窗口报告，不能代表当前实时表现。"
              : "按账户当地完整日复盘，不包含今天尚未完成的投放。"}
          </>
        ) : (
          "这份历史报告没有归档可供展示的指标概览；请查看原始分析或发起新复盘。"
        )}
      </div>
      {performance?.windows.length ? (
        <>
          <div className="flex flex-wrap gap-3">
            <Select
              value={windowIndex}
              onValueChange={(value) => {
                setWindowIndex(value)
                setPartitionIndex("0")
              }}
            >
              <SelectTrigger aria-label="收入观察窗口" className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {performance.windows.map((w, i) => (
                  <SelectItem key={w.horizon} value={String(i)}>
                    {horizonLabel[w.horizon] || w.horizon}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!!window?.partitions.length && (
              <Select value={partitionIndex} onValueChange={setPartitionIndex}>
                <SelectTrigger aria-label="报表币种与时区" className="w-64">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {window.partitions.map((p, i) => (
                    <SelectItem key={i} value={String(i)}>
                      {p.currency} · {p.timezone} ·{" "}
                      {p.attribution || "默认归因"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          {partition ? (
            <>
              <p className="text-sm text-muted-foreground">
                {partition.start_date} 至 {partition.end_date} · 账户汇总 ·{" "}
                {partition.currency} · {partition.timezone}
              </p>
              <div
                className="grid grid-cols-2 gap-3 lg:grid-cols-4"
                data-testid="report-metrics"
              >
                {[
                  ["投放消耗", amount(partition.spend)],
                  ["回收比 ROAS", amount(partition.roas, 3)],
                  ["已归因收入", amount(partition.revenue)],
                  ["收入减消耗", amount(partition.revenue_minus_spend)],
                ].map(([label, value]) => (
                  <div key={label} className="rounded-lg border p-4">
                    <div className="text-xs text-muted-foreground">{label}</div>
                    <strong className="mt-2 block text-xl tabular-nums">
                      {value}
                    </strong>
                  </div>
                ))}
              </div>
              <p className="text-xs leading-6 text-muted-foreground">
                ROAS＝收入÷消耗；收入减消耗为平台金额差，未计分成、返点及补贴，不是经营净利润。各观察窗口日期不同，不能作为同一批对象的增长趋势。
              </p>
            </>
          ) : (
            <p className="text-sm">
              该窗口没有合格的金额数据，未将缺失记为零。
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            可用账户 {window?.coverage.available_accounts ?? "—"} /{" "}
            {window?.coverage.requested_accounts ?? "—"}，覆盖{" "}
            {window?.coverage.available_accounts_percent == null
              ? "—"
              : `${amount(window.coverage.available_accounts_percent, 1)}%`}
            。缺失或过期数据已排除；该覆盖为整个观察窗口，不代表单个分区。
          </p>
        </>
      ) : null}
      {!!performance?.timelines.length && (
        <section
          className="flex flex-col gap-3"
          aria-label="近期逐日消耗与ROAS"
        >
          <h4 className="font-medium">最近完整日：消耗和 ROAS 怎样变化</h4>
          <div className="flex flex-wrap gap-3">
            <Select value={timelineIndex} onValueChange={setTimelineIndex}>
              <SelectTrigger aria-label="逐日分析对象" className="w-72">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {performance.timelines.map((t, i) => (
                  <SelectItem key={i} value={String(i)}>
                    {levelLabel[t.dimension]} · {t.currency} · {t.timezone}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={panel} onValueChange={setPanel}>
              <SelectTrigger aria-label="趋势比较范围" className="w-52">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="fixed">固定账户比较</SelectItem>
                <SelectItem value="all">每日可用账户</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <p className="text-xs leading-6 text-muted-foreground">
            {levelLabel[timeline?.dimension || ""]}口径 · {timeline?.start_date}{" "}
            至 {timeline?.end_date} · {timeline?.currency} ·{" "}
            {timeline?.timezone} ·{" "}
            {panel === "fixed"
              ? `固定 ${timeline?.balanced_accounts ?? 0} 个账户`
              : "每日覆盖可能不同"}
            。使用冻结时累计归因收入，最近日期回收可能尚未完整；不能与上方当天收入观察的
            ROAS 直接比较。
          </p>
          {daily?.length &&
          (panel !== "fixed" || !!timeline?.balanced_accounts) ? (
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    {["日期", "消耗", "较前日", "收入", "ROAS"].map((label) => (
                      <TableHead key={label}>{label}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {daily.map((day, i) => (
                    <TableRow key={day.date}>
                      <TableCell>{day.date}</TableCell>
                      <TableCell>{amount(day.spend)}</TableCell>
                      <TableCell>
                        {i ? change(day.spend, daily[i - 1].spend) : "—"}
                      </TableCell>
                      <TableCell>{amount(day.revenue_total)}</TableCell>
                      <TableCell>{amount(day.roas, 3)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <p className="text-sm">
              没有完整固定账户样本，无法作同范围趋势判断。
            </p>
          )}
        </section>
      )}
    </section>
  )
}
