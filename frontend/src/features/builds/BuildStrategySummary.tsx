import type { VersionPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { normalizeDecimal } from "@/features/strategies/validation"

export function BuildStrategySummary({ version }: { version: VersionPublic }) {
  const config = version.config
  const budget = `${config.budget_strategy === "ADGROUP" ? "组预算" : "系列预算"} ${config.currency} ${normalizeDecimal(config.budget)}`
  const bid =
    config.bid_strategy === "TARGET_ROAS"
      ? `目标 ROAS ${config.target_roas}`
      : "最高价值"
  return (
    <div className="mt-2 flex items-center gap-3 border-t pt-2">
      <span className="shrink-0 text-xs text-muted-foreground">
        策略摘要 · v{version.number}
      </span>
      <p className="min-w-0 flex-1 text-xs text-muted-foreground">
        {budget} · {bid} ·{" "}
        {config.group_generation_mode === "BY_MATERIAL"
          ? "按素材分组"
          : `固定 ${config.group_count} 组`}
      </p>
      <Dialog>
        <DialogTrigger asChild>
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs">
            查看详情
          </Button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>投放策略详情 · v{version.number}</DialogTitle>
            <DialogDescription>
              本次选择的策略参数，调整策略请前往投放策略页面。
            </DialogDescription>
          </DialogHeader>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
            <dt className="text-muted-foreground">预算</dt>
            <dd>{budget}</dd>
            <dt className="text-muted-foreground">出价</dt>
            <dd>{bid}</dd>
            <dt className="text-muted-foreground">广告组</dt>
            <dd>
              {config.group_generation_mode === "BY_MATERIAL"
                ? `按素材数量（上限 ${config.max_materials_per_group}）`
                : `固定 ${config.group_count} 组`}
            </dd>
            <dt className="text-muted-foreground">广告</dt>
            <dd>
              {config.ad_generation_mode === "BY_MATERIAL"
                ? `按素材数量（每个广告上限 ${config.max_materials_per_ad}）`
                : `固定 ${config.ads_per_group} 个/组`}
            </dd>
            <dt className="text-muted-foreground">创意数量</dt>
            <dd>{config.creative_count}</dd>
          </dl>
        </DialogContent>
      </Dialog>
    </div>
  )
}
