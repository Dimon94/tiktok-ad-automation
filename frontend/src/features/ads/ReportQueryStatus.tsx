import { LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"

export function ReportQueryStatus({
  loading,
  hasPreviousData,
  snapshotAt,
  metricsObservedAt,
  projectionStatus,
}: {
  loading: boolean
  hasPreviousData: boolean
  snapshotAt?: string
  metricsObservedAt?: string | null
  projectionStatus?: string
}) {
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    setSlow(false)
    if (!loading) return
    // 仅对实际请求计时；提示耗时，不虚构进度或判定请求失败。
    const timer = window.setTimeout(() => setSlow(true), 8000)
    return () => window.clearTimeout(timer)
  }, [loading])

  const time = (value: string) => new Date(value).toLocaleTimeString()
  return (
    <div className="space-y-2">
      {loading && (
        <Alert role="status" aria-live="polite">
          <LoaderCircle className="animate-spin" aria-hidden="true" />
          <AlertDescription>
            <span>
              {slow ? "查询耗时较长，仍在加载，请稍候…" : "正在查询广告报表…"}
            </span>
            {hasPreviousData && (
              <span>下方暂时显示上一次查询结果，完成后自动更新。</span>
            )}
          </AlertDescription>
        </Alert>
      )}
      {snapshotAt && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="report-query-times"
        >
          顺序和汇总固定于 {time(snapshotAt)}；
          {metricsObservedAt
            ? `行指标观察于 ${time(metricsObservedAt)}`
            : "行指标尚未准备完成"}
          。
        </p>
      )}
      {(projectionStatus === "BUILDING" || projectionStatus === "STALE") && (
        <Alert role="status">
          <AlertDescription>
            {projectionStatus === "BUILDING"
              ? "素材数据正在整理，尚未完成的金额暂不显示。"
              : "素材数据正在更新，当前显示上一次完整结果。"}
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}
