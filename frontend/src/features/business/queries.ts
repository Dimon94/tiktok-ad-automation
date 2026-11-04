import { BusinessService } from "@/client"

export type BusinessOptions = {
  end_date: string
  accounts: {
    id: string
    name: string
    currency: string
    timezone: string
    end_date?: string
  }[]
  providers: { id: string; name: string }[]
}
export const businessKey = (tenantId: string) =>
  ["tenant", tenantId, "business"] as const
export const rateKey = (tenantId: string, kind: string, scopeId: string) =>
  [...businessKey(tenantId), "rate", kind, scopeId] as const

export function shiftedDay(day: string, amount: number) {
  const value = new Date(`${day}T12:00:00Z`)
  value.setUTCDate(value.getUTCDate() + amount)
  return value.toISOString().slice(0, 10)
}

export function formatValue(
  value: string | number | null | undefined,
  ratio = false,
) {
  if (value == null) return "—"
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: ratio ? 4 : 2,
    maximumFractionDigits: ratio ? 4 : 2,
  })
}

export function percentageRate(value: string) {
  const [whole, fraction = ""] = value.split(".")
  const precision = fraction.length + 2
  const digits = BigInt(`${whole || "0"}${fraction}`)
    .toString()
    .padStart(precision + 1, "0")
  return `${digits.slice(0, -precision)}.${digits.slice(-precision)}`
    .replace(/0+$/, "")
    .replace(/\.$/, "")
}

export const optionsQuery = (tenantId: string, bcId: string) => ({
  queryKey: [...businessKey(tenantId), "options", bcId],
  queryFn: async ({ signal }: { signal: AbortSignal }) =>
    (
      await BusinessService.options({
        path: { tenant_id: tenantId },
        query: { bc_id: bcId },
        signal,
      })
    ).data as unknown as BusinessOptions,
})

export const ISSUE_LABELS: Record<string, string> = {
  DRAMA_UNMAPPED:
    "部分系列的版权方标签无对应连接或存在歧义，相关经营收入暂不核算",
  DRAMA_IDENTITY_CONFLICT:
    "部分系列在期间内出现不同剧目身份，相关补贴暂留空，请核对系列归属",
  DRAMA_FROM_REPORT_NAME:
    "部分历史剧目按广告报表命名识别；没有可靠剧编号的分组暂不预计新剧补贴",
  COMPENSATION_EVIDENCE_MISSING:
    "部分赔付缺少当日出价或资格依据，预计金额暂留空",
  SUBSIDY_SCOPE_INCOMPLETE:
    "补贴所需季度历史、币种或归属信息未齐，相关预计金额暂留空",
  SUBSIDY_POLICY_UNCONFIGURED: "所选日期尚未配置补贴政策，预计补贴暂不可计算",
  SOURCE_INCOMPLETE: "所选日期存在日报缺口，金额为已核算小计",
  POLICY_SCOPE_RESTRICTED: "补贴依赖范围包含未授权账户，预计补贴暂不展示",
}
