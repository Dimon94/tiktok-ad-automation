import type { CatalogPublic } from "@/client"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  ACTION_LABELS,
  type Definition,
  FREQUENCY_LABELS,
  LEVEL_LABELS,
  MODE_LABELS,
  summarizeConditions,
  summarizeScope,
} from "../automation-rules/ruleForm"

type RuleSnapshot = {
  name?: string
  enabled?: boolean
  definition?: Definition
}
export function ruleSnapshot(value: unknown): RuleSnapshot {
  return value && typeof value === "object" ? (value as RuleSnapshot) : {}
}
export function proposalTitle(after: unknown) {
  return ruleSnapshot(after).name
}
export function publicationEffect(after: unknown) {
  const rule = ruleSnapshot(after)
  if (rule.enabled === false) return "确认后保存为停用规则，不会执行广告操作。"
  if (rule.enabled === true) {
    if (rule.definition?.mode === "AUTO")
      return "确认后规则将启用，命中条件时自动执行广告操作。"
    if (rule.definition?.mode === "NOTIFY")
      return "确认后规则将启用，仅发送通知，不修改广告。"
    return "确认后规则将启用，命中后仍需人工确认广告操作。"
  }
  return "确认后发布此变更，请先核对建议与影响范围。"
}

/** 按运营决策分组比较；账户范围只显示数量，绝不展开成上千行配置。 */
export function RuleProposalSummary({
  before,
  after,
  catalog,
}: {
  before: unknown
  after: unknown
  catalog?: CatalogPublic
}) {
  const old = ruleSnapshot(before),
    next = ruleSnapshot(after)
  const hasBefore = Object.keys(old).length > 0
  const def = next.definition
  if (
    !def?.accounts ||
    !def.apply_to ||
    !def.conditions ||
    !def.action ||
    !def.schedule
  )
    return (
      <p className="text-sm text-muted-foreground">
        此建议没有标准规则摘要，请展开技术记录核对完整变更后再决定。
      </p>
    )
  function rows(rule: RuleSnapshot) {
    const d = rule.definition
    if (!d) return []
    const action = d.action.adjustment
    const adjustment = action
      ? `，${{ INCREASE: "增加", DECREASE: "减少", SET: "调整至" }[action.mode]} ${action.value}${action.value_type === "PERCENT" ? "%" : ""}${action.limit ? `，限值 ${action.limit}` : ""}`
      : ""
    const schedule = d.schedule
    return [
      { label: "规则名称", value: rule.name || "未命名规则", raw: rule.name },
      {
        label: "保存后状态",
        value: rule.enabled ? "启用" : "停用",
        raw: rule.enabled,
      },
      {
        label: "适用范围",
        value: `${summarizeScope(d)} · ${LEVEL_LABELS[d.apply_to.level]} · ${{ ACTIVE: "正在投放", PAUSED: "已暂停", SELECTED: "指定对象" }[d.apply_to.status]}${d.apply_to.object_ids?.length ? `（${d.apply_to.object_ids.length} 个）` : ""}`,
        raw: [d.accounts, d.apply_to],
      },
      {
        label: "触发条件",
        value: catalog
          ? summarizeConditions(d, catalog)
          : "正在读取指标说明；完整条件见技术记录",
        raw: [d.aggregation, d.conditions],
      },
      {
        label: "执行方式",
        value: `${MODE_LABELS[d.mode]} · ${d.mode === "NOTIFY" ? "仅发送通知" : `${ACTION_LABELS[d.action.type]}${LEVEL_LABELS[d.action.level]}${adjustment}`}`,
        raw: [d.mode, d.action],
      },
      {
        label: "检查时间",
        value: `${FREQUENCY_LABELS[schedule.frequency]} · ${{ CONTINUOUS: "全天检查", CUSTOM: "指定时段", ONCE: "仅一次" }[schedule.window] ?? "按设定时段"} · ${schedule.timezone_basis === "ACCOUNT" ? "按账户当地时间" : "按规则时区"}${schedule.daily_at ? `，${schedule.daily_at}` : ""}${schedule.once_at ? `，${schedule.once_at}` : ""}${schedule.periods?.length ? `，${schedule.periods.map((p) => `${{ DAY: "每天", WEEK: "每周", MONTH: "每月" }[p.date_type]}${p.days?.join("、") || ""} ${p.start}–${p.end}`).join("；")}` : ""}`,
        raw: schedule,
      },
      {
        label: "通知",
        value: d.notification?.enabled
          ? `${d.notification.timing === "SUMMARY" ? "定时汇总通知" : "每次检查后通知"}${d.notification.summary_times?.length ? `，${d.notification.summary_times.join("、")}` : ""}；${d.notification.notify_without_hits ? "无命中也通知" : "仅有命中时通知"}`
          : "不发送通知",
        raw: d.notification,
      },
    ]
  }
  const previous = rows(old),
    upcoming = rows(next)
  const changed = upcoming.filter(
    (row, i) =>
      !hasBefore ||
      JSON.stringify(row.raw) !== JSON.stringify(previous[i]?.raw),
  )
  return (
    <div className="flex flex-col gap-2">
      {hasBefore && (
        <p className="text-sm text-muted-foreground">
          适用于 {summarizeScope(def)} 的{LEVEL_LABELS[def.apply_to.level]}
        </p>
      )}
      <h4 className="text-sm font-medium">
        {hasBefore ? "具体调整" : "新增规则内容"}
      </h4>
      <div className="overflow-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>调整内容</TableHead>
              {hasBefore && <TableHead>现在</TableHead>}
              <TableHead>{hasBefore ? "建议改为" : "建议设置"}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {changed.map((row) => {
              const previousRow = previous.find(
                (item) => item.label === row.label,
              )
              return (
                <TableRow key={row.label}>
                  <TableCell className="whitespace-normal font-medium">
                    {row.label}
                  </TableCell>
                  {hasBefore && (
                    <TableCell className="whitespace-normal">
                      {previousRow?.value || "尚未设置"}
                    </TableCell>
                  )}
                  <TableCell className="whitespace-normal">
                    {row.value}
                    {previousRow?.value === row.value && (
                      <span className="block text-xs text-muted-foreground">
                        具体设置有变化，请展开技术记录核对
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
            {!changed.length && (
              <TableRow>
                <TableCell colSpan={hasBefore ? 3 : 2}>
                  没有上述运营设置的变化；请在技术记录中核对其他变更。
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

/** 策略建议沿用相同业务对照，复杂设置留在完整记录中核对。 */
export function StrategyProposalSummary({
  before,
  after,
}: {
  before: unknown
  after: unknown
}) {
  function config(value: unknown): Record<string, unknown> {
    const snapshot =
      value && typeof value === "object"
        ? (value as Record<string, unknown>)
        : {}
    return (snapshot.config || snapshot) as Record<string, unknown>
  }
  const previous = config(before),
    next = config(after)
  const labels: Record<string, string> = {
    budget: "预算",
    currency: "币种",
    target_roas: "目标回收比",
    group_count: "广告组数量",
    ads_per_group: "每组广告数量",
    creative_count: "创意数量",
    creation_status: "搭建后状态",
    bid_strategy: "出价方式",
    budget_strategy: "预算层级",
    schedule_start_time: "开始时间",
    schedule_end_time: "结束时间",
  }
  const words: Record<string, string> = {
    ENABLE: "启用",
    DISABLE: "停用",
    HIGHEST_VALUE: "最高价值",
    TARGET_ROAS: "目标回收比",
    SERIES: "推广系列",
    ADGROUP: "广告组",
  }
  const changed = Array.from(
    new Set([...Object.keys(previous), ...Object.keys(next)]),
  ).filter((key) => JSON.stringify(previous[key]) !== JSON.stringify(next[key]))
  const readable = (value: unknown) =>
    value == null ? "未设置" : words[String(value)] || String(value)
  return (
    <div className="flex flex-col gap-2">
      <h4 className="text-sm font-medium">具体调整</h4>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>调整内容</TableHead>
            <TableHead>现在</TableHead>
            <TableHead>建议改为</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {changed
            .filter((key) => labels[key])
            .map((key) => (
              <TableRow key={key}>
                <TableCell>{labels[key]}</TableCell>
                <TableCell>{readable(previous[key])}</TableCell>
                <TableCell>{readable(next[key])}</TableCell>
              </TableRow>
            ))}
        </TableBody>
      </Table>
      {changed.some((key) => !labels[key]) && (
        <p className="text-sm text-muted-foreground">
          另有复杂策略设置变化，请展开技术记录核对完整设置后再确认。
        </p>
      )}
    </div>
  )
}
