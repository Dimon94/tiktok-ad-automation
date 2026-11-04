import { CircleHelp } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { ISSUE_LABELS } from "./queries"

export const METRIC_HELP: Record<string, string> = {
  spend: "所选期间平台消耗，按账户当地完整日统计，不含今天。",
  revenue: "所选期间平台广告收入。币种按当前筛选，不做自动换汇。",
  platform_roas:
    "平台总收入 ÷ 总消耗。合计后计算，不平均各行ROAS；消耗为0时留空。",
  provider_share: "平台收入 × 当日生效的版权方抽成比例。",
  net_revenue: "平台收入减去版权方分成。",
  rebate: "平台消耗 × 当日生效的其他返点比例。",
  business_revenue: "分成后收入加其他返点，不含预计赔付和新剧补贴。",
  business_roas: "经营收入 ÷ 总消耗。合计后计算，不平均各行ROAS。",
  scenario_revenue:
    "基础经营收入；选择含预计情景后，加预计赔付及所选一种新剧补贴。依据不足时留空。",
  scenario_roas: "当前经营口径的合计收入 ÷ 合计消耗。消耗为0时留空。",
  compensation: "按系列逐日核算的预计赔付；缺少当日出价或适用资格依据时留空。",
  subsidy_comic:
    "新开漫剧预计补贴。按季度累计消耗对应档位计算，仅展示所选期间的增量；系列按季度累计消耗占比分摊。三种情景独立，不相加。",
  subsidy_other:
    "新开其他剧预计补贴。按季度累计消耗对应档位计算，仅展示所选期间的增量；系列按季度累计消耗占比分摊。三种情景独立，不相加。",
  subsidy_existing:
    "在投闭环预计补贴。按季度累计消耗对应档位计算，仅展示所选期间的增量；系列按季度累计消耗占比分摊。三种情景独立，不相加。",
}

export function MetricHint({
  metric,
  label,
  note,
}: {
  metric: string
  label: string
  note?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`${label}说明`}
          className="inline-flex shrink-0 items-center text-muted-foreground"
        >
          <CircleHelp className="size-3.5" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-80">
        {METRIC_HELP[metric]}
        {note ? ` ${note}` : ""}
      </TooltipContent>
    </Tooltip>
  )
}

export function BusinessNotes({
  issues,
  pending,
}: {
  issues: string[]
  pending: boolean
}) {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm">
          <CircleHelp data-icon="inline-start" />
          数据说明{issues.length ? ` · ${issues.length}` : ""}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>数据说明</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-[60vh] flex-col gap-3 overflow-y-auto text-sm">
          {pending && (
            <p>
              后台正在核算，最近区间优先生成，历史分批补齐。当前展示已有结果。
            </p>
          )}
          {issues.map((issue) => (
            <p key={issue}>{ISSUE_LABELS[issue] ?? issue}</p>
          ))}
          <p>
            按账户当地日期统计，不包含当天。存在日报缺口时，卡片展示已核算小计。
          </p>
          <p>抽成和返点使用业务日期对应的生效比例；补贴及赔付均为预计值。</p>
        </div>
      </DialogContent>
    </Dialog>
  )
}
