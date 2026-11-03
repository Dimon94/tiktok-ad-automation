import { expect, type Page, test } from "@playwright/test"
import type { ProviderConnectionPublic, ResolvedLink } from "../src/client"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

const tenantId = "11111111-1111-4111-8111-111111111111"
const connectionId = "22222222-2222-4222-8222-222222222222"
const taskId = "33333333-3333-4333-8333-333333333333"
const inputId = "44444444-4444-4444-8444-444444444444"
const token = "provider-browser-test-token"
const connections: ProviderConnectionPublic[] = [
  {
    id: connectionId,
    kind: "wangyan",
    display_name: "网眼独立连接",
    status: "active",
    verified_at: null,
    error_code: null,
  },
]
const base: ResolvedLink = {
  input_id: inputId,
  line_no: 1,
  raw_input: "Moon",
  provider_kind: "wangyan",
  connection_id: connectionId,
  application_id: "com.real.external.app",
  language: "en",
  title: "Moon",
  status: "ready",
  drama_id: inputId,
  external_drama_id: "drama-1",
  link_id: inputId,
  url: `https://example.test/${"original-".repeat(35)}?channel=original`,
  protected_base: `original-${"attribution-".repeat(35)}`,
  candidates: [],
}
async function boundary(
  page: Page,
  options: {
    role?: "tenant_admin" | "operator" | "viewer"
    count?: number
    verifyFails?: boolean
    denied?: boolean
  } = {},
) {
  const requests: {
    method: string
    path: string
    query: URLSearchParams
    body: any
  }[] = []
  const rows: ResolvedLink[] = options.count
    ? Array.from({ length: options.count }, (_, i) => ({
        ...base,
        input_id: `44444444-4444-4444-8444-${String(i + 1).padStart(12, "0")}`,
        line_no: i + 1,
        raw_input: `原文 ${i + 1}`,
        title: `正式剧名 ${i + 1}`,
      }))
    : [
        base,
        {
          ...base,
          input_id: "55555555-5555-4555-8555-555555555555",
          line_no: 2,
          raw_input: "Star",
          title: null,
          external_drama_id: null,
          status: "needs_resolution",
          url: null,
          protected_base: null,
          candidates: [
            { external_drama_id: "star-en", title: "Star", language: "en" },
            { external_drama_id: "star-es", title: "Star", language: "es" },
          ],
        },
      ]
  let configuredMini: string | null = null
  const stored = structuredClone(connections)
  await page.addInitScript((value) => {
    localStorage.setItem("access_token", value)
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async (text: string) => {
          ;(window as any).__copied = text
        },
      },
    })
  }, token)
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      method = req.method(),
      path = url.pathname,
      query = url.searchParams
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    expect(req.headers().authorization).toBe(`Bearer ${token}`)
    const body = req.postData() ? req.postDataJSON() : undefined
    requests.push({ method, path, query, body })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    const paginate = (items: unknown[]) => {
      const limit = Number(query.get("page_size") || query.get("limit") || 50),
        start = Number(query.get("cursor") || 0)
      return {
        items: items.slice(start, start + limit),
        next_cursor:
          start + limit < items.length ? String(start + limit) : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: inputId,
        username: "operator",
        full_name: "测试用户",
        is_active: true,
        is_superuser: false,
      })
    if (path === "/api/me/tenants")
      return reply({
        items: [
          {
            id: tenantId,
            name: "版权方测试租户",
            active: true,
            role: options.role || "tenant_admin",
          },
        ],
        next_cursor: null,
      })
    if (path.endsWith("/bcs")) return reply({ items: [], next_cursor: null })
    if (options.denied && path.includes("/providers/"))
      return reply(
        {
          code: "action_forbidden",
          message: "当前角色无权操作",
          retryable: false,
        },
        403,
      )
    if (path.endsWith("/connections") && method === "GET")
      return reply(
        paginate(
          stored.filter(
            (row) =>
              (!query.get("query") ||
                row.display_name.includes(query.get("query")!)) &&
              (!query.get("kind") || row.kind === query.get("kind")) &&
              (!query.get("status") || row.status === query.get("status")),
          ),
        ),
      )
    if (path.endsWith("/connections") && method === "POST") {
      const row = {
        ...stored[0],
        ...body,
        id: "66666666-6666-4666-8666-666666666666",
        status: "pending",
      }
      delete row.credentials
      stored.push(row)
      return reply(row, 201)
    }
    if (path.endsWith("/verify")) {
      if (options.verifyFails)
        return reply(
          {
            code: "provider_auth_failed",
            message: "版权方认证失败，请检查账号与密码。",
            retryable: false,
          },
          409,
        )
      const row = stored.find((row) => path.includes(row.id))!
      row.status = "active"
      return reply(row)
    }
    if (path.endsWith("/applications/com.real.external.app/capabilities"))
      return reply({
        provider_kind: "wangyan",
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
        application_defaults: {},
        schema_version: 2,
      })
    if (path.endsWith("/applications/com.real.external.app/link-defaults"))
      return reply({
        provider_kind: "wangyan",
        fields: [],
        defaults: { episode: 1 },
        application_defaults: body.link_defaults,
        schema_version: 2,
      })
    if (path.endsWith("/applications/com.real.external.app/minis")) {
      configuredMini = route.request().postDataJSON().minis_id
      return reply({
        external_id: "com.real.external.app",
        name: "已核实应用",
        tiktok_minis_id: configuredMini,
        available: true,
      })
    }
    if (path.endsWith("/applications"))
      return reply({
        items: [
          {
            external_id: "com.real.external.app",
            name: "已核实应用",
            tiktok_minis_id: configuredMini,
            available: true,
          },
          {
            external_id: "unavailable-app",
            name: "历史不可用应用",
            tiktok_minis_id: "99999999999999999999",
            available: false,
          },
        ],
        next_cursor: null,
      })
    if (path.endsWith(`/link-preparations/${taskId}/summary`))
      return reply({
        task_id: taskId,
        connection_id: connectionId,
        connection_name: "网眼独立连接",
        provider_kind: "wangyan",
        application_id: base.application_id,
        application_name: "已核实应用",
        status: "pending",
        config: { episode: 1 },
        config_display_incomplete: false,
        total_count: rows.length,
        ready_count: rows.filter((r) => r.status === "ready").length,
        pending_count: rows.filter((r) => r.status === "pending").length,
        exception_count: rows.filter(
          (r) => !["pending", "ready"].includes(r.status),
        ).length,
        counts: Object.fromEntries(
          ["pending", "ready", "result_unknown"].map((status) => [
            status,
            rows.filter((r) => r.status === status).length,
          ]),
        ),
      })
    if (path.endsWith(`/link-preparations/${taskId}`))
      return reply(
        paginate(
          rows.filter(
            (row) =>
              (!query.get("status") || row.status === query.get("status")) &&
              (query.get("exceptions_only") !== "true" ||
                !["ready", "pending"].includes(row.status)),
          ),
        ),
      )
    const links = rows
      .filter((r) => r.status === "ready")
      .map((r) => ({
        link_id: r.input_id,
        drama_id: r.drama_id,
        external_drama_id: r.external_drama_id,
        title: r.title,
        language: r.language,
        provider_kind: r.provider_kind,
        connection_id: r.connection_id,
        connection_name: "网眼独立连接",
        connection_status: "active",
        application_id: r.application_id,
        application_name: "已核实应用",
        tiktok_minis_id: null,
        status: "ready",
        version: 1,
        url: r.url,
        protected_base: r.protected_base,
        verified_at: null,
        config: { episode: 1 },
        config_display_incomplete: false,
      }))
    if (path.endsWith("/links")) {
      if (query.get("query") === "暂时错误")
        return reply({ message: "暂时无法读取链接" }, 500)
      return reply(
        paginate(
          links.filter(
            (row) =>
              (!query.get("query") ||
                row.title?.includes(query.get("query")!) ||
                row.external_drama_id === query.get("query")) &&
              (!query.get("connection_id") ||
                row.connection_id === query.get("connection_id")) &&
              (!query.get("application_id") ||
                row.application_id === query.get("application_id")) &&
              (!query.get("status") || row.status === query.get("status")),
          ),
        ),
      )
    }
    if (path.includes("/providers/links/"))
      return reply(links.find((r) => path.endsWith(r.link_id)))
    if (path.endsWith("/candidate")) {
      const row = rows.find((row) => path.includes(row.input_id))!
      Object.assign(row, {
        status: "ready",
        title: "Star",
        external_drama_id: body.external_drama_id,
        url: "https://example.test/star",
        protected_base: "star-original",
        candidates: [],
      })
      return reply({ task_id: taskId }, 202)
    }
    if (method === "PATCH") {
      const row = stored.find((row) => path.endsWith(row.id))!
      Object.assign(row, body)
      delete (row as any).credentials
      return reply(row)
    }
    return reply({ message: `Unexpected fixture route ${path}` }, 404)
  })
  return { requests, rows, stored }
}
const resultsUrl = `/tenants/${tenantId}/providers?tab=links&task_id=${taskId}`
test("ready 行复制完整原文；候选只提交对应输入，刷新不重复取链", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(resultsUrl)
  await expect(
    page.getByRole("heading", { name: "版权方连接", exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "处理候选" })).toBeVisible()
  await expect(page.getByRole("checkbox")).toHaveCount(0)
  await page.getByRole("button", { name: "复制推广链接", exact: true }).click()
  await expect
    .poll(() => page.evaluate(() => (window as any).__copied))
    .toBe(base.url)
  await page.getByRole("button", { name: "处理候选" }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog.getByRole("row")).toHaveCount(3)
  expect(requests.filter((r) => r.method === "POST")).toHaveLength(0)
  await dialog
    .getByRole("row")
    .filter({ hasText: "star-es" })
    .getByRole("button", { name: "使用此剧目" })
    .click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "复制推广链接", exact: true }),
  ).toHaveCount(2)
  expect(
    requests.filter((r) => r.method === "POST").map((r) => [r.path, r.body]),
  ).toEqual([
    [
      `/api/tenants/${tenantId}/providers/inputs/55555555-5555-4555-8555-555555555555/candidate`,
      { external_drama_id: "star-es" },
    ],
  ])
  await page.reload()
  await expect(
    page.getByRole("button", { name: "复制推广链接", exact: true }),
  ).toHaveCount(2)
  expect(requests.filter((r) => r.method === "POST")).toHaveLength(1)
})
test("只读成员可复制但没有凭据或候选写操作", async ({ page }) => {
  await boundary(page, { role: "viewer" })
  await page.goto(resultsUrl)
  await expect(
    page.getByRole("button", { name: "复制推广链接", exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "处理候选" })).toHaveCount(0)
  await page.getByRole("tab", { name: "连接", exact: true }).click()
  await expect(page.getByText("网眼独立连接", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "新增连接" })).toHaveCount(0)
  await expect(page.getByRole("button", { name: "重新验证" })).toHaveCount(0)
})
test("连接保存为待验证，显式验证失败清空密码并保留名称", async ({ page }) => {
  const { requests } = await boundary(page, { verifyFails: true })
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByRole("button", { name: "新增连接" }).click()
  const sheet = page.getByRole("dialog")
  await sheet.getByLabel("连接名称").fill("新网眼连接")
  await sheet.getByLabel("邮箱").fill("provider@example.com")
  await sheet.getByLabel("密码", { exact: true }).fill("fixture-secret")
  await sheet.getByRole("button", { name: "保存并验证" }).click()
  await expect(sheet.getByLabel("密码", { exact: true })).toHaveValue("")
  await expect(sheet.getByLabel("连接名称")).toHaveValue("新网眼连接")
  await expect(
    sheet.getByText("版权方认证失败，请检查账号与密码。"),
  ).toBeVisible()
  expect(
    requests.filter((r) => r.method === "POST").map((r) => r.path),
  ).toEqual([
    `/api/tenants/${tenantId}/providers/connections`,
    `/api/tenants/${tenantId}/providers/connections/66666666-6666-4666-8666-666666666666/verify`,
  ])
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    "fixture-secret",
  )
})
test("205 行结果按 50/100 服务端游标分页，原始行号不重排", async ({ page }) => {
  const { requests } = await boundary(page, { count: 205 })
  await page.goto(resultsUrl)
  for (const [i, count] of [50, 50, 50, 50, 5].entries()) {
    await expect(page.locator("tbody tr")).toHaveCount(count)
    await expect(
      page.getByText(`第 ${i * 50 + 1} 行 · 原文 ${i * 50 + 1}`, {
        exact: true,
      }),
    ).toBeVisible()
    if (i < 4)
      await page.getByRole("button", { name: "下一页", exact: true }).click()
  }
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  for (const [i, count] of [100, 100, 5].entries()) {
    await expect(page.locator("tbody tr")).toHaveCount(count)
    await expect(
      page.getByText(`第 ${i * 100 + 1} 行 · 原文 ${i * 100 + 1}`, {
        exact: true,
      }),
    ).toBeVisible()
    if (i < 2)
      await page.getByRole("button", { name: "下一页", exact: true }).click()
  }
  const reads = requests.filter((r) =>
    r.path.endsWith(`/link-preparations/${taskId}`),
  )
  expect(
    reads.map((r) => [r.query.get("page_size"), r.query.get("cursor")]),
  ).toEqual([
    ["50", null],
    ["50", "50"],
    ["50", "100"],
    ["50", "150"],
    ["50", "200"],
    ["100", null],
    ["100", "100"],
    ["100", "200"],
  ])
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("未保存连接关闭确认保留输入，取消不会写 API", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByRole("button", { name: "新增连接" }).click()
  await page.getByRole("dialog").getByLabel("连接名称").fill("未保存草稿")
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page.getByRole("dialog").getByLabel("连接名称")).toHaveValue(
    "未保存草稿",
  )
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("连接停用后不能重新验证或更新认证；应用外部 ID 不冒充 Minis", async ({
  page,
}) => {
  const { stored, requests } = await boundary(page)
  stored[0].status = "disabled"
  await page.goto(`/tenants/${tenantId}/providers`)
  await expect(
    page.locator("tbody").getByText("已停用", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "重新验证" })).toHaveCount(0)
  await expect(page.getByRole("button", { name: "编辑连接" })).toHaveCount(0)
  await page.getByRole("button", { name: "查看已发现应用" }).click()
  const sheet = page.getByRole("dialog")
  await expect(
    sheet.getByRole("row").filter({ hasText: "已核实应用" }),
  ).toContainText("可用")
  await expect(
    sheet.getByRole("row").filter({ hasText: "历史不可用应用" }),
  ).toContainText("不可用")
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("版权方 403 保留平台登录与权限反馈", async ({ page }) => {
  await boundary(page, { denied: true })
  await page.goto(resultsUrl)
  await expect(
    page.getByText("当前角色无权执行此操作，请联系管理员检查租户权限。"),
  ).toBeVisible()
  expect(new URL(page.url()).pathname).toContain("/providers")
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})
test("历史目录读取跨任务链接，详情完整值独立复制且无写请求", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${tenantId}/providers?tab=links`)
  await expect(page.getByText("Moon", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "查看详情", exact: true }).click()
  const sheet = page.getByRole("dialog")
  await expect(sheet.getByText(base.url!, { exact: true })).toBeVisible()
  await sheet.getByRole("button", { name: "复制归因名称", exact: true }).click()
  await expect
    .poll(() => page.evaluate(() => (window as any).__copied))
    .toBe(base.protected_base)
  expect(requests.some((r) => r.path.endsWith(`/links/${inputId}`))).toBe(true)
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("摘要使用全任务计数，异常筛选服务端执行并显示配置事实", async ({
  page,
}) => {
  const { rows, requests } = await boundary(page)
  rows.push({
    ...base,
    input_id: "66666666-6666-4666-8666-666666666666",
    line_no: 3,
    raw_input: "配置冲突原文",
    title: "冲突剧目",
    status: "config_conflict",
    url: null,
    protected_base: null,
    existing_config: { episode: 1 },
    requested_config: { episode: 5 },
    error_code: "config_conflict",
  })
  await page.goto(resultsUrl)
  await expect(
    page.getByText("共 3 行 · 已就绪 1 · 处理中 0 · 异常 2"),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "结果状态" }).click()
  await page.getByRole("option", { name: "仅异常", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(2)
  expect(
    requests
      .filter((r) => r.path.endsWith(`/link-preparations/${taskId}`))
      .slice(-1)[0]
      ?.query.get("exceptions_only"),
  ).toBe("true")
  await page
    .getByRole("row")
    .filter({ hasText: "配置冲突原文" })
    .getByRole("button", { name: "查看详情" })
    .click()
  const sheet = page.getByRole("dialog")
  await expect(sheet.getByText("已有配置", { exact: true })).toBeVisible()
  await expect(sheet.locator("dd")).toHaveText(["1", "5"])
  await expect(
    sheet.getByRole("button", { name: /覆盖|重试|重新创建/ }),
  ).toHaveCount(0)
})
test("历史筛选使用连接与外部应用 ID，清除筛选恢复租户目录", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(
    `/tenants/${tenantId}/providers?tab=links&connection_id=${connectionId}`,
  )
  await page.getByRole("button", { name: "筛选应用" }).click()
  await page
    .getByRole("row")
    .filter({ hasText: "已核实应用" })
    .getByRole("button", { name: "筛选此应用" })
    .click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await page.getByLabel("剧名或剧目 ID").fill("drama-1")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect
    .poll(() =>
      requests
        .filter((r) => r.path.endsWith("/links"))
        .slice(-1)[0]
        ?.query.get("query"),
    )
    .toBe("drama-1")
  const latest = requests.filter((r) => r.path.endsWith("/links")).slice(-1)[0]!
  expect(latest.query.get("application_id")).toBe("com.real.external.app")
  expect(latest.query.get("connection_id")).toBe(connectionId)
  await page.getByRole("button", { name: "清除筛选" }).click()
  await expect(page.getByLabel("剧名或剧目 ID")).toHaveValue("")
  expect(new URL(page.url()).searchParams.has("connection_id")).toBe(false)
})
test("连接名称/版权方/验证状态服务端过滤，不下载全表", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByLabel("连接名称", { exact: true }).fill("不存在")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("没有符合条件的记录", { exact: true }),
  ).toBeVisible()
  const last = requests
    .filter((r) => r.path.endsWith("/connections"))
    .slice(-1)[0]!
  expect(last.query.get("query")).toBe("不存在")
  expect(last.query.get("limit")).toBe("50")
  await page.getByRole("button", { name: "清除筛选" }).click()
  await expect(page.getByText("网眼独立连接", { exact: true })).toBeVisible()
})
test("未知结果只刷新已记录进度，认证异常不注销平台登录", async ({ page }) => {
  const { rows, requests } = await boundary(page, { role: "operator" })
  rows[0] = {
    ...rows[0],
    status: "result_unknown",
    url: null,
    protected_base: null,
    error_code: "provider_result_unknown",
    error_message: "远端结果待核实，不会自动重放未知写入",
  }
  rows[1] = {
    ...rows[1],
    status: "blocked_auth",
    candidates: [],
    error_code: "provider_session_expired",
  }
  await page.goto(resultsUrl)
  await expect(
    page.locator("tbody").getByText("结果待核实", { exact: true }),
  ).toBeVisible()
  await page
    .getByRole("row")
    .filter({ hasText: "Moon" })
    .getByRole("button", { name: "查看详情" })
    .click()
  await expect(
    page.getByRole("dialog").getByText(/超时不代表未创建/),
  ).toBeVisible()
  await page.getByRole("button", { name: "关闭", exact: true }).click()
  await page.getByRole("button", { name: "刷新结果", exact: true }).click()
  await page
    .getByRole("row")
    .filter({ hasText: "Star" })
    .getByRole("button", { name: "查看详情" })
    .click()
  await expect(
    page.getByText("请联系租户管理员检查账号凭据、应用配置或操作权限。"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /重试|重新创建|批量|更新认证/ }),
  ).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("后台完成后摘要驱动最终结果刷新，筛选行退出不留下旧状态", async ({
  page,
}) => {
  const { rows, requests } = await boundary(page)
  rows[0] = { ...rows[0], status: "pending", url: null, protected_base: null }
  await page.goto(resultsUrl)
  await expect(
    page.getByText("共 2 行 · 已就绪 0 · 处理中 1 · 异常 1"),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "结果状态" }).click()
  await page.getByRole("option", { name: "处理中", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(1)
  rows[0] = { ...base }
  await expect(
    page.getByText("共 2 行 · 已就绪 1 · 处理中 0 · 异常 1"),
  ).toBeVisible({ timeout: 10000 })
  await expect(page.locator("tbody tr")).toHaveCount(0)
  await page.getByRole("combobox", { name: "结果状态" }).click()
  await page.getByRole("option", { name: "全部结果状态", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "复制推广链接", exact: true }),
  ).toHaveCount(1)
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("两个独立连接的应用请求取消后不会覆盖新连接", async ({ page }) => {
  const { stored } = await boundary(page)
  const other = "77777777-7777-4777-8777-777777777777"
  stored.push({ ...stored[0], id: other, display_name: "网眼第二连接" })
  let release!: () => void
  const delayed = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(
    `**/api/tenants/${tenantId}/providers/connections/${connectionId}/applications*`,
    async (route) => {
      await delayed
      await route
        .fulfill({
          json: {
            items: [
              {
                external_id: "old-app",
                name: "旧连接延迟应用",
                available: true,
              },
            ],
            next_cursor: null,
          },
          headers: { "Access-Control-Allow-Origin": "*" },
        })
        .catch(() => {})
    },
  )
  await page.goto(`/tenants/${tenantId}/providers`)
  await page
    .getByRole("row")
    .filter({ hasText: "网眼独立连接" })
    .getByRole("button", { name: "查看已发现应用" })
    .click()
  await expect(
    page.getByRole("dialog").locator('[aria-busy="true"]'),
  ).toBeVisible()
  await page.getByRole("button", { name: "关闭", exact: true }).click()
  await page
    .getByRole("row")
    .filter({ hasText: "网眼第二连接" })
    .getByRole("button", { name: "查看已发现应用" })
    .click()
  await expect(
    page.getByRole("dialog").getByText("已核实应用", { exact: true }),
  ).toBeVisible()
  release()
  await expect(
    page.getByRole("dialog").getByText("旧连接延迟应用"),
  ).toHaveCount(0)
})
test("历史重新读取失败保留已有数据并可重试", async ({ page }) => {
  await boundary(page)
  await page.goto(`/tenants/${tenantId}/providers?tab=links`)
  await expect(page.getByText("Moon", { exact: true })).toBeVisible()
  await page.getByLabel("剧名或剧目 ID").fill("暂时错误")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("暂时无法读取链接", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("Moon", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toBeVisible()
})
test("历史链接也使用 50/100 游标，不全量下载", async ({ page }) => {
  const { requests } = await boundary(page, { count: 205 })
  await page.goto(`/tenants/${tenantId}/providers?tab=links`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText("正式剧名 51", { exact: true })).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  expect(
    requests
      .filter((r) => r.path.endsWith("/links"))
      .map((r) => [r.query.get("limit"), r.query.get("cursor")]),
  ).toEqual([
    ["50", null],
    ["50", "50"],
    ["100", null],
  ])
})
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 900, height: 900 },
  { width: 390, height: 844 },
])
  test(`版权方表格 ${viewport.width}px 有界滚动、固定表头、键盘复制`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport)
    await boundary(page, { count: 50 })
    await page.goto(resultsUrl)
    await expect(page.locator("tbody tr")).toHaveCount(50)
    await expectWorkspaceLayout(page)
    await page.screenshot({
      path: testInfo.outputPath(`providers-${viewport.width}.png`),
      fullPage: true,
    })
    const geometry = await page
      .locator('[data-slot="table-container"]')
      .evaluate((el) => {
        el.scrollTop = 400
        return {
          height: el.clientHeight,
          scrollHeight: el.scrollHeight,
          headerStyle: getComputedStyle(el.querySelector("thead")!).position,
          bodyWidth: document.documentElement.scrollWidth,
        }
      })
    expect(geometry.height).toBeLessThanOrEqual(viewport.height * 0.61)
    expect(geometry.scrollHeight).toBeGreaterThan(geometry.height)
    expect(geometry.headerStyle).toBe("sticky")
    expect(geometry.bodyWidth).toBeLessThanOrEqual(viewport.width)
    const copy = page
      .getByRole("button", { name: "复制推广链接", exact: true })
      .first()
    await copy.focus()
    await expect(copy).toBeFocused()
    await page.keyboard.press("Enter")
    await expect
      .poll(() => page.evaluate(() => (window as any).__copied))
      .toBe(base.url)
  })
test("停用连接需要明确确认，取消保留正在编辑的草稿", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByRole("button", { name: "编辑连接" }).click()
  await page.getByRole("dialog").getByLabel("连接名称").fill("保留名称草稿")
  await page.getByRole("button", { name: "停用连接", exact: true }).click()
  await page.getByRole("button", { name: "保留连接", exact: true }).click()
  await expect(page.getByRole("dialog").getByLabel("连接名称")).toHaveValue(
    "保留名称草稿",
  )
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  await page.getByRole("button", { name: "停用连接", exact: true }).click()
  await page.getByRole("button", { name: "确认停用", exact: true }).click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  expect(
    requests.filter((r) => r.method === "PATCH").map((r) => r.body),
  ).toEqual([{ status: "disabled" }])
})

test("过期会话显示自动恢复且不会要求用户重新登录", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.route(
    "**/api/tenants/*/providers/connections?*",
    async (route) => {
      await route.fulfill({
        json: {
          items: [
            {
              ...connections[0],
              status: "reauth_required",
              error_code: "provider_session_refreshing",
            },
          ],
          next_cursor: null,
        },
      })
    },
  )
  await page.goto(`/tenants/${tenantId}/providers`)
  await expect(
    page.locator("tbody").getByText("等待自动恢复", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText(/取链时会自动恢复过期会话/)).toBeVisible()
  expect(requests.filter((request) => request.method === "POST")).toHaveLength(
    0,
  )
})

test("版权方连接不再要求配置 Mini", async ({ page }) => {
  await boundary(page, { role: "tenant_admin" })
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByRole("button", { name: "查看已发现应用" }).click()
  await expect(page.getByText("已核实应用", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "配置 Mini" })).toHaveCount(0)
  await expect(page.getByText("Minis 关联", { exact: true })).toHaveCount(0)
})

test("应用表可设置取链默认值并刷新列表", async ({ page }) => {
  const { requests } = await boundary(page, { role: "tenant_admin" })
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByRole("button", { name: "查看已发现应用" }).click()
  await page.getByRole("button", { name: "取链默认值" }).first().click()
  await page.getByLabel("起播集").fill("3")
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await expect
    .poll(
      () =>
        requests.find(
          (r) => r.method === "PATCH" && r.path.endsWith("/link-defaults"),
        )?.body,
    )
    .toEqual({ link_defaults: { episode: 3 } })
  await expect(page.getByRole("dialog", { name: /取链默认值/ })).toHaveCount(0)
})

test("明略使用账号密码创建连接，保存后按现有流程验证", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${tenantId}/providers`)
  await page.getByRole("button", { name: "新增连接" }).click()
  const sheet = page.getByRole("dialog")
  await sheet.getByLabel("连接名称").fill("明略独立连接")
  await sheet.getByRole("combobox").click()
  await page.getByRole("option", { name: "明略", exact: true }).click()
  await expect(sheet.getByLabel("账号", { exact: true })).toHaveAttribute(
    "type",
    "text",
  )
  await sheet.getByLabel("账号", { exact: true }).fill("minglue-fixture")
  await sheet.getByLabel("密码", { exact: true }).fill("fixture-secret")
  await sheet.getByRole("button", { name: "保存并验证" }).click()
  await expect
    .poll(() => requests.filter((r) => r.method === "POST").length)
    .toBe(2)
  const created = requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/connections"),
  )!
  expect(created.body).toEqual({
    kind: "minglue",
    display_name: "明略独立连接",
    credentials: { account: "minglue-fixture", password: "fixture-secret" },
  })
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    "fixture-secret",
  )
})
