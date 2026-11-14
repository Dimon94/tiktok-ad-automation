// 设置页签的"字段 → 中文标签 → 值格式化"映射（规格 6.1）。
// 本文件不得引入 `@/` 别名：纯函数用例直接以相对路径导入。

export type ConfigKind = "campaign" | "adgroup" | "ad"
export type ConfigSection = {
  title: string
  items: Array<{ label: string; value: string }>
}
type Field = { key: string; label: string; format?: (value: unknown) => string }

const ENUMS: Record<string, string> = {
  APP_PROMOTION: "应用推广",
  MINIS: "Minis",
  REGULAR_CAMPAIGN: "常规系列",
  BUDGET_MODE_DYNAMIC_DAILY_BUDGET: "动态日预算",
  BUDGET_MODE_DAY: "日预算",
  BUDGET_MODE_TOTAL: "总预算",
  BUDGET_MODE_INFINITE: "不限",
  BID_TYPE_NO_BID: "最大化投放",
  BID_TYPE_CUSTOM: "自定义出价",
  VALUE: "价值",
  VO_MIN_ROAS: "目标 ROAS",
  VO_HIGHEST_VALUE: "最高价值",
  AD_REVENUE_VALUE: "广告收入价值",
  ZERO_DAY: "第 0 天",
  SEVEN_DAYS: "7 天",
  ONE_DAY: "1 天",
  OFF: "关闭",
  EVERY: "每次",
  ONCE: "一次",
  OCPM: "oCPM",
  MINI_APP: "小程序",
  IMPRESSION_LEVEL_AD_REVENUE: "广告收入价值",
  SCHEDULE_FROM_NOW: "从现在起长期投放",
  SCHEDULE_START_END: "指定起止时间",
  PLACEMENT_TYPE_NORMAL: "手动版位",
  PLACEMENT_TYPE_AUTOMATIC: "自动版位",
  PLACEMENT_TIKTOK: "TikTok",
  PLACEMENT_PANGLE: "Pangle",
  GENDER_UNLIMITED: "不限",
  GENDER_MALE: "男",
  GENDER_FEMALE: "女",
  OVER_EIGHTEEN: "18 岁以上",
  MULTIPLE: "多广告组",
  SINGLE_VIDEO: "单视频",
  CAROUSEL_ADS: "轮播",
  CATALOG_CAROUSEL: "目录轮播",
}

// 未登记的枚举原样显示，平台新增值时不会被吞掉。
function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  if (Array.isArray(value))
    return value.length ? value.map(text).join("、") : "—"
  if (typeof value === "boolean") return value ? "开启" : "关闭"
  if (typeof value === "object") return JSON.stringify(value)
  const raw = String(value)
  return ENUMS[raw] ?? raw
}

const age = (value: unknown) =>
  Array.isArray(value)
    ? value
        .map((v) =>
          String(v).replace(/^AGE_/, "").replace("_100", "+").replace("_", "-"),
        )
        .join("、")
    : text(value)

// 地区本期只显示 ID 列表，不做地区名解析。
const ids = (value: unknown) =>
  Array.isArray(value) ? value.map(String).join("、") || "—" : text(value)

const listOf =
  (key: string, separator: string) =>
  (value: unknown): string =>
    Array.isArray(value)
      ? value
          .map((item) =>
            item && typeof item === "object" && key in item
              ? String((item as Record<string, unknown>)[key])
              : text(item),
          )
          .join(separator) || "—"
      : text(value)

// 创意列表展示每个创意的形式与素材名称（视频文件名、视频/帖子 ID 依次兜底）。
function creatives(value: unknown): string {
  if (!Array.isArray(value)) return text(value)
  if (!value.length) return "—"
  return value
    .map((item) => {
      const info = ((item as Record<string, unknown>)?.creative_info ??
        item) as Record<string, unknown>
      const video = (info.video_info ?? {}) as Record<string, unknown>
      const name =
        video.file_name ??
        video.video_id ??
        info.tiktok_item_id ??
        (Array.isArray(info.image_info)
          ? `${info.image_info.length} 张图片`
          : null)
      const format = text(info.ad_format)
      return name ? `${format}：${String(name)}` : format
    })
    .join("\n")
}

const SECTIONS: Record<
  ConfigKind,
  Array<{ title: string; fields: Field[] }>
> = {
  campaign: [
    {
      title: "基本信息",
      fields: [
        { key: "objective_type", label: "推广目标" },
        { key: "app_promotion_type", label: "推广类型" },
        { key: "campaign_type", label: "系列类型" },
        { key: "smart_plus_adgroup_mode", label: "Smart+ 广告组模式" },
        { key: "create_time", label: "创建时间" },
        { key: "modify_time", label: "修改时间" },
      ],
    },
    {
      title: "预算",
      fields: [
        { key: "budget", label: "预算" },
        { key: "budget_mode", label: "预算模式" },
        { key: "budget_optimize_on", label: "系列预算优化" },
      ],
    },
  ],
  adgroup: [
    {
      title: "优化与出价",
      fields: [
        { key: "promotion_type", label: "推广类型" },
        { key: "minis_id", label: "Minis ID" },
        { key: "optimization_goal", label: "优化目标" },
        { key: "optimization_event", label: "价值类型" },
        { key: "bid_type", label: "出价方式" },
        { key: "deep_bid_type", label: "竞价策略" },
        { key: "roas_bid", label: "目标 ROAS" },
        { key: "conversion_bid_price", label: "转化出价" },
        { key: "vbo_window", label: "价值优化窗口" },
        { key: "billing_event", label: "计费方式" },
      ],
    },
    {
      title: "预算与排期",
      fields: [
        { key: "budget", label: "预算" },
        { key: "budget_mode", label: "预算模式" },
        { key: "min_budget", label: "最低预算" },
        { key: "schedule_type", label: "排期类型" },
        { key: "schedule_start_time", label: "开始时间" },
        { key: "schedule_end_time", label: "结束时间" },
        { key: "dayparting", label: "分时投放" },
      ],
    },
    {
      title: "原生 Series 与身份",
      fields: [
        { key: "native_series_id", label: "Series ID" },
        { key: "native_series_name", label: "Series 名称" },
        { key: "identity_type", label: "组身份类型" },
        { key: "identity_id", label: "组身份 ID" },
        { key: "identity_authorized_bc_id", label: "组授权 BC ID" },
        { key: "tiktok_subplacements", label: "TikTok 子版位" },
      ],
    },
    {
      title: "版位与定向",
      fields: [
        { key: "placement_type", label: "版位类型" },
        { key: "placements", label: "版位" },
        { key: "targeting_spec.location_ids", label: "地区 ID", format: ids },
        { key: "targeting_spec.age_groups", label: "年龄", format: age },
        { key: "targeting_spec.gender", label: "性别" },
        { key: "targeting_spec.languages", label: "语言" },
        { key: "targeting_spec.spc_audience_age", label: "Smart+ 受众年龄" },
      ],
    },
    {
      title: "归因",
      fields: [
        { key: "click_attribution_window", label: "点击归因窗口" },
        { key: "view_attribution_window", label: "浏览归因窗口" },
        {
          key: "engaged_view_attribution_window",
          label: "专注观看归因窗口",
        },
        { key: "attribution_event_count", label: "事件计数方式" },
      ],
    },
    {
      title: "互动设置",
      fields: [
        { key: "comment_disabled", label: "关闭评论" },
        { key: "share_disabled", label: "关闭分享" },
        { key: "video_download_disabled", label: "关闭下载" },
        { key: "create_time", label: "创建时间" },
        { key: "modify_time", label: "修改时间" },
      ],
    },
  ],
  ad: [
    {
      title: "内容",
      fields: [
        {
          key: "ad_text_list",
          label: "文案",
          format: listOf("ad_text", " / "),
        },
        {
          key: "landing_page_url_list",
          label: "落地页",
          format: listOf("landing_page_url", "\n"),
        },
        { key: "creative_list", label: "创意列表", format: creatives },
        { key: "create_time", label: "创建时间" },
        { key: "modify_time", label: "修改时间" },
      ],
    },
    {
      title: "广告配置",
      fields: [
        { key: "ad_configuration.identity_type", label: "身份类型" },
        { key: "ad_configuration.identity_id", label: "身份 ID" },
        {
          key: "ad_configuration.identity_authorized_bc_id",
          label: "身份授权 BC",
        },
        { key: "ad_configuration.call_to_action_id", label: "CTA ID" },
        { key: "ad_configuration.dark_post_status", label: "仅广告可见" },
      ],
    },
  ],
}

// 嵌套对象：其已映射子项之外的键追加到所在分组末尾，以原字段名显示。
const NESTED: Partial<Record<ConfigKind, Record<string, string>>> = {
  adgroup: { targeting_spec: "版位与定向" },
  ad: { ad_configuration: "广告配置" },
}

// 已在表头/概览展示或属于身份信息的字段，不放进"其他字段"。
const IDENTITY = new Set([
  "advertiser_id",
  "campaign_id",
  "adgroup_id",
  "ad_id",
  "smart_plus_ad_id",
  "campaign_name",
  "adgroup_name",
  "ad_name",
  "operation_status",
  "secondary_status",
])

function read(config: Record<string, unknown>, path: string): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (value, key) =>
        value && typeof value === "object"
          ? (value as Record<string, unknown>)[key]
          : undefined,
      config,
    )
}

export function configSections(
  kind: ConfigKind,
  config: Record<string, unknown>,
): ConfigSection[] {
  const used = new Set<string>()
  const nested = NESTED[kind] ?? {}
  const sections: ConfigSection[] = SECTIONS[kind].map(({ title, fields }) => {
    const items = fields
      .filter((field) => read(config, field.key) !== undefined)
      .map((field) => {
        used.add(field.key)
        return {
          label: field.label,
          value: (field.format ?? text)(read(config, field.key)),
        }
      })
    for (const [parent, sectionTitle] of Object.entries(nested)) {
      if (sectionTitle !== title) continue
      const value = config[parent]
      if (!value || typeof value !== "object" || Array.isArray(value)) continue
      for (const [key, child] of Object.entries(value))
        if (!used.has(`${parent}.${key}`))
          items.push({ label: key, value: text(child) })
    }
    return { title, items }
  })
  const handled = (key: string) =>
    IDENTITY.has(key) ||
    used.has(key) ||
    // 嵌套对象若为对象则已在所在分组逐项展开；非对象时仍进"其他字段"。
    (key in nested &&
      typeof config[key] === "object" &&
      config[key] !== null &&
      !Array.isArray(config[key]))
  const rest = Object.keys(config).filter((key) => !handled(key))
  const result = sections.filter((section) => section.items.length > 0)
  if (rest.length)
    result.push({
      title: "其他字段",
      items: rest.map((key) => ({ label: key, value: text(config[key]) })),
    })
  return result
}
