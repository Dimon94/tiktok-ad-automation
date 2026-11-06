import type { ReactNode } from "react"
import type { StepPublic, SubmissionView } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { displayTime } from "@/features/accounts/presentation"
import { ServerTable } from "@/features/tenants/shared"
export const submissionStates: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "进行中",
  NEEDS_REVIEW: "待核实",
  COMPLETED: "已完成",
  PARTIAL: "部分失败",
  FAILED: "全部失败",
}
export const stepKinds: Record<string, string> = {
  MATERIAL: "素材准备",
  CTA: "CTA 准备",
  CAMPAIGN: "Campaign",
  ADGROUP: "Ad Group",
  AD: "Ad",
  READBACK: "结果核查",
}
export const stepStates: Record<string, string> = {
  VERIFIED_REPLACEMENT: "补建已核实",
  PENDING: "待处理",
  QUEUED: "排队中",
  RUNNING: "处理中",
  SUCCEEDED: "已成功",
  RETRYABLE: "可重试",
  UNKNOWN: "结果待核实",
  FAILED: "确定失败",
}
export function SubmissionBadge({
  status,
  correctedAdCount = 0,
}: {
  status: string
  correctedAdCount?: number
}) {
  return (
    <Badge
      variant={
        status === "FAILED"
          ? "destructive"
          : status === "COMPLETED"
            ? "secondary"
            : "outline"
      }
    >
      {submissionStates[status] || stepStates[status] || status}
      {status === "COMPLETED" &&
        correctedAdCount > 0 &&
        `（含${correctedAdCount}条补建）`}
    </Badge>
  )
}
export function PlatformState({ step }: { step?: StepPublic | null }) {
  const state = step?.correction || step
  return (
    <div className="flex flex-col gap-1 text-xs">
      <span>
        操作状态：
        {state?.operation_status === "ENABLE"
          ? "已启用（ENABLE）"
          : state?.operation_status || "暂未获取"}
      </span>
      <span>审核/投放状态：{state?.review_status || "暂未获取"}</span>
      {state?.checked_at && (
        <span className="text-muted-foreground">
          核查于 {displayTime(state.checked_at)}
        </span>
      )}
      {step?.mismatch && !step.correction && (
        <span className="font-semibold">存在结果差异，需核实</span>
      )}
      {step?.correction && (
        <span className="text-muted-foreground">
          补建已核实；原组 {step.correction.original_adgroup_id}{" "}
          已停用（DISABLE）。
        </span>
      )}
    </div>
  )
}
export function SubmissionProgress({
  data,
  metadata,
  onTechnicalDetails,
}: {
  data: SubmissionView
  metadata: ReactNode
  onTechnicalDetails: () => void
}) {
  return (
    <Card className="min-w-0 gap-4 py-4 lg:gap-4 lg:py-4">
      <CardHeader className="min-w-0">
        <div className="flex items-center justify-between gap-3">
          <CardTitle>
            <h2>任务概览</h2>
          </CardTitle>
          <Button variant="ghost" size="sm" onClick={onTechnicalDetails}>
            技术详情
          </Button>
        </div>
        {metadata}
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-3">
        <dl
          className="grid grid-cols-3 divide-x rounded-lg border bg-muted/30"
          aria-label="创建数量摘要"
        >
          {(
            [
              ["campaign_count", "广告系列"],
              ["adgroup_count", "广告组"],
              ["ad_count", "广告"],
            ] as const
          ).map(([field, label]) => (
            <div
              key={field}
              className="flex min-w-0 flex-col items-start justify-between gap-1 p-3 lg:flex-row lg:items-center lg:gap-4 lg:px-4"
            >
              <dt className="text-sm text-muted-foreground">{label}</dt>
              <dd className="text-xl font-semibold tabular-nums">
                {data.succeeded[field]}
                <span className="ml-1 text-sm font-normal text-muted-foreground">
                  / {data.submitted[field]}
                </span>
              </dd>
            </div>
          ))}
        </dl>
        <CardDescription className="text-xs">
          数量为已创建 /
          已提交。已创建不代表已投放；启用、审核与实际消耗以平台结果为准。
        </CardDescription>
        {(countObjects(data.failed) > 0 ||
          countObjects(data.unknown) > 0 ||
          Object.entries(data.stage_counts).some(
            ([key, count]) =>
              /^(MATERIAL|CTA|READBACK):(UNKNOWN|FAILED|RETRYABLE)$/.test(
                key,
              ) && count > 0,
          )) && (
          <section className="flex flex-wrap gap-2" aria-label="待处理数量">
            {countObjects(data.failed) > 0 && (
              <Badge variant="destructive">
                失败 {countObjects(data.failed)} 个广告对象
              </Badge>
            )}
            {countObjects(data.unknown) > 0 && (
              <Badge variant="outline">
                待核实 {countObjects(data.unknown)} 个广告对象
              </Badge>
            )}
            {Object.entries(data.stage_counts)
              .filter(
                ([key, count]) =>
                  /^(MATERIAL|CTA|READBACK):(UNKNOWN|FAILED|RETRYABLE)$/.test(
                    key,
                  ) && count > 0,
              )
              .map(([key, count]) => {
                const [kind, status] = key.split(":")
                return (
                  <Badge
                    key={key}
                    variant={status === "FAILED" ? "destructive" : "outline"}
                  >
                    {stepKinds[kind] || kind} · {stepStates[status]} {count}
                  </Badge>
                )
              })}
          </section>
        )}
        <details>
          <summary className="cursor-pointer text-sm text-muted-foreground">
            完整统计与执行阶段
          </summary>
          <div className="mt-3 flex min-w-0 flex-col gap-3">
            <div className="min-w-0 overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>对象</TableHead>
                    {[
                      "计划",
                      "提交",
                      "已创建",
                      "失败",
                      "待核实",
                      "待处理",
                      "排除",
                    ].map((x) => (
                      <TableHead key={x}>{x}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(
                    [
                      ["campaign_count", "Campaign", "campaign"],
                      ["adgroup_count", "Ad Group", "group"],
                      ["ad_count", "Ad", "ad"],
                    ] as const
                  ).map(([field, label, id]) => (
                    <TableRow key={field}>
                      <TableCell className="font-medium">{label}</TableCell>
                      {(
                        [
                          "planned",
                          "submitted",
                          "succeeded",
                          "failed",
                          "unknown",
                          "pending",
                          "excluded",
                        ] as const
                      ).map((key) => (
                        <TableCell key={key} data-testid={`count-${id}-${key}`}>
                          {data[key][field]}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <p className="text-xs text-muted-foreground">
              已创建事实与结果差异分别记录；素材、CTA
              与核查步骤不计入上述广告对象数。续建任务的成功数量包含复用对象。
              {(data.corrected_ad_count ?? 0) > 0 &&
                ` 已创建包含${data.corrected_ad_count}条已核实补建，原尝试保留在明细审计中。`}
            </p>
            <div className="flex min-w-0 flex-wrap gap-3 text-xs">
              {Object.entries(data.stage_counts)
                .filter(([key]) => /^(MATERIAL|CTA|READBACK):/.test(key))
                .map(([key, count]) => {
                  const [kind, status] = key.split(":")
                  return (
                    <span key={key}>
                      {stepKinds[kind]} · {stepStates[status] || status} {count}
                    </span>
                  )
                })}
            </div>
          </div>
        </details>
      </CardContent>
    </Card>
  )
}
export function countObjects(value: {
  campaign_count?: number
  adgroup_count?: number
  ad_count?: number
}) {
  return (
    (value.campaign_count ?? 0) +
    (value.adgroup_count ?? 0) +
    (value.ad_count ?? 0)
  )
}

export function SubmissionTable<T>(
  props: Parameters<typeof ServerTable<T>>[0],
) {
  return (
    <div className="min-w-0 [&_th:first-child]:sticky [&_th:first-child]:left-0 [&_th:first-child]:bg-muted [&_td:first-child]:sticky [&_td:first-child]:left-0 [&_td:first-child]:min-w-40 [&_td:first-child]:max-w-60 [&_td:first-child]:whitespace-normal [&_td:first-child]:bg-card [&_td:first-child_span]:break-all">
      <ServerTable {...props} />
    </div>
  )
}
