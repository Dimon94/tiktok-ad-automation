import { AlertCircle } from "lucide-react"
import type { MetricDefinitionPublic } from "@/client"
import { Card } from "@/components/ui/card"
import { formatMetric } from "./metricCatalog"

type SummaryBucket = {
  currency: string
  level?: string
  source_kind?: string
  contract_revision?: string
  values?: Record<string, string | null>
  availability?: Record<string, string>
}

type AdsSummaryProps = {
  summary?: {
    buckets?: SummaryBucket[]
    incomplete_advertiser_count?: number
    incomplete_dates?: string[]
  }
  catalog: MetricDefinitionPublic[]
}

// 汇总卡片只放 6 个核心指标，一行排满；页面以列表为主，卡片保持紧凑。
// 第 N 天 ROAS 在回收窗口内会持续增长，不适合做顶部汇总，只在表格列里看。
const SUMMARY_KEYS = [
  "spend",
  "revenue_total",
  "roas_total",
  "impressions",
  "ctr",
  "cpm",
]

export function AdsSummary({ summary, catalog }: AdsSummaryProps) {
  const buckets = summary?.buckets ?? []
  const incompleteCount = summary?.incomplete_advertiser_count ?? 0
  const incompleteDates = summary?.incomplete_dates ?? []

  // 等待指标字典加载
  if (catalog.length === 0) {
    return null
  }

  return (
    <div className="space-y-2">
      {/* 与页面其他卡片同一套圆角、阴影和字号层级，一行 6 张，数值保持醒目。 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {SUMMARY_KEYS.map((key) => {
          const def = catalog.find((d) => d.key === key)
          if (!def) return null
          return (
            <Card
              key={key}
              className="gap-1 px-4 py-3 lg:gap-1 lg:py-3"
              data-testid={`summary-${key}`}
            >
              <div
                className="truncate text-sm text-muted-foreground"
                title={def.basic_name_zh}
              >
                {def.basic_label}
              </div>
              <div className="truncate text-2xl font-semibold tabular-nums">
                {buckets.length > 0 ? (
                  <span
                    title={
                      buckets.some(
                        (b) => b.availability?.[key] === "UNSUPPORTED",
                      )
                        ? "平台未提供"
                        : undefined
                    }
                  >
                    {buckets
                      .map((bucket) => {
                        const availability = bucket.availability?.[key]
                        // 平台不提供显示"—"；PARTIAL 仍显示数值，加一个小标记。
                        if (availability === "UNSUPPORTED") return "—"
                        const formatted = formatMetric(
                          bucket.values?.[key],
                          def.unit,
                        )
                        const prefix =
                          buckets.length > 1 ? `${bucket.currency} ${bucket.level ?? ""} ${bucket.source_kind ?? ""} ${bucket.contract_revision ?? ""} ` : ""
                        const suffix = availability === "PARTIAL" ? " 部分" : ""
                        return `${prefix}${formatted}${suffix}`
                      })
                      .join(" / ")}
                  </span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </div>
            </Card>
          )
        })}
      </div>

      {incompleteCount > 0 && (
        <div
          className="flex items-center gap-2 text-sm text-muted-foreground"
          title={
            incompleteDates.length > 0
              ? `缺失日期：${incompleteDates.join("、")}`
              : undefined
          }
        >
          <AlertCircle className="h-4 w-4" />
          <span>{incompleteCount} 个账户数据不完整</span>
        </div>
      )}
    </div>
  )
}
