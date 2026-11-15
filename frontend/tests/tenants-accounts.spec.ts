import { expect, type Page, test } from "@playwright/test"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

// Synthetic HTTP boundary fixtures; the real application, router, generated SDK,
// dialogs and queries render unmodified. No TikTok or live user data is involved.
const A = "11111111-1111-4111-8111-111111111111"
const B = "22222222-2222-4222-8222-222222222222"
const C = "33333333-3333-4333-8333-333333333333"
const U = "44444444-4444-4444-8444-444444444444"
const V = "55555555-5555-4555-8555-555555555555"
const token = "tenant-browser-test-token"
type Role = "platform_admin" | "tenant_admin" | "operator" | "viewer"
type Tenant = {
  id: string
  name: string
  active: boolean
  role: Role
  default_bc_id?: string | null
}
type Member = {
  tenant_id: string
  user_id: string
  username: string
  full_name: string
  role: Exclude<Role, "platform_admin">
  active: boolean
  user_active: boolean
}
type Request = {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
  authorization?: string
}
async function boundary(
  page: Page,
  options: {
    platform?: boolean
    role?: Role
    memberCount?: number
    count?: number
    lastAdmin?: boolean
    productionFailure?: boolean
    productionStatus?: "failed" | "pending" | "ready"
    delayedLogin?: Promise<void>
    members403?: boolean
    delayedA?: Promise<void>
    listFailure?: boolean
    defaultBC?: string
    bcs?: { bc_id: string; name: string }[]
  } = {},
) {
  const tenants: Tenant[] = [
    {
      id: A,
      name: "租户甲",
      active: true,
      role: options.role ?? "platform_admin",
      default_bc_id: options.defaultBC,
    },
    {
      id: B,
      name: "租户乙",
      active: true,
      role: options.role ?? "platform_admin",
    },
    { id: C, name: "停用租户", active: false, role: "platform_admin" },
  ]
  if (options.count)
    for (let i = 4; i <= options.count; i++)
      tenants.push({
        id: `88888888-8888-4888-8888-${String(i).padStart(12, "0")}`,
        name: `分页租户 ${i}`,
        active: true,
        role: "platform_admin",
      })
  const members: Record<string, Member[]> = {
    [A]: [
      {
        tenant_id: A,
        user_id: U,
        username: "jia",
        full_name: "甲管理员",
        role: "tenant_admin",
        active: true,
        user_active: true,
      },
    ],
    [B]: [
      {
        tenant_id: B,
        user_id: V,
        username: "yi-user",
        full_name: "乙投手",
        role: "operator",
        active: true,
        user_active: true,
      },
    ],
  }
  if (options.memberCount)
    for (let i = 2; i <= options.memberCount; i++)
      members[A].push({
        tenant_id: A,
        user_id: `99999999-9999-4999-8999-${String(i).padStart(12, "0")}`,
        username: `member${i}`,
        full_name: `分页成员 ${i}`,
        role: "operator",
        active: true,
        user_active: true,
      })
  const requests: Request[] = []
  const candidates = [
    { id: U, username: "jia", full_name: "甲管理员" },
    { id: V, username: "candidate", full_name: "候选用户" },
  ]
  await page.addInitScript(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const q = url.searchParams
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    const body = request.postData() ? request.postDataJSON() : undefined
    requests.push({
      method,
      path,
      query: q,
      body,
      authorization: request.headers().authorization,
    })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    if (path.endsWith("/material-production/login-link")) {
      await options.delayedLogin
      return options.productionFailure
        ? reply({ message: "素材制作尚未开通，请联系平台管理员" }, 409)
        : reply({
            login_url:
              "https://manjumt.gzjunbo.net/api/login-links/consume?token=browser-fixture",
            expires_at: new Date(Date.now() + 300000).toISOString(),
          })
    }
    if (path.endsWith("/material-production/retry")) {
      options.productionStatus = "pending"
      return reply({
        status: "pending",
        username: "material-test",
        attempts: 0,
      })
    }
    if (path.endsWith("/material-production")) {
      return reply({
        status: options.productionStatus ?? "ready",
        username: "material-test",
        attempts: 1,
        message:
          options.productionStatus === "failed"
            ? "素材工具暂时不可用，请稍后重试"
            : null,
      })
    }
    if (path === "/api/login-links/consume")
      return route.fulfill({
        contentType: "text/html",
        body: "<p>素材工具已接收登录链接</p>",
      })
    if (path.endsWith("/bcs"))
      return reply({
        items: options.bcs ?? [],
        total: options.bcs?.length ?? 0,
        next_cursor: null,
      })
    const denied = () =>
      reply(
        {
          code: "action_forbidden",
          message: "当前角色不能执行此操作",
          retryable: false,
        },
        403,
      )
    const paginate = <T extends { id?: string; user_id?: string }>(
      rows: T[],
    ) => {
      const matching = [...rows].sort((a, b) =>
        (a.id ?? a.user_id!).localeCompare(b.id ?? b.user_id!),
      )
      const ordered = matching.filter(
        (row) =>
          !q.get("after_id") || (row.id ?? row.user_id!) > q.get("after_id")!,
      )
      const size = Number(q.get("limit") ?? 50)
      const items = ordered.slice(0, size)
      return {
        items,
        total: matching.length,
        next_cursor:
          ordered.length > size
            ? (items[items.length - 1]?.id ?? items[items.length - 1]?.user_id)
            : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: U,
        username: "admin",
        full_name: "当前用户",
        is_active: true,
        is_superuser: options.platform ?? true,
      })
    if (
      path === "/api/me/tenants" ||
      (path === "/api/platform/tenants" && method === "GET")
    ) {
      if (path.startsWith("/api/platform") && options.platform === false)
        return denied()
      if (options.listFailure && q.get("search") === "错误")
        return reply({ message: "暂时无法读取租户" }, 500)
      const search = q.get("search")?.toLowerCase() ?? ""
      return reply(
        paginate(
          tenants.filter(
            (tenant) =>
              (!q.has("active") ||
                tenant.active === (q.get("active") === "true")) &&
              (tenant.name.toLowerCase().includes(search) ||
                tenant.id === search),
          ),
        ),
      )
    }
    if (
      path.endsWith("user-candidates") ||
      path.endsWith("member-candidates")
    ) {
      expect(q.get("query")).toBeTruthy()
      return reply(
        paginate(
          candidates.filter((candidate) =>
            `${candidate.username}${candidate.full_name}`.includes(
              q.get("query") ?? "",
            ),
          ),
        ),
      )
    }
    if (path === "/api/platform/tenants" && method === "POST") {
      const created = {
        id: "77777777-7777-4777-8777-777777777777",
        name: body.name,
        active: true,
        role: "platform_admin" as const,
      }
      tenants.push(created)
      return reply(created, 201)
    }
    if (path.startsWith("/api/platform/tenants/") && method === "PATCH") {
      const tenant = tenants.find((row) => path.endsWith(row.id))!
      Object.assign(tenant, body)
      return reply(tenant)
    }
    const scope = /^\/api\/tenants\/([^/]+)\/members$/.exec(path)?.[1]
    if (scope && method === "GET") {
      if (
        options.members403 ||
        options.role === "viewer" ||
        options.role === "operator"
      )
        return denied()
      if (scope === A && options.delayedA) await options.delayedA
      const search = q.get("search")?.toLowerCase() ?? ""
      return reply(
        paginate(
          (members[scope] ?? []).filter(
            (row) =>
              (!q.has("role") || row.role === q.get("role")) &&
              (!q.has("active") ||
                row.active === (q.get("active") === "true")) &&
              `${row.username}${row.full_name}`.toLowerCase().includes(search),
          ),
        ),
      )
    }
    if (scope && method === "PUT") {
      if (options.lastAdmin)
        return reply(
          {
            code: "last_tenant_admin",
            message: "不能移除最后一位管理员",
            retryable: false,
          },
          409,
        )
      const candidate = candidates.find((row) => row.id === body.user_id)!
      const row = {
        tenant_id: scope,
        user_id: candidate.id,
        username: candidate.username,
        full_name: candidate.full_name,
        role: body.role,
        active: body.active,
        user_active: true,
      }
      members[scope] = [
        ...(members[scope] ?? []).filter(
          (item) => item.user_id !== row.user_id,
        ),
        row,
      ]
      return reply(row)
    }
    const createdScope = /^\/api\/tenants\/([^/]+)\/members\/users$/.exec(
      path,
    )?.[1]
    if (createdScope && method === "POST") {
      const row = {
        tenant_id: createdScope,
        user_id: "66666666-6666-4666-8666-666666666666",
        username: body.username,
        full_name: body.full_name ?? null,
        role: body.role,
        active: true,
        user_active: true,
      }
      members[createdScope] = [...(members[createdScope] ?? []), row]
      return reply(row, 201)
    }
    return reply({ message: "Unexpected API boundary request" }, 404)
  })
  return { requests, tenants, members }
}
async function chooseTenant(page: Page, name: string) {
  await page.getByRole("combobox", { name: "当前租户" }).click()
  const dialog = page.getByRole("dialog", { name: "选择当前租户" })
  await dialog.getByRole("option", { name: new RegExp(name) }).click()
}

test("platform login entry lists real tenant records and retains separate user provisioning", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/")
  await expect(page).toHaveURL("/platform/tenants")
  await expect(
    page.getByRole("heading", { name: "平台租户管理" }),
  ).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
  await expect(page.getByRole("link", { name: "用户管理" })).toHaveAttribute(
    "href",
    "/admin",
  )
  await expect(page.getByText("BC 未连接", { exact: true })).toHaveCount(0)
  expect(
    requests.find((request) => request.path === "/api/platform/tenants")
      ?.authorization,
  ).toBe(`Bearer ${token}`)
})

test("platform tenant creation opens a dedicated account and queues material provisioning", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/platform/tenants")
  await page.getByRole("button", { name: "新建租户" }).click()
  const sheet = page.getByRole("dialog", { name: "新建租户", exact: true })
  await sheet.getByRole("button", { name: "创建租户" }).click()
  await expect(sheet.getByText("请输入租户名称")).toBeVisible()
  await sheet.getByLabel("租户名称").fill("新租户")
  await sheet.getByLabel("登录账号", { exact: true }).fill("new-company")
  await sheet.getByLabel("初始密码", { exact: true }).fill("Password123")
  await sheet.getByRole("button", { name: "创建租户" }).click()
  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /新租户/ })).toBeVisible()
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({
    name: "新租户",
    username: "new-company",
    password: "Password123",
    request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
  })
  expect(
    requests.some((request) => /invite|\/users\/signup/.test(request.path)),
  ).toBe(false)
})

test("platform administrator can select and clear tenant default BC", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    bcs: [
      { bc_id: "bc-first", name: "第一 BC" },
      { bc_id: "bc-second", name: "默认目标" },
    ],
  })
  await page.goto("/platform/tenants")
  const row = page.getByRole("row", { name: new RegExp(A) })
  await row.getByRole("button", { name: "编辑", exact: true }).click()
  let sheet = page.getByRole("dialog", { name: "编辑租户", exact: true })
  await sheet.getByRole("combobox", { name: "默认 BC" }).click()
  await page
    .getByRole("dialog", { name: "选择默认 BC" })
    .getByRole("option", { name: /默认目标/ })
    .click()
  await sheet.getByRole("button", { name: "保存修改" }).click()
  await expect(sheet).not.toBeVisible()
  expect(requests.filter((r) => r.method === "PATCH")[0]?.body).toEqual({
    name: "租户甲",
    default_bc_id: "bc-second",
  })
  await row.getByRole("button", { name: "编辑", exact: true }).click()
  sheet = page.getByRole("dialog", { name: "编辑租户", exact: true })
  await expect(sheet.getByRole("combobox", { name: "默认 BC" })).toContainText(
    "bc-second",
  )
  await sheet.getByRole("button", { name: "清除默认 BC" }).click()
  await sheet.getByRole("button", { name: "保存修改" }).click()
  await expect(sheet).not.toBeVisible()
  expect(requests.filter((r) => r.method === "PATCH")[1]?.body).toEqual({
    name: "租户甲",
    default_bc_id: null,
  })
})

test("initial tenant navigation honors configured BC, then first BC when unset", async ({
  page,
}) => {
  await boundary(page, {
    defaultBC: "2002",
    bcs: [
      { bc_id: "1001", name: "第一 BC" },
      { bc_id: "2002", name: "默认目标" },
    ],
  })
  await page.goto(`/tenants/${A}/accounts`)
  await expect(page).toHaveURL(/bc_id=2002/)
  await page.goto(`/tenants/${B}/accounts`)
  await expect(page).toHaveURL(/bc_id=1001/)
})

test("unsaved Sheet close can retain text or discard without sending any mutation", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/platform/tenants")
  await page.getByRole("button", { name: "新建租户" }).click()
  const sheet = page.getByRole("dialog", { name: "新建租户", exact: true })
  await sheet.getByLabel("租户名称").fill("未保存名称")
  await sheet.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(sheet.getByLabel("租户名称")).toHaveValue("未保存名称")
  await page.keyboard.press("Escape")
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(sheet).not.toBeVisible()
  expect(requests.some((request) => request.method !== "GET")).toBe(false)
})

test("tenant edit, disable and reactivate use explicit writes; inactive details never fetch members", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/platform/tenants")
  const row = page.getByRole("row", { name: new RegExp(A) })
  await row.getByRole("button", { name: "编辑", exact: true }).click()
  await page
    .getByRole("dialog", { name: "编辑租户" })
    .getByLabel("租户名称")
    .fill("甲的新名称")
  await page.getByRole("button", { name: "保存修改" }).click()
  await expect(row).toContainText("甲的新名称")
  await row.getByRole("button", { name: "停用", exact: true }).click()
  await expect(
    page.getByText(/已在 TikTok 启用的广告不会自动停止/),
  ).toBeVisible()
  await page.getByRole("button", { name: "确认停用" }).click()
  await expect(row.getByRole("button", { name: "进入租户" })).toBeDisabled()
  const readsBefore = requests.filter((request) =>
    request.path.endsWith("/members"),
  ).length
  await row.getByRole("button", { name: "查看" }).click()
  await expect(page.getByText("恢复租户后可查看成员。")).toBeVisible()
  expect(
    requests.filter((request) => request.path.endsWith("/members")),
  ).toHaveLength(readsBefore)
  await page
    .getByRole("dialog", { name: "租户详情" })
    .getByRole("button", { name: "关闭", exact: true })
    .click()
  await row.getByRole("button", { name: "恢复", exact: true }).click()
  await page.getByRole("button", { name: "确认恢复" }).click()
  await expect(row.getByRole("button", { name: "进入租户" })).toBeEnabled()
  expect(
    requests
      .filter((request) => request.method === "PATCH")
      .map((request) => request.body),
  ).toEqual([{ name: "甲的新名称" }, { active: false }, { active: true }])
})

test("tenant list uses seek pagination at 50/100 and resets cursor on server filtering", async ({
  page,
}) => {
  const { requests } = await boundary(page, { count: 101 })
  await page.goto("/platform/tenants")
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await expect(page.getByText("共 101 条 · 第 1 / 3 页")).toBeVisible()
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText("共 101 条 · 第 2 / 3 页")).toBeVisible()
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.getByText("共 101 条 · 第 1 / 2 页")).toBeVisible()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await page.getByLabel("搜索租户名称或 ID").fill("租户乙")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(1)
  await expect(page.getByText("共 1 条 · 第 1 / 1 页")).toBeVisible()
  await page.getByLabel("搜索租户名称或 ID").fill("不存在的租户")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.getByText("共 0 条 · 第 1 / 1 页")).toBeVisible()
  const last = requests
    .filter((request) => request.path === "/api/platform/tenants")
    .slice(-1)[0]!
  expect(last.query.get("search")).toBe("不存在的租户")
  expect(last.query.has("after_id")).toBe(false)
  expect(last.query.get("limit")).toBe("100")
})

test("members can be added by tenant administrators through scoped candidate search", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.getByRole("row", { name: /甲管理员/ })).toBeVisible()
  await page.getByRole("button", { name: "添加成员" }).click()
  const sheet = page.getByRole("dialog", { name: "添加成员", exact: true })
  await sheet.getByRole("tab", { name: "选择已有用户" }).click()
  await sheet.getByRole("combobox", { name: "已有用户" }).click()
  const picker = page.getByRole("dialog", { name: "选择已有用户" })
  await picker.getByLabel("搜索已有用户").fill("候选")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("option", { name: /候选用户/ }).click()
  await sheet.getByLabel("租户角色").click()
  await expect(page.getByRole("option")).toHaveCount(3)
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await sheet.getByRole("button", { name: "保存成员" }).click()
  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /候选用户/ })).toContainText(
    "只读成员",
  )
  expect(requests.find((request) => request.method === "PUT")).toMatchObject({
    path: `/api/tenants/${A}/members`,
    body: { user_id: V, role: "viewer", active: true },
  })
  expect(requests.some((request) => request.path === "/api/users/")).toBe(false)
})

test("tenant administrator creates a login and binds it to the current tenant", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  await page.goto(`/tenants/${A}/members`)
  await page.getByRole("button", { name: "添加成员" }).click()
  const sheet = page.getByRole("dialog", { name: "添加成员", exact: true })

  await expect(sheet.getByRole("tab", { name: "新建用户" })).toHaveAttribute(
    "data-state",
    "active",
  )
  await sheet.getByLabel("登录账号").fill("new-operator")
  await sheet.getByLabel("姓名（选填）").fill("新投手")
  await sheet.getByLabel("初始密码", { exact: true }).fill("initial-password")
  await sheet.getByLabel("确认密码").fill("different-password")
  await sheet.getByRole("button", { name: "创建并添加" }).click()
  await expect(sheet.getByText("两次输入的密码不一致")).toBeVisible()
  expect(
    requests.filter((request) => request.path.endsWith("/members/users")),
  ).toHaveLength(0)

  await sheet.getByLabel("确认密码").fill("initial-password")
  await sheet.getByLabel("租户角色").click()
  await page.getByRole("option", { name: "投手", exact: true }).click()
  await sheet.getByRole("button", { name: "创建并添加" }).click()

  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /新投手/ })).toContainText("投手")
  const request = requests.find((item) => item.path.endsWith("/members/users"))
  expect(request).toMatchObject({
    method: "POST",
    path: `/api/tenants/${A}/members/users`,
    body: {
      username: "new-operator",
      full_name: "新投手",
      password: "initial-password",
      role: "operator",
    },
  })
  expect(request?.body).not.toHaveProperty("confirm_password")
  expect(request?.body).not.toHaveProperty("is_superuser")
})

test("last administrator rejection stays inside the editor and preserves changed role", async ({
  page,
}) => {
  await boundary(page, { lastAdmin: true })
  await page.goto(`/tenants/${A}/members`)
  await page.getByRole("button", { name: "编辑成员" }).click()
  const sheet = page.getByRole("dialog", { name: "编辑成员" })
  await sheet.getByLabel("租户角色").click()
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await sheet.getByRole("button", { name: "保存成员" }).click()
  await expect(
    sheet.getByText("请先设置其他租户管理员，再调整这位管理员。"),
  ).toBeVisible()
  await expect(sheet.getByLabel("租户角色")).toContainText("只读成员")
})

test("tenant switching changes URLs and clears old members while cancelling their request", async ({
  page,
}) => {
  let release!: () => void
  const delayedA = new Promise<void>((resolve) => {
    release = resolve
  })
  const { requests } = await boundary(page, { delayedA })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.getByRole("combobox", { name: "当前租户" })).toContainText(
    "租户甲",
  )
  const cancelled = page.waitForEvent("requestfailed", {
    predicate: (request) => request.url().includes(`/tenants/${A}/members`),
  })
  await chooseTenant(page, "租户乙")
  await expect(page).toHaveURL(`/tenants/${B}/members`)
  await cancelled
  release()
  await expect(page.getByRole("row", { name: /乙投手/ })).toBeVisible()
  await expect(page.getByText("甲管理员", { exact: true })).toHaveCount(0)
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("browser navigation with an unsaved member form can cancel or discard without cross-tenant writes", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/members`)
  await chooseTenant(page, "租户乙")
  await page.getByRole("button", { name: "编辑成员" }).click()
  const sheet = page.getByRole("dialog", { name: "编辑成员" })
  await sheet.getByLabel("租户角色").click()
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await page.goBack()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page).toHaveURL(`/tenants/${B}/members`)
  await expect(sheet.getByLabel("租户角色")).toContainText("只读成员")
  await page.goBack()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(page).toHaveURL(`/tenants/${A}/members`)
  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /甲管理员/ })).toBeVisible()
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("member creation draft stays guarded after switching to the existing-user tab", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/members`)
  await chooseTenant(page, "租户乙")
  await page.getByRole("button", { name: "添加成员" }).click()
  const sheet = page.getByRole("dialog", { name: "添加成员", exact: true })
  await sheet.getByLabel("登录账号").fill("unfinished-user")
  await sheet.getByRole("tab", { name: "选择已有用户" }).click()

  await page.goBack()
  await expect(page.getByRole("button", { name: "留在当前页" })).toBeVisible()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(sheet).toBeVisible()
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

for (const role of ["tenant_admin", "operator", "viewer"] as const)
  test(`${role} can open the demo from the tenant sidebar`, async ({ page }) => {
    await boundary(page, { platform: false, role })
    await page.goto(`/tenants/${A}/builds/new`)
    const demoLink = page.getByRole("link", { name: "打开体验版", exact: true })
    await expect(demoLink).toBeVisible()
    await expect(demoLink).toHaveAttribute("href", "/demo/")
    await expect(demoLink).toHaveAttribute("target", "_blank")
    await expect(demoLink).toHaveAttribute("rel", "noopener noreferrer")
    await page.context().route("**/demo/", (route) =>
      route.fulfill({ contentType: "text/html", body: "<title>体验版</title>" }),
    )
    const popupPromise = page.waitForEvent("popup")
    await demoLink.click()
    const popup = await popupPromise
    await expect(popup).toHaveURL(/\/demo\/$/)
    await expect(page).toHaveURL(`/tenants/${A}/builds/new`)
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe(token)
    await popup.close()
  })

for (const role of ["viewer", "operator"] as const)
  test(`${role} has no member write/navigation entry and keeps login on direct denial`, async ({
    page,
  }) => {
    await boundary(page, { platform: false, role })
    await page.goto(`/tenants/${A}/members`)
    await expect(
      page.getByText("无权访问此页面", { exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("link", { name: "成员管理", exact: true }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: "添加成员" })).toHaveCount(0)
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe(token)
    await page.getByRole("link", { name: "广告搭建", exact: true }).click()
    await expect(page).toHaveURL(`/tenants/${A}/builds/new`)
    await expect(
      page.getByRole("heading", { name: "尚未连接 TikTok BC", exact: true }),
    ).toBeVisible()
    if (role === "viewer")
      await expect(page.getByRole("button", { name: "新建搭建" })).toHaveCount(
        0,
      )
  })

test("server permission revocation removes write actions while retaining the session", async ({
  page,
}) => {
  await boundary(page, { members403: true })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "添加成员" })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})

test("members pagination and role/status search are scoped server queries", async ({
  page,
}) => {
  const { requests } = await boundary(page, { memberCount: 101 })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  await page.getByRole("button", { name: "上一页", exact: true }).click()
  await expect(page.getByRole("row", { name: /甲管理员/ })).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await page.getByRole("combobox", { name: "角色筛选" }).click()
  await page.getByRole("option", { name: "租户管理员", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(1)
  await page.getByRole("combobox", { name: "状态筛选" }).click()
  await page.getByRole("option", { name: "正常", exact: true }).click()
  await page.getByLabel("搜索成员姓名或账号").fill("甲")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect
    .poll(() =>
      requests
        .filter((request) => request.path.endsWith("/members"))
        .slice(-1)[0]
        ?.query.get("search"),
    )
    .toBe("甲")
  const last = requests
    .filter((request) => request.path.endsWith("/members"))
    .slice(-1)[0]!
  expect(last.path).toBe(`/api/tenants/${A}/members`)
  expect(last.query.get("role")).toBe("tenant_admin")
  expect(last.query.get("active")).toBe("true")
  expect(last.query.get("limit")).toBe("100")
  expect(last.query.has("after_id")).toBe(false)
})

test("filter failures retain prior rows and remain distinct from zero matches", async ({
  page,
}) => {
  await boundary(page, { listFailure: true })
  await page.goto("/platform/tenants")
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
  await page.getByLabel("搜索租户名称或 ID").fill("错误")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.getByText("暂时无法读取租户")).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toBeVisible()
  await page.getByLabel("搜索租户名称或 ID").fill("不匹配的租户")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("没有符合条件的记录", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toHaveCount(0)
  await page.getByRole("button", { name: "清除筛选", exact: true }).click()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
})

for (const width of [1440, 390])
  test(`tenant platform and edit sheet remain readable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await boundary(page)
    await page.goto("/platform/tenants")
    await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    await page.screenshot({
      path: test.info().outputPath(`tenants-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    await page.getByRole("button", { name: "新建租户" }).click()
    const sheet = page.getByRole("dialog", { name: "新建租户", exact: true })
    await expect(sheet.getByLabel("租户名称")).toBeFocused()
    await page.screenshot({
      path: test.info().outputPath(`tenant-sheet-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    await page.keyboard.press("Escape")
    await expect(sheet).not.toBeVisible()
    await expect(page.getByRole("button", { name: "新建租户" })).toBeFocused()
  })

test("ordinary entry resolves an authorized tenant with scoped navigation and no invented BC", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  await page.goto("/")
  await expect(page).toHaveURL(`/tenants/${A}/builds/new`)
  await expect(page.getByRole("combobox", { name: "当前租户" })).toContainText(
    "租户甲",
  )
  await expect(
    page
      .getByRole("navigation", { name: "设置" })
      .getByRole("link", { name: "成员管理", exact: true }),
  ).toHaveAttribute("href", `/tenants/${A}/members`)
  await expect(
    page.getByRole("link", { name: "素材库", exact: true }),
  ).toHaveAttribute("href", `/tenants/${A}/materials`)
  await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
  expect(requests.some((request) => /\/accounts/.test(request.path))).toBe(
    false,
  )
})

test("an unavailable tenant URL never reuses another tenant's scope or member data", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  const unavailable = "00000000-0000-4000-8000-000000000000"
  await page.goto(`/tenants/${unavailable}/members`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByText("甲管理员", { exact: true })).toHaveCount(0)
  expect(
    requests.some((request) =>
      request.path.includes(`/tenants/${unavailable}/members`),
    ),
  ).toBe(false)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})

for (const submit of ["button", "Enter"] as const)
  test(`member candidate re-search by ${submit} never submits the parent editor`, async ({
    page,
  }) => {
    const { requests } = await boundary(page)
    await page.goto(`/tenants/${A}/members`)
    const title = "添加成员"
    const label = "已有用户"
    await page.getByRole("button", { name: title }).click()
    const sheet = page.getByRole("dialog", { name: title, exact: true })
    await sheet.getByRole("tab", { name: "选择已有用户" }).click()
    await sheet.getByRole("combobox", { name: label }).click()
    const picker = page.getByRole("dialog", { name: `选择${label}` })
    await picker.getByLabel(`搜索${label}`).fill("候选")
    await picker.getByRole("button", { name: "搜索", exact: true }).click()
    await picker.getByRole("option", { name: /候选用户/ }).click()
    await sheet.getByRole("combobox", { name: label }).click()
    await picker.getByLabel(`搜索${label}`).fill("甲管理员")
    if (submit === "Enter")
      await picker.getByLabel(`搜索${label}`).press("Enter")
    else await picker.getByRole("button", { name: "搜索", exact: true }).click()
    await expect(picker.getByRole("option", { name: /甲管理员/ })).toBeVisible()
    expect(requests.filter((request) => request.method !== "GET")).toEqual([])
    await page.keyboard.press("Escape")
    await expect(sheet).toBeVisible()
    await expect(sheet.getByRole("combobox", { name: label })).toContainText(
      "候选用户",
    )
    await sheet.getByRole("button", { name: "保存成员" }).click()
    await expect(sheet).not.toBeVisible()
    expect(requests.filter((request) => request.method !== "GET")).toHaveLength(
      1,
    )
  })

for (const width of [1440, 390])
  for (const kind of ["tenant", "member"] as const)
    test(`${kind} long table keeps headers visible within its scroll region at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 })
      await boundary(page, { count: 101, memberCount: 101 })
      await page.goto(
        kind === "tenant" ? "/platform/tenants" : `/tenants/${A}/members`,
      )
      const container = page.locator('[data-slot="table-container"]')
      const header = page.locator('[data-slot="table-header"]')
      for (const limit of [50, 100]) {
        if (limit === 100) {
          await page.getByRole("combobox", { name: "每页条数" }).click()
          await page.getByRole("option", { name: "100 条" }).click()
        }
        await expect(page.locator("tbody tr")).toHaveCount(limit)
        await expectWorkspaceLayout(page)
        const dimensions = await container.evaluate((element) => ({
          height: element.clientHeight,
          scrollHeight: element.scrollHeight,
          width: element.clientWidth,
          scrollWidth: element.scrollWidth,
        }))
        expect(dimensions.height).toBeLessThanOrEqual(540)
        expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.height)
        await container.evaluate((element) => {
          element.scrollTop = 1000
        })
        await expect
          .poll(() => container.evaluate((element) => element.scrollTop))
          .toBeGreaterThan(500)
        const region = await container.boundingBox()
        const heading = await header.boundingBox()
        expect(Math.abs(heading!.y - region!.y)).toBeLessThan(3)
        await expect(header).toBeInViewport()
        if (width === 390) {
          expect(dimensions.scrollWidth).toBeGreaterThan(dimensions.width)
          await container.evaluate((element) => {
            element.scrollLeft = element.scrollWidth
          })
          expect(
            await container.evaluate((element) => element.scrollLeft),
          ).toBeGreaterThan(0)
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true)
        expect(
          await page.evaluate(() => document.documentElement.scrollHeight),
        ).toBeLessThan(1400)
      }
    })

const BC1 = "7000000000000000001"
const BC2 = "7000000000000000002"
const BC3 = "7000000000000000003"
const CONN = "66666666-6666-4666-8666-666666666666"
const MCP_CONN = "77777777-7777-4777-8777-000000000003"

test("authorization settings live only in their tab and unnamed connections remain distinguishable", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { connectionCount: 4 })
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}`)
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toBeVisible()
  await expect(page.getByText(/^使用授权：/)).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "更换授权", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await expect(
    page.getByText("使用授权：官方 API 授权 · 66666666", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(CONN) })).toContainText(
    "官方 API 授权 · 66666666",
  )
  await expect(
    page.getByRole("row", { name: new RegExp(MCP_CONN) }),
  ).toContainText("官方 API 授权 · 00000003")
  await expect(page.getByText(/未命名授权/)).toHaveCount(0)
  await expect(
    page.getByRole("link", { name: "管理授权", exact: true }),
  ).toHaveCount(0)
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

for (const role of ["operator", "viewer"] as const) {
  test(`${role} can view BC authorization but cannot change it`, async ({
    page,
  }) => {
    const { requests } = await accountBoundary(page, { role, singleBC: true })
    await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}&tab=connections`)
    await expect(
      page.getByText("使用授权：官方 API 授权 · 66666666", { exact: true }),
    ).toBeVisible()
    await expect(page.getByRole("button", { name: "更换授权" })).toHaveCount(0)
    await expect(
      page.getByRole("combobox", { name: "查看账户的连接" }),
    ).toHaveCount(0)
    expect(requests.every((request) => request.method === "GET")).toBe(true)
  })
}

test("BC authorization change is staged until save and cancel preserves original", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { mcpConnection: true })
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}&tab=connections`)
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await page.getByRole("button", { name: "更换授权", exact: true }).click()
  await expect(
    page.locator('[id="authorization-77777777-7777-4777-8777-000000000001"]'),
  ).toBeDisabled()
  await expect(
    page.locator('[id="authorization-77777777-7777-4777-8777-000000000002"]'),
  ).toBeDisabled()
  await page.getByRole("radio", { name: /MCP 业务连接/ }).click()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page
    .getByRole("button", { name: "丢弃未保存修改", exact: true })
    .click()
  await expect(
    page.getByText("使用授权：官方 API 授权 · 66666666", { exact: true }),
  ).toBeVisible()
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

test("BC authorization setting handles a rejected save without changing the account route", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { mcpConnection: true })
  await page.route("**/default-connection", (route) =>
    route.fulfill({
      status: 403,
      json: { code: "action_forbidden", message: "当前角色不能执行此操作" },
    }),
  )
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}&tab=connections`)
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await page.getByRole("button", { name: "更换授权", exact: true }).click()
  await page.getByRole("radio", { name: /MCP 业务连接/ }).click()
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await expect(
    page.getByRole("dialog").getByText("无权访问此页面", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存", exact: true }),
  ).toBeDisabled()
  expect(
    requests
      .filter((request) => request.path.endsWith("/accounts"))
      .every((request) => request.query.get("connection_id") === CONN),
  ).toBe(true)
})

test("BC authorization finds the persisted default beyond the first page", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    defaultConnectionId: "77777777-7777-4777-8777-000000000100",
  })
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}&tab=connections`)
  await expect(
    page.getByText("使用授权：官方 API 授权 · 00000100", { exact: true }),
  ).toBeVisible()
  expect(
    requests.some(
      (request) =>
        request.path.endsWith("/connections") &&
        request.query.get("cursor") === "100",
    ),
  ).toBe(true)
  expect(
    requests
      .filter((request) => request.path.endsWith("/accounts"))
      .every((request) =>
        request.query.get("connection_id")?.endsWith("000000000100"),
      ),
  ).toBe(true)
})

test("single BC authorization still requires an explicit initial save", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    noDefault: true,
    connectionCount: 1,
  })
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}&tab=connections`)
  await page.getByRole("button", { name: "设置使用授权", exact: true }).click()
  await expect(page.getByText(/当前 BC 只有一个可用授权/)).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存", exact: true }),
  ).toBeDisabled()
  await page.getByRole("radio").click()
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await page.getByRole("tab", { name: "广告账户", exact: true }).click()
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toBeVisible()
  expect(requests.filter((request) => request.method === "PUT")).toHaveLength(1)
})

for (const width of [1440, 390]) {
  test(`BC authorization settings remain readable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await accountBoundary(page, { mcpConnection: true, connectionCount: 4 })
    await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}&tab=connections`)
    await expect(
      page.getByText("使用授权：官方 API 授权 · 66666666", { exact: true }),
    ).toBeVisible()
    await page.screenshot({
      path: test.info().outputPath(`bc-authorization-${width}.png`),
      animations: "disabled",
    })
    await page.getByRole("tab", { name: "授权管理", exact: true }).click()
    await page.getByRole("button", { name: "更换授权", exact: true }).click()
    await page.getByRole("radio", { name: /MCP 业务连接/ }).click()
    await expect(
      page.getByRole("button", { name: "保存", exact: true }),
    ).toBeInViewport()
    await page.screenshot({
      path: test.info().outputPath(`bc-authorization-settings-${width}.png`),
      animations: "disabled",
    })
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width)
  })
}

for (const width of [900, 1440, 390]) {
  test(`associated BC details expose sync at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await accountBoundary(page, {
      mcpConnection: true,
      bindingBCs: [
        {
          bc_id: BC1,
          name: "有较长名称的业务中心用于核实同步账户操作是否可见",
          ownership_conflict: false,
          binding_status: "ACTIVE",
          discovery_status: "COMPLETE",
          last_discovery: "2026-09-16T01:00:00Z",
          is_default: true,
        },
      ],
    })
    await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC1}`)
    await page
      .getByRole("row", { name: /MCP 业务连接/ })
      .getByRole("button", { name: "查看详情" })
      .click()
    const sheet = page.getByRole("dialog", { name: "连接详情", exact: true })
    const sync = sheet.getByRole("button", { name: "同步账户", exact: true })
    await expect(sync).toBeVisible()
    if (width >= 900) {
      const geometry = await sync.evaluate((button) => {
        const container = button.closest('[data-slot="table-container"]')!
        const outer = container.getBoundingClientRect(),
          inner = button.getBoundingClientRect()
        return {
          left: inner.left - outer.left,
          right: outer.right - inner.right,
          scrollLeft: container.scrollLeft,
        }
      })
      expect(geometry.left).toBeGreaterThanOrEqual(0)
      expect(geometry.right).toBeGreaterThanOrEqual(0)
      expect(geometry.scrollLeft).toBe(0)
    } else
      await expect(
        sheet.getByText("可左右滑动表格查看全部内容与 BC 操作。", {
          exact: true,
        }),
      ).toBeVisible()
    await sheet
      .locator('[data-slot="table-container"]')
      .scrollIntoViewIfNeeded()
    await page.screenshot({
      path: test.info().outputPath(`bc-details-${width}.png`),
      animations: "disabled",
    })
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width)
  })
}

type CandidateBCFixture = {
  bc_id: string
  name: string
  connected?: boolean
  binding_status?: "SYNCING" | "ACTIVE" | "ERROR" | "DISABLED" | null
}
type BindingBCFixture = CandidateBCFixture & {
  ownership_conflict: boolean
  is_default?: boolean
  discovery_status?: "RUNNING" | "COMPLETE" | "ERROR" | "CANCELLED"
  last_discovery?: string | null
  error_code?: string | null
}
async function accountBoundary(
  page: Page,
  options: {
    role?: Role
    noBC?: boolean
    singleBC?: boolean
    configured?: boolean
    apiReady?: boolean
    mcpReady?: boolean
    mcpConnection?: boolean
    noDefault?: boolean
    defaultConnectionId?: string
    connectionCount?: number
    candidateReady?: boolean
    candidateBCs?: CandidateBCFixture[]
    availableBCs?: CandidateBCFixture[]
    refreshedBCs?: CandidateBCFixture[]
    bindingBCs?: BindingBCFixture[]
    account403?: boolean
    accountFailure?: boolean
    connection403?: boolean
    authorization403?: boolean
    delayedAccounts?: Promise<void>
  } = {},
) {
  const tenantBoundary = await boundary(page, {
    platform: !options.role,
    role: options.role,
  })
  const requests: Request[] = []
  const bcs = [
    {
      bc_id: BC1,
      name: "业务 BC 一",
      ownership_conflict: false,
      default_connection_id: options.noDefault
        ? null
        : options.defaultConnectionId || CONN,
    },
    {
      bc_id: BC2,
      name: "业务 BC 二",
      ownership_conflict: false,
      default_connection_id: options.noDefault ? null : CONN,
    },
  ]
  const defaultSelections = new Map<string, string>()
  const connections = Array.from(
    { length: options.connectionCount ?? 101 },
    (_, i) => ({
      id:
        i === 0
          ? CONN
          : `77777777-7777-4777-8777-${String(i).padStart(12, "0")}`,
      tenant_id: A,
      kind: options.mcpConnection && i === 3 ? "OFFICIAL_MCP" : "OFFICIAL_API",
      display_name: options.mcpConnection && i === 3 ? "MCP 业务连接" : "",
      binding_count: 1,
      is_default: i === 0 && !options.noDefault,
      read_authorized: true,
      upload_authorized: true,
      build_authorized: true,
      status: i === 1 ? "DISABLED" : i === 2 ? "REAUTH_REQUIRED" : "ACTIVE",
      last_discovery: "2026-09-08T10:00:00Z",
      last_authorized_at: "2026-09-08T09:00:00Z",
      error_code: null,
      authorization_attempt_id:
        options.candidateReady && i === 0
          ? "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
          : null,
      authorization_status:
        options.candidateReady && i === 0 ? "CANDIDATE_READY" : null,
    }),
  )
  await page.route("**/api/tenants/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname,
      q = url.searchParams
    if (
      !/\/(bcs|bcs\/[^/]+\/default-connection|accounts|tiktok\/.*)$/.test(path)
    )
      return route.fallback()
    const method = req.method()
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    const body = req.postData() ? req.postDataJSON() : undefined
    requests.push({
      method,
      path,
      query: q,
      body,
      authorization: req.headers().authorization,
    })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    const denied = () =>
      reply(
        {
          code: "action_forbidden",
          message: "当前角色不能执行此操作",
          retryable: false,
        },
        403,
      )
    const paginate = <T>(items: T[]) => {
      const start = Number(q.get("cursor") ?? 0),
        limit = Number(q.get("limit") ?? 50)
      return {
        items: items.slice(start, start + limit),
        next_cursor:
          start + limit < items.length ? String(start + limit) : null,
      }
    }
    if (
      (path.includes("/tiktok/mcp/candidates/") && path.endsWith("/bcs")) ||
      path.endsWith("/available-bcs")
    ) {
      const items = path.endsWith("/available-bcs")
        ? ((q.get("refresh") === "true"
            ? (options.refreshedBCs ?? options.availableBCs)
            : options.availableBCs) ?? bcs)
        : (options.candidateBCs ?? bcs)
      const page = Number(q.get("page") ?? 1)
      const pageSize = Number(q.get("page_size") ?? 50)
      return reply({
        items: items
          .slice((page - 1) * pageSize, page * pageSize)
          .map((item) => ({ connected: false, binding_status: null, ...item })),
        page,
        page_size: pageSize,
        total: items.length,
      })
    }
    if (
      path.includes("/tiktok/mcp/") &&
      (path.endsWith("/binding") || path.endsWith("/bindings"))
    )
      return reply({
        connection_id: MCP_CONN,
        items: body.bc_ids.map((bc_id: string) => ({
          bc_id,
          discovery_run_id: CONN,
          status: "RUNNING",
        })),
      })
    if (path.includes("/tiktok/mcp/connections/") && path.endsWith("/sync")) {
      const bc = options.bindingBCs?.find(
        (item) => item.bc_id === path.split("/").at(-2),
      )
      if (bc) {
        bc.binding_status = "ACTIVE"
        bc.discovery_status = "COMPLETE"
        bc.error_code = null
      }
      return reply({ discovery_run_id: CONN })
    }
    if (path.includes("/tiktok/mcp/connections/") && method === "DELETE") {
      const bc = options.bindingBCs?.find(
        (item) => item.bc_id === path.split("/").at(-1),
      )
      if (bc) {
        bc.binding_status = "DISABLED"
        bc.discovery_status = "CANCELLED"
        bc.is_default = false
      }
      return route.fulfill({ status: 204, headers })
    }
    if (path.endsWith("/default-connection") && method === "PUT") {
      defaultSelections.set(path.split("/").at(-2)!, body.connection_id)
      const bc = bcs.find((item) => item.bc_id === path.split("/").at(-2))
      if (bc) bc.default_connection_id = body.connection_id
      return reply({
        tenant_id: A,
        bc_id: path.split("/").at(-2),
        connection_id: body.connection_id,
        channel: "OFFICIAL_API",
        authorization_revision: 1,
        adapter_contract_revision: "fixture",
      })
    }
    if (path.endsWith("/bcs")) {
      if (q.get("connection_id") && options.bindingBCs)
        return reply(paginate(options.bindingBCs))
      if (q.get("connection_id"))
        return reply(
          paginate(
            Array.from({ length: 101 }, (_, i) => ({
              bc_id: String(8000000000000000001n + BigInt(i)),
              name: `连接专属 BC ${i + 1}`,
              ownership_conflict: false,
              is_default:
                defaultSelections.get(
                  String(8000000000000000001n + BigInt(i)),
                ) === q.get("connection_id"),
            })),
          ),
        )
      const items = options.noBC
        ? []
        : path.includes(B)
          ? [
              {
                bc_id: BC3,
                name: "乙租户 BC",
                ownership_conflict: false,
                default_connection_id: CONN,
              },
            ]
          : options.singleBC
            ? bcs.slice(0, 1)
            : bcs
      return reply(
        paginate(
          items.filter(
            (item) =>
              !q.get("query") ||
              `${item.name}${item.bc_id}`.includes(q.get("query")!),
          ),
        ),
      )
    }
    if (path.endsWith("/accounts")) {
      if (options.account403) return denied()
      if (options.accountFailure && q.get("query") === "错误")
        return reply(
          { code: "temporary_failure", message: "目录刷新暂时失败" },
          503,
        )
      if (q.get("bc_id") === BC1 && options.delayedAccounts)
        await options.delayedAccounts
      const items = Array.from({ length: 101 }, (_, i) => ({
        advertiser_id: String(90071992547409931n + BigInt(i)),
        bc_id: q.get("bc_id"),
        name: `${path.includes(B) ? "乙租户" : q.get("bc_id") === BC2 ? "第二BC" : "第一BC"}账户 ${i + 1}`,
        currency: i === 0 ? "" : "USD",
        timezone: i === 0 ? "" : "Asia/Shanghai",
        remote_status: i === 0 ? "STATUS_DISABLE" : "STATUS_ENABLE",
        ownership_conflict: i === 0,
        can_build:
          i !== 0 &&
          q.get("connection_id") !== "77777777-7777-4777-8777-000000000003",
        can_upload:
          i !== 0 &&
          q.get("connection_id") !== "77777777-7777-4777-8777-000000000003",
        permission_state: i === 0 ? "UNKNOWN" : "VERIFIED",
        availability:
          i === 0
            ? "OWNERSHIP_CONFLICT"
            : q.get("connection_id") === "77777777-7777-4777-8777-000000000003"
              ? "PERMISSION_UNKNOWN"
              : "AVAILABLE",
        checked_at: i === 0 ? null : "2026-09-08T10:00:00Z",
      }))
      return reply(
        paginate(
          items.filter(
            (item) =>
              (!q.get("query") ||
                `${item.name}${item.advertiser_id}`.includes(
                  q.get("query")!,
                )) &&
              (!q.get("remote_status") ||
                item.remote_status === q.get("remote_status")) &&
              (!q.get("availability") ||
                item.availability === q.get("availability")),
          ),
        ),
      )
    }
    if (path.endsWith("/configuration"))
      return reply({
        channels: [
          {
            kind: "OFFICIAL_API",
            configured: options.apiReady ?? options.configured !== false,
            status:
              (options.apiReady ?? options.configured !== false)
                ? "READY"
                : "INCOMPLETE",
            code:
              (options.apiReady ?? options.configured !== false)
                ? null
                : "app_not_configured",
          },
          {
            kind: "OFFICIAL_MCP",
            configured: options.mcpReady ?? false,
            status: options.mcpReady ? "READY" : "CLIENT_UNREGISTERED",
            code: options.mcpReady ? null : "mcp_client_unregistered",
          },
        ],
      })
    if (path.endsWith("/authorizations"))
      return options.authorization403
        ? denied()
        : reply({
            url: path.includes("/mcp/")
              ? "https://business-api.tiktok.com/portal/mcp-tt4b-authorize?state=synthetic-state"
              : "https://business-api.tiktok.com/portal/auth?state=synthetic-state",
          })
    if (method === "PATCH") {
      const item = connections.find((item) => path.endsWith(item.id))!
      item.status = "DISABLED"
      return reply(item)
    }
    if (path.endsWith("/connections"))
      return options.connection403
        ? denied()
        : reply(
            paginate(
              connections.filter(
                (item) => !q.get("status") || item.status === q.get("status"),
              ),
            ),
          )
    return reply({ code: "unexpected_test_request" }, 500)
  })
  return { requests, tenantRequests: tenantBoundary.requests }
}

test("account directory preserves full IDs and real metadata without discovery or build selection", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { singleBC: true })
  await page.goto(`/tenants/${A}/accounts?tab=accounts`)
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
  const row = page.getByRole("row", { name: /第一BC账户 1 / })
  await expect(row).toContainText("90071992547409931")
  await expect(row).toContainText("待完善")
  await expect(row).toContainText("STATUS_DISABLE")
  await expect(row).toContainText("归属冲突")
  await expect(row).toContainText("尚未核验")
  await expect(page.getByRole("combobox", { name: "当前 BC" })).toHaveCount(0)
  await expect(page.getByRole("checkbox")).toHaveCount(0)
  await expect(page.getByRole("button", { name: /搭建|素材账户/ })).toHaveCount(
    0,
  )
  await row.getByRole("button", { name: "查看详情" }).click()
  const sheet = page.getByRole("dialog", { name: "账户详情" })
  await expect(sheet).toContainText("该账户存在其他租户归属")
  await expect(sheet).toContainText("UNKNOWN")
  expect(requests.every((req) => req.method === "GET")).toBe(true)
  expect(requests.filter((req) => req.path.endsWith("/accounts"))).toHaveLength(
    1,
  )
  expect(requests.every((req) => req.authorization === `Bearer ${token}`)).toBe(
    true,
  )
})

test("account filters and 50/100 cursor pages stay on the URL BC", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC2}`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条" }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await page.getByLabel("账户名称或 ID").fill("第二BC账户 2")
  await page.getByLabel("平台状态", { exact: true }).fill("STATUS_ENABLE")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByRole("combobox", { name: "可用性", exact: true }).click()
  await page.getByRole("option", { name: "可用", exact: true }).click()
  await expect
    .poll(() =>
      requests
        .filter((req) => req.path.endsWith("/accounts"))
        .slice(-1)[0]
        ?.query.get("availability"),
    )
    .toBe("AVAILABLE")
  const accountRequests = requests.filter((req) =>
    req.path.endsWith("/accounts"),
  )
  expect(accountRequests.some((req) => req.query.get("cursor") === "50")).toBe(
    true,
  )
  expect(accountRequests.every((req) => req.query.get("bc_id") === BC2)).toBe(
    true,
  )
  expect(accountRequests.slice(-1)[0]?.query.get("query")).toBe("第二BC账户 2")
  expect(accountRequests.slice(-1)[0]?.query.get("remote_status")).toBe(
    "STATUS_ENABLE",
  )
  expect(accountRequests.slice(-1)[0]?.query.get("cursor")).toBeNull()
})

test("changing BC cancels old account requests and starts with fresh page state", async ({
  page,
}) => {
  let release!: () => void
  const delayedAccounts = new Promise<void>((resolve) => {
    release = resolve
  })
  const { requests } = await accountBoundary(page, { delayedAccounts })
  const cancelled: string[] = []
  page.on("requestfailed", (req) => cancelled.push(req.url()))
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}`)
  await expect
    .poll(() => requests.some((req) => req.path.endsWith("/accounts")))
    .toBe(true)
  await page.getByRole("combobox", { name: "当前 BC" }).click()
  await page.getByRole("option", { name: /业务 BC 二/ }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
  await expect(page.getByRole("row", { name: /第二BC账户 1 / })).toBeVisible()
  release()
  await expect
    .poll(() => cancelled.some((url) => url.includes(`/accounts?bc_id=${BC1}`)))
    .toBe(true)
  await expect(page.getByRole("row", { name: /第一BC账户/ })).toHaveCount(0)
})

test("tenant changes reset the previous BC and retain only the new tenant account scope", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC2}`)
  await expect(page.getByRole("row", { name: /第二BC账户 1 / })).toBeVisible()
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /租户乙/ }).click()
  await expect(page).toHaveURL(
    new RegExp(`/tenants/${B}/accounts.*bc_id=${BC3}`),
  )
  await expect(page.getByRole("row", { name: /乙租户账户 1 / })).toBeVisible()
  expect(
    requests
      .filter((req) => req.path === `/api/tenants/${B}/accounts`)
      .every((req) => req.query.get("bc_id") === BC3),
  ).toBe(true)
  await expect(page.getByRole("row", { name: /第二BC账户/ })).toHaveCount(0)
})

test("unsaved member edits guard BC changes without saving or rebinding", async ({
  page,
}) => {
  const { requests, tenantRequests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/members?bc_id=${BC2}`)
  await page.getByRole("combobox", { name: "当前 BC" }).click()
  await page.getByRole("option", { name: /业务 BC 一/ }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
  await page.getByRole("button", { name: "编辑成员", exact: true }).click()
  const sheet = page.getByRole("dialog", { name: "编辑成员" })
  await sheet.getByLabel("租户角色").click()
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await page.goBack()
  await expect(
    page.getByRole("dialog", { name: "有未保存的内容" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(sheet.getByLabel("租户角色")).toContainText("只读成员")
  expect(
    [...requests, ...tenantRequests].filter((req) => req.method !== "GET"),
  ).toHaveLength(0)
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
  await page.goBack()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
  await expect(sheet).not.toBeVisible()
  expect(
    [...requests, ...tenantRequests].filter((req) => req.method !== "GET"),
  ).toHaveLength(0)
})

test("connections remain tenant wide without BCs and configuration disables authorization", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    noBC: true,
    configured: false,
  })
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await expect(page.getByRole("tab", { name: "授权管理" })).toHaveAttribute(
    "aria-selected",
    "true",
  )
  await expect(page.getByText("接入准备状态", { exact: true })).toBeVisible()
  await expect(
    page.getByText("官方 API：等待平台完成接入配置", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "新增连接" })).toBeDisabled()
  await expect(page.locator("tbody tr")).toHaveCount(50)
  expect(
    requests
      .filter((req) => req.path.endsWith("/connections"))
      .every((req) => !req.query.has("bc_id")),
  ).toBe(true)
  expect(
    requests.some(
      (req) => req.path.endsWith("/accounts") || req.method !== "GET",
    ),
  ).toBe(false)
})

test("explicit authorization identifies the tenant and leaves for the official URL only after confirmation", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.route("https://business-api.tiktok.com/**", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: "<p>TikTok test boundary</p>",
    }),
  )
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC2}`)
  await page.getByRole("button", { name: "新增连接" }).click()
  await page.getByRole("button", { name: "官方 API", exact: true }).click()
  const sheet = page.getByRole("dialog", { name: "新增 TikTok 授权" })
  await expect(sheet).toContainText("所属租户：租户甲")
  expect(requests.some((req) => req.method === "POST")).toBe(false)
  await sheet.getByRole("button", { name: "前往 TikTok 授权" }).click()
  await expect(page).toHaveURL(
    "https://business-api.tiktok.com/portal/auth?state=synthetic-state",
  )
  expect(requests.find((req) => req.method === "POST")).toMatchObject({
    path: `/api/tenants/${A}/tiktok/authorizations`,
    body: { connection_id: null },
  })
})

test("disabling uses the explicit status API and disabled connections cannot reauthorize", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  const row = page.getByRole("row", { name: new RegExp(CONN) })
  await row.getByRole("button", { name: "停用", exact: true }).click()
  const sheet = page.getByRole("dialog", { name: "停用授权连接" })
  await expect(sheet).toContainText("不会停止 TikTok 上已启用的广告")
  await sheet.getByRole("button", { name: "确认停用" }).click()
  await expect(sheet).not.toBeVisible()
  await expect(row).toContainText("已停用")
  await expect(row.getByRole("button", { name: "重新授权" })).toHaveCount(0)
  expect(requests.find((req) => req.method === "PATCH")).toMatchObject({
    path: `/api/tenants/${A}/tiktok/connections/${CONN}`,
    body: { status: "DISABLED" },
  })
})

for (const role of ["viewer", "operator"] as const)
  test(`${role} can inspect connections but has no authorization actions`, async ({
    page,
  }) => {
    await accountBoundary(page, { role })
    await page.goto(`/tenants/${A}/accounts?tab=connections`)
    await expect(page.locator("tbody tr")).toHaveCount(50)
    await expect(
      page.getByRole("button", { name: /新增连接|重新授权|停用/ }),
    ).toHaveCount(0)
    await page.getByRole("button", { name: "查看详情" }).first().click()
    await expect(page.getByRole("dialog", { name: "连接详情" })).toBeVisible()
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe(token)
  })

test("authorization 403 retains the session and confirmation context", async ({
  page,
}) => {
  await accountBoundary(page, { authorization403: true })
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await page.getByRole("button", { name: "新增连接" }).click()
  await page.getByRole("button", { name: "官方 API", exact: true }).click()
  const sheet = page.getByRole("dialog", { name: "新增 TikTok 授权" })
  await sheet.getByRole("button", { name: "前往 TikTok 授权" }).click()
  await expect(sheet).toContainText("当前角色无权执行此操作")
  await expect(
    sheet.getByRole("button", { name: "前往 TikTok 授权" }),
  ).toBeDisabled()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})

for (const authorization of [
  "CANDIDATE_READY",
  "CANCELLED",
  "invalid_oauth_state",
])
  test(`callback ${authorization} opens the connection tab without repeating authorization`, async ({
    page,
  }) => {
    const { requests } = await accountBoundary(page, { noBC: true })
    await page.goto(
      `/tenants/${A}/accounts?tab=connections&authorization=${authorization}&connection_id=${CONN}`,
    )
    await expect(page.getByRole("tab", { name: "授权管理" })).toHaveAttribute(
      "aria-selected",
      "true",
    )
    await expect(
      page.getByText(
        authorization === "CANDIDATE_READY"
          ? "授权已返回，等待账户发现完成"
          : authorization === "CANCELLED"
            ? "已取消授权"
            : "授权未完成",
        { exact: true },
      ),
    ).toBeVisible()
    expect(requests.every((req) => req.method === "GET")).toBe(true)
  })

// 浏览器内加载开发服务器上的工装页/源码模块，见 playwright.config.ts 的 devServerOnly。
test("exported bulk input resolves exact lines and displays only parent-paged results without selection", {
  tag: "@dev-server",
}, async ({ page }) => {
  let submitted: unknown
  await page.route("**/api/**", (route) => {
    submitted = route.request().postDataJSON()
    return route.fulfill({
      json: [
        {
          line_no: 1,
          raw: "90071992547409931",
          status: "MATCHED",
          advertiser_id: "90071992547409931",
        },
        {
          line_no: 2,
          raw: "重复名称",
          status: "AMBIGUOUS",
          candidates: ["90071992547409932", "90071992547409933"],
          reason: "多个完整名称匹配",
        },
        {
          line_no: 3,
          raw: "不存在",
          status: "NOT_FOUND",
          reason: "当前 BC 未找到该账户",
        },
      ],
    })
  })
  await page.goto("/tests/fixtures/bulk-account-input.html")
  const input = page.getByLabel("批量粘贴账户")
  await input.fill("90071992547409931\n重复名称\n不存在")
  await page.getByRole("button", { name: "解析账户", exact: true }).click()
  await expect(page.getByRole("listitem")).toHaveCount(2)
  await expect(page.getByText("已解析，可直接使用")).toBeVisible()
  await expect(page.getByText("名称有歧义：多个完整名称匹配")).toBeVisible()
  await expect(page.getByRole("checkbox")).toHaveCount(0)
  await expect(page.getByText("第 3 行", { exact: false })).toHaveCount(0)
  expect(submitted).toEqual({
    bc_id: BC1,
    lines: [
      { line_no: 1, raw: "90071992547409931" },
      { line_no: 2, raw: "重复名称" },
      { line_no: 3, raw: "不存在" },
    ],
  })
  await page.getByRole("button", { name: "下一段结果" }).click()
  await expect(page.getByRole("listitem")).toHaveCount(1)
  await expect(page.getByText("未找到：当前 BC 未找到该账户")).toBeVisible()
  await expect(input).toHaveValue("90071992547409931\n重复名称\n不存在")
})

test("connection BCs load only in details and paginate by connection without topbar filtering", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC2}`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  expect(requests.some((req) => req.query.has("connection_id"))).toBe(false)
  await page
    .getByRole("row", { name: new RegExp(CONN) })
    .getByRole("button", { name: "查看详情" })
    .click()
  const sheet = page.getByRole("dialog", { name: "连接详情" })
  await expect(sheet).toContainText("最近授权生效")
  await expect(sheet.locator("tbody tr")).toHaveCount(50)
  await expect(sheet).toContainText("8000000000000000001")
  await sheet.getByRole("button", { name: "下一页" }).click()
  await expect(sheet.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  await sheet.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条" }).click()
  await expect(sheet.locator("tbody tr")).toHaveCount(100)
  const reads = requests.filter((req) => req.query.has("connection_id"))
  expect(
    reads.every(
      (req) =>
        req.query.get("connection_id") === CONN && !req.query.has("bc_id"),
    ),
  ).toBe(true)
  expect(reads.some((req) => req.query.get("cursor") === "50")).toBe(true)
})

test("connection status filtering and pagination use tenant cursors", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC2}`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条" }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await page.getByRole("combobox", { name: "连接状态" }).click()
  await page.getByRole("option", { name: "已停用", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(1)
  const reads = requests.filter((req) => req.path.endsWith("/connections"))
  expect(reads.some((req) => req.query.get("cursor") === "50")).toBe(true)
  expect(reads.slice(-1)[0]?.query.get("status")).toBe("DISABLED")
  expect(reads.slice(-1)[0]?.query.get("cursor")).toBeNull()
})

test("account refresh errors retain rows while zero results remain a distinct state", async ({
  page,
}) => {
  await accountBoundary(page, { accountFailure: true })
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}`)
  await expect(page.getByRole("row", { name: /第一BC账户 1 / })).toBeVisible()
  await page.getByLabel("账户名称或 ID").fill("错误")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.getByText("目录刷新暂时失败")).toBeVisible()
  await expect(page.getByRole("row", { name: /第一BC账户 1 / })).toBeVisible()
  await page.getByLabel("账户名称或 ID").fill("没有这个账户")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.getByText("没有符合条件的记录")).toBeVisible()
  await expect(page.locator("tbody tr")).toHaveCount(0)
  await page.getByRole("button", { name: "清除筛选" }).click()
  await expect(page.locator("tbody tr")).toHaveCount(50)
})

for (const kind of ["account", "connection"] as const)
  test(`${kind} list permission denial keeps login and hides protected content`, async ({
    page,
  }) => {
    await accountBoundary(page, {
      account403: kind === "account",
      connection403: kind === "connection",
    })
    await page.goto(
      `/tenants/${A}/accounts?tab=${kind === "account" ? "accounts" : "connections"}&bc_id=${BC1}`,
    )
    await expect(
      page.getByText("无权访问此页面", { exact: true }).first(),
    ).toBeVisible()
    await expect(page.locator("tbody tr")).toHaveCount(0)
    await expect(
      page.getByRole("button", { name: /新增连接|重新授权|停用/ }),
    ).toHaveCount(0)
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe(token)
  })

for (const width of [1440, 390])
  test(`accounts and connection details keep bounded tables and readable sheets at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await accountBoundary(page)
    await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}`)
    await expect(page.locator("tbody tr")).toHaveCount(50)
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
    const container = page.locator('[data-slot="table-container"]')
    await container.evaluate((element) => {
      element.scrollTop = 1000
    })
    await expect(page.locator("thead")).toBeInViewport()
    await page.screenshot({
      path: test.info().outputPath(`accounts-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    await page.getByRole("tab", { name: "授权管理" }).click()
    await page.getByRole("button", { name: "查看详情" }).first().click()
    const sheet = page.getByRole("dialog", { name: "连接详情" })
    await expect(sheet.locator("tbody tr")).toHaveCount(50)
    await page.screenshot({
      path: test.info().outputPath(`connection-detail-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    expect(
      await sheet.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true)
    await page.keyboard.press("Escape")
    await expect(sheet).not.toBeVisible()
    await expect(
      page.getByRole("button", { name: "查看详情" }).first(),
    ).toBeFocused()
  })

for (const arrival of ["initial", "refetch"] as const)
  test(`a ${arrival} default BC waits for the unsaved member form navigation guard`, async ({
    page,
  }) => {
    const { requests, tenantRequests } = await accountBoundary(page)
    let release!: () => void
    const delayed = new Promise<void>((resolve) => {
      release = resolve
    })
    let reads = 0
    await page.route("**/api/tenants/*/bcs*", async (route) => {
      reads++
      if (arrival === "refetch" && reads === 1)
        return route.fulfill({ json: { items: [], next_cursor: null } })
      await delayed
      return route.fulfill({
        json: {
          items: [
            { bc_id: BC1, name: "稍后发现的 BC", ownership_conflict: false },
          ],
          next_cursor: null,
        },
      })
    })
    await page.goto(`/tenants/${A}/members`)
    await page.getByRole("button", { name: "编辑成员", exact: true }).click()
    const sheet = page.getByRole("dialog", { name: "编辑成员", exact: true })
    await sheet.getByLabel("租户角色").click()
    await page.getByRole("option", { name: "只读成员", exact: true }).click()
    if (arrival === "refetch") {
      // Drive the browser lifecycle boundary used by Query's default refetch.
      await page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "hidden",
        })
        window.dispatchEvent(new Event("visibilitychange"))
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "visible",
        })
        window.dispatchEvent(new Event("visibilitychange"))
      })
      await expect.poll(() => reads).toBeGreaterThan(1)
    }
    release()
    const guard = page.getByRole("dialog", { name: "有未保存的内容" })
    await expect(guard).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`/tenants/${A}/members$`))
    await guard.getByRole("button", { name: "留在当前页" }).click()
    await expect(sheet.getByLabel("租户角色")).toContainText("只读成员")
    await expect(page).toHaveURL(new RegExp(`/tenants/${A}/members$`))
    expect(
      [...requests, ...tenantRequests].filter((req) => req.method !== "GET"),
    ).toHaveLength(0)
    await page.keyboard.press("Escape")
    await page.getByRole("button", { name: "丢弃未保存修改" }).click()
    await expect(sheet).not.toBeVisible()
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByRole("option", { name: /稍后发现的 BC/ }).click()
    await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
    expect(
      [...requests, ...tenantRequests].filter((req) => req.method !== "GET"),
    ).toHaveLength(0)
  })

test("completed first discovery refreshes the empty BC directory without reloading the browser", async ({
  page,
}) => {
  await accountBoundary(page)
  let completed = false
  let bcReads = 0
  await page.route("**/api/tenants/*/bcs*", (route) => {
    bcReads++
    return route.fulfill({
      json: {
        items: completed
          ? [{ bc_id: BC1, name: "新发现 BC", ownership_conflict: false }]
          : [],
        next_cursor: null,
      },
    })
  })
  await page.route("**/api/tenants/*/tiktok/connections*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: CONN,
            tenant_id: A,
            status: completed ? "ACTIVE" : "DISCOVERING",
            discovery_status: completed ? "COMPLETE" : "RUNNING",
            last_discovery: completed ? "2026-09-09T10:00:00Z" : null,
            last_authorized_at: completed ? "2026-09-09T10:00:00Z" : null,
            error_code: null,
          },
        ],
        next_cursor: null,
      },
    }),
  )
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await expect(page.getByRole("row", { name: new RegExp(CONN) })).toContainText(
    "正在发现账户",
  )
  await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
  completed = true
  await expect.poll(() => bcReads, { timeout: 12000 }).toBeGreaterThan(1)
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
  // 新授权发现 BC 后，仍需在统一入口保存使用授权。
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await expect(
    page.getByText("使用授权：尚未设置", { exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "设置使用授权", exact: true }).click()
  await expect(page.getByRole("radio")).toHaveCount(1)
  await expect(
    page.getByRole("button", { name: "保存", exact: true }),
  ).toBeDisabled()
})

for (const phase of ["QUEUED", "RUNNING", "ADMISSION_WAIT"] as const)
  test(`an ACTIVE connection with ${phase} discovery refreshes exact BC and open details on completion`, async ({
    page,
  }) => {
    await accountBoundary(page)
    let completed = false
    let exactReads = 0
    let detailReads = 0
    await page.route("**/api/tenants/*/bcs*", (route) => {
      const query = new URL(route.request().url()).searchParams
      if (query.has("connection_id")) {
        detailReads++
        return route.fulfill({
          json: {
            items: [
              {
                bc_id: BC3,
                name: completed ? "新关联范围" : "旧关联范围",
                ownership_conflict: false,
              },
            ],
            next_cursor: null,
          },
        })
      }
      if (query.get("query") === BC3) {
        exactReads++
        return route.fulfill({
          json: {
            items: [
              {
                bc_id: BC3,
                name: completed ? "核验后 BC" : "原有 BC",
                ownership_conflict: false,
              },
            ],
            next_cursor: null,
          },
        })
      }
      return route.fulfill({
        json: {
          items: [{ bc_id: BC1, name: "首页 BC", ownership_conflict: false }],
          next_cursor: "more",
        },
      })
    })
    await page.route("**/api/tenants/*/tiktok/connections*", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: CONN,
              tenant_id: A,
              status: "ACTIVE",
              discovery_status: completed ? "COMPLETE" : phase,
              last_discovery: completed
                ? "2026-09-09T10:00:00Z"
                : "2026-09-08T10:00:00Z",
              last_authorized_at: completed
                ? "2026-09-09T10:00:00Z"
                : "2026-09-08T10:00:00Z",
              error_code: null,
            },
          ],
          next_cursor: null,
        },
      }),
    )
    await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC3}`)
    await expect(page.getByRole("combobox", { name: "当前 BC" })).toContainText(
      "原有 BC",
    )
    const row = page.getByRole("row", { name: new RegExp(CONN) })
    await expect(row).toContainText("可用")
    await row.getByRole("button", { name: "查看详情" }).click()
    const sheet = page.getByRole("dialog", { name: "连接详情", exact: true })
    await expect(sheet).toContainText("旧关联范围")
    completed = true
    await expect.poll(() => exactReads, { timeout: 12000 }).toBeGreaterThan(1)
    await expect.poll(() => detailReads).toBeGreaterThan(1)
    await expect(sheet).toContainText("新关联范围")
    await expect(sheet).not.toContainText("旧关联范围")
    await page.keyboard.press("Escape")
    await expect(page.getByRole("combobox", { name: "当前 BC" })).toContainText(
      "核验后 BC",
    )
  })

for (const phase of ["ERROR", "CANCELLED", "COMPLETE", "DISABLED"] as const)
  test(`discovery ${phase} stops polling and preserves the actual connection status`, async ({
    page,
  }) => {
    await page.clock.install()
    await accountBoundary(page, { noBC: true })
    let reads = 0
    await page.route("**/api/tenants/*/tiktok/connections*", (route) => {
      reads++
      return route.fulfill({
        json: {
          items: [
            {
              id: CONN,
              tenant_id: A,
              status: phase === "DISABLED" ? "DISABLED" : "ACTIVE",
              discovery_status: phase === "DISABLED" ? "RUNNING" : phase,
              last_discovery: "2026-09-08T10:00:00Z",
              last_authorized_at: "2026-09-08T10:00:00Z",
              error_code: phase === "ERROR" ? "discovery_failed" : null,
            },
          ],
          next_cursor: null,
        },
      })
    })
    await page.goto(`/tenants/${A}/accounts?tab=connections`)
    const row = page.getByRole("row", { name: new RegExp(CONN) })
    await expect(row).toContainText(phase === "DISABLED" ? "已停用" : "可用")
    if (phase === "ERROR") await expect(row).toContainText("发现失败")
    if (phase === "CANCELLED") await expect(row).toContainText("已取消")
    const initialReads = reads
    // Advance past two 5-second intervals to assert no terminal-state polling.
    await page.clock.fastForward(11000)
    expect(reads).toBe(initialReads)
    await expect(row).toContainText(phase === "DISABLED" ? "已停用" : "可用")
  })

test("failed candidate discovery retains the accepted BC and accounts without repeating authorization", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { singleBC: true })
  let failed = false
  await page.route("**/api/tenants/*/tiktok/connections*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: CONN,
            tenant_id: A,
            status: "ACTIVE",
            discovery_status: failed ? "ERROR" : "RUNNING",
            last_discovery: "2026-09-08T10:00:00Z",
            last_authorized_at: "2026-09-08T10:00:00Z",
            error_code: failed ? "discovery_failed" : null,
          },
        ],
        next_cursor: null,
      },
    }),
  )
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}`)
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toContainText(
    "可搭建",
  )
  await page.getByRole("tab", { name: "授权管理" }).click()
  const row = page.getByRole("row", { name: new RegExp(CONN) })
  await expect(row).toContainText("正在发现账户")
  failed = true
  await expect(row).toContainText("发现失败", { timeout: 12000 })
  await expect(row).toContainText("可用")
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
  await page.getByRole("tab", { name: "广告账户", exact: true }).click()
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toContainText(
    "可搭建",
  )
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

async function filteredDiscoveryBoundary(page: Page) {
  const { requests } = await accountBoundary(page)
  const state = {
    completed: false,
    bcReads: 0,
    filteredEmpty: 0,
    filteredPending: 0,
  }
  await page.route("**/api/tenants/*/bcs*", (route) => {
    state.bcReads++
    return route.fulfill({
      json: {
        items: state.completed
          ? [
              {
                bc_id: BC1,
                name: "已接受的 BC",
                ownership_conflict: false,
                default_connection_id: CONN,
              },
            ]
          : [],
        next_cursor: null,
      },
    })
  })
  await page.route("**/api/tenants/*/tiktok/connections*", (route) => {
    const query = new URL(route.request().url()).searchParams
    const status = state.completed ? "ACTIVE" : "DISCOVERING"
    const pending = {
      id: CONN,
      tenant_id: A,
      status,
      discovery_status: state.completed ? "COMPLETE" : "RUNNING",
      last_discovery: state.completed ? "2026-09-09T10:00:00Z" : null,
    }
    if (query.get("status") && query.get("status") !== status) {
      state.filteredEmpty++
      return route.fulfill({ json: { items: [], next_cursor: null } })
    }
    if (query.get("status") === "DISCOVERING") state.filteredPending++
    if (query.get("cursor"))
      return route.fulfill({
        json: {
          items: [
            {
              id: "88888888-8888-4888-8888-888888888888",
              tenant_id: A,
              status: "ACTIVE",
              discovery_status: null,
              last_discovery: null,
            },
          ],
          next_cursor: null,
        },
      })
    return route.fulfill({ json: { items: [pending], next_cursor: "next" } })
  })
  return { state, requests }
}

test("a completed connection leaving the DISCOVERING filter refreshes accepted BCs", async ({
  page,
}) => {
  const { state, requests } = await filteredDiscoveryBoundary(page)
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await expect(page.getByRole("row", { name: new RegExp(CONN) })).toContainText(
    "正在发现账户",
  )
  await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
  await page.getByRole("combobox", { name: "连接状态", exact: true }).click()
  await page.getByRole("option", { name: "正在发现账户", exact: true }).click()
  await expect.poll(() => state.filteredPending).toBeGreaterThan(0)
  state.completed = true
  await expect
    .poll(() => state.filteredEmpty, { timeout: 12000 })
    .toBeGreaterThan(0)
  await expect.poll(() => state.bcReads).toBeGreaterThan(1)
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC1}`))
  await page.getByRole("tab", { name: "广告账户", exact: true }).click()
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toContainText(
    "可搭建",
  )
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

for (const leaveBy of ["filter", "page"] as const)
  test(`a pending connection leaving by ${leaveBy} is not declared complete and account entry rechecks accepted BCs`, async ({
    page,
  }) => {
    const { state, requests } = await filteredDiscoveryBoundary(page)
    await page.goto(`/tenants/${A}/accounts?tab=connections`)
    await expect(
      page.getByRole("row", { name: new RegExp(CONN) }),
    ).toContainText("正在发现账户")
    await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
    if (leaveBy === "filter") {
      await page
        .getByRole("combobox", { name: "连接状态", exact: true })
        .click()
      await page.getByRole("option", { name: "连接错误", exact: true }).click()
      await expect(
        page.getByText("没有符合条件的记录", { exact: true }),
      ).toBeVisible()
    } else {
      await page.getByRole("button", { name: "下一页", exact: true }).click()
      await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
    }
    await expect.poll(() => state.bcReads).toBeGreaterThan(1)
    await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
    await expect(
      page.getByText("发现进度：已完成", { exact: true }),
    ).toHaveCount(0)
    const readsBeforeEntry = state.bcReads
    state.completed = true
    await page.getByRole("tab", { name: "广告账户", exact: true }).click()
    await expect.poll(() => state.bcReads).toBeGreaterThan(readsBeforeEntry)
    await expect(
      page.getByRole("row", { name: /第一BC账户 2 / }),
    ).toContainText("可搭建")
    expect(requests.filter((request) => request.method !== "GET")).toHaveLength(
      0,
    )
  })

for (const width of [1440, 390])
  test(`platform navigation requires explicit tenant entry and keeps BC-independent tools usable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    const { requests } = await boundary(page)
    await page.route("**/api/tenants/*/tiktok/configuration", (route) =>
      route.fulfill({
        json: {
          channels: [
            {
              kind: "OFFICIAL_API",
              configured: false,
              status: "NOT_CONFIGURED",
              code: "tiktok_app_not_configured",
            },
            {
              kind: "OFFICIAL_MCP",
              configured: false,
              status: "CLIENT_UNREGISTERED",
              code: "mcp_client_unregistered",
            },
          ],
        },
      }),
    )
    await page.route("**/api/tenants/*/tiktok/connections?*", (route) =>
      route.fulfill({ json: { items: [], next_cursor: null } }),
    )
    await page.route("**/api/tenants/*/strategies?*", (route) =>
      route.fulfill({ json: { items: [], next_cursor: null } }),
    )
    await page.route("**/api/tenants/*/providers/connections?*", (route) =>
      route.fulfill({ json: { items: [], next_cursor: null } }),
    )
    await page.goto("/")
    await expect(page).toHaveURL("/platform/tenants")
    await expect(
      page.getByRole("link", { name: "投放策略", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByText(
        "请从租户列表点击“进入租户”，再使用该租户的投放与管理功能。",
        { exact: true },
      ),
    ).toBeVisible()
    expect(requests.filter((r) => r.path.includes("/bcs"))).toHaveLength(0)
    await page
      .getByRole("row", { name: new RegExp(A) })
      .getByRole("button", { name: "进入租户", exact: true })
      .click()
    await expect(page).toHaveURL(`/tenants/${A}/builds/new`)
    const main = page.getByRole("main")
    await expect(main.getByText("尚未接入租户", { exact: true })).toHaveCount(0)
    await main
      .getByRole("link", { name: "查看账户与授权", exact: true })
      .click()
    await expect(page.getByText("接入准备状态", { exact: true })).toBeVisible()
    await expect(
      page.getByRole("button", { name: "新增连接", exact: true }),
    ).toBeDisabled()
    await page.goto(`/tenants/${A}/builds/new`)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
    await expect(
      main.getByRole("heading", { name: "尚未连接 TikTok BC", exact: true }),
    ).toBeVisible()
    await page.screenshot({
      path: test.info().outputPath(`tenant-no-bc-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    await main.getByRole("link", { name: "投放策略", exact: true }).click()
    await expect(page).toHaveURL(`/tenants/${A}/strategies`)
    await page.getByRole("link", { name: "新建策略", exact: true }).click()
    await expect(page.getByLabel("策略名称", { exact: true })).toBeEditable()
    await page.goto(`/tenants/${A}/builds/new`)
    await main.getByRole("link", { name: "版权方连接", exact: true }).click()
    await page.getByRole("button", { name: "新增连接", exact: true }).click()
    await expect(
      page.getByRole("dialog", { name: "新增版权方连接", exact: true }),
    ).toBeVisible()
    await page.goto(`/tenants/${A}/builds/new`)
    await main.getByRole("link", { name: "成员管理", exact: true }).click()
    await page.getByRole("button", { name: "添加成员", exact: true }).click()
    await expect(
      page.getByRole("dialog", { name: "添加成员", exact: true }),
    ).toBeVisible()
    expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  })

for (const destination of [
  "build-tasks",
  `build-drafts/${U}`,
  `build-previews/${U}`,
  `build-tasks/${U}`,
])
  test(`tenant BC empty state retains context for ${destination}`, async ({
    page,
  }) => {
    await boundary(page)
    await page.goto(`/tenants/${A}/${destination}`)
    const main = page.getByRole("main")
    await expect(main.getByText("尚未接入租户", { exact: true })).toHaveCount(0)
    await expect(
      main.getByRole("link", { name: "查看账户与授权", exact: true }),
    ).toHaveAttribute("href", `/tenants/${A}/accounts?tab=connections`)
    await expect(
      main.getByRole("link", { name: "投放策略", exact: true }),
    ).toBeVisible()
  })

for (const role of ["viewer", "operator"] as const)
  test(`${role} BC empty state offers read access without admin shortcuts`, async ({
    page,
  }) => {
    await boundary(page, { platform: false, role })
    await page.goto(`/tenants/${A}/build-tasks`)
    const main = page.getByRole("main")
    await expect(
      main.getByRole("link", { name: "成员管理", exact: true }),
    ).toHaveCount(0)
    await expect(
      main.getByRole("link", { name: "投放策略", exact: true }),
    ).toBeVisible()
    await expect(
      main.getByText(/请联系租户管理员完成 TikTok 授权/),
    ).toBeVisible()
  })

test("MCP new authorization is paused while official API stays available", async ({
  page,
}) => {
  // 官方 MCP 通道退役中：即使 MCP 已配置，也不能发起新授权。
  const { requests } = await accountBoundary(page, {
    role: "tenant_admin",
    mcpReady: true,
    apiReady: true,
  })
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await page.getByRole("button", { name: "新增连接", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "官方 MCP", exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "官方 API", exact: true }),
  ).toBeEnabled()
  await expect(page.getByText(/官方 MCP 通道正在退役/)).toBeVisible()
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("BC authorization setting persists and keeps unknown MCP permissions separate", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    mcpConnection: true,
  })
  await page.goto(`/tenants/${A}/accounts`)
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toContainText(
    "可搭建",
  )
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await page.getByRole("button", { name: "更换授权", exact: true }).click()
  await page.getByRole("radio", { name: /MCP 业务连接/ }).click()
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await page.getByRole("tab", { name: "广告账户", exact: true }).click()
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toContainText(
    "权限待核实",
  )
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toContainText(
    "不可搭建",
  )
  const reads = requests.filter((request) => request.path.endsWith("/accounts"))
  expect(reads[0]?.query.get("connection_id")).toBe(CONN)
  expect(reads.at(-1)?.query.get("connection_id")).toBe(
    "77777777-7777-4777-8777-000000000003",
  )
  const writes = requests.filter((request) => request.method !== "GET")
  expect(writes).toHaveLength(1)
  expect(writes[0]?.path).toBe(
    `/api/tenants/${A}/bcs/${BC1}/default-connection`,
  )
  expect(writes[0]?.body).toEqual({ connection_id: MCP_CONN })
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await page.reload()
  await expect(
    page.getByText("使用授权：MCP 业务连接 · 官方 MCP", { exact: true }),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option", { name: /业务 BC 二/ }).click()
  await page.getByRole("tab", { name: "广告账户", exact: true }).click()
  await expect(page.getByRole("row", { name: /第二BC账户 2 / })).toContainText(
    "可搭建",
  )
})

test("an account directory without a default waits for the BC authorization setting", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    noDefault: true,
    singleBC: true,
  })
  await page.goto(`/tenants/${A}/accounts`)
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await expect(
    page.getByText("使用授权：尚未设置", { exact: true }),
  ).toBeVisible()
  expect(requests.some((request) => request.path.endsWith("/accounts"))).toBe(
    false,
  )
  await page.getByRole("button", { name: "设置使用授权", exact: true }).click()
  await page.locator(`[id="authorization-${CONN}"]`).click()
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await page.getByRole("tab", { name: "广告账户", exact: true }).click()
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toBeVisible()
})

test("connection details direct authorization changes to the unified setting", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await page
    .getByRole("row", { name: new RegExp(CONN) })
    .getByRole("button", { name: "查看详情", exact: true })
    .click()
  const detail = page.getByRole("dialog", { name: "连接详情", exact: true })
  await expect(detail.getByText(/在“授权管理”页上方统一设置/)).toBeVisible()
  await expect(
    detail.getByRole("button", { name: "设为默认执行连接" }),
  ).toHaveCount(0)
  const writes = requests.filter((request) => request.method !== "GET")
  expect(writes).toHaveLength(0)
})

test("MCP candidate binds all accessible BCs only after submission and returns to connections", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    candidateReady: true,
    mcpReady: true,
  })
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
  await expect(sheet.getByText(/2 个新 BC 将自动接入/)).toBeVisible()
  await expect(sheet.getByRole("checkbox")).toHaveCount(0)
  expect(requests.every((request) => request.method === "GET")).toBe(true)
  await expect(
    sheet.getByRole("button", { name: "接入并同步账户", exact: true }),
  ).toBeEnabled()
  await sheet
    .getByRole("button", { name: "接入并同步账户", exact: true })
    .click()
  await expect(sheet).not.toBeVisible()
  const writes = requests.filter((request) => request.method !== "GET")
  expect(writes).toHaveLength(1)
  expect(writes[0]?.path).toBe(
    `/api/tenants/${A}/tiktok/mcp/candidates/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/binding`,
  )
  expect(writes[0]?.body).toEqual({ bc_ids: [BC1, BC2] })
  await expect(page).not.toHaveURL(/attempt_id=|mcp_authorization=/)
  await page.reload()
  await expect(page.getByRole("heading", { name: "账户与授权" })).toBeVisible()
  await expect(sheet).not.toBeVisible()
})

for (const result of ["CANCELLED", "FAILED"] as const)
  test(`MCP ${result} preserves existing connections without a new write`, async ({
    page,
  }) => {
    const { requests } = await accountBoundary(page, { mcpReady: true })
    await page.goto(
      `/tenants/${A}/accounts?tab=connections&mcp_authorization=${result}`,
    )
    await expect(
      page.getByText(
        result === "CANCELLED" ? "已取消 MCP 授权" : "MCP 授权未完成",
        { exact: true },
      ),
    ).toBeVisible()
    await expect(
      page.getByRole("row", { name: new RegExp(CONN) }),
    ).toContainText("可用")
    expect(requests.every((request) => request.method === "GET")).toBe(true)
  })

for (const role of ["operator", "viewer"] as const)
  test(`MCP ${role} cannot manage or load authorization candidates`, async ({
    page,
  }) => {
    const { requests } = await accountBoundary(page, {
      role,
      candidateReady: true,
      mcpReady: true,
    })
    await page.goto(
      `/tenants/${A}/accounts?tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
    )
    await expect(
      page.getByRole("row", { name: new RegExp(CONN) }),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "新增连接", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("button", { name: "选择 BC", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("dialog", { name: "选择授权 BC", exact: true }),
    ).toHaveCount(0)
    expect(
      requests.some((request) => request.path.includes("/mcp/candidates/")),
    ).toBe(false)
  })

test("MCP candidate is cleared when tenant navigation changes", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    candidateReady: true,
    mcpReady: true,
  })
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
  await expect(
    sheet.getByText(`业务 BC 一 · ${BC1}`, { exact: true }),
  ).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(sheet).not.toBeVisible()
  await chooseTenant(page, "租户乙")
  await expect(page).toHaveURL(new RegExp(`/tenants/${B}/accounts`))
  await expect(
    page.getByRole("dialog", { name: "选择授权 BC", exact: true }),
  ).toHaveCount(0)
  expect(
    requests.filter((request) =>
      request.path.includes(`/tenants/${B}/tiktok/mcp/candidates/`),
    ),
  ).toHaveLength(0)
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

for (const terminal of ["PUBLISHED", "OUTCOME_UNKNOWN"] as const) {
  test(`MCP credential refresh polls to ${terminal} then stops`, async ({
    page,
  }) => {
    await accountBoundary(page, { mcpReady: true, singleBC: true })
    let reads = 0
    let completed = false
    await page.route("**/api/tenants/*/tiktok/connections*", (route) => {
      if (!new URL(route.request().url()).searchParams.has("bc_id")) reads++
      return route.fulfill({
        json: {
          items: [
            {
              id: CONN,
              tenant_id: A,
              kind: "OFFICIAL_MCP",
              display_name: "刷新中的连接",
              status:
                completed && terminal === "OUTCOME_UNKNOWN"
                  ? "REAUTH_REQUIRED"
                  : "ACTIVE",
              discovery_status: "COMPLETE",
              refresh_status: completed ? terminal : "CLAIMED",
              binding_count: 1,
              is_default: true,
              read_authorized: null,
              build_authorized: null,
              upload_authorized: null,
            },
          ],
          next_cursor: null,
        },
      })
    })
    await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC1}`)
    await page
      .getByRole("row", { name: /刷新中的连接/ })
      .getByRole("button", { name: "查看详情", exact: true })
      .click()
    const dialog = page.getByRole("dialog", { name: "连接详情", exact: true })
    await expect(
      dialog.getByText("正在刷新，任务暂候", { exact: true }),
    ).toBeVisible()
    completed = true
    await expect(
      dialog.getByText(
        terminal === "PUBLISHED"
          ? "刷新完成"
          : "刷新结果未知，需重新授权后继续",
        { exact: true },
      ),
    ).toBeVisible({ timeout: 12000 })
    const terminalReads = reads
    await page.waitForTimeout(6000)
    expect(reads).toBe(terminalReads)
  })
}

test("authorization setting removes retained names after a later page denies access", async ({
  page,
}) => {
  await accountBoundary(page, {
    singleBC: true,
    mcpReady: true,
    mcpConnection: true,
  })
  let forbidden = false
  await page.route("**/api/tenants/*/tiktok/connections*", (route) =>
    forbidden
      ? route.fulfill({ status: 403, json: { code: "action_forbidden" } })
      : route.fallback(),
  )
  await page.goto(`/tenants/${A}/accounts?bc_id=${BC1}`)
  await expect(page.getByRole("row", { name: /第一BC账户 2 / })).toBeVisible()
  await page.getByRole("tab", { name: "授权管理", exact: true }).click()
  await page.getByRole("button", { name: "更换授权", exact: true }).click()
  forbidden = true
  await page.getByRole("button", { name: "加载更多授权", exact: true }).click()
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("combobox", { name: "查看账户的连接", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "加载更多授权", exact: true }),
  ).toHaveCount(0)
  await expect(page.getByRole("radio", { name: /MCP 业务连接/ })).toHaveCount(0)
  await expect(page.getByRole("dialog")).toHaveCount(0)
})

test("MCP single accessible BC is selected automatically without binding until submitted", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    mcpReady: true,
    candidateBCs: [{ bc_id: BC1, name: "唯一 BC" }],
  })
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
  await expect(
    sheet.getByText(`唯一 BC · ${BC1}`, { exact: true }),
  ).toBeVisible()
  await expect(sheet.getByText(/1 个新 BC 将自动接入/)).toBeVisible()
  await expect(sheet.getByRole("checkbox")).toHaveCount(0)
  expect(requests.every((request) => request.method === "GET")).toBe(true)
  await expect(
    sheet.getByRole("button", { name: "接入并同步账户" }),
  ).toBeEnabled()
  await sheet.getByRole("button", { name: "接入并同步账户" }).click()
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({
    bc_ids: [BC1],
  })
})

test("MCP automatic binding covers the complete directory across displayed pages", async ({
  page,
}) => {
  const candidateBCs = Array.from({ length: 101 }, (_, i) => ({
    bc_id: `bc-${i + 1}`,
    name: `跨页 BC ${i + 1}`,
  }))
  const { requests } = await accountBoundary(page, {
    mcpReady: true,
    candidateBCs,
  })
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
  await expect(sheet.getByText(/101 个新 BC 将自动接入/)).toBeVisible()
  await expect(
    sheet.getByText("跨页 BC 1 · bc-1", { exact: true }),
  ).toBeVisible()
  await sheet.getByRole("button", { name: "下一页 BC" }).click()
  await expect(sheet.getByText("共 101 条 · 第 2 / 3 页")).toBeVisible()
  await expect(
    sheet.getByText("跨页 BC 51 · bc-51", { exact: true }),
  ).toBeVisible()
  await sheet.getByRole("button", { name: "上一页 BC" }).click()
  await expect(
    sheet.getByText("跨页 BC 1 · bc-1", { exact: true }),
  ).toBeVisible()
  expect(requests.every((request) => request.method === "GET")).toBe(true)
  await sheet.getByRole("button", { name: "接入并同步账户" }).click()
  const writes = requests.filter((request) => request.method !== "GET")
  expect(writes).toHaveLength(1)
  expect(writes[0]?.body).toEqual({
    bc_ids: candidateBCs.map((item) => item.bc_id),
  })
  expect(
    new Set(
      requests
        .filter(
          (request) =>
            request.path.includes("/candidates/") && request.method === "GET",
        )
        .map((request) => request.query.get("page")),
    ),
  ).toEqual(new Set(["1", "2", "3"]))
})

test("existing MCP connection cannot reauthorize or add BCs but can still be disabled", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    mcpReady: true,
    mcpConnection: true,
  })
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC1}`)
  const row = page.getByRole("row", { name: new RegExp(MCP_CONN) })
  await expect(
    row.getByRole("button", { name: "添加 BC", exact: true }),
  ).toBeDisabled()
  await expect(
    row.getByRole("button", { name: "重新授权", exact: true }),
  ).toBeDisabled()
  await expect(
    row.getByRole("button", { name: "停用", exact: true }),
  ).toBeEnabled()
  await expect(
    row.getByRole("button", { name: "查看详情", exact: true }),
  ).toBeEnabled()
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("MCP per BC retry and unbind preserve the other BC and its default", async ({
  page,
}) => {
  const bindingBCs: BindingBCFixture[] = [
    {
      bc_id: BC1,
      name: "失败待重试 BC",
      ownership_conflict: false,
      binding_status: "ERROR",
      discovery_status: "ERROR",
      error_code: "discovery_failed",
    },
    {
      bc_id: BC2,
      name: "保留默认 BC",
      ownership_conflict: false,
      binding_status: "ACTIVE",
      discovery_status: "COMPLETE",
      is_default: true,
    },
  ]
  const { requests } = await accountBoundary(page, {
    mcpReady: true,
    mcpConnection: true,
    bindingBCs,
  })
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC2}`)
  await page
    .getByRole("row", { name: new RegExp(MCP_CONN) })
    .getByRole("button", { name: "查看详情", exact: true })
    .click()
  const sheet = page.getByRole("dialog", { name: "连接详情", exact: true })
  const failed = sheet.getByRole("row", { name: /失败待重试 BC/ })
  const kept = sheet.getByRole("row", { name: /保留默认 BC/ })
  await failed.scrollIntoViewIfNeeded()
  await page.screenshot({
    path: test.info().outputPath("mcp-binding-details.png"),
    animations: "disabled",
  })
  await failed.getByRole("button", { name: "重试同步" }).click()
  await expect(failed.getByText("已接入", { exact: true })).toBeVisible()
  await failed.getByRole("button", { name: "解绑 BC", exact: true }).click()
  await expect(
    sheet.getByText("解绑 失败待重试 BC", { exact: true }),
  ).toBeVisible()
  expect(
    requests.filter((request) => request.method === "DELETE"),
  ).toHaveLength(0)
  await sheet.getByRole("button", { name: "确认解绑 BC", exact: true }).click()
  await expect(failed.getByText("已解绑", { exact: true })).toBeVisible()
  await expect(kept.getByText("已接入", { exact: true })).toBeVisible()
  await expect(kept.getByText("当前使用", { exact: true })).toBeVisible()
  expect(
    requests
      .filter((request) => request.method !== "GET")
      .map((request) => [request.method, request.path]),
  ).toEqual([
    [
      "POST",
      `/api/tenants/${A}/tiktok/mcp/connections/${MCP_CONN}/bcs/${BC1}/sync`,
    ],
    [
      "DELETE",
      `/api/tenants/${A}/tiktok/mcp/connections/${MCP_CONN}/bcs/${BC1}`,
    ],
  ])
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
})

test("MCP shared connection polls every pending BC and refreshes directories after each completion", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, {
    mcpReady: true,
    singleBC: true,
  })
  let pending = 2
  let connectionReads = 0
  await page.route("**/api/tenants/*/tiktok/connections?*", (route) => {
    connectionReads++
    return route.fulfill({
      json: {
        items: [
          {
            id: MCP_CONN,
            tenant_id: A,
            kind: "OFFICIAL_MCP",
            status: "ACTIVE",
            binding_count: 2,
            pending_binding_count: pending,
            discovery_status: "COMPLETE",
            last_discovery: "2026-09-12T10:00:00Z",
          },
        ],
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC1}`)
  await expect(
    page.getByText("2 个 BC 正在同步", { exact: true }),
  ).toBeVisible()
  const firstBCReads = requests.filter((request) =>
    request.path.endsWith("/bcs"),
  ).length
  pending = 1
  await expect(page.getByText("1 个 BC 正在同步", { exact: true })).toBeVisible(
    { timeout: 12000 },
  )
  await expect
    .poll(
      () => requests.filter((request) => request.path.endsWith("/bcs")).length,
    )
    .toBeGreaterThan(firstBCReads)
  const secondBCReads = requests.filter((request) =>
    request.path.endsWith("/bcs"),
  ).length
  pending = 0
  await expect(page.getByText("1 个 BC 正在同步", { exact: true })).toHaveCount(
    0,
    { timeout: 12000 },
  )
  await expect
    .poll(
      () => requests.filter((request) => request.path.endsWith("/bcs")).length,
    )
    .toBeGreaterThan(secondBCReads)
  const terminalReads = connectionReads
  await page.waitForTimeout(5500)
  expect(connectionReads).toBe(terminalReads)
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

test("MCP incomplete BC directory blocks all selection and binding", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { mcpReady: true })
  await page.route("**/api/tenants/*/tiktok/mcp/candidates/*/bcs?*", (route) =>
    route.fulfill({
      json: {
        items: [],
        total: 2,
        page: 1,
        page_size: 50,
      },
    }),
  )
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
  await expect(
    sheet.getByText("BC 目录未完整返回，请刷新后重试。", { exact: true }),
  ).toBeVisible()
  await expect(
    sheet.getByRole("button", { name: "接入并同步账户" }),
  ).toBeDisabled()
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

for (const total of [1001, 5000]) {
  test(`MCP ${total} accessible BCs are bound completely within the 5000 BC limit`, async ({
    page,
  }) => {
    const candidateBCs = Array.from({ length: total }, (_, i) => ({
      bc_id: `limit-bc-${i + 1}`,
      name: `目录 BC ${i + 1}`,
    }))
    const { requests } = await accountBoundary(page, {
      mcpReady: true,
      candidateBCs,
    })
    await page.goto(
      `/tenants/${A}/accounts?tab=connections&bc_id=${BC1}&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
    )
    const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
    await expect(
      sheet.getByText(new RegExp(`共 ${total} 个可访问 BC`)),
    ).toBeVisible({ timeout: 15000 })
    await expect(sheet.getByRole("checkbox")).toHaveCount(0)
    expect(requests.every((request) => request.method === "GET")).toBe(true)
    await expect(
      sheet.getByRole("button", { name: "接入并同步账户" }),
    ).toBeEnabled()
    await sheet.getByRole("button", { name: "接入并同步账户" }).click()
    const writes = requests.filter((request) => request.method !== "GET")
    expect(writes).toHaveLength(1)
    expect(writes[0]?.body).toEqual({
      bc_ids: candidateBCs.map((item) => item.bc_id),
    })
    expect(
      new Set(
        requests
          .filter(
            (request) =>
              request.path.includes("/candidates/") && request.method === "GET",
          )
          .map((request) => Number(request.query.get("page"))),
      ).size,
    ).toBe(Math.ceil(total / 50))
  })
}

test("MCP provider failure callback displays its outcome without restarting OAuth", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { mcpReady: true })
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&bc_id=${BC1}&mcp_authorization=mcp_candidate_superseded&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  await expect(page.getByText("MCP 授权未完成", { exact: true })).toBeVisible()
  await expect(
    page.getByText("原有可用连接保留，请查看连接状态后重新授权。", {
      exact: true,
    }),
  ).toBeVisible()
  expect(
    requests.some(
      (request) =>
        request.path.includes("/mcp/candidates/") ||
        request.path.endsWith("/authorizations"),
    ),
  ).toBe(false)
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

// 完整目录上限与后端一致，不能截断后自动提交部分授权范围。
test("MCP more than 5000 accessible BCs blocks binding without writes", async ({
  page,
}) => {
  const { requests } = await accountBoundary(page, { mcpReady: true })
  await page.route("**/api/tenants/*/tiktok/mcp/candidates/*/bcs?*", (route) =>
    route.fulfill({ json: { items: [], total: 5001, page: 1, page_size: 50 } }),
  )
  await page.goto(
    `/tenants/${A}/accounts?tab=connections&bc_id=${BC1}&mcp_authorization=CANDIDATE_READY&attempt_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`,
  )
  const sheet = page.getByRole("dialog", { name: "选择授权 BC", exact: true })
  await expect(
    sheet.getByText(
      "可访问 BC 超过 5000 个，目录无法完整读取，请联系管理员。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    sheet.getByRole("button", { name: "接入并同步账户" }),
  ).toBeDisabled()
  expect(requests.every((request) => request.method === "GET")).toBe(true)
})

test("material production only issues a login link on click and navigates", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "operator",
  })
  await page.goto(`/tenants/${A}/accounts`)
  const entry = page.getByRole("button", { name: "素材制作", exact: true })
  await expect(entry).toBeVisible()
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toHaveLength(0)
  await entry.click()
  await expect(page).toHaveURL(
    "https://manjumt.gzjunbo.net/api/login-links/consume?token=browser-fixture",
  )
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toEqual([
    expect.objectContaining({
      method: "POST",
      path: `/api/tenants/${A}/material-production/login-link`,
      body: {},
    }),
  ])
})

test("material production launcher opens the named tenant's workbench", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "operator",
  })
  await page.goto("/material-production?tenant=%E7%A7%9F%E6%88%B7%E7%94%B2")
  await expect(page).toHaveURL(
    "https://manjumt.gzjunbo.net/api/login-links/consume?token=browser-fixture",
  )
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toEqual([
    expect.objectContaining({
      method: "POST",
      path: `/api/tenants/${A}/material-production/login-link`,
    }),
  ])
})

test("material production launcher refuses tenants the account cannot use", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "operator",
  })
  await page.goto("/material-production?tenant=junbo")
  await expect(
    page.getByText("当前账号不能打开 junbo 的素材制作"),
  ).toBeVisible()
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toEqual([])
})

test("viewer has no material production entry", async ({ page }) => {
  await boundary(page, { platform: false, role: "viewer" })
  await page.goto(`/tenants/${A}/accounts`)
  await expect(
    page.getByRole("heading", { name: "账户与授权", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "素材制作", exact: true }),
  ).toHaveCount(0)
})

test("material login pending prevents duplicate clicks and cannot cross tenant switches", async ({
  page,
}) => {
  let release!: () => void
  const delayedLogin = new Promise<void>((resolve) => {
    release = resolve
  })
  const { requests } = await boundary(page, { delayedLogin })
  await page.goto(`/tenants/${A}/accounts`)
  const entry = page.getByRole("button", { name: "素材制作", exact: true })
  await entry.click()
  await expect(entry).toBeDisabled()
  await expect(entry).toHaveAttribute("aria-busy", "true")
  await expect(
    page.getByText("正在登录素材工作台…", { exact: true }),
  ).toBeVisible()
  await expect
    .poll(() => requests.filter((r) => r.path.endsWith("/login-link")).length)
    .toBe(1)
  const settled = page.waitForEvent("requestfailed", {
    predicate: (request) =>
      request.url().endsWith("/material-production/login-link"),
  })
  await chooseTenant(page, "租户乙")
  await expect(page).toHaveURL((url) => url.pathname.includes(`/tenants/${B}/`))
  release()
  await settled
  await expect(entry).toBeEnabled()
  await expect(page).toHaveURL((url) => url.pathname.includes(`/tenants/${B}/`))
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toHaveLength(1)
})

test("@dev-server material login keeps feedback while the destination is loading", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.route(
    "https://manjumt.gzjunbo.net/api/login-links/consume?**",
    (route) =>
      route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: "素材工作台",
      }),
  )
  await page.goto(`/tenants/${A}/accounts`)
  // 只延迟整页导航运输；真实登录逻辑继续返回并执行 finally，旧文档仍可观察。
  await page.evaluate(async () => {
    const modulePath = "/src/lib/browser-navigation.ts"
    const { setAssignLocation } = await import(modulePath)
    setAssignLocation((href: string) => {
      document.body.dataset.materialNavigationHref = href
    })
  })
  const entry = page.getByRole("button", { name: "素材制作", exact: true })
  await entry.click()
  await expect(page.locator("body")).toHaveAttribute(
    "data-material-navigation-href",
    "https://manjumt.gzjunbo.net/api/login-links/consume?token=browser-fixture",
  )
  await expect(entry).toBeDisabled()
  await expect(entry).toHaveAttribute("aria-busy", "true")
  await expect(
    page.getByText("正在打开素材工作台…", { exact: true }),
  ).toBeVisible()
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toHaveLength(1)
  await page.evaluate(() => {
    const href = document.body.dataset.materialNavigationHref!
    setTimeout(() => window.location.assign(href), 0)
  })
  await expect(page).toHaveURL(/manjumt\.gzjunbo\.net/)
})

test("material login times out with feedback and ignores a late response", async ({
  page,
}) => {
  let release!: () => void
  const delayedLogin = new Promise<void>((resolve) => {
    release = resolve
  })
  const { requests } = await boundary(page, { delayedLogin })
  await page.goto(`/tenants/${A}/accounts`)
  await page.clock.install()
  const entry = page.getByRole("button", { name: "素材制作", exact: true })
  await entry.click()
  await expect
    .poll(() => requests.filter((r) => r.path.endsWith("/login-link")).length)
    .toBe(1)
  const aborted = page.waitForEvent("requestfailed", {
    predicate: (request) =>
      request.url().endsWith("/material-production/login-link"),
  })
  await page.clock.fastForward(25_001)
  await aborted
  await expect(
    page.getByText("登录素材工作台超时，请稍后重试", { exact: true }),
  ).toBeVisible()
  await expect(entry).toBeEnabled()
  release()
  await expect(page).toHaveURL(
    (url) => url.pathname === `/tenants/${A}/accounts`,
  )
})

test("material login failure stays in workspace and can retry", async ({
  page,
}) => {
  const { requests } = await boundary(page, { productionFailure: true })
  await page.goto(`/tenants/${A}/accounts`)
  await page.getByRole("button", { name: "素材制作", exact: true }).click()
  await expect(
    page.getByText("素材制作尚未开通，请联系平台管理员"),
  ).toBeVisible()
  await expect(page).toHaveURL(
    (url) => url.pathname === `/tenants/${A}/accounts`,
  )
  await expect(
    page.getByRole("button", { name: "素材制作", exact: true }),
  ).toBeEnabled()
  expect(requests.filter((r) => r.path.endsWith("/login-link"))).toHaveLength(1)
})

test("platform can retry failed material provisioning in tenant details", async ({
  page,
}) => {
  const { requests } = await boundary(page, { productionStatus: "failed" })
  await page.goto("/platform/tenants")
  await page
    .getByRole("row", { name: new RegExp(A) })
    .getByRole("button", { name: "查看", exact: true })
    .click()
  const status = page.getByRole("region", { name: "素材制作开通状态" })
  await expect(status.getByText("开通失败", { exact: true })).toBeVisible()
  await status.getByRole("button", { name: "重试开通", exact: true }).click()
  await expect(status.getByText("开通中", { exact: true })).toBeVisible()
  expect(
    requests.filter((r) => r.path.endsWith("/material-production/retry")),
  ).toHaveLength(1)
  expect(requests.filter((r) => r.method === "PATCH")).toHaveLength(0)
})

test("工作台菜单恢复标签查询条件，切换 BC 清空旧标签", async ({ page }) => {
  await accountBoundary(page)
  await page.goto(`/tenants/${A}/accounts?tab=connections&bc_id=${BC1}`)
  await expect(
    page.getByRole("tab", { name: "授权管理", exact: true }),
  ).toHaveAttribute("data-state", "active")
  await page
    .getByRole("navigation", { name: "设置", exact: true })
    .getByRole("link", { name: "成员管理", exact: true })
    .click()
  await expect(
    page.getByRole("tablist", { name: "已打开的页面" }).getByRole("tab"),
  ).toHaveCount(2)
  await page
    .getByRole("navigation", { name: "设置", exact: true })
    .getByRole("link", { name: "账户与授权", exact: true })
    .click()
  await expect(page).toHaveURL(
    new RegExp(`tab=connections.*bc_id=${BC1}|bc_id=${BC1}.*tab=connections`),
  )
  await expect(
    page.getByRole("tab", { name: "授权管理", exact: true }),
  ).toHaveAttribute("data-state", "active")
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option", { name: /业务 BC 二/ }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
  await expect(
    page.getByRole("tablist", { name: "已打开的页面" }).getByRole("tab"),
  ).toHaveCount(1)
  await expect(
    page.getByRole("tab", { name: "成员管理", exact: true }),
  ).toHaveCount(0)
})
