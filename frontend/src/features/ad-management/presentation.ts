import type {
  ManagementCounts,
  ManagementTaskMutation,
  ManagementTaskPublic,
} from "@/client"

export const statusLabel: Record<string, string> = {
  PREPARING: "准备中",
  READY: "待提交",
  QUEUED: "排队中",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  NEEDS_REVIEW: "需要核查",
  CANCELLED: "已取消",
}

export const KIND_LABEL: Record<string, string> = {
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  creative: "素材",
  material: "素材",
  account: "账户",
}
const FIELD_LABEL: Record<ManagementTaskMutation["field"], string> = {
  budget: "预算",
  roas: "目标 ROAS",
  status: "启停",
  name: "名称",
}

// 改名动作没有数值，摘要按改名规则区分。
const RENAME_KIND_LABEL: Record<
  NonNullable<ManagementTaskMutation["rename_kind"]>,
  string
> = {
  replace: "改名 · 查找替换",
  affix: "改名 · 前缀后缀",
  explicit: "改名 · 表格粘贴",
}

/**
 * 改名相关原因代码的中文说明：预览项的不可修改原因（后端 rename.py），
 * 以及表格粘贴解析接口返回的行问题（id_invalid / not_found / duplicate_campaign_id）。
 */
export const renameReasonLabel: Record<string, string> = {
  no_change: "新名称与原名称相同，跳过",
  already_deleted: "对象已删除，无需暂停",
  management_local_deferred: "等待本地资源后继续执行",
  name_empty: "新名称为空",
  name_too_long: "新名称超过 512 个字符",
  name_has_emoji: "新名称不能包含表情符号",
  name_duplicate_in_account: "账户内已有同名推广系列",
  name_duplicate_in_batch: "本批中有同名",
  rename_campaign_only: "只能修改推广系列的名称",
  rename_missing: "表格中没有该推广系列的新名称",
  id_invalid: "ID 格式无效，请把表格列设为文本后重新复制",
  not_found: "未找到该推广系列",
  duplicate_campaign_id: "表格中该 ID 出现多次",
}

/** 动作摘要，例如「暂停」「预算 设为 500」「目标 ROAS 提高 10%」。 */
export function mutationText(mutation?: ManagementTaskMutation | null) {
  if (!mutation) return "—"
  if (mutation.field === "status")
    return mutation.value === "ENABLE" ? "启用" : "暂停"
  if (mutation.field === "name")
    return mutation.rename_kind
      ? RENAME_KIND_LABEL[mutation.rename_kind]
      : FIELD_LABEL.name
  const field = FIELD_LABEL[mutation.field]
  const value = String(mutation.value)
  switch (mutation.mode) {
    case "set":
      return `${field} 设为 ${value}`
    case "add":
      return `${field} 增加 ${value}`
    case "subtract":
      return `${field} 减少 ${value}`
    case "increase_percent":
      return `${field} 提高 ${value}%`
    case "decrease_percent":
      return `${field} 降低 ${value}%`
  }
}

/** 按层级列出目标对象数，例如「广告组 3 · 系列 1」。 */
export function targetText(task: ManagementTaskPublic) {
  const entries = Object.entries(task.target_kinds ?? {})
  if (!entries.length) return `${task.counts?.targets ?? 0} 个对象`
  return entries
    .map(([kind, total]) => `${KIND_LABEL[kind] ?? kind} ${total}`)
    .join(" · ")
}

/** 来源：自动规则显示规则名，规则已删除时只标“自动规则”；其余为人工操作人。 */
export function sourceText(task: ManagementTaskPublic) {
  if (task.source?.kind === "rule")
    return task.source.rule_name
      ? `自动规则 · ${task.source.rule_name}`
      : "自动规则（已删除）"
  return task.actor_name ? `人工 · ${task.actor_name}` : "人工"
}

/** 结果计数：只列出非零项，未知结果单独提示需要核查。 */
export function resultParts(counts?: ManagementCounts) {
  const parts: Array<{ label: string; value: number; warn?: boolean }> = [
    { label: "成功", value: counts?.accepted ?? 0 },
    { label: "失败", value: counts?.rejected ?? 0, warn: true },
    { label: "待核查", value: counts?.unknown ?? 0, warn: true },
    { label: "未发送", value: counts?.not_sent ?? 0 },
    { label: "待执行", value: counts?.pending ?? 0 },
    { label: "不支持", value: counts?.unsupported ?? 0 },
  ]
  return parts.filter((part) => part.value > 0)
}

export function displayTime(value?: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—"
}

// 平台接收与目录回读是不同阶段，不能把“已接收”显示成“已投放”。
const RECEIPT_LABEL: Record<string, string> = {
  PENDING: "待执行",
  ACCEPTED: "平台已接收",
  REJECTED: "平台已拒绝",
  NOT_SENT: "未发送",
  UNKNOWN: "结果待核查",
  NO_CHANGE: "无需修改",
  CONFLICT: "数据冲突",
  UNSUPPORTED: "不支持",
  CANCELLED: "已取消",
  DELIVERED: "已送达",
  ATTEMPT_RECORDED: "已记录请求",
  REFRESH_PENDING: "等待数据刷新",
  NEEDS_REVIEW: "需要核查",
  RECONCILE_REQUESTED: "等待平台只读核查",
  OBSERVED_MATCH: "观察值符合目标，受理仍未知",
  OBSERVED_DIFFERENT: "观察值与目标不同，受理仍未知",
  RECONCILE_FAILED: "核查未取得新鲜证据，需人工检查",
  admission_deferred: "调用额度不足，未发送",
  management_preflight_failed: "发送前校验失败，未发送",
  management_result_unknown: "平台响应不明确，请核查",
  tiktok_call_deadline_exceeded: "请求准备超时，未发送",
  management_original_changed: "原值已变化，请重新操作",
  management_capability_unverified: "操作权限尚未核实",
  management_preview_expired: "操作预览已过期",
  route_authorization_changed: "授权已变化，请重新操作",
  route_binding_changed: "BC 绑定已变化，请重新操作",
  route_contract_changed: "接口能力已变化，请重新操作",
  account_access_denied: "账户授权不可用",
  connection_unavailable: "连接不可用",
}

/** 未知状态给运营中文提示，原始代码保留在 title 中供排查。 */
export function receiptLabel(value?: string | null) {
  return value ? (RECEIPT_LABEL[value] ?? "需要核查") : "—"
}
