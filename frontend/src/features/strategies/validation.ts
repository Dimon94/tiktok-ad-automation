import type { StrategyConfig_Output } from "@/client"
import { normalizedTargeting } from "@/features/targeting/TargetingForm"
export const DEFAULT_NAME_TEMPLATE = "{provider_drama}-{drama_id}"
// 固定结构会直接展开为内存计划，必须与后端保持同一安全上限，避免误填值触发巨量数组。
export const MAX_FIXED_GROUP_COUNT = 100
export const MAX_FIXED_ADS_PER_GROUP = 100
export const NAME_LABELS: Record<string, string> = {
  provider_drama: "版权方＋剧名",
  provider_label: "版权方标识",
  drama_name: "剧名",
  drama_id: "剧目 ID",
  YYYYMMDD: "日期",
  provider_pinyin: "版权方拼音",
}
const NAME_FIELDS = Object.keys(NAME_LABELS)

// 编辑界面使用中文字段，API 只保存规范变量；字面花括号保持原样。
export function displayNameTemplate(value: string) {
  return value.replace(/\{\{|\}\}|\{([^{}]+)\}/g, (token, field: string) =>
    NAME_FIELDS.includes(field) ? `[${NAME_LABELS[field]}]` : token,
  )
}
export function parseNameTemplate(value: string) {
  return value
    .replace(/\[([^[\]]*)\]/g, (_, label: string) => {
      const field = NAME_FIELDS.find((key) => NAME_LABELS[key] === label)
      return `{${field || label}}`
    })
    .replace(/\[/g, "{")
    .replace(/\]/g, "}")
}
function templateError(
  value: string,
  allowed: string[],
  required: string[],
): string | undefined {
  if (!value.trim()) return "请填写命名模板。"
  if (Array.from(value).length > 1000) return "模板不能超过 1000 个字符。"
  if (
    Array.from(value).some(
      (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
    )
  )
    return "模板不能包含控制字符。"
  const fields = new Set<string>()
  for (let i = 0; i < value.length; i++) {
    if (
      (value[i] === "{" && value[i + 1] === "{") ||
      (value[i] === "}" && value[i + 1] === "}")
    ) {
      i++
      continue
    }
    if (value[i] === "}") return "模板括号不完整。"
    if (value[i] !== "{") continue
    const end = value.indexOf("}", i + 1),
      field = value.slice(i + 1, end)
    if (end < 0 || !allowed.includes(field))
      return "请使用剧名、版权方标识、剧目 ID、日期或版权方拼音字段；批次编号由系统自动添加。"
    if (fields.has(field)) return `${NAME_LABELS[field]}只能出现一次。`
    fields.add(field)
    i = end
  }
  const missing = required.filter((field) => !fields.has(field))
  if (missing.length)
    return `请保留${missing.map((name) => NAME_LABELS[name]).join("、")}字段。`
}
export function nameTemplateError(value: string) {
  const error = templateError(value, NAME_FIELDS, ["drama_id"])
  if (error) return error
  // 排除转义的字面花括号，再校验组合字段与拆分字段互斥。
  const fields = new Set(
    Array.from(value.matchAll(/\{\{|\}\}|\{([^{}]+)\}/g), (match) => match[1]),
  )
  if (fields.has("provider_drama")) {
    if (fields.has("provider_label") || fields.has("drama_name"))
      return "版权方＋剧名与独立的版权方标识、剧名字段不能同时使用。"
  } else if (!fields.has("provider_label") || !fields.has("drama_name")) {
    return "请保留版权方标识、剧名字段，或使用版权方＋剧名组合字段。"
  }
}
export function renderNameExample(
  value: string,
  providerDrama = "jiashu-The Bond",
  dramaId = "106001",
  providerPinyin = "jiashu",
) {
  if (nameTemplateError(value)) return null
  const values: Record<string, string> = {
    "{{": "{",
    "}}": "}",
    "{provider_drama}": providerDrama,
    "{provider_label}": providerDrama.endsWith("-The Bond")
      ? providerDrama.slice(0, -"-The Bond".length)
      : providerDrama,
    "{drama_name}": "The Bond",
    "{drama_id}": dramaId,
    "{provider_pinyin}": providerPinyin,
    "{YYYYMMDD}": "20260913",
  }
  return `${value.replace(/\{\{|\}\}|\{[^{}]+\}/g, (token) => values[token]!)}-A7K2`
}
export function normalizeDecimal(value: string) {
  let plainValue = value
  const exponential = /^(\d+)(?:\.(\d+))?[eE]([+-]?\d+)$/.exec(value)
  if (exponential) {
    const [, whole, fraction = "", exponent] = exponential
    const digits = whole + fraction
    if (!/[1-9]/.test(digits)) return "0"
    // API 的 Decimal 可能使用指数形式；仅移动小数点，金额本身不转 Number，避免精度丢失。
    const decimalPosition = whole.length + Number(exponent)
    if (decimalPosition <= 0) {
      plainValue = `0.${"0".repeat(-decimalPosition)}${digits}`
    } else if (decimalPosition >= digits.length) {
      plainValue = digits + "0".repeat(decimalPosition - digits.length)
    } else {
      plainValue = `${digits.slice(0, decimalPosition)}.${digits.slice(decimalPosition)}`
    }
  }
  const [whole, fraction = ""] = plainValue.split(".")
  return `${whole.replace(/^0+(?=\d)/, "") || "0"}${fraction.replace(/0+$/, "") ? `.${fraction.replace(/0+$/, "")}` : ""}`
}
export function decimalError(value: string) {
  if (!/^\d+(\.\d+)?$/.test(value) || !/[1-9]/.test(value))
    return "请输入大于 0 的十进制数。"
  const normalized = normalizeDecimal(value),
    [whole, fraction = ""] = normalized.split(".")
  if (
    fraction.length > 12 ||
    whole.replace(/^0+/, "").length > 26 ||
    whole.replace(/^0+/, "").length + fraction.length > 38
  )
    return "最多 26 位整数、12 位小数。"
}
export const configFingerprint = (config: StrategyConfig_Output) =>
  JSON.stringify({
    targeting: normalizedTargeting(config.targeting),
    currency: config.currency,
    budget_strategy: config.budget_strategy ?? "SERIES",
    bid_strategy: config.bid_strategy ?? "HIGHEST_VALUE",
    creation_status: config.creation_status ?? "ENABLE",
    group_generation_mode: config.group_generation_mode ?? "FIXED",
    group_count: config.group_count ?? null,
    group_material_allocation: config.group_material_allocation ?? null,
    max_materials_per_group: config.max_materials_per_group ?? null,
    ad_generation_mode: config.ad_generation_mode ?? "BY_MATERIAL",
    ads_per_group: config.ads_per_group ?? null,
    ad_material_allocation: config.ad_material_allocation ?? null,
    max_materials_per_ad: config.max_materials_per_ad ?? null,
    creative_count: config.creative_count ?? 1,
    copy_pool_version: config.copy_pool_version,
    budget: normalizeDecimal(config.budget),
    target_roas: config.target_roas
      ? normalizeDecimal(config.target_roas)
      : null,
    cta_option_ids: config.cta_option_ids || [],
    campaign_name_template:
      config.campaign_name_template ?? DEFAULT_NAME_TEMPLATE,
  })
export const issueMessages: Record<string, string> = {
  configuration_invalid: "整体策略配置无效，请检查各项生成规则和互斥字段。",
  copy_pool_exhausted: "创意数量超过有效且不重复的英文文案数。",
  copy_pool_not_found: "引用的文案池版本不可用。",
  invalid_name_template: "广告名称格式无效，请检查字段和括号。",
  invalid_cta_options: "已有 CTA 配置无效，请联系管理员核实。",
  invalid_strategy_name: "请输入有效策略名称。",
  version_conflict: "服务器版本已变化，本地输入已保留。",
  idempotency_conflict: "保存请求身份与内容不一致，请核实原保存结果。",
}
