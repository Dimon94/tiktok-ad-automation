import { useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
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
      "native-plan-readiness",
      tenantId,
      summary.draft_id,
      summary.revision,
    ],
    queryFn: async () => {
      const { data } = await BuildsService.nativeCheckpoint({
        path: { tenant_id: tenantId, draft_id: summary.draft_id },
        query: { view: "plans" },
      })
      if (!("plans" in data)) throw new Error("计划回执形状不匹配")
      return data
    },
    refetchInterval: summary.status === "PREPARING" ? 2000 : false,
  })
  if (query.error) return <BuildError error={query.error} />
  return (
    <Card>
      <CardHeader>
        <CardTitle>逐计划准备回执 · 修订 {summary.revision}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {query.data?.plans.map((plan) => {
          const selection =
            plan.state === "READY" ? plan.checkpoint.selection : plan.selection
          return (
            <section key={plan.plan_key} className="rounded-lg border p-3">
              <p>
                {plan.plan_key} · {plan.state}
              </p>
              <p>
                账户 {selection.advertiser_id} · Series{" "}
                {selection.native_series_id}
              </p>
              <p>
                承载身份 {selection.series_identity?.identity_id} · 展示身份{" "}
                {selection.creative_identity?.identity_id}
              </p>
              <p>地区 {selection.regions.join("、")}</p>
              <p>
                {plan.state === "READY"
                  ? `已准备 ${(plan.checkpoint.materials ?? []).length} 个有序素材；有效至 ${plan.checkpoint.expires_at}`
                  : plan.reason_codes.join("、")}
              </p>
            </section>
          )
        })}
        {(query.data?.plans.length ?? 0) > 1 && (
          <p>冻结预览将列出可执行计划、排除原因及纳入计划的总预算。</p>
        )}
      </CardContent>
    </Card>
  )
}
