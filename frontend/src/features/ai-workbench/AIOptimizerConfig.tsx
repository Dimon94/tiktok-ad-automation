import {
  optimizationModeLabel,
  priorityLabel,
  workbenchNumber,
} from "./presentation"
import { ArrowUpRight, LockKeyhole } from "lucide-react"
import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { AIOptimizer } from "./AIOptimizerList"
import { aiRequest, errorMessage } from "./api"

type Props = {
  tenantId: string
  optimizer: AIOptimizer | null
  recordId?: string
  versionId?: string
  onEdit: () => void
  onSaved: (optimizer: AIOptimizer) => void
  returnContext: string
}

/** 投手只绑定一个策略版本；编辑入口复用既有页面，并携带完整返回上下文。 */
export function AIOptimizerConfig({
  tenantId,
  optimizer,
  recordId,
  returnContext,
  onEdit,
  onSaved,
}: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function toggleActive() {
    if (!optimizer) return
    setBusy(true)
    setError(null)
    try {
      onSaved(
        await aiRequest<AIOptimizer>(
          `/api/tenants/${tenantId}/ai-optimizers/${optimizer.id}`,
          {
            method: "PATCH",
            body: {
              active: !optimizer.active,
              request_id: crypto.randomUUID(),
            },
          },
        ),
      )
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy(false)
    }
  }
  if (!optimizer)
    return (
      <Card>
        <CardContent className="py-10 text-sm text-muted-foreground">
          创建或选择一个 AI 投手。
        </CardContent>
      </Card>
    )
  const scopeParams = `ai_optimizer_id=${encodeURIComponent(optimizer.id)}&record_id=${encodeURIComponent(recordId ?? "")}&version_id=${encodeURIComponent(optimizer.strategy_version_id)}&return_to=${encodeURIComponent(returnContext)}`
  const strategyHref = `/tenants/${tenantId}/strategies?${scopeParams}&scope=bound&readonly=true`
  const ruleIds = optimizer.rule_ids ?? []
  const rulesHref = `/tenants/${tenantId}/automation-rules?${scopeParams}&scope=bound&rule_ids=${encodeURIComponent(ruleIds.join(","))}`
  return (
    <div
      className="grid min-w-0 gap-4 xl:grid-cols-2"
      data-testid="ai-optimizer-config"
    >
      <Card>
        <CardHeader>
          <CardTitle>
            投手配置 · {optimizer.active ? "运行中" : "已暂停"}
          </CardTitle>
          <CardDescription>
            当前版本保存后，执行会重新校验租户、BC 和授权范围。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <div className="flex gap-2">
            <Button onClick={onEdit}>编辑投手</Button>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void toggleActive()}
            >
              {busy ? "处理中…" : optimizer.active ? "暂停投手" : "恢复投手"}
            </Button>
          </div>
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">BC</span>
            <strong>{optimizer.bc_id}</strong>
          </div>
          <div className="flex items-start justify-between gap-4">
            <span className="text-muted-foreground">广告账户</span>
            <span className="text-right font-medium">
              {optimizer.advertiser_ids.length} 个账户
            </span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">优化模式</span>
            <Badge>{optimizationModeLabel(optimizer.optimization_mode)}</Badge>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">目标优先级</span>
            <span>{priorityLabel(optimizer.priority)}</span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">日消耗目标</span>
            <span>{workbenchNumber(optimizer.daily_spend)}</span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">目标 ROAS</span>
            <span>{workbenchNumber(optimizer.target_roas)}</span>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>唯一绑定与剧单排期</CardTitle>
          <CardDescription>
            一个投手只绑定一个策略版本；剧单排期不选择素材。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <div className="grid gap-2">
            <Label htmlFor="ai-bound-strategy">绑定策略版本</Label>
            <select
              id="ai-bound-strategy"
              aria-label="绑定策略版本"
              className="h-9 rounded-md border bg-background px-3"
              value={optimizer.strategy_version_id}
              disabled
            >
              <option value={optimizer.strategy_version_id}>
                {optimizer.strategy_version_id}
              </option>
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="ai-schedule-frequency">运行频率</Label>
            <select
              id="ai-schedule-frequency"
              aria-label="运行频率（只读）"
              className="h-9 rounded-md border bg-background px-3"
              value="DAILY"
              disabled
            >
              <option value="DAILY">每天</option>
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="ai-schedule-time">每日复盘时间</Label>
            <Input
              id="ai-schedule-time"
              aria-label="复盘时间（只读）"
              type="time"
              value={(optimizer.review_time ?? "13:00").slice(0, 5)}
              disabled
            />
          </div>
          <p className="text-xs text-muted-foreground">
            运营时区：{optimizer.timezone ?? "Asia/Shanghai"}
            。搭建执行时间按每日剧单逐行设置。
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>已绑定配置</CardTitle>
          <CardDescription>
            这里打开的是当前投手实际绑定的策略版本和自动规则，编辑仍在原配置模块完成。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          <Button asChild variant="outline">
            <a href={strategyHref}>
              <LockKeyhole />
              打开当前绑定策略 <ArrowUpRight />
            </a>
          </Button>
          <Button asChild variant="outline">
            <a href={rulesHref}>
              <LockKeyhole />
              打开当前绑定规则 <ArrowUpRight />
            </a>
          </Button>
          <p className="basis-full text-xs text-muted-foreground">
            已绑定一个策略版本、{ruleIds.length} 条自动规则。
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
