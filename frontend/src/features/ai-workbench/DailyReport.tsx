import { optimizationModeLabel } from "./presentation"
import { useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { aiRequest, businessToday, errorMessage } from "./api"

type Report = {
  business_date?: string
  optimization_mode?: string
  target_progress?: Record<string, number>
  schedule_rows?: Array<Record<string, unknown>>
  material?: Record<string, number>
  rule_actions?: Array<Record<string, unknown>>
  pending_proposals?: Array<Record<string, unknown>>
  next_trigger?: { kind?: string; at?: string } | null
  frozen_evidence_ids?: string[]
}

type Props = { tenantId: string; optimizerId: string; businessDate?: string }
const today = businessToday

/** 每日报告只展示后端冻结的证据，不在前端重新计算投放指标。 */
export function DailyReport({
  tenantId,
  optimizerId,
  businessDate = today(),
}: Props) {
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!optimizerId) return
    const controller = new AbortController()
    setReport(null)
    setError(null)
    aiRequest<Report>(
      `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}/daily-report?business_date=${encodeURIComponent(businessDate)}`,
      { signal: controller.signal },
    )
      .then((payload) => {
        if (!controller.signal.aborted) setReport(payload)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
    return () => controller.abort()
  }, [businessDate, optimizerId, tenantId])
  const progress = report?.target_progress ?? {}
  const material = report?.material ?? {}
  return (
    <div className="space-y-4" data-testid="ai-daily-report">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">每日报告</h2>
          <p className="text-sm text-muted-foreground">
            {report?.business_date ?? businessDate} · 使用冻结证据
          </p>
        </div>
        <Badge variant="outline">
          {optimizationModeLabel(report?.optimization_mode)}
        </Badge>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!report && !error && (
        <p className="text-sm text-muted-foreground">正在读取每日报告…</p>
      )}
      {report && (
        <>
          <div className="grid gap-3 sm:grid-cols-5">
            <Metric label="计划" value={progress.planned ?? 0} />
            <Metric label="完成" value={progress.completed ?? 0} />
            <Metric label="部分完成" value={progress.partial ?? 0} />
            <Metric label="待执行" value={progress.pending ?? 0} />
            <Metric
              label="失败/未知"
              value={(progress.failed ?? 0) + (progress.unknown ?? 0)}
            />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>素材与下单</CardTitle>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-2 text-sm">
                <Metric label="下单意图" value={material.intent ?? 0} />
                <Metric label="已下单" value={material.requested ?? 0} />
                <Metric
                  label="下单受阻"
                  value={(material.failed ?? 0) + (material.blocked ?? 0)}
                />
                <Metric label="无需下单" value={material.not_requested ?? 0} />
                <Metric label="等待素材" value={material.pending ?? 0} />
                <Metric label="已匹配" value={material.matched ?? 0} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>下一触发</CardTitle>
              </CardHeader>
              <CardContent className="text-sm">
                {report.next_trigger
                  ? `${report.next_trigger.kind === "rule" ? "规则" : "搭建"} · ${report.next_trigger.at ?? "—"}`
                  : "暂无待触发任务"}
              </CardContent>
            </Card>
          </div>
          <Card>
            <CardHeader>
              <CardTitle>剧单排期与回读</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {(report.schedule_rows ?? []).map((row) => (
                <div
                  className="flex flex-wrap items-center justify-between gap-2 rounded border p-2 text-sm"
                  key={String(row.id)}
                >
                  <span>{String(row.title ?? "未命名剧目")}</span>
                  <span className="text-muted-foreground">
                    {String(row.status ?? "—")} ·{" "}
                    {row.material_order
                      ? String(
                          (row.material_order as { status?: string }).status ??
                            "—",
                        )
                      : row.order_blocked_reason
                        ? String(row.order_blocked_reason)
                        : row.order_materials
                          ? "计划下单（尚无订单回执）"
                          : "无需下单"}
                  </span>
                </div>
              ))}
              {!report.schedule_rows?.length && (
                <p className="text-sm text-muted-foreground">暂无剧单排期。</p>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>规则动作与待确认</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p>
                规则证据 {report.rule_actions?.length ?? 0} 条 · 待确认{" "}
                {report.pending_proposals?.length ?? 0} 条
              </p>
              <p className="text-xs text-muted-foreground">
                冻结证据 ID：{report.frozen_evidence_ids?.length ?? 0} 个
              </p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded border bg-muted/20 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold">{value}</p>
    </div>
  )
}
