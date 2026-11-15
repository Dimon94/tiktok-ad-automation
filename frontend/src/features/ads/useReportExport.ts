import { useCallback, useEffect, useRef, useState } from "react"
import { AdsReportingService } from "@/client"

export type ExportState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "done" }
  | { phase: "failed"; message: string }

const POLL_MS = 2_000
// 导出在后台任务里生成；超过这个时长仍未完成就提示用户稍后重试，避免无限轮询。
const MAX_POLLS = 150

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * 报表导出：按当前查询快照生成 CSV（当前维度、筛选与日期下的全部行，不只当前页），
 * 后台完成后自动下载到本地。切换 BC 或组件卸载后不再继续下载旧任务的结果。
 */
export function useReportExport(
  tenantId?: string | null,
  bcId?: string | null,
) {
  const [state, setState] = useState<ExportState>({ phase: "idle" })
  const runRef = useRef(0)
  // 切换租户/BC 或卸载时作废进行中的导出，旧任务完成后不再下载、不改状态。
  const scope = `${tenantId ?? ""}:${bcId ?? ""}`
  useEffect(() => {
    void scope
    setState({ phase: "idle" })
    return () => {
      runRef.current += 1
    }
  }, [scope])

  const start = useCallback(
    async (snapshotId: string, filename: string) => {
      if (!tenantId || !bcId) return
      const run = ++runRef.current
      const stale = () => runRef.current !== run
      setState({ phase: "running" })
      try {
        const created = (
          await AdsReportingService.createReportExport({
            path: { tenant_id: tenantId },
            query: { bc_id: bcId },
            body: {
              snapshot_id: snapshotId,
              idempotency_key: `ads-export-${snapshotId}-${Date.now()}`,
            },
          })
        ).data
        let status = created.status
        for (let i = 0; status === "QUEUED" || status === "RUNNING"; i++) {
          if (i >= MAX_POLLS) throw new Error("导出耗时较长，请稍后重试")
          await sleep(POLL_MS)
          if (stale()) return
          status = (
            await AdsReportingService.getReportExport({
              path: { tenant_id: tenantId, export_id: created.id },
              query: { bc_id: bcId },
            })
          ).data.status
        }
        if (stale()) return
        if (status !== "COMPLETE")
          throw new Error(status === "EXPIRED" ? "导出文件已过期" : "导出失败，请重新查询后重新导出")
        const file = (
          await AdsReportingService.downloadReportExport({
            path: { tenant_id: tenantId, export_id: created.id },
            query: { bc_id: bcId },
            responseType: "blob",
          })
        ).data as unknown as Blob
        if (stale()) return
        const url = URL.createObjectURL(file)
        const link = document.createElement("a")
        link.href = url
        link.download = filename
        link.click()
        URL.revokeObjectURL(url)
        setState({ phase: "done" })
      } catch (error) {
        if (!stale())
          setState({
            phase: "failed",
            message: error instanceof Error ? error.message : String(error),
          })
      }
    },
    [tenantId, bcId],
  )
  return { state, start }
}
