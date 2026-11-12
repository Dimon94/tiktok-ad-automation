import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import { flushSync } from "react-dom"
import { BuildsService, type Recovery, type RecoveryReceipt } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { isForbidden } from "@/features/tenants/shared"
import {
  BuildGuard,
  BuildReason,
  reportError,
  unknownOutcome,
} from "./presentation"
import { submissionKey } from "./submission-page"

type Kind = "RETRY" | "RECONCILE"
type Saved = { requestId: string; kind: Kind; receipt?: RecoveryReceipt }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
function restored(key: string, submissionId: string): Saved | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null")
    if (
      !value ||
      !uuid.test(value.requestId) ||
      !["RETRY", "RECONCILE"].includes(value.kind)
    )
      return null
    if (
      value.receipt &&
      (value.receipt.submission_id !== submissionId ||
        value.receipt.request_id !== value.requestId ||
        !uuid.test(value.receipt.recovery_id))
    )
      return null
    return value
  } catch {
    return null
  }
}
export function SubmissionRecoveryActions({
  tenantId,
  bcId,
  submissionId,
  recovery,
  canCancel,
  localCancelled,
  write,
}: {
  tenantId: string
  bcId: string
  submissionId: string
  recovery?: Recovery
  canCancel?: boolean
  localCancelled?: boolean
  write: boolean
}) {
  const key = `submission-recovery:${tenantId}:${bcId}:${submissionId}`
  const [saved, setSaved] = useState<Saved | null>(() =>
    restored(key, submissionId),
  )
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [forbidden, setForbidden] = useState(false)
  const controller = useRef<AbortController | null>(null),
    lock = useRef(false),
    client = useQueryClient()
  useEffect(() => () => controller.current?.abort(), [])
  const progress = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "recovery",
      saved?.receipt?.recovery_id,
    ],
    enabled: !!saved?.receipt,
    queryFn: async ({ signal }) => {
      const { data } = await BuildsService.getSubmissionRecovery({
        path: { tenant_id: tenantId, recovery_id: saved!.receipt!.recovery_id },
        signal,
      })
      if (
        data.submission_id !== submissionId ||
        data.request_id !== saved!.requestId
      )
        throw new Error("恢复进度范围不一致")
      return data
    },
    refetchInterval: (q) =>
      !q.state.error &&
      ["QUEUED", "RUNNING"].includes(
        q.state.data?.state || saved?.receipt?.state || "",
      )
        ? 2000
        : false,
    retry: false,
  })
  const current = progress.data
  const signature = current
    ? JSON.stringify([
        current.recovery_id,
        current.state,
        current.scheduled_count,
        current.reason_code,
      ])
    : undefined
  useEffect(() => {
    if (!signature) return
    void client.invalidateQueries({
      queryKey: [...submissionKey(tenantId, bcId), submissionId],
      predicate: (q) => q.queryKey[5] !== "recovery",
    })
    void client.invalidateQueries({
      queryKey: [...submissionKey(tenantId, bcId), "list"],
    })
  }, [signature, client, tenantId, bcId, submissionId])
  const unknown = !!saved && !saved.receipt
  const terminal = current?.state === "COMPLETED" || current?.state === "FAILED"
  const blocked = busy || unknown || (!!saved?.receipt && !terminal)
  const denied = forbidden || isForbidden(error) || isForbidden(progress.error)
  function keep(value: Saved) {
    sessionStorage.setItem(key, JSON.stringify(value))
    setSaved(value)
  }
  async function perform(kind: Kind) {
    if (
      lock.current ||
      blocked ||
      !write ||
      denied ||
      !(kind === "RETRY" ? recovery?.can_retry : recovery?.can_reconcile)
    )
      return
    lock.current = true
    setBusy(true)
    setError(undefined)
    const ctrl = new AbortController()
    controller.current = ctrl
    const requestId = crypto.randomUUID()
    let persisted = false
    try {
      keep({ requestId, kind })
      persisted = true
      const options = {
        path: { tenant_id: tenantId, submission_id: submissionId },
        body: { request_id: requestId },
        signal: ctrl.signal,
      }
      const { data } =
        kind === "RETRY"
          ? await BuildsService.retrySubmission(options)
          : await BuildsService.reconcileSubmission(options)
      if (
        data.submission_id !== submissionId ||
        data.request_id !== requestId ||
        data.kind !== kind
      )
        throw new Error("恢复回执范围不一致")
      if (!ctrl.signal.aborted) keep({ requestId, kind, receipt: data })
    } catch (e) {
      if (!ctrl.signal.aborted) {
        if (e instanceof AxiosError && e.response?.status === 401) {
          sessionStorage.removeItem(key)
          flushSync(() => {
            setSaved(null)
            setBusy(false)
          })
          await client.cancelQueries({ queryKey: ["tenant", tenantId] })
          reportError(e)
          return
        }
        reportError(e)
        setError(e)
        if (isForbidden(e)) setForbidden(true)
        if (
          persisted &&
          !unknownOutcome(e) &&
          !(e instanceof AxiosError && e.response?.status === 408)
        ) {
          sessionStorage.removeItem(key)
          setSaved(null)
          void client.invalidateQueries({
            queryKey: [
              ...submissionKey(tenantId, bcId),
              submissionId,
              "summary",
            ],
          })
        }
      }
    } finally {
      lock.current = false
      if (!ctrl.signal.aborted) setBusy(false)
    }
  }
  async function confirmRequest() {
    if (!saved || saved.receipt || lock.current) return
    lock.current = true
    setBusy(true)
    setError(undefined)
    const ctrl = new AbortController()
    controller.current = ctrl
    try {
      const { data } = await BuildsService.savedSubmissionRecovery({
        path: { tenant_id: tenantId, request_id: saved.requestId },
        signal: ctrl.signal,
      })
      if (
        data.submission_id !== submissionId ||
        data.request_id !== saved.requestId ||
        data.kind !== saved.kind
      )
        throw new Error("恢复回执范围不一致")
      if (!ctrl.signal.aborted) keep({ ...saved, receipt: data })
    } catch (e) {
      if (!ctrl.signal.aborted) {
        reportError(e)
        setError(e)
      }
    } finally {
      lock.current = false
      if (!ctrl.signal.aborted) setBusy(false)
    }
  }
  async function cancelUnsent() {
    if (lock.current || !canCancel || localCancelled) return
    lock.current = true
    setBusy(true)
    setError(undefined)
    try {
      await BuildsService.cancelSubmission({
        path: { tenant_id: tenantId, submission_id: submissionId },
      })
    } catch (e) {
      setError(e)
      reportError(e)
    } finally {
      lock.current = false
      setBusy(false)
      // 取消幂等；响应丢失时刷新同一结果，不创建另一份任务。
      void client.invalidateQueries({ queryKey: [...submissionKey(tenantId, bcId), submissionId] })
    }
  }
  const code =
    error instanceof AxiosError ? error.response?.data?.code : undefined
  const reasons =
    recovery?.reasons?.filter(
      (reason) => reason !== "recovery_no_candidates",
    ) || []
  // 无待办时不占页面空间；已保存的未知请求和恢复回执必须继续可见。
  if (
    !canCancel &&
    !localCancelled &&
    !saved &&
    !error &&
    !busy &&
    !recovery?.can_retry &&
    !recovery?.can_reconcile &&
    !reasons.length
  )
    return null
  return (
    <section className="flex flex-col gap-3" aria-label="任务恢复操作">
      <BuildGuard
        dirty={busy || unknown}
        title="恢复请求尚未确认"
        description="离开会停止本页等待，原请求编号会保留。服务器可能已经受理；返回此任务后请继续核实原请求。"
        leaveLabel="离开并稍后核实"
      />
      {(canCancel || localCancelled) && (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-muted-foreground">
            {localCancelled ? "已取消未发送步骤。" : "取消仅跳过尚未发送的本地步骤。"}
            已发送请求继续等待实际结果，已创建广告保持现状；待核实项仍可只读核查。
          </p>
          {canCancel && !localCancelled && (
            <Button className="self-start" variant="outline" disabled={busy} onClick={() => void cancelUnsent()}>
              取消未发送步骤
            </Button>
          )}
        </div>
      )}
      {write && !denied && (
        <div className="flex flex-wrap gap-2">
          {recovery?.can_reconcile &&
            (recovery.reconcilable_step_count ?? 0) > 0 && (
              <Button
                disabled={blocked}
                onClick={() => void perform("RECONCILE")}
              >
                核查待核实项（{recovery.reconcilable_step_count}）
              </Button>
            )}
          {recovery?.can_retry && (recovery.retryable_step_count ?? 0) > 0 && (
            <Button
              variant="outline"
              disabled={blocked}
              onClick={() => void perform("RETRY")}
            >
              重试失败步骤（{recovery.retryable_step_count}）
            </Button>
          )}
        </div>
      )}
      {(!write || denied) && (
        <p className="text-sm text-muted-foreground">
          {denied ? "当前角色无权执行恢复操作。" : "当前角色仅可查看任务结果。"}
        </p>
      )}
      {error != null && !unknown && !denied && (
        <Alert variant="destructive">
          <AlertTitle>恢复操作未完成</AlertTitle>
          <AlertDescription>
            {code === "recovery_no_candidates"
              ? "当前已没有符合条件的步骤，已重新读取任务结果。"
              : denied
                ? "当前角色无权执行恢复操作。"
                : "未能完成此次请求，请先刷新任务结果。"}
          </AlertDescription>
        </Alert>
      )}
      {unknown && (
        <Alert>
          <AlertTitle>恢复请求结果尚未确认。</AlertTitle>
          <AlertDescription>
            <div className="flex flex-col gap-2">
              <p>
                {error instanceof AxiosError && error.response?.status === 404
                  ? "尚未查到原请求，不能据此重新安排。请继续核实。"
                  : "请使用原请求编号确认是否已安排，已创建对象保持现状。"}
              </p>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => void confirmRequest()}
              >
                确认恢复请求结果
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
      {saved?.receipt && (
        <Alert>
          <AlertDescription>
            <div className="flex flex-col gap-2" role="status">
              <p>
                {current?.state === "COMPLETED"
                  ? `扫描完成，已安排 ${current.scheduled_count} 个步骤。实际执行结果以任务统计为准。`
                  : current?.state === "FAILED"
                    ? `扫描未完成，已安排 ${current.scheduled_count} 个步骤；已安排的工作保持现状。`
                    : current?.state === "RUNNING"
                      ? `正在扫描，已安排 ${current.scheduled_count} 个步骤。`
                      : `已安排${saved.kind === "RETRY" ? "重试" : "核查"}，等待扫描。`}
              </p>
              {current?.reason_code && (
                <BuildReason code={current.reason_code} />
              )}
              {progress.error != null && (
                <p>暂时无法读取最新进度；原受理回执和上次结果已保留。</p>
              )}
              <Button
                variant="outline"
                disabled={progress.isFetching}
                onClick={() => void progress.refetch()}
              >
                刷新恢复进度
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
      {!!reasons.length && (
        <div className="flex flex-col gap-1">
          {reasons.map((code) => (
            <BuildReason key={code} code={code} />
          ))}
        </div>
      )}
    </section>
  )
}
