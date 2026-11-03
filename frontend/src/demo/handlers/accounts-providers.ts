import type {
  AccountPublic,
  BCPublic,
  ConnectionPublic,
  LinkConfigSchema,
  ProviderApplicationPublic,
  ProviderConnectionPublic,
  ProviderLinkPublic,
} from "@/client"
import {
  DEMO_ADVERTISERS,
  DEMO_BCS,
  DEMO_DRAMAS,
  DEMO_PROVIDERS,
  MAIN_BC_ID,
  MAIN_TENANT_ID,
} from "../fixtures/world"
import { demoUuid, isoDaysAgo, now, stageAt } from "../runtime/clock"
import { registerDemoRedirect } from "../runtime/navigation"
import { matchesSearch, paginate } from "../runtime/paging"
import { demoError, handle } from "../runtime/registry"

// 账户授权 + 版权方领域的假后端实现。

// ============ 内部状态 ============

/** TikTok 连接的可变状态（授权、发现、BC 绑定）。 */
const demoConnections: ConnectionPublic[] = [
  {
    id: demoUuid(5001),
    tenant_id: MAIN_TENANT_ID,
    status: "ACTIVE",
    kind: "OFFICIAL_API",
    display_name: "官方 API · 主连接",
    bound_bc_id: null,
    binding_count: DEMO_BCS.length,
    pending_binding_count: 0,
    is_default: true,
    authorization_status: null,
    authorization_attempt_id: null,
    refresh_status: null,
    read_authorized: true,
    upload_authorized: true,
    build_authorized: true,
    evidence_checked_at: isoDaysAgo(1, 8),
    last_discovery: isoDaysAgo(0, 6),
    last_authorized_at: isoDaysAgo(2, 10),
    discovery_status: "COMPLETE",
    error_code: null,
  },
]

/** BC 绑定状态（由连接发现任务改变）。 */
const demoBCBindings = new Map<
  string,
  { status: BCPublic["binding_status"]; last_discovery: string }
>(
  DEMO_BCS.map((bc) => [
    bc.bc_id,
    { status: "ACTIVE", last_discovery: isoDaysAgo(0, 6) },
  ]),
)

/** 版权方连接的可变状态。 */
const demoProviderConnections: ProviderConnectionPublic[] = DEMO_PROVIDERS.map(
  (p) => ({
    id: p.id,
    kind: p.kind,
    display_name: p.display_name,
    status: "active",
    verified_at: isoDaysAgo(3, 9),
    error_code: null,
  }),
)

/** 版权方应用（每个连接有 1–2 个应用）。 */
const demoProviderApps: ProviderApplicationPublic[] = [
  {
    external_id: "wangyan-app-001",
    name: "网眼主应用",
    tiktok_minis_id: "7350000000000000001",
    available: true,
    link_defaults: { episode: 1 },
  },
  {
    external_id: "jiashu-reelbox-001",
    name: "嘉书 ReelBox",
    tiktok_minis_id: "7350000000000000002",
    available: true,
    link_defaults: {},
  },
  {
    external_id: "gangganhao-app-001",
    name: "刚刚好主应用",
    tiktok_minis_id: "7350000000000000003",
    available: true,
    link_defaults: { episode_seq: 1 },
  },
  {
    external_id: "duiba-qijing-001",
    name: "兑吧奇境分销",
    tiktok_minis_id: "7350000000000000004",
    available: true,
    link_defaults: { episode: 2 },
  },
]

/** 版权方连接 ID 到应用列表的映射。 */
const appsByConnection = new Map<string, ProviderApplicationPublic[]>([
  [DEMO_PROVIDERS[0].id, [demoProviderApps[0]]], // 网眼
  [DEMO_PROVIDERS[1].id, [demoProviderApps[1]]], // 嘉书
  [DEMO_PROVIDERS[2].id, [demoProviderApps[2]]], // 刚刚好
  [DEMO_PROVIDERS[3].id, [demoProviderApps[3]]], // 兑吧
])

/** 推广链接（按剧生成，每部剧一个链接）。 */
const demoProviderLinks: ProviderLinkPublic[] = DEMO_DRAMAS.map((drama) => {
  const provider = DEMO_PROVIDERS.find((p) => p.id === drama.provider_id)!
  const apps = appsByConnection.get(provider.id) ?? []
  const app = apps[0]
  return {
    source: "provider" as const,
    link_id: demoUuid(6000 + Number.parseInt(drama.id.slice(-4), 16)),
    drama_id: drama.id,
    external_drama_id: drama.external_id,
    display_drama_id: drama.external_id,
    title: drama.title,
    language: drama.language,
    provider_kind: drama.provider_kind,
    connection_id: provider.id,
    connection_name: provider.display_name,
    connection_status: "active",
    application_id: app.external_id,
    application_name: app.name,
    tiktok_minis_id: app.tiktok_minis_id ?? null,
    status: "verified",
    version: 1,
    url: `https://minis.example.invalid/${drama.provider_kind}/${drama.external_id}`,
    protected_base: null,
    verified_at: isoDaysAgo(2, 11),
    config: {},
    config_display_incomplete: false,
  }
})

/** 取链任务（内存里按时间推进）。 */
interface DemoPreparationTask {
  request_id: string
  connection_id: string
  application_id: string
  started_at: number
  lines: string[]
  config: Record<string, unknown>
}
const demoPreparationTasks = new Map<string, DemoPreparationTask>()

// ============ TikTok 授权与连接 ============

// 注册 TikTok 授权跳转：体验版里点"前往 TikTok 授权"后回调到账户页，并带上授权成功标记。
registerDemoRedirect(
  (url) =>
    url.hostname === "business-api.tiktok.com" &&
    url.pathname === "/portal/auth/authorize",
  async (_url) => {
    // 模拟用户在 TikTok 页面确认授权后回跳：与真实回调一致，带 CANDIDATE_READY 与连接 ID。
    return `/tenants/${MAIN_TENANT_ID}/accounts?bc_id=${MAIN_BC_ID}&tab=connections&authorization=CANDIDATE_READY&connection_id=${demoConnections[0].id}`
  },
)

registerDemoRedirect(
  (url) =>
    url.hostname === "business-api.tiktok.com" &&
    url.pathname === "/portal/mcp-tt4b-authorize",
  async (_url) => {
    // MCP 授权回跳，带上 attempt_id
    const attemptId = demoUuid(9000 + (now() % 10000))
    return `/tenants/${MAIN_TENANT_ID}/accounts?bc_id=${MAIN_BC_ID}&tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=${attemptId}`
  },
)

handle("accounts-get_configuration", () => ({
  channels: [
    {
      kind: "OFFICIAL_API" as const,
      configured: true,
      status: "READY" as const,
      code: null,
    },
    {
      kind: "OFFICIAL_MCP" as const,
      configured: true,
      status: "READY" as const,
      code: null,
    },
  ],
}))

handle("accounts-get_connections", ({ query }) => {
  const filtered = demoConnections.filter(
    (conn) => !query.status || conn.status === query.status,
  )
  return paginate(filtered, query)
})

handle("accounts-post_authorization", ({ body }) => {
  // 官方 API 授权：返回 TikTok 授权地址，体验版会拦截并回到账户页
  const connectionId = body.connection_id ?? demoConnections[0].id
  return {
    url: `https://business-api.tiktok.com/portal/auth/authorize?app_id=demo&state=${connectionId}`,
  }
})

handle("accounts-authorize", ({ body }) => {
  // MCP 授权：返回 MCP 授权地址
  const connectionId = body.connection_id ?? demoConnections[0].id
  return {
    url: `https://business-api.tiktok.com/portal/mcp-tt4b-authorize?state=${connectionId}`,
  }
})

handle("accounts-candidate_bcs", ({ query }) => {
  // MCP 授权后的候选 BC 列表（自动接入全部可见 BC）
  const items = DEMO_BCS.map((bc) => ({
    bc_id: bc.bc_id,
    name: bc.name,
    connected: false,
    binding_status: null as null,
  }))
  const page = query.page ?? 1
  const pageSize = query.page_size ?? 50
  const start = (page - 1) * pageSize
  return {
    items: items.slice(start, start + pageSize),
    page,
    page_size: pageSize,
    total: items.length,
  }
})

handle("accounts-binding", ({ body }) => {
  // MCP 接入 BC：立即成功
  const connection = demoConnections[0]
  return {
    connection_id: connection.id,
    items: body.bc_ids.map((bcId) => ({
      bc_id: bcId,
      discovery_run_id: demoUuid(9100 + (now() % 1000)),
      status: "COMPLETE" as const,
    })),
  }
})

handle("availableBcs", ({ query }) => {
  // 已有连接的可用 BC（用于添加 BC）
  const items = DEMO_BCS.map((bc) => {
    const binding = demoBCBindings.get(bc.bc_id)
    return {
      bc_id: bc.bc_id,
      name: bc.name,
      connected: true,
      binding_status: binding?.status ?? null,
    }
  })
  const page = query.page ?? 1
  const pageSize = query.page_size ?? 50
  const start = (page - 1) * pageSize
  return {
    items: items.slice(start, start + pageSize),
    page,
    page_size: pageSize,
    total: items.length,
  }
})

handle("addBindings", ({ body }) => {
  // 添加 BC 绑定
  const connection = demoConnections[0]
  const bcIds = (body as { bc_ids: string[] }).bc_ids
  return {
    connection_id: connection.id,
    items: bcIds.map((bcId: string) => ({
      bc_id: bcId,
      discovery_run_id: demoUuid(9200 + (now() % 1000)),
      status: "COMPLETE" as const,
    })),
  }
})

handle("accounts-patch_connection", ({ path, body }) => {
  // 禁用连接
  const conn = demoConnections.find((c) => c.id === path.connection_id)
  if (!conn) return demoError(404, "NOT_FOUND", "连接不存在")
  if (body.status === "DISABLED") {
    conn.status = "DISABLED"
  }
  return conn
})

handle("syncApiConnection", () => {
  // 官方 API 同步：立即返回受理
  return { discovery_run_id: demoUuid(9300 + (now() % 1000)) }
})

handle("syncMcpBc", () => {
  // MCP 同步：立即返回受理
  return { discovery_run_id: demoUuid(9400 + (now() % 1000)) }
})

handle("unbindMcpBc", () => {
  // 解绑 MCP BC：立即成功
  return undefined
})

handle("accounts-put_default_connection", ({ path, body }) => {
  // 设置默认执行连接
  demoConnections.forEach((c) => {
    c.is_default = c.id === body.connection_id
  })
  return {
    tenant_id: path.tenant_id,
    bc_id: path.bc_id,
    connection_id: body.connection_id,
    channel: "OFFICIAL_API" as const,
    authorization_revision: 1,
    adapter_contract_revision: "1",
    binding_revision: 1,
  }
})

handle("accounts-refresh_capabilities", ({ path }) => {
  // 刷新账户能力：立即受理
  return {
    job_id: demoUuid(9500 + (now() % 1000)),
    bc_id: path.bc_id,
    connection_id: demoConnections[0].id,
    status: "PENDING" as const,
    phase: "READ" as const,
    remote_read_count: 0,
    remote_total_count: null,
    published_account_count: 0,
    permissions_known: false,
    error_code: null,
    created_at: isoDaysAgo(0, 10),
    completed_at: null,
  }
})

handle("accounts-capability_status", ({ path }) => {
  // 查询刷新任务状态：立即完成
  return {
    job_id: path.job_id,
    bc_id: path.bc_id,
    connection_id: demoConnections[0].id,
    status: "COMPLETE" as const,
    phase: "DONE" as const,
    remote_read_count: DEMO_ADVERTISERS.length,
    remote_total_count: DEMO_ADVERTISERS.length,
    published_account_count: DEMO_ADVERTISERS.length,
    permissions_known: true,
    error_code: null,
    created_at: isoDaysAgo(0, 10),
    completed_at: isoDaysAgo(0, 10, 5),
  }
})

handle("accounts-post_resolve", () => {
  // 解析账户 ID：立即返回空数组
  return []
})

handle("accounts-revoke", () => {
  // 撤销授权：返回空对象
  return {}
})

handle("accounts-disable", () => {
  // 禁用账户
  return undefined
})

// ============ BC 与账户目录 ============

handle("accounts-get_bcs", ({ path, query }) => {
  // BC 列表（页面顶部 BC 选择器依赖这个接口）- 只返回当前租户的 BC
  const filtered = DEMO_BCS.filter(
    (bc) =>
      bc.tenant_id === path.tenant_id &&
      matchesSearch(query.query ?? "", bc.name, bc.bc_id),
  )
  const items: BCPublic[] = filtered.map((bc) => {
    const binding = demoBCBindings.get(bc.bc_id)
    return {
      bc_id: bc.bc_id,
      name: bc.name,
      ownership_conflict: false,
      is_default: bc.bc_id === MAIN_BC_ID,
      default_connection_id: demoConnections[0].id,
      binding_status: binding?.status ?? "ACTIVE",
      last_discovery: binding?.last_discovery ?? isoDaysAgo(0, 6),
      discovery_status: "COMPLETE",
      error_code: null,
    }
  })
  return paginate(items, query)
})

handle("accounts-get_accounts", ({ query }) => {
  // 广告账户列表
  const filtered = DEMO_ADVERTISERS.filter(
    (adv) =>
      (!query.bc_id || adv.bc_id === query.bc_id) &&
      matchesSearch(query.query ?? "", adv.name, adv.advertiser_id),
  )
  const items: AccountPublic[] = filtered.map((adv) => ({
    advertiser_id: adv.advertiser_id,
    bc_id: adv.bc_id,
    name: adv.name,
    currency: adv.currency,
    timezone: adv.timezone,
    remote_status: "STATUS_ENABLE",
    ownership_conflict: false,
    can_build: adv.purpose === "delivery",
    can_upload: adv.purpose === "material",
    permission_state: "read_write",
    availability: "AVAILABLE",
    checked_at: isoDaysAgo(0, 6),
  }))
  return paginate(items, query)
})

handle("accounts-configuration", () => {
  // MCP 配置
  return {
    configured: true,
    code: null,
    status: "READY" as const,
    revocation_supported: true,
  }
})

// ============ 版权方连接 ============

handle("providers-list_connections", ({ query }) => {
  const filtered = demoProviderConnections.filter((conn) =>
    matchesSearch(query.query ?? "", conn.display_name),
  )
  return paginate(filtered, query)
})

handle("providers-post_connection", ({ body }) => {
  // 新建版权方连接
  const newConn: ProviderConnectionPublic = {
    id: demoUuid(7000 + (now() % 10000)),
    kind: body.kind,
    display_name: body.display_name,
    status: "active",
    verified_at: null,
    error_code: null,
  }
  demoProviderConnections.push(newConn)
  // 同时为它准备一个虚拟应用
  const newApp: ProviderApplicationPublic = {
    external_id: `${body.kind}-app-${now() % 1000}`,
    name: `${body.display_name}应用`,
    tiktok_minis_id: `735000000000000${demoProviderConnections.length}`,
    available: true,
    link_defaults: {},
  }
  appsByConnection.set(newConn.id, [newApp])
  return newConn
})

handle("providers-patch_connection", ({ path, body }) => {
  // 更新版权方连接
  const conn = demoProviderConnections.find((c) => c.id === path.connection_id)
  if (!conn) return demoError(404, "NOT_FOUND", "连接不存在")
  if (body.display_name !== undefined && body.display_name !== null) {
    conn.display_name = body.display_name
  }
  if (body.status !== undefined && body.status !== null) {
    conn.status = body.status
  }
  return conn
})

handle("providers-post_verify", ({ path }) => {
  // 验证版权方连接
  const conn = demoProviderConnections.find((c) => c.id === path.connection_id)
  if (!conn) return demoError(404, "NOT_FOUND", "连接不存在")
  conn.verified_at = isoDaysAgo(0, 10)
  conn.error_code = null
  return conn
})

handle("providers-list_applications", ({ path, query }) => {
  // 版权方应用列表
  const apps = appsByConnection.get(path.connection_id) ?? []
  return paginate(apps, query)
})

handle("providers-application_capabilities", ({ path }) => {
  // 应用能力配置（包含推广链接字段定义）
  const conn = demoProviderConnections.find((c) => c.id === path.connection_id)
  if (!conn) return demoError(404, "NOT_FOUND", "连接不存在")

  const app = (appsByConnection.get(path.connection_id) ?? []).find(
    (a) => a.external_id === path.application_id,
  )
  if (!app) return demoError(404, "NOT_FOUND", "应用不存在")

  // 根据版权方类型返回对应的链接配置 schema
  const linkSchemas: Record<string, LinkConfigSchema> = {
    wangyan: {
      provider_kind: "wangyan",
      fields: [
        {
          name: "episode",
          label: "起播集",
          type: "integer",
          required: false,
          default: 1,
          options: [],
          source: "application_default",
          rule: null,
          minimum: 1,
        },
      ],
      defaults: { episode: 1 },
      application_defaults: {},
      schema_version: 2,
    },
    minglue: {
      provider_kind: "minglue",
      fields: [
        {
          name: "episode",
          label: "起播集",
          type: "integer",
          default: 1,
          source: "application_default",
          minimum: 1,
          required: false,
          options: [],
        },
      ],
      defaults: { episode: 1 },
      application_defaults: app.link_defaults ?? {},
      schema_version: 2,
    },
    jiashu: {
      provider_kind: "jiashu",
      fields: [],
      defaults: {},
      application_defaults: {},
      schema_version: 2,
    },
    gangganhao: {
      provider_kind: "gangganhao",
      fields: [
        {
          name: "episode_seq",
          label: "跳转集",
          type: "integer",
          required: false,
          default: 1,
          options: [],
          source: "application_default",
          rule: null,
          minimum: 1,
        },
      ],
      defaults: { episode_seq: 1 },
      application_defaults: {},
      schema_version: 2,
    },
    duiba: {
      provider_kind: "duiba",
      fields: [
        {
          name: "episode",
          label: "起播集",
          type: "integer",
          required: false,
          default: 1,
          options: [],
          source: "application_default",
          rule: null,
          minimum: 1,
        },
        {
          name: "card_point_episode",
          label: "卡点集",
          type: "integer",
          required: false,
          default: undefined,
          options: [],
          source: "per_drama",
          rule: "按剧免费集数",
          minimum: 1,
        },
      ],
      defaults: { episode: 1 },
      application_defaults: { episode: 2 },
      schema_version: 2,
    },
  }

  return linkSchemas[conn.kind] ?? linkSchemas.wangyan
})

handle("providers-patch_application_link_defaults", ({ path, body }) => {
  // 更新应用的推广链接默认配置 - 返回 LinkConfigSchema
  const conn = demoProviderConnections.find((c) => c.id === path.connection_id)
  if (!conn) return demoError(404, "NOT_FOUND", "连接不存在")
  const apps = appsByConnection.get(path.connection_id) ?? []
  const app = apps.find((a) => a.external_id === path.application_id)
  if (!app) return demoError(404, "NOT_FOUND", "应用不存在")
  app.link_defaults = body.link_defaults ?? {}

  // 返回完整的 LinkConfigSchema
  const linkSchemas: Record<string, LinkConfigSchema> = {
    wangyan: {
      provider_kind: "wangyan",
      fields: [
        {
          name: "episode",
          label: "起播集",
          type: "integer",
          required: false,
          default: 1,
          options: [],
          source: "application_default",
          rule: null,
          minimum: 1,
        },
      ],
      defaults: { episode: 1 },
      application_defaults: app.link_defaults,
      schema_version: 2,
    },
    minglue: {
      provider_kind: "minglue",
      fields: [
        {
          name: "episode",
          label: "起播集",
          type: "integer",
          default: 1,
          source: "application_default",
          minimum: 1,
          required: false,
          options: [],
        },
      ],
      defaults: { episode: 1 },
      application_defaults: app.link_defaults ?? {},
      schema_version: 2,
    },
    jiashu: {
      provider_kind: "jiashu",
      fields: [],
      defaults: {},
      application_defaults: app.link_defaults,
      schema_version: 2,
    },
    gangganhao: {
      provider_kind: "gangganhao",
      fields: [
        {
          name: "episode_seq",
          label: "跳转集",
          type: "integer",
          required: false,
          default: 1,
          options: [],
          source: "application_default",
          rule: null,
          minimum: 1,
        },
      ],
      defaults: { episode_seq: 1 },
      application_defaults: app.link_defaults,
      schema_version: 2,
    },
    duiba: {
      provider_kind: "duiba",
      fields: [
        {
          name: "episode",
          label: "起播集",
          type: "integer",
          required: false,
          default: 1,
          options: [],
          source: "application_default",
          rule: null,
          minimum: 1,
        },
        {
          name: "card_point_episode",
          label: "卡点集",
          type: "integer",
          required: false,
          default: undefined,
          options: [],
          source: "per_drama",
          rule: "按剧免费集数",
          minimum: 1,
        },
      ],
      defaults: { episode: 1 },
      application_defaults: app.link_defaults,
      schema_version: 2,
    },
  }
  return linkSchemas[conn.kind] ?? linkSchemas.wangyan
})

handle("providers-get_links", ({ query }) => {
  // 推广链接列表
  const filtered = demoProviderLinks.filter(
    (link) =>
      (!query.connection_id || link.connection_id === query.connection_id) &&
      (!query.application_id || link.application_id === query.application_id) &&
      matchesSearch(query.query ?? "", link.title, link.external_drama_id),
  )
  return paginate(filtered, query)
})

handle("providers-link_details", ({ path }) => {
  // 推广链接详情
  const link = demoProviderLinks.find((l) => l.link_id === path.link_id)
  if (!link) return demoError(404, "NOT_FOUND", "推广链接不存在")
  return link
})

handle("providers-post_candidate", () => {
  // 候选剧查询（搭建时使用）：返回 202 受理
  return { task_id: demoUuid(8500 + (now() % 1000)) }
})

handle("providers-post_preparation", ({ body }) => {
  // 提交取链任务
  const taskId = demoUuid(8600 + (now() % 1000))
  const task: DemoPreparationTask = {
    request_id: taskId,
    connection_id: body.connection_id,
    application_id: body.application_id,
    started_at: now(),
    lines: body.lines,
    config: body.config ?? {},
  }
  demoPreparationTasks.set(taskId, task)
  return { task_id: taskId }
})

handle("providers-get_preparation", ({ path }) => {
  // 查询取链任务状态 - 返回 ResolvedLink 列表
  const task = demoPreparationTasks.get(path.task_id)
  if (!task) return demoError(404, "NOT_FOUND", "任务不存在")

  const stages = [
    ["PREPARING", 1500],
    ["FETCHING", 2000],
    ["COMPLETE", 0],
  ] as const

  const { done } = stageAt(task.started_at, stages)

  // 任务完成后返回 ResolvedLink 列表
  if (done) {
    const items = task.lines.map((line, index) => {
      const drama =
        DEMO_DRAMAS.find(
          (d) =>
            d.provider_id === task.connection_id &&
            (d.title === line || d.external_id === line),
        ) ?? DEMO_DRAMAS[index % DEMO_DRAMAS.length]

      return {
        input_id: demoUuid(8700 + index),
        line_no: index + 1,
        raw_input: line,
        provider_kind: drama.provider_kind,
        connection_id: task.connection_id,
        application_id: task.application_id,
        drama_id: drama.id,
        external_drama_id: drama.external_id,
        title: drama.title,
        language: drama.language,
        display_drama_id: drama.external_id,
        link_id: demoUuid(8000 + (now() % 10000) + index),
        url: `https://minis.example.invalid/${drama.provider_kind}/${drama.external_id}`,
        protected_base: null,
        tiktok_minis_id:
          (appsByConnection.get(task.connection_id) ?? [])[0]
            ?.tiktok_minis_id ?? null,
        status: "ready" as const,
        candidates: undefined,
        error_code: null,
        error_message: null,
        existing_config: null,
        requested_config: task.config as Record<
          string,
          string | number | boolean | null
        >,
      }
    })

    return {
      items,
      next_cursor: null,
      total: items.length,
    }
  }

  return {
    items: [],
    next_cursor: null,
    total: 0,
  }
})

handle("providers-get_preparation_summary", ({ path }) => {
  // 取链任务摘要（用于任务列表）
  const task = demoPreparationTasks.get(path.task_id)
  if (!task) return demoError(404, "NOT_FOUND", "任务不存在")

  const stages = [
    ["PREPARING", 1500],
    ["FETCHING", 2000],
    ["COMPLETE", 0],
  ] as const

  const { stage, done } = stageAt(task.started_at, stages)
  const conn = demoProviderConnections.find((c) => c.id === task.connection_id)

  return {
    task_id: task.request_id,
    connection_id: task.connection_id,
    connection_name: conn?.display_name ?? "",
    provider_kind: conn?.kind ?? "",
    application_id: task.application_id,
    application_name:
      (appsByConnection.get(task.connection_id) ?? [])[0]?.name ?? "",
    status: stage,
    config: task.config as Record<string, string | number | boolean | null>,
    config_display_incomplete: false,
    total_count: task.lines.length,
    ready_count: done ? task.lines.length : 0,
    pending_count: done ? 0 : task.lines.length,
    exception_count: 0,
    counts: {
      ready: done ? task.lines.length : 0,
      pending: done ? 0 : task.lines.length,
    },
    created_at: isoDaysAgo(0, 10),
    completed_at: done ? isoDaysAgo(0, 10, 2) : null,
  }
})

// ============ integrations（TikTok 回调）============

handle("integrations-tiktok_callback", () => {
  // TikTok 授权回调（体验版不会真正调用，因为已被拦截跳转）
  return { success: true }
})

handle("integrations-callback", () => {
  // MCP 授权回调
  return { success: true }
})
