import { useEffect, useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Field, FieldLabel } from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectGroup,
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
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { aiRequest, createRequestIdentity, errorMessage } from "./api"
import {
  activeStatuses,
  dateLabel,
  ProposalDiff,
  ReadableValues,
  ReviewReport,
  ReviewText,
  statusLabel,
  type Proposal,
  type ReviewTask,
} from "./reviewPresentation"

type Props = { tenantId: string; optimizerId: string }
type HistoryPage = {
  items: ReviewTask[]
  total: number
  offset: number
  limit: number
  has_more: boolean
  runtime_ready: boolean
  active_task: ReviewTask | null
}
const PAGE_SIZE = 10

export function AIOptimizerReviews(props: Props) {
  // 租户或投手变化时撤销全部请求与草稿，禁止迟到结果穿透作用域。
  return (
    <ReviewWorkspace
      key={`${props.tenantId}:${props.optimizerId}`}
      {...props}
    />
  )
}
function ReviewWorkspace({ tenantId, optimizerId }: Props) {
  const base = `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}`
  const [page, setPage] = useState<HistoryPage | null>(null)
  const [historyOffset, setHistoryOffset] = useState(0)
  const [historyStatus, setHistoryStatus] = useState("all")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [task, setTask] = useState<ReviewTask | null>(null)
  const [active, setActive] = useState<ReviewTask | null>(null)
  const [items, setItems] = useState<Proposal[]>([])
  const [proposalOffset, setProposalOffset] = useState(0)
  const [filter, setFilter] = useState("all")
  const [scope, setScope] = useState("all")
  const [refresh, setRefresh] = useState(0)
  const [detailRefresh, setDetailRefresh] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(true)
  const [loadingProposals, setLoadingProposals] = useState(true)
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [decisionResult, setDecisionResult] = useState<Proposal | null>(null)
  const lifecycle = useRef(new AbortController())
  const selectedRef = useRef(selectedId)
  selectedRef.current = selectedId
  const decisionIdentity = useRef(createRequestIdentity())
  const reviewIdentity = useRef(createRequestIdentity())
  useEffect(() => {
    const controller = new AbortController()
    lifecycle.current = controller
    return () => controller.abort()
  }, [])
  useEffect(() => {
    if (!optimizerId) {
      setLoadingHistory(false)
      return
    }
    const controller = new AbortController()
    setLoadingHistory(true)
    const params = new URLSearchParams({
      offset: String(historyOffset),
      limit: String(PAGE_SIZE),
    })
    if (historyStatus !== "all") params.set("status", historyStatus)
    void aiRequest<HistoryPage>(`${base}/reviews?${params}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (controller.signal.aborted) return
        setPage(result)
        setActive(result.active_task)
        setSelectedId((previous) => previous ?? result.items[0]?.id ?? null)
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingHistory(false)
      })
    return () => controller.abort()
  }, [base, optimizerId, historyOffset, historyStatus, refresh])
  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    setTask(null)
    void aiRequest<ReviewTask>(`${base}/reviews/${selectedId}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setTask(result)
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
    return () => controller.abort()
  }, [base, selectedId, detailRefresh])
  useEffect(() => {
    if (!optimizerId) {
      setLoadingProposals(false)
      return
    }
    const controller = new AbortController()
    setLoadingProposals(true)
    setItems([])
    const params = new URLSearchParams({
      offset: String(proposalOffset),
      limit: String(PAGE_SIZE + 1),
    })
    if (filter !== "all")
      params.set("status", filter === "pending" ? "PENDING" : "PROCESSED")
    if (scope !== "all" && selectedId) params.set("review_task_id", selectedId)
    void aiRequest<Proposal[]>(`${base}/proposals?${params}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setItems(result)
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingProposals(false)
      })
    return () => controller.abort()
  }, [base, optimizerId, proposalOffset, filter, scope, selectedId, refresh])
  useEffect(() => {
    if (!active || !activeStatuses.includes(active.status)) return
    const controller = new AbortController()
    // 轮询只依赖活动任务，不依赖选中的历史轮次。
    const timer = window.setTimeout(() => {
      void aiRequest<ReviewTask>(`${base}/reviews/${active.id}`, {
        signal: controller.signal,
      })
        .then((next) => {
          if (controller.signal.aborted) return
          if (selectedRef.current === next.id) setTask(next)
          if (activeStatuses.includes(next.status)) setActive(next)
          else {
            setActive(null)
            setRefresh((value) => value + 1)
          }
        })
        .catch((reason) => {
          if (!controller.signal.aborted) {
            setError(errorMessage(reason))
            setActive((previous) => (previous ? { ...previous } : null))
          }
        })
    }, 2000)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [base, active])
  async function decide(item: Proposal, decision: "accept" | "reject") {
    if (item.status !== "PENDING" || busy) return
    const signal = lifecycle.current.signal
    setBusy(item.id)
    setError(null)
    const reason = reasons[item.id]?.trim() || "工作台人工不采纳"
    try {
      const updated = await aiRequest<Proposal>(
        `${base}/proposals/${item.id}/${decision}`,
        {
          method: "POST",
          signal,
          body: {
            request_id: decisionIdentity.current({
              optimizerId,
              proposalId: item.id,
              decision,
              ...(decision === "reject" ? { reason } : {}),
            }),
            ...(decision === "reject" ? { reason } : {}),
          },
        },
      )
      if (!signal.aborted) {
        setDecisionResult(updated)
        setRefresh((value) => value + 1)
      }
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  async function review() {
    if (busy || active || !page || !page.runtime_ready) return
    const signal = lifecycle.current.signal
    setBusy("review")
    setError(null)
    try {
      const result = await aiRequest<{ task: ReviewTask }>(`${base}/reviews`, {
        method: "POST",
        signal,
        body: {
          request_id: reviewIdentity.current({
            optimizerId,
            previousTask: page.items[0]?.id,
          }),
          evidence_ids: [],
          knowledge_refs: [],
        },
      })
      if (!signal.aborted) {
        setActive(result.task)
        setSelectedId(result.task.id)
        setTask(result.task)
        setHistoryOffset(0)
        setRefresh((value) => value + 1)
      }
    } catch (reason) {
      if (!signal.aborted) setError(`发起复盘失败：${errorMessage(reason)}`)
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  async function controlTask(
    action: "refresh" | "cancel" | "retry",
    target: ReviewTask,
  ) {
    const signal = lifecycle.current.signal
    setBusy("task")
    setError(null)
    try {
      const next = await aiRequest<ReviewTask>(
        `${base}/reviews/${target.id}${action === "refresh" ? "" : `/${action}`}`,
        {
          signal,
          ...(action === "refresh" ? {} : { method: "POST", body: {} }),
        },
      )
      if (!signal.aborted) {
        if (selectedRef.current === next.id) setTask(next)
        if (activeStatuses.includes(next.status)) setActive(next)
        else if (active?.id === next.id) setActive(null)
        setRefresh((value) => value + 1)
      }
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  return (
    <Card data-testid="ai-review-view" className="min-w-0">
      <CardHeader className="flex flex-wrap items-start justify-between gap-3">
        <CardTitle>复盘与建议</CardTitle>
        <Button
          disabled={
            !optimizerId ||
            !page?.runtime_ready ||
            !!busy ||
            !!active ||
            loadingHistory
          }
          onClick={() => void review()}
        >
          {busy === "review" ? "正在准备复盘证据…" : "发起复盘"}
        </Button>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-6">
        {busy === "review" && (
          <p role="status" className="text-sm text-muted-foreground">
            正在读取账户数据、规则执行与历史证据，创建复盘任务，请稍候。
          </p>
        )}
        {page?.runtime_ready === false && (
          <p className="text-sm text-muted-foreground">
            复盘服务尚未配置，暂不能发起复盘。
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        {active && (
          <div
            role="status"
            className="flex flex-wrap items-center gap-3 rounded-md border p-3 text-sm"
            data-testid="active-review"
          >
            <span>
              活动复盘 · {statusLabel(active.status)}
              {active.preparation &&
                ` · 账户数据准备 ${active.preparation.completed_accounts ?? 0} / ${active.preparation.total_accounts ?? 0}`}
            </span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setSelectedId(active.id)}
            >
              查看活动复盘
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!!busy}
              onClick={() => void controlTask("cancel", active)}
            >
              取消复盘
            </Button>
          </div>
        )}
        <section className="flex min-w-0 flex-col gap-3" aria-label="复盘历史">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-semibold">复盘历史（{page?.total ?? 0} 轮）</h3>
            <Select
              value={historyStatus}
              onValueChange={(value) => {
                setHistoryStatus(value)
                setHistoryOffset(0)
              }}
            >
              <SelectTrigger aria-label="复盘状态" className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {[
                    ["all", "全部状态"],
                    ["COMPLETE", "已完成"],
                    ["FAILED", "失败"],
                    ["CANCELLED", "已取消"],
                  ].map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>
          <div className="overflow-auto rounded-md border">
            <Table data-testid="review-history">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>短结论</TableHead>
                  <TableHead>详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {!loadingHistory &&
                  page?.items.map((item) => (
                    <TableRow
                      key={item.id}
                      data-state={
                        selectedId === item.id ? "selected" : undefined
                      }
                    >
                      <TableCell className="text-xs">
                        {dateLabel(item.created_at)}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {statusLabel(item.status)}
                        </Badge>
                      </TableCell>
                      <TableCell
                        className="max-w-sm truncate"
                        title={item.headline || item.summary}
                      >
                        {item.headline || item.summary || "尚无结论"}
                      </TableCell>
                      <TableCell>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`查看复盘 ${item.id}`}
                          onClick={() => {
                            setSelectedId(item.id)
                            setProposalOffset(0)
                          }}
                        >
                          查看
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                {(loadingHistory || !page?.items.length) && (
                  <TableRow>
                    <TableCell colSpan={4}>
                      {loadingHistory ? "正在读取历史…" : "暂无复盘历史。"}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-3 text-sm">
            <span>第 {Math.floor(historyOffset / PAGE_SIZE) + 1} 页</span>
            <Button
              size="sm"
              variant="outline"
              disabled={loadingHistory || !historyOffset}
              onClick={() =>
                setHistoryOffset((value) => Math.max(0, value - PAGE_SIZE))
              }
            >
              历史上一页
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={loadingHistory || !page?.has_more}
              onClick={() => setHistoryOffset((value) => value + PAGE_SIZE)}
            >
              历史下一页
            </Button>
          </div>
        </section>
        {selectedId && (
          <section
            className="flex min-w-0 flex-col gap-4 rounded-lg border p-4"
            aria-label="单轮复盘详情"
            data-testid="review-detail"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="font-semibold">单轮复盘详情</h3>
              <Button
                size="sm"
                variant="outline"
                disabled={!!busy}
                onClick={() => {
                  setDetailRefresh((value) => value + 1)
                  setRefresh((value) => value + 1)
                }}
              >
                刷新复盘状态
              </Button>
            </div>
            <p className="break-all text-xs text-muted-foreground">
              复盘 {selectedId}
            </p>
            {!task ? (
              <p role="status">正在读取报告…</p>
            ) : (
              <>
                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge variant="outline">{statusLabel(task.status)}</Badge>
                  <span>
                    创建 {dateLabel(task.created_at)} · 完成{" "}
                    {dateLabel(task.finished_at)} · 投手版本 v
                    {task.optimizer_version ?? "—"}
                  </span>
                </div>
                {task.preparation && (
                  <p className="text-sm">
                    账户数据准备 {task.preparation.completed_accounts ?? 0} /{" "}
                    {task.preparation.total_accounts ?? 0}
                    {!!task.preparation.failed_accounts &&
                      `，${task.preparation.failed_accounts} 个账户准备失败，复盘将注明缺口`}
                  </p>
                )}
                {task.error_message && (
                  <p className="text-sm text-destructive">
                    {task.error_message}
                  </p>
                )}
                {task.review_context && (
                  <p className="text-xs text-muted-foreground">
                    本轮发布方式：
                    {task.review_context.publication === "AUTHORIZED_AUTO"
                      ? "授权范围内自动发布"
                      : "人工确认后发布"}
                  </p>
                )}
                <ReviewReport task={task} />
                {task.status === "FAILED" && (
                  <Button
                    variant="outline"
                    className="self-start"
                    disabled={!!busy || !!active || !page?.runtime_ready}
                    onClick={() => void controlTask("retry", task)}
                  >
                    重试复盘
                  </Button>
                )}
              </>
            )}
          </section>
        )}
        <section
          className="flex min-w-0 flex-col gap-4"
          aria-label="可执行变更"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-semibold">可执行变更</h3>
            <Select
              value={scope}
              onValueChange={(value) => {
                setScope(value)
                setProposalOffset(0)
              }}
            >
              <SelectTrigger aria-label="变更轮次范围" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="all">全部复盘轮次</SelectItem>
                  <SelectItem value="selected" disabled={!selectedId}>
                    当前复盘轮次
                  </SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>
          <Tabs
            value={filter}
            onValueChange={(value) => {
              setFilter(value)
              setProposalOffset(0)
            }}
            aria-label="变更筛选"
          >
            <TabsList>
              <TabsTrigger value="all">全部变更</TabsTrigger>
              <TabsTrigger value="pending">待确认</TabsTrigger>
              <TabsTrigger value="history">已处理</TabsTrigger>
            </TabsList>
          </Tabs>
          {decisionResult && (
            <p role="status" className="break-all text-sm">
              变更{statusLabel(decisionResult.status)}
              {decisionResult.published_version_id
                ? `，已发布版本：${decisionResult.published_version_id}`
                : decisionResult.status === "REJECTED"
                  ? `，原因：${decisionResult.decision_reason || "人工不采纳"}`
                  : "，尚无发布版本回读"}
            </p>
          )}
          {loadingProposals ? (
            <p className="text-sm">正在读取变更…</p>
          ) : (
            !items.length && (
              <p className="text-sm text-muted-foreground">
                {filter === "pending"
                  ? "暂无待确认变更。"
                  : filter === "history"
                    ? "暂无已处理变更。"
                    : "暂无可执行变更。"}{" "}
                普通建议不等于规则调整，请结合本轮规则调整结论查看。
              </p>
            )
          )}
          {items.slice(0, PAGE_SIZE).map((item) => (
            <Card
              key={item.id}
              data-testid={`proposal-${item.id}`}
              className="min-w-0"
            >
              <CardHeader>
                <CardTitle className="flex flex-wrap justify-between gap-2">
                  {item.title ||
                    (item.kind === "rule"
                      ? "自动规则调整"
                      : item.kind === "strategy"
                        ? "投放策略调整"
                        : "可执行变更")}
                  <Badge variant="outline">{statusLabel(item.status)}</Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="flex min-w-0 flex-col gap-4">
                {item.review_task_id ? (
                  <Button
                    variant="link"
                    className="h-auto justify-start self-start whitespace-normal break-all p-0 text-left"
                    onClick={() => {
                      setSelectedId(item.review_task_id!)
                      setProposalOffset(0)
                    }}
                  >
                    来源复盘：{item.review_task_id}
                  </Button>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    未记录所属轮次
                  </p>
                )}
                {[
                  ["问题", item.problem],
                  ["调整依据", item.rationale || item.summary],
                  ["预期效果", item.expected_effect],
                  ["验证条件", item.validation],
                ].map(([label, text]) => (
                  <div key={label}>
                    <h4 className="mb-1 text-sm font-medium">{label}</h4>
                    <ReviewText text={text || "未记录"} />
                  </div>
                ))}
                <ProposalDiff before={item.before} after={item.after} />
                <p className="break-all text-xs text-muted-foreground">
                  投手基准 v{item.base_optimizer_version ?? "—"} · 目标{" "}
                  {item.target_id ?? "—"} · 策略版本{" "}
                  {item.base_strategy_version_id ?? "—"} · 规则版本{" "}
                  {item.base_rule_version ?? "—"}
                </p>
                <div className="grid min-w-0 gap-4 lg:grid-cols-2">
                  <div>
                    <h4 className="mb-2 text-sm font-medium">授权与影响范围</h4>
                    <ReadableValues value={item.authorization} />
                  </div>
                  <div>
                    <h4 className="mb-2 text-sm font-medium">决策证据</h4>
                    <ReadableValues value={item.evidence} />
                  </div>
                </div>
                <p className="break-all text-sm">
                  发布状态：
                  {item.published_version_id
                    ? `已发布版本 ${item.published_version_id}`
                    : item.status === "PENDING"
                      ? "等待人工确认"
                      : item.status === "REJECTED"
                        ? "已拒绝，未发布"
                        : "暂无发布版本回读"}
                </p>
                {item.decision_reason && (
                  <p className="whitespace-pre-wrap text-sm">
                    决定原因：{item.decision_reason}
                  </p>
                )}
                <details>
                  <summary className="cursor-pointer text-sm text-muted-foreground">
                    原始记录
                  </summary>
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">
                    {JSON.stringify(item, null, 2)}
                  </pre>
                </details>
                {item.status === "PENDING" && (
                  <>
                    <Field>
                      <FieldLabel htmlFor={`reject-${item.id}`}>
                        拒绝原因（选填）
                      </FieldLabel>
                      <Input
                        id={`reject-${item.id}`}
                        value={reasons[item.id] || ""}
                        disabled={!!busy}
                        onChange={(event) =>
                          setReasons((previous) => ({
                            ...previous,
                            [item.id]: event.target.value,
                          }))
                        }
                      />
                    </Field>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        disabled={!!busy}
                        onClick={() => void decide(item, "accept")}
                      >
                        接受并发布
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!!busy}
                        onClick={() => void decide(item, "reject")}
                      >
                        拒绝
                      </Button>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          ))}
          <div className="flex flex-wrap items-center justify-end gap-3 text-sm">
            <span>第 {Math.floor(proposalOffset / PAGE_SIZE) + 1} 页</span>
            <Button
              size="sm"
              variant="outline"
              disabled={loadingProposals || !proposalOffset}
              onClick={() =>
                setProposalOffset((value) => Math.max(0, value - PAGE_SIZE))
              }
            >
              变更上一页
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={loadingProposals || items.length <= PAGE_SIZE}
              onClick={() => setProposalOffset((value) => value + PAGE_SIZE)}
            >
              变更下一页
            </Button>
          </div>
        </section>
      </CardContent>
    </Card>
  )
}
