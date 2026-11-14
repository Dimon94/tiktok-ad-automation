import { CircleAlert, CircleCheck, LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"
import type {
  SyncAccountStatus,
  SyncHealthPublic,
  SyncStatusPublic,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"

import { cn } from "@/lib/utils"

type Tone = "ok" | "pending" | "warn" | "error"

const TONE_CLASS: Record<Tone, string> = {
  ok: "text-muted-foreground",
  pending: "text-muted-foreground",
  warn: "text-amber-600",
  error: "text-destructive",
}

const HEALTH_COUNTS = [
  ["stale_ad_accounts", "广告", "个账户超时"],
  ["stale_material_accounts", "素材", "个账户超时"],
  ["missing_directory_ads", "目录", "条待补齐或刷新"],
  ["unmapped_creatives", "创意", "项待关联"],
  ["pending_actions", "动作", "项积压"],
] as const
const HEALTH_ISSUES: Record<string, string> = {
  ACCOUNT_SCREEN_STALE: "账户筛选已过期，活跃账户覆盖待确认",
  MONITOR_PENDING: "健康监测尚未完成，当前同步状态待确认",
  MONITOR_STALE: "健康监测已过期，当前同步状态待确认",
  MONITOR_QUERY_FAILED: "健康监测读取失败，当前同步状态待确认",
  COVERAGE_PENDING: "目录与素材关联尚未核验完成",
  COVERAGE_STALE: "目录与素材关联核验已过期，完整性待确认",
  COVERAGE_QUERY_FAILED: "目录与素材关联核验失败，完整性待确认",
}

function healthWarnings(
  health: SyncHealthPublic | null | undefined,
  now: number,
): string[] {
  if (!health || health.status === "DISABLED") return []
  // 页面没有重新取得响应时，完整覆盖观测也不能随分钟水位或本地时钟续期。
  const issues = new Set(health.issues ?? [])
  if (
    health.coverage_observed_at &&
    new Date(health.coverage_observed_at).getTime() <= now - 5 * 60_000
  )
    issues.add("COVERAGE_STALE")
  if (health.status !== "DEGRADED" && !issues.size) return []
  const messages: string[] = []
  const explained = new Set<string>()
  for (const [key, label, unit] of HEALTH_COUNTS) {
    const count = health[key] ?? 0
    if (count > 0) {
      messages.push(`该 BC ${label} ${count} ${unit}`)
      explained.add(key)
    }
  }
  let unknown = false
  for (const issue of issues) {
    if (HEALTH_ISSUES[issue]) messages.push(`该 BC ${HEALTH_ISSUES[issue]}`)
    else if (!explained.has(issue)) unknown = true
  }
  // 全零不代表监测已成功；新原因或缺失原因也必须保留降级结论。
  if (unknown || !messages.length)
    messages.push("该 BC 同步健康异常，原因待确认")
  return messages
}

function accountTime(account: SyncAccountStatus) {
  return new Date(account.last_success_at!).toLocaleTimeString("zh-CN", {
    timeZone: account.timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })
}

/** 范围覆盖由最旧账户决定；混时区时明确统一显示为浏览器本地时间。 */
function oldestUpdate(accounts: SyncAccountStatus[]): string | null {
  const synced = accounts.filter((account) => account.last_success_at)
  if (!synced.length) return null
  const oldest = synced.reduce((a, b) =>
    Date.parse(a.last_success_at!) < Date.parse(b.last_success_at!) ? a : b,
  )
  const timezones = new Set(synced.map((account) => account.timezone))
  return timezones.size === 1
    ? `最旧有效数据更新到 ${accountTime(oldest)}（账户本地）`
    : `最旧有效数据更新到 ${new Date(oldest.last_success_at!).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })}（浏览器本地，账户时区不同）`
}

/**
 * 报表页唯一的状态区：把同步计划、各账户同步进度和查询覆盖收敛成一行结论。
 * 账户级明细是排查用信息，默认收起到抽屉里，不再逐个账户铺在页面上。
 */
export function ReportStatusBar({
  status,
  selectedAdvertiserIds,
  startDate,
  coverage,
  notice,
  onLoadLatest,
}: {
  status?: SyncStatusPublic
  selectedAdvertiserIds?: string[]
  startDate?: string
  coverage?: Record<string, unknown>
  notice: string | null
  /** 正在操作时保留快照，用户仍可主动加载后台发布的新数据。 */
  onLoadLatest?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    // 同步停滞时接口内容可能不变，时效提示仍须随时间更新，卸载后停止计时。
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])
  const accounts = (status?.accounts ?? []).filter(
    (account) =>
      !selectedAdvertiserIds?.length ||
      selectedAdvertiserIds.includes(account.advertiser_id),
  )
  // 页面日期是各账户的本地日；尚未进入查询起日的账户不计作缺数或失败。
  const future = accounts.filter(
    (account) =>
      startDate && account.local_today && account.local_today < startDate,
  )
  const applicable = accounts.filter((account) => !future.includes(account))
  const pending = applicable.filter((account) => !account.last_success_at)
  const stale = applicable.filter(
    (account) =>
      account.last_success_at &&
      now - Date.parse(account.last_success_at) > 20 * 60_000,
  )
  const incomplete = applicable.filter(
    (account) =>
      (account.incomplete_dates ?? []).length > 0 ||
      (account.failed_dates ?? []).length > 0,
  )
  const coverageStatus = String(
    coverage?.status ?? coverage?.state ?? "",
  ).toUpperCase()

  let tone: Tone = "ok"
  let headline: string
  const reason = status?.plan.disabled_reason
  if (reason === "sync_disabled") {
    tone = "error"
    headline = "报表同步未开启"
  } else if (reason === "api_binding_required") {
    tone = "error"
    headline = "该 BC 未接入 API，无法同步报表"
  } else if (
    status &&
    applicable.length &&
    pending.length === applicable.length
  ) {
    tone = "pending"
    headline = "首次同步进行中，完成后自动显示数据"
  } else {
    headline =
      status && accounts.length && !applicable.length
        ? `${future.length} 个账户当地尚未到所选日期`
        : ((status && oldestUpdate(applicable)) ?? "正在读取同步状态")
  }

  const extras: { text: string; tone: Tone }[] = [
    { text: "查询日期按各账户本地日", tone: "ok" },
  ]
  for (const [level, label] of [["campaign", "系列"], ["adgroup", "广告组"], ["ad", "广告"]]) {
    const buckets = accounts.flatMap(account => account.levels ?? []).filter(bucket => bucket.level === level)
    if (!buckets.length) continue
    const complete = buckets.filter(bucket => bucket.status === "COMPLETE").length
    const success = buckets.map(bucket => bucket.last_success_at).filter(Boolean).sort()[0]
    const attempt = buckets.map(bucket => bucket.last_attempt_at).filter(Boolean).sort().slice(-1)[0]
    extras.push({ text: `${label}：${complete}/${buckets.length} 账户日完整；成功 ${success ? new Date(success).toLocaleString("zh-CN") : "—"}；尝试 ${attempt ? new Date(attempt).toLocaleString("zh-CN") : "—"}`, tone: complete === buckets.length ? "ok" : "warn" })
  }
  if (stale.length)
    extras.push({
      text: `${stale.length} 个账户数据超过 20 分钟`,
      tone: "warn",
    })
  if (future.length && applicable.length)
    extras.push({
      text: `${future.length} 个账户当地尚未到所选日期`,
      tone: "ok",
    })
  for (const text of healthWarnings(status?.health, now))
    extras.push({ text, tone: "error" })
  if (status?.health?.coverage_observed_at)
    extras.push({
      text: `目录与素材关联核验于 ${new Date(status.health.coverage_observed_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}`,
      tone: "ok",
    })
  // 历史回填只覆盖到开户日之后的部分区间：告诉用户从哪天起数据完整，
  // 选择更早的日期时就能理解缺口来自回填范围，而不是同步出错。
  if (status?.complete_from)
    extras.push({ text: `完整数据自 ${status.complete_from} 起`, tone: "ok" })
  if (pending.length && pending.length < applicable.length)
    extras.push({ text: `${pending.length} 个账户同步中`, tone: "pending" })
  if (incomplete.length)
    extras.push({ text: `${incomplete.length} 个账户有日期缺失`, tone: "warn" })
  if (status?.plan.enabled && status.plan.slowed)
    extras.push({ text: "历史补数暂缓", tone: "warn" })
  // 未来本地日保持空事实/部分汇总；若账户状态没有真正缺口，不把这种部分覆盖说成同步失败。
  const futureOnlyGap =
    future.length > 0 && !incomplete.length && !pending.length
  if (applicable.length && !futureOnlyGap && coverageStatus === "FAILED")
    extras.push({ text: "部分数据读取失败，请刷新重试", tone: "error" })
  else if (
    applicable.length &&
    !(futureOnlyGap && coverageStatus === "INCOMPLETE") &&
    !incomplete.length &&
    !pending.length &&
    ["INCOMPLETE", "PENDING", "QUEUED", "RUNNING", "WAITING_REMOTE"].includes(
      coverageStatus,
    )
  )
    extras.push({ text: "当前范围部分数据仍在同步", tone: "pending" })
  // 刷新按钮的回执与主结论相同时不重复显示。
  if (notice && notice !== headline) extras.push({ text: notice, tone: "ok" })

  // 更新时间只反映所选账户的数据覆盖；全 BC 的目录、关联等告警独立着色。
  if (tone === "ok" && (stale.length || incomplete.length)) tone = "warn"
  const Icon =
    tone === "pending"
      ? LoaderCircle
      : tone === "ok"
        ? CircleCheck
        : CircleAlert

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
    >
      <span
        className={cn("inline-flex items-center gap-1.5", TONE_CLASS[tone])}
      >
        <Icon
          className={cn("size-4", tone === "pending" && "animate-spin")}
          aria-hidden="true"
        />
        {headline}
      </span>
      {extras.map((item) => (
        <span key={item.text} className={cn("text-xs", TONE_CLASS[item.tone])}>
          {item.text}
        </span>
      ))}
      {onLoadLatest && (
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={onLoadLatest}
        >
          有新数据，点击加载
        </Button>
      )}
      {accounts.length > 0 && (
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={() => setOpen(true)}
        >
          同步明细
        </Button>
      )}
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-md">
          <SheetHeader>
            <SheetTitle>账户同步明细</SheetTitle>
            <SheetDescription>
              共 {accounts.length}{" "}
              个账户，查询日期及明细时间均为各账户本地时间。
            </SheetDescription>
          </SheetHeader>
          <ul className="divide-y px-4 text-xs">
            {accounts.map((account) => {
              const missing = [
                ...(account.incomplete_dates ?? []),
                ...(account.failed_dates ?? []),
              ]
              return (
                <li key={account.advertiser_id} className="space-y-0.5 py-2">
                  <div className="flex justify-between gap-2">
                    <span className="font-mono">{account.advertiser_id}</span>
                    <span className="text-muted-foreground">
                      {account.last_success_at
                        ? `数据更新到 ${accountTime(account)}（账户本地）`
                        : "同步中"}
                    </span>
                  </div>
                  {future.includes(account) && (
                    <p className="text-muted-foreground">
                      账户当地今天为 {account.local_today}，尚未到查询日期
                    </p>
                  )}
                  {!future.includes(account) && missing.length > 0 && (
                    <p className="text-amber-600">
                      以下日期数据不完整：{missing.join("、")}
                    </p>
                  )}
                </li>
              )
            })}
          </ul>
        </SheetContent>
      </Sheet>
    </div>
  )
}
