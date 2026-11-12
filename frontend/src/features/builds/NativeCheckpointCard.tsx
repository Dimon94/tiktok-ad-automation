import { useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { BuildError } from "./presentation"

export function NativeCheckpointCard({
  tenantId,
  summary,
}: {
  tenantId: string
  summary: DraftSummary
}) {
  const query = useQuery({
    queryKey: [
      "native-checkpoint",
      tenantId,
      summary.draft_id,
      summary.revision,
    ],
    queryFn: async () =>
      (
        await BuildsService.nativeCheckpoint({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
        })
      ).data,
    refetchInterval: summary.status === "PREPARING" ? 2000 : false,
  })
  if (query.error) return <BuildError error={query.error} />
  const result = query.data
  return (
    <Card>
      <CardHeader>
        <CardTitle>原生目的地与身份证据</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p>
          Series：
          {summary.scene_selection?.kind === "NATIVE_SERIES_DIRECT_VIDEO"
            ? summary.scene_selection.native_series_id
            : ""}
        </p>
        {result && (
          <>
            <p>
              检查点：{result.state} · 草稿版本 {result.draft_revision}
            </p>
            <p>
              剧集承载身份：{result.selection.series_identity?.identity_type} /{" "}
              {result.selection.series_identity?.identity_id}
            </p>
            <p>
              创意展示身份：{result.selection.creative_identity?.identity_type}{" "}
              / {result.selection.creative_identity?.identity_id}
            </p>
            <p>地区：{result.selection.regions.join("、")}</p>
            <Alert>
              <AlertDescription>
                {result.reason_codes.join("、") ||
                  (result.state === "READY"
                    ? "目的地与身份证据已准备"
                    : "正在读取当前账户目录")}
              </AlertDescription>
            </Alert>
            <p className="text-xs text-muted-foreground">
              证据 {(result.evidence_ids ?? []).length} 页 · 有效至{" "}
              {result.expires_at ?? "待准备"}
              。视频与封面尚未逐条准备，当前不能提交广告。
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
