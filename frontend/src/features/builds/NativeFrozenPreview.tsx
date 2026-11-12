import { Link } from "@tanstack/react-router"
import type { PreviewSummary } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"

type Plan = {
  selection: {
    native_series_id: string
    advertiser_id: string
    regions: string[]
    series_identity: object
    creative_identity: object
  }
  campaign: { budget: string; currency: string }
  adgroup: { target_roas: string }
  occupancy: {
    directory_request_available?: boolean
    coverage: string
    observed_at: string | null
    published_version: number | null
    scope_limits: string[]
    visible_conflicts: object[]
  }
  ads: {
    base_ad_no: number
    creative_no: number
    text: string
    cta: object
    materials: { material_id: string; video_id: string; image_id: string }[]
  }[]
}

export function NativeFrozenPreview({
  preview,
  back,
  tenantId,
}: {
  tenantId: string
  preview: PreviewSummary
  back: () => void
}) {
  const plan = preview.native_request as unknown as Plan
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">
          Series 直接视频投放预览 · {preview.status}
        </h1>
        <Button variant="outline" onClick={back}>
          返回草稿
        </Button>
      </div>
      <Card>
        <CardHeader>投放范围与预算</CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p>
            账户 {plan.selection.advertiser_id} · Series{" "}
            {plan.selection.native_series_id} · 地区{" "}
            {plan.selection.regions.join("、")}
          </p>
          <p>
            1 Campaign · 1 Adgroup · {plan.ads.length} 个 API Ad · CBO 日预算{" "}
            {plan.campaign.budget} {plan.campaign.currency} · Target ROAS{" "}
            {plan.adgroup.target_roas} · ZERO_DAY · ENABLE
          </p>
          <p>
            Series 承载身份：{JSON.stringify(plan.selection.series_identity)}
          </p>
          <p>
            创意展示身份：{JSON.stringify(plan.selection.creative_identity)}
          </p>
          <p className="break-all">
            内容摘要 {preview.content_digest} · 草稿版本{" "}
            {preview.draft_revision}
          </p>
          <p>已冻结全部分配。修改输入后此预览失效，请重新准备并生成预览。</p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>占用观察 · {plan.occupancy.coverage}</CardHeader>
        <CardContent>
          <Button asChild variant="outline">
            <Link
              to="/tenants/$tenantId/ads"
              params={{ tenantId }}
              search={{ bc_id: preview.bc_id }}
            >
              前往现有目录请求更新
            </Link>
          </Button>
          {plan.occupancy.directory_request_available === false && (
            <p className="mt-2 text-sm text-muted-foreground">
              当前目录同步入口不可用；以上观察保留原有范围和时间。
            </p>
          )}
        </CardContent>
        <CardContent className="space-y-2 text-sm">
          <p>
            观察时间 {plan.occupancy.observed_at ?? "未知"} · 发布版本{" "}
            {plan.occupancy.published_version ?? "未知"}
          </p>
          {plan.occupancy.scope_limits.map((x) => (
            <p key={x}>{x}</p>
          ))}
          {plan.occupancy.visible_conflicts.length ? (
            <pre className="whitespace-pre-wrap break-all">
              {JSON.stringify(plan.occupancy.visible_conflicts, null, 2)}
            </pre>
          ) : (
            <p>当前可见范围未发现已知交集；不代表全域无冲突。</p>
          )}
        </CardContent>
      </Card>
      {plan.ads.map((ad) => (
        <Card key={`${ad.base_ad_no}:${ad.creative_no}`}>
          <CardHeader>
            基础广告 {ad.base_ad_no} · 文案版本 {ad.creative_no}
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p>{ad.text}</p>
            <p>动态 CTA：{JSON.stringify(ad.cta)}</p>
            <table className="w-full text-left">
              <thead>
                <tr>
                  <th>素材</th>
                  <th>目标 VID</th>
                  <th>核实封面</th>
                </tr>
              </thead>
              <tbody>
                {ad.materials.map((m, i) => (
                  <tr key={`${m.material_id}:${i}`}>
                    <td className="break-all">{m.material_id}</td>
                    <td>{m.video_id}</td>
                    <td>{m.image_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      ))}
      <details>
        <summary>冻结范围、授权与证据</summary>
        <pre className="overflow-x-auto whitespace-pre-wrap break-all text-xs">
          {JSON.stringify(preview.native_request, null, 2)}
        </pre>
      </details>
      <p className="text-sm text-muted-foreground">
        当前可审阅冻结计划。原生广告执行尚未开放。
      </p>
    </div>
  )
}
