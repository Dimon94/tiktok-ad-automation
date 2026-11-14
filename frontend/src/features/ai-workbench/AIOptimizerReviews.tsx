import { Check, X } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { aiRequest, createRequestIdentity, errorMessage } from "./api"

type Proposal = {
  id: string
  status: string
  title?: string
  summary?: string
  kind?: string
  before?: unknown
  after?: unknown
  evidence?: unknown
  authorization?: unknown
  base_optimizer_version?: number
  base_strategy_version_id?: string
  base_rule_version?: number
  target_id?: string
  decision_reason?: string
}
type ReviewTask = {
  id: string
  status: string
  error?: unknown
  preparation?: { total_accounts?: number; completed_accounts?: number; failed_accounts?: number }
  error_code?: string
  error_message?: string
  result?: { summary?: string; [key: string]: unknown }
}
type Props = { tenantId: string; optimizerId: string }

function Evidence({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <h3 className="text-sm font-medium">{label}</h3>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">
        {value === undefined ? "暂无记录" : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  )
}

/** 每个视图请求都随投手卸载取消，迟到的复盘/决定不得覆盖新投手状态。 */
export function AIOptimizerReviews({ tenantId, optimizerId }: Props) {
  const [items, setItems] = useState<Proposal[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [history, setHistory] = useState<ReviewTask[]>([])
  const [runtimeReady, setRuntimeReady] = useState<boolean | undefined>()
  const [filter, setFilter] = useState("all")
  const [task, setTask] = useState<ReviewTask | null>(null)
  const decisionIdentity = useRef(createRequestIdentity())
  const reviewIdentity = useRef(createRequestIdentity())
  const lifecycle = useRef(new AbortController())
  const base = `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}`
  const load = useCallback(
    async (signal: AbortSignal) => {
      if (!optimizerId) return
      const result = await aiRequest<Proposal[]>(`${base}/proposals`, {
        signal,
      })
      if (!signal.aborted) setItems(result)
    },
    [base, optimizerId],
  )
  useEffect(() => {
    const controller = new AbortController()
    lifecycle.current = controller
    setItems([])
    setError(null)
    setTask(null)
    setHistory([])
    setRuntimeReady(undefined)
    setBusy(null)
    void load(controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setError(errorMessage(reason))
    })
    if (optimizerId) {
      void aiRequest<{ runtime_ready?: boolean; items: ReviewTask[] }>(
        `${base}/reviews`,
        { signal: controller.signal },
      )
        .then((result) => {
          if (!controller.signal.aborted) {
            setHistory((previous) =>
              previous.length ? previous : result.items,
            )
            setTask((previous) => previous ?? result.items[0] ?? null)
            setRuntimeReady(result.runtime_ready)
          }
        })
        .catch((reason) => {
          if (!controller.signal.aborted) setError(errorMessage(reason))
        })
    }
    return () => controller.abort()
  }, [load, optimizerId, base])

  useEffect(() => {
    if (
      !task ||
      !["PREPARING", "PENDING", "QUEUED", "RUNNING", "CREATED"].includes(task.status)
    )
      return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      void aiRequest<ReviewTask>(`${base}/reviews/${task.id}`, {
        signal: controller.signal,
      })
        .then(async (next) => {
          if (controller.signal.aborted) return
          await load(controller.signal)
          if (!controller.signal.aborted) {
            setTask(next)
            setHistory((previous) =>
              previous.map((item) => (item.id === next.id ? next : item)),
            )
          }
        })
        .catch((reason) => {
          if (!controller.signal.aborted) setError(errorMessage(reason))
        })
    }, 2000)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [base, load, task])

  async function decide(item: Proposal, decision: "accept" | "reject") {
    if (item.status !== "PENDING" || busy) return
    const signal = lifecycle.current.signal
    setBusy(item.id)
    setError(null)
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
            }),
            ...(decision === "reject" ? { reason: "工作台人工不采纳" } : {}),
          },
        },
      )
      if (signal.aborted) return
      setItems((previous) =>
        previous.map((entry) => (entry.id === item.id ? updated : entry)),
      )
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  async function review() {
    const signal = lifecycle.current.signal
    setBusy("review")
    setError(null)
    try {
      const result = await aiRequest<{
        task: ReviewTask
        proposals: Proposal[]
      }>(`${base}/reviews`, {
        method: "POST",
        signal,
        body: {
          request_id: reviewIdentity.current({
            optimizerId,
            previousTask: task?.id,
          }),
          evidence_ids: [],
          knowledge_refs: [],
        },
      })
      if (!signal.aborted) {
        setTask(result.task)
        setHistory((previous) => [
          result.task,
          ...previous.filter((item) => item.id !== result.task.id),
        ])
        await load(signal)
      }
    } catch (reason) {
      if (!signal.aborted) setError(`发起复盘失败：${errorMessage(reason)}`)
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  async function controlTask(action: "refresh" | "cancel" | "retry") {
    if (!task) return
    const signal = lifecycle.current.signal
    setBusy("task")
    setError(null)
    try {
      const next = await aiRequest<ReviewTask>(
        `${base}/reviews/${task.id}${action === "refresh" ? "" : `/${action}`}`,
        {
          signal,
          ...(action === "refresh" ? {} : { method: "POST", body: {} }),
        },
      )
      if (!signal.aborted) {
        setTask(next)
        setHistory((previous) =>
          previous.map((item) => (item.id === next.id ? next : item)),
        )
        await load(signal)
      }
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  const pendingCount = items.filter((item) => item.status === "PENDING").length
  const visibleItems = items.filter((item) =>
    filter === "pending"
      ? item.status === "PENDING"
      : filter === "history"
        ? item.status !== "PENDING"
        : true,
  )
  return (
    <Card data-testid="ai-review-view">
      <CardHeader className="flex flex-wrap items-start justify-between gap-3">
        <CardTitle>复盘与建议</CardTitle>
        <Button
          disabled={
            !optimizerId ||
            runtimeReady === false ||
            !!busy ||
            (!!task &&
              ["PREPARING", "PENDING", "QUEUED", "RUNNING", "CREATED"].includes(task.status))
          }
          onClick={() => void review()}
        >
          {busy === "review" ? "正在准备复盘证据…" : "发起复盘"}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {busy === "review" && (
          <p role="status" className="text-sm text-muted-foreground">
            正在读取账户数据、规则执行与历史证据，创建复盘任务，请稍候。
          </p>
        )}
        {runtimeReady === false && (
          <p className="text-sm text-muted-foreground">
            复盘服务尚未配置，暂不能发起复盘。
          </p>
        )}
        {history.length > 1 && (
          <div className="flex flex-wrap gap-2">
            {history.map((item) => (
              <Button
                key={item.id}
                size="sm"
                variant="outline"
                onClick={() => setTask(item)}
              >
                复盘 {item.id.slice(0, 8)} · {item.status}
              </Button>
            ))}
          </div>
        )}
        {task && (
          <div role="status" className="text-sm">
            复盘任务 {task.id} · {task.status}
            {task.preparation && (
              <p>
                账户数据准备 {task.preparation.completed_accounts ?? 0} / {task.preparation.total_accounts ?? 0}
                {!!task.preparation.failed_accounts && `，${task.preparation.failed_accounts} 个账户准备失败，复盘将注明缺口`}
              </p>
            )}
            {task.error_message && <p>{task.error_message}</p>}
            {task.error_code && <p>{task.error_code}</p>}
            {task.result?.summary && <p>{task.result.summary}</p>}
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!!busy}
                onClick={() => void controlTask("refresh")}
              >
                刷新复盘状态
              </Button>
              {["PREPARING", "PENDING", "QUEUED", "RUNNING"].includes(task.status) && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!!busy}
                  onClick={() => void controlTask("cancel")}
                >
                  取消复盘
                </Button>
              )}
              {task.status === "FAILED" && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!!busy || runtimeReady === false}
                  onClick={() => void controlTask("retry")}
                >
                  重试复盘
                </Button>
              )}
            </div>
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <Tabs value={filter} onValueChange={setFilter} aria-label="建议筛选">
          <TabsList>
            <TabsTrigger value="all">全部建议</TabsTrigger>
            <TabsTrigger value="pending">待确认（{pendingCount}）</TabsTrigger>
            <TabsTrigger value="history">已处理</TabsTrigger>
          </TabsList>
          <TabsContent value={filter} className="flex flex-col gap-4">
            {!visibleItems.length && !error && (
              <p className="text-sm text-muted-foreground">
                {filter === "pending"
                  ? "暂无待确认建议。"
                  : filter === "history"
                    ? "暂无已处理建议。"
                    : "暂无复盘建议。"}
              </p>
            )}
            {visibleItems.map((item) => (
              <Card key={item.id}>
                <CardHeader>
                  <CardTitle className="flex flex-wrap justify-between gap-2">
                    {item.title ??
                      (item.kind === "rule"
                        ? "自动规则调整"
                        : item.kind === "strategy"
                          ? "投放策略调整"
                          : item.id)}
                    <Badge variant="outline">{item.status}</Badge>
                  </CardTitle>
                  <p className="text-sm text-muted-foreground">
                    {item.summary ?? item.decision_reason ?? ""}
                  </p>
                </CardHeader>
                <CardContent className="flex flex-col gap-4">
                  <p className="break-all text-xs text-muted-foreground">
                    投手基准 v{item.base_optimizer_version ?? "—"} · 目标{" "}
                    {item.target_id ?? "—"} · 策略版本{" "}
                    {item.base_strategy_version_id ?? "—"} · 规则版本{" "}
                    {item.base_rule_version ?? "—"}
                  </p>
                  <div className="grid min-w-0 gap-4 lg:grid-cols-2">
                    <Evidence label="调整前" value={item.before} />
                    <Evidence label="调整后" value={item.after} />
                    <Evidence label="决策证据" value={item.evidence} />
                    <Evidence
                      label="授权与影响范围"
                      value={item.authorization}
                    />
                  </div>
                  {item.status === "PENDING" && (
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        disabled={!!busy}
                        onClick={() => void decide(item, "accept")}
                      >
                        <Check />
                        接受
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!!busy}
                        onClick={() => void decide(item, "reject")}
                      >
                        <X />
                        拒绝
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  )
}
