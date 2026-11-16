import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export type Proposal = {
  id: string
  status: string
  review_task_id?: string
  title?: string
  summary?: string
  kind?: string
  operation?: string
  rationale?: string
  problem?: string
  expected_effect?: string
  validation?: string
  before?: unknown
  after?: unknown
  evidence?: unknown
  authorization?: unknown
  base_optimizer_version?: number
  base_strategy_version_id?: string
  base_rule_version?: number
  target_id?: string
  decision_reason?: string
  published_version_id?: string
}
export type ReviewTask = {
  id: string
  status: string
  created_at?: string
  finished_at?: string
  optimizer_version?: number
  headline?: string
  summary?: string
  proposal_count?: number
  recommendation_count?: number
  preparation?: {
    total_accounts?: number
    completed_accounts?: number
    failed_accounts?: number
  }
  error_code?: string
  error_message?: string
  review_context?: {
    optimization_mode: string
    proposal_submission_allowed: boolean
    publication: string
  }
  result?: {
    summary?: string
    report?: {
      headline: string
      verdict: string
      sections: Array<{
        title: string
        body: string
        evidence_ids: string[]
        table?: { columns: string[]; rows: string[][] }
      }>
      rule_decision: { status: string; reason: string }
    }
    recommendations?: Array<{
      problem: string
      suggestion: string
      validation?: string
      evidence_ids?: string[]
    }>
    proposals?: unknown[]
  }
}
export const activeStatuses = [
  "PREPARING",
  "PENDING",
  "QUEUED",
  "RUNNING",
  "CREATED",
]
export const statusLabel = (status: string) =>
  ({
    PREPARING: "准备中",
    PENDING: "待处理",
    QUEUED: "排队中",
    RUNNING: "运行中",
    CREATED: "已创建",
    COMPLETE: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
    ACCEPTED: "已接受",
    REJECTED: "已拒绝",
    STALE: "版本已过期",
    PUBLISHED: "已发布",
  })[status] ?? status
export const dateLabel = (value?: string) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—"

/** 历史正文保留换行与段落；React 负责转义，绝不执行模型输出 HTML。 */
export function ReviewText({ text }: { text?: string }) {
  return (
    <div className="flex flex-col gap-3 text-sm leading-7">
      {(text || "暂无记录").split(/\n\s*\n/).map((part, index) => (
        <p key={index} className="whitespace-pre-wrap break-words">
          {part}
        </p>
      ))}
    </div>
  )
}
export function EvidenceIds({ ids }: { ids?: string[] }) {
  return ids?.length ? (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer">
        查看依据来源（{ids.length} 项）
      </summary>
      <p className="mt-2 break-all">{ids.join("、")}</p>
    </details>
  ) : null
}
export function ReviewReport({ task }: { task: ReviewTask }) {
  const result = task.result
  if (
    !result ||
    (!result.report && !result.summary && !result.recommendations?.length)
  )
    return null
  const report = result.report
  return (
    <div className="flex min-w-0 flex-col gap-5" data-testid="review-report">
      <section className="flex flex-col gap-3" aria-label="复盘报告">
        <h3 className="font-semibold">{report?.headline || "复盘报告"}</h3>
        {report ? (
          <>
            <ReviewText text={report.verdict} />
            {report.sections.map((section, index) => (
              <details
                key={index}
                open={/^决策[一二三123：:]|^(控损|放量|新剧测试)(建议|决策)/.test(
                  section.title,
                )}
                className="rounded-md border p-4"
              >
                <summary className="cursor-pointer font-medium">
                  {section.title}
                </summary>
                <div className="mt-3 flex flex-col gap-3">
                  <ReviewText text={section.body} />
                  {section.table && (
                    <div className="overflow-auto rounded-md border">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            {section.table.columns.map((column, col) => (
                              <TableHead key={col}>{column}</TableHead>
                            ))}
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {section.table.rows.map((row, index) => (
                            <TableRow key={index}>
                              {row.map((cell, col) => (
                                <TableCell
                                  key={col}
                                  className="whitespace-normal"
                                >
                                  {cell}
                                </TableCell>
                              ))}
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  )}
                  <EvidenceIds ids={section.evidence_ids} />
                </div>
              </details>
            ))}
          </>
        ) : (
          <ReviewText text={result.summary} />
        )}
      </section>
      <section className="flex flex-col gap-3" aria-label="规则调整结论">
        <h3 className="font-semibold">规则调整结论</h3>
        <Badge variant="outline" className="self-start">
          {{
            PROPOSED: "已提出可执行变更",
            NO_CHANGE: "本轮不调整",
            INSUFFICIENT_EVIDENCE: "证据不足",
          }[report?.rule_decision.status ?? ""] ?? "未记录结构化结论"}
        </Badge>
        <ReviewText
          text={
            report?.rule_decision.reason ||
            (result.proposals?.length
              ? "本轮有可执行变更，请查看下方变更记录及其发布状态。"
              : "本轮未提交可执行变更。报告中的分析和普通建议不会自动修改规则；历史报告未记录不调整或证据不足的具体原因。")
          }
        />
      </section>
      <section className="flex flex-col gap-3" aria-label="普通建议">
        <h3 className="font-semibold">
          普通建议（{result.recommendations?.length ?? 0}）
        </h3>
        <p className="text-xs text-muted-foreground">
          用于后续行动和验证，不会作为规则变更发布。
        </p>
        {result.recommendations?.map((item, index) => (
          <div key={index} className="flex flex-col gap-2 border-l-2 pl-4">
            <strong className="text-sm">问题：{item.problem}</strong>
            <ReviewText text={item.suggestion} />
            <p className="whitespace-pre-wrap text-sm">
              验证条件：{item.validation || "未提供"}
            </p>
            <EvidenceIds ids={item.evidence_ids} />
          </div>
        ))}
        {!result.recommendations?.length && (
          <p className="text-sm text-muted-foreground">本轮没有普通建议。</p>
        )}
      </section>
    </div>
  )
}
