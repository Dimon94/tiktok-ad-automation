import { expect, type Page, test } from "@playwright/test"
import type {
  StrategyConfig_Output,
  StrategyPublic,
  VersionPublic,
} from "../src/client"
import { demoStructureEstimate } from "../src/demo/fixtures/structure-estimate"

const A = "11111111-1111-4111-8111-111111111111",
  B = "22222222-2222-4222-8222-222222222222",
  S = "294999ce-f767-45c6-8da0-d2cbeaf71963",
  V = "33333333-3333-4333-8333-333333333333",
  U = "44444444-4444-4444-8444-444444444444",
  POOL = "02a4e656-a330-40dc-864c-26e81961f3ca"
const config: StrategyConfig_Output = {
  budget: "100.00",
  currency: "USD",
  budget_strategy: "SERIES",
  bid_strategy: "TARGET_ROAS",
  creation_status: "ENABLE",
  target_roas: "1.08",
  group_generation_mode: "BY_MATERIAL",
  group_count: null,
  group_material_allocation: null,
  max_materials_per_group: 10,
  ad_generation_mode: "BY_MATERIAL",
  ads_per_group: null,
  ad_material_allocation: null,
  max_materials_per_ad: 1,
  creative_count: 2,
  copy_pool_version: POOL,
  cta_option_ids: ["official-existing-id"],
  campaign_name_template: "{provider_drama}-{drama_id}",
}
const original: StrategyPublic = {
  id: S,
  name: "租户策略",
  active: true,
  latest_version: 1,
  version_id: V,
  config,
  created_by: U,
  created_at: "2026-09-08T08:00:00Z",
}
async function boundary(
  page: Page,
  options: {
    role?: "operator" | "viewer" | "tenant_admin"
    count?: number
    capacity?: number
    mode?: "conflict" | "unknown" | "unknown404" | "failure" | "rate_limited"
    empty?: boolean
    deny?: boolean
    loggedOut?: boolean
  } = {},
) {
  const requests: {
      path: string
      method: string
      query: URLSearchParams
      body: any
    }[] = [],
    records = options.empty
      ? []
      : Array.from({ length: options.count || 1 }, (_, i) => ({
          ...structuredClone(original),
          id:
            i === 0
              ? S
              : `88888888-8888-4888-8888-${String(i).padStart(12, "0")}`,
          name: i === 0 ? "租户策略" : `分页策略 ${i + 1}`,
        }))
  const versions: VersionPublic[] = [
      {
        id: V,
        strategy_id: S,
        number: 1,
        config: structuredClone(config),
        created_by: U,
        created_at: original.created_at,
        request_id: "55555555-5555-4555-8555-555555555555",
      },
    ],
    saved = new Map<string, VersionPublic>()
  await page.addInitScript((loggedOut) => {
    if (loggedOut && !sessionStorage.getItem("strategy-fixture-initialized")) {
      localStorage.removeItem("access_token")
      sessionStorage.setItem("strategy-fixture-initialized", "true")
    } else if (!loggedOut)
      localStorage.setItem("access_token", "strategy-test-token")
  }, options.loggedOut)
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname,
      method = req.method(),
      query = url.searchParams
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    if (path === "/api/login/access-token")
      return route.fulfill({
        json: { access_token: "strategy-test-token", token_type: "bearer" },
        headers,
      })
    expect(req.headers().authorization).toBe("Bearer strategy-test-token")
    const body = req.postData() ? req.postDataJSON() : undefined
    if (path.endsWith("/strategies/structure-estimate")) {
      try {
        return route.fulfill({ json: demoStructureEstimate(body), headers })
      } catch {
        return route.fulfill({
          status: 422,
          json: { detail: "invalid structure" },
          headers,
        })
      }
    }
    requests.push({ path, method, query, body })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    const pageOf = (items: unknown[]) => {
      const size = Number(query.get("limit") || 50),
        offset = Number(query.get("cursor") || 0)
      return {
        items: items.slice(offset, offset + size),
        next_cursor:
          offset + size < items.length ? String(offset + size) : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: U,
        username: "operator",
        full_name: "策略测试用户",
        is_active: true,
        is_superuser: false,
      })
    if (path === "/api/me/tenants")
      return reply({
        items: [
          {
            id: A,
            name: "策略租户甲",
            active: true,
            role: options.role || "operator",
          },
          {
            id: B,
            name: "策略租户乙",
            active: true,
            role: options.role || "operator",
          },
        ].filter(
          (row) =>
            !query.get("search") ||
            row.id === query.get("search") ||
            row.name.includes(query.get("search")!),
        ),
        next_cursor: null,
      })
    if (path.endsWith("/bcs")) return reply({ items: [], next_cursor: null })
    if (options.deny)
      return reply(
        {
          code: "action_forbidden",
          message: "当前角色无权操作",
          retryable: false,
        },
        403,
      )
    if (path.includes("/copy-pools/"))
      return reply({
        id: POOL,
        name: "通用英文文案池 v1",
        entries: Array.from({ length: options.capacity ?? 100 }, (_, i) => ({
          id: `99999999-9999-4999-8999-${String(i).padStart(12, "0")}`,
          text: `Watch the story continue ${i + 1}.`,
          position: i + 1,
        })),
      })
    if (path.endsWith("/strategies/validate"))
      return reply({ valid: true, errors: [], scene_check_pending: true })
    if (path.includes("/strategy-save-requests/")) {
      const row = saved.get(path.split("/").pop()!)
      return row && options.mode !== "unknown404"
        ? reply(row)
        : reply(
            {
              code: "strategy_not_found",
              message: "尚无该保存记录",
              retryable: false,
            },
            404,
          )
    }
    if (path.includes("/strategy-versions/"))
      return reply(versions.find((row) => path.endsWith(row.id)))
    if (path.endsWith("/strategies") && method === "GET")
      return reply(
        pageOf(
          records.filter(
            (row) =>
              (!query.has("active") ||
                row.active === (query.get("active") === "true")) &&
              (!query.get("query") || row.name.includes(query.get("query")!)),
          ),
        ),
      )
    if (path.endsWith("/versions") && method === "GET")
      return reply(pageOf([...versions].reverse()))
    if (method === "GET")
      return reply(records.find((row) => path.endsWith(row.id)) || original)
    if (method === "PATCH") {
      const row = records.find((row) => path.endsWith(row.id))!
      if (body.active !== undefined) row.active = body.active
      if (body.name !== undefined) row.name = body.name.trim()
      return reply(row)
    }
    if (method === "POST") {
      if (options.mode === "failure")
        return reply(
          {
            code: "copy_pool_exhausted",
            message: "文案数量不足",
            retryable: false,
          },
          422,
        )
      if (options.mode === "conflict") {
        records[0].latest_version = 2
        records[0].config = { ...config, budget: "120.00" }
        return reply(
          { code: "version_conflict", message: "版本已变化", retryable: false },
          409,
        )
      }
      const create = path.endsWith("/strategies"),
        strategyId = create ? "66666666-6666-4666-8666-666666666666" : S
      const row: VersionPublic = {
        id: "77777777-7777-4777-8777-777777777777",
        strategy_id: strategyId,
        number: create ? 1 : 2,
        config: body.config,
        created_by: U,
        created_at: "2026-09-09T08:00:00Z",
        request_id: body.request_id,
      }
      versions.push(row)
      saved.set(body.request_id, row)
      if (create)
        records.push({
          ...original,
          id: strategyId,
          name: body.name,
          config: body.config,
          version_id: row.id,
          latest_version: row.number,
        })
      else
        Object.assign(records[0], {
          ...(body.name === undefined ? {} : { name: body.name.trim() }),
          version_id: row.id,
          config: body.config,
          latest_version: 2,
        })
      if (options.mode === "rate_limited")
        return reply(
          {
            code: "request_rate_limited",
            message: "保存请求正在处理中",
            retryable: true,
          },
          429,
        )
      if (options.mode === "unknown" || options.mode === "unknown404")
        return route.abort("timedout")
      return reply(row, 201)
    }
    return reply({ message: `Unexpected fixture ${method} ${path}` }, 404)
  })
  return { requests, records, versions, saved }
}
const editUrl = `/tenants/${A}/strategies/${S}`
test("23 条素材的创意数量 2→3 只改变 Ad 数量，不倍增预算", async ({ page }) => {
  await boundary(page)
  await page.goto(editUrl)
  const example = page.getByRole("region", { name: "结构与预算示例" })
  await expect(example).toContainText("最终 46 个广告")
  await page.getByLabel("每个广告创意数量", { exact: true }).fill("3")
  await expect(example.getByText("3 个广告组", { exact: true })).toBeVisible()
  await expect(example).toContainText("最终 69 个广告")
  await expect(
    example.getByText("USD 100 / 系列 / 天", { exact: true }),
  ).toBeVisible()
  await expect(page.getByLabel("自定义 CTA")).toHaveCount(0)
})
test("复制只读原版本；保存创建新策略使用真实 strategy_id 与版本号", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  await page.getByRole("button", { name: "复制为新策略", exact: true }).click()
  await expect(page.getByLabel("策略名称", { exact: true })).toHaveValue(
    "租户策略 副本",
  )
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  await page.getByRole("button", { name: "创建策略", exact: true }).click()
  await expect(page).toHaveURL(
    /\/strategies\/66666666-6666-4666-8666-666666666666/,
  )
  await expect(page.getByText("已保存 v1", { exact: true })).toBeVisible()
  const writes = requests.filter(
    (r) => r.method === "POST" && !r.path.endsWith("/validate"),
  )
  expect(writes).toHaveLength(1)
  expect(writes[0].body.config.budget).toBe("100.00")
})
test("策略无有效改动时不产生重复版本，金额仅格式变化也不写", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await page.getByLabel("日预算", { exact: true }).fill("100.0")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("修改已有策略名称只更新显示标签，不创建配置版本", async ({ page }) => {
  const { requests, records, versions } = await boundary(page)
  await page.goto(editUrl)

  const name = page.getByLabel("策略名称", { exact: true })
  await expect(name).toBeEditable()
  await name.fill("  新策略名称  ")
  await page.getByRole("button", { name: "保存名称", exact: true }).click()

  await expect(page.getByText("策略名称已更新", { exact: true })).toBeVisible()
  await expect(name).toHaveValue("新策略名称")
  expect(records[0].name).toBe("新策略名称")
  expect(versions).toHaveLength(1)
  const writes = requests.filter((request) => request.method !== "GET")
  expect(writes).toHaveLength(1)
  expect(writes[0]).toMatchObject({
    method: "PATCH",
    body: { name: "新策略名称" },
  })
})

test("名称和配置同时修改时只发送一次原子版本保存", async ({ page }) => {
  const { requests, records, versions } = await boundary(page)
  await page.goto(editUrl)

  await page.getByLabel("策略名称", { exact: true }).fill("新策略名称")
  await page.getByLabel("日预算", { exact: true }).fill("200")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()

  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  expect(records[0].name).toBe("新策略名称")
  expect(versions).toHaveLength(2)
  const writes = requests.filter(
    (request) =>
      request.method !== "GET" &&
      !request.path.endsWith("/strategies/validate"),
  )
  expect(writes).toHaveLength(1)
  expect(writes[0]).toMatchObject({
    method: "POST",
    body: { name: "新策略名称", expected_version: 1 },
  })
})
test("超时保存按 request_id 精确确认，不重复发送版本写入", async ({ page }) => {
  const { requests } = await boundary(page, { mode: "unknown" })
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("101.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const writes = requests.filter(
    (r) => r.method === "POST" && !r.path.endsWith("/validate"),
  )
  expect(writes).toHaveLength(1)
  expect(
    requests.some((r) =>
      r.path.endsWith(`/strategy-save-requests/${writes[0].body.request_id}`),
    ),
  ).toBe(true)
  expect(new URL(page.url()).searchParams.get("version_id")).toBe(
    "77777777-7777-4777-8777-777777777777",
  )
})
for (const kind of ["append", "create"] as const) {
  test(`429 ${kind} 保存保留原 request ledger，不重复写入`, async ({
    page,
  }) => {
    const { requests } = await boundary(page, {
      mode: "rate_limited",
      empty: kind === "create",
    })
    // 模拟 429 后回查暂时不可用，验证 ledger 在结果未确认时持续保留。
    await page.route("**/api/tenants/*/strategy-save-requests/*", (route) =>
      route.fulfill({
        status: 404,
        json: { code: "strategy_not_found", message: "尚无该保存记录" },
      }),
    )
    await page.goto(
      kind === "append" ? editUrl : `/tenants/${A}/strategies/new`,
    )
    if (kind === "create") {
      await page.getByLabel("策略名称", { exact: true }).fill("限流策略")
      await page.getByLabel("日预算", { exact: true }).fill("101.00")
    } else {
      await page.getByLabel("日预算", { exact: true }).fill("101.00")
    }
    await page
      .getByRole("button", {
        name: kind === "append" ? "保存为新版本" : "创建策略",
        exact: true,
      })
      .click()
    await expect(
      page.getByText("保存结果待确认", { exact: true }),
    ).toBeVisible()
    const write = requests.find(
      (r) => r.method === "POST" && !r.path.endsWith("/validate"),
    )!
    expect(write.body.request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(
      await page.evaluate(
        ({ tenantId, strategyId }) =>
          sessionStorage.getItem(
            `strategy-save-pending:${tenantId}:${strategyId}`,
          ),
        { tenantId: A, strategyId: kind === "append" ? S : "new" },
      ),
    ).toBe(write.body.request_id)
    await page.reload()
    await expect(
      page.getByText("保存结果待确认", { exact: true }),
    ).toBeVisible()
    expect(
      await page.evaluate(
        ({ tenantId, strategyId }) =>
          sessionStorage.getItem(
            `strategy-save-pending:${tenantId}:${strategyId}`,
          ),
        { tenantId: A, strategyId: kind === "append" ? S : "new" },
      ),
    ).toBe(write.body.request_id)
    expect(
      requests.filter(
        (r) => r.method === "POST" && !r.path.endsWith("/validate"),
      ),
    ).toHaveLength(1)
  })
}
test("未知保存回查404不视为未保存，刷新后仍只回查同一请求", async ({
  page,
}) => {
  const { requests } = await boundary(page, { mode: "unknown404" })
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("101.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("保存结果待确认", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  const write = requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/versions"),
  )!
  await page.getByRole("button", { name: "确认保存结果", exact: true }).click()
  await expect(page.getByText(/暂未确认这次保存记录/)).toBeVisible()
  page.once("dialog", (dialog) => dialog.accept())
  await page.reload()
  await expect(page.getByText("保存结果待确认", { exact: true })).toBeVisible()
  expect(
    requests.filter((r) => r.method === "POST" && r.path.endsWith("/versions")),
  ).toHaveLength(1)
  expect(
    requests
      .filter((r) => r.path.includes("/strategy-save-requests/"))
      .every((r) => r.path.endsWith(write.body.request_id)),
  ).toBe(true)
})
test("409保留输入并显示服务器差异；不会自动变更版本基线", async ({ page }) => {
  const { requests } = await boundary(page, { mode: "conflict" })
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("130.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(
    page.getByText("服务器当前为 v2，本地输入已保留。"),
  ).toBeVisible()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("130.00")
  await page.getByRole("button", { name: "查看服务器差异" }).click()
  await expect(page.getByRole("dialog")).toContainText("服务器：120.00")
  await expect(page.getByRole("dialog")).toContainText("本地：130.00")
  expect(
    requests.filter((r) => r.method === "POST" && r.path.endsWith("/versions")),
  ).toHaveLength(1)
})
test("98条有效文案阻止99创意，非法变量定位字段；保留已有CTA ID", async ({
  page,
}) => {
  const { requests } = await boundary(page, { capacity: 98 })
  await page.goto(editUrl)
  await page.getByLabel("每个广告创意数量", { exact: true }).fill("99")
  await expect(
    page.getByLabel("每个广告创意数量", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await page.getByLabel("每个广告创意数量", { exact: true }).fill("3")
  await page
    .getByLabel("广告名称格式", { exact: true })
    .fill("[版权方＋剧名]-[剧目 ID]-[错误字段]")
  await expect(
    page.getByLabel("广告名称格式", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true")
  await expect(
    page.getByText("已有 ID：official-existing-id", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("CTA 候选尚未就绪，待场景核实。", { exact: true }),
  ).toBeVisible()
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("预算大数十进制原文保存，结构和命名示例不进入配置", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("9007199254740993.12")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const write = requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/versions"),
  )!
  expect(write.body.config.budget).toBe("9007199254740993.12")
  expect(write.body.config.target_roas).toBe("1.08")
  expect(write.body.config.creation_status).toBe("ENABLE")
  expect(write.body.config.cta_option_ids).toEqual(["official-existing-id"])
  expect(Object.keys(write.body.config).sort()).toEqual(
    [...Object.keys(config), "targeting"].sort(),
  )
  expect(write.body.expected_version).toBe(1)
})
test("策略卡片在2304px展开和收起侧栏时保留留白与固定短列", async ({ page }) => {
  await page.setViewportSize({ width: 2304, height: 1080 })
  await boundary(page, { count: 55 })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  const section = page.locator('[data-presentation="strategy-list"]')
  const card = section.locator('[data-slot="card"]')
  const table = section.locator('[data-slot="table-container"]')
  const sidebar = page.locator('[data-slot="sidebar"]')
  await expect(sidebar).toHaveAttribute("data-state", "expanded")
  let expandedWidth = 0
  let expandedColumns: number[] = []
  for (const state of ["expanded", "collapsed"] as const) {
    if (state === "collapsed") {
      await page.getByRole("button", { name: "切换导航" }).click()
      await expect(sidebar).toHaveAttribute("data-state", state)
    }
    await expect
      .poll(async () =>
        card.evaluate((el) => {
          const rect = el.getBoundingClientRect()
          const panel = el
            .closest('[data-slot="sidebar-inset"]')!
            .getBoundingClientRect()
          return Math.max(
            Math.abs(rect.left - panel.left - 20),
            Math.abs(panel.right - rect.right - 20),
          )
        }),
      )
      .toBeLessThanOrEqual(1)
    const innerGap = await table.evaluate((el) => {
      const card = el.closest('[data-slot="card"]')!
      const edge = el.parentElement!.getBoundingClientRect()
      const bounds = card.getBoundingClientRect()
      const style = getComputedStyle(card)
      return [
        edge.left - bounds.left - parseFloat(style.borderLeftWidth),
        bounds.right - edge.right - parseFloat(style.borderRightWidth),
      ]
    })
    for (const gap of innerGap) expect(gap).toBeCloseTo(24, 0)
    if (state === "collapsed") {
      await expect
        .poll(async () => (await table.boundingBox())!.width)
        .toBeGreaterThan(expandedWidth + 150)
      await expect
        .poll(
          async () =>
            (await page
              .locator('[data-slot="sidebar-container"]')
              .boundingBox())!.width,
        )
        .toBe(48)
    }
    const widths = await section
      .locator("thead th")
      .evaluateAll((cells) =>
        cells.map((cell) => cell.getBoundingClientRect().width),
      )
    expect(widths[0]).toBeGreaterThanOrEqual(220)
    if (state === "expanded") {
      expandedWidth = (await table.boundingBox())!.width
      expect(expandedWidth).toBeGreaterThan(1536)
      expandedColumns = widths
    } else {
      for (let i = 1; i < widths.length; i++)
        expect(widths[i]).toBeCloseTo(expandedColumns[i], 0)
      expect(widths[0]).toBeGreaterThan(expandedColumns[0] + 150)
    }
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(2304)
  }
})

for (const width of [390, 1024, 1440]) {
  test(`策略卡片在${width}px保留内距、横滚与搜索分页`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await boundary(page, { count: 55 })
    await page.goto(`/tenants/${A}/strategies`)
    await expect(page.locator("tbody tr")).toHaveCount(50)
    const section = page.locator('[data-presentation="strategy-list"]')
    const card = section.locator('[data-slot="card"]')
    const table = section.locator('[data-slot="table-container"]')
    const toolbar = card.locator("form")
    const next = page.getByRole("button", { name: "下一页", exact: true })
    const cardBox = (await card.boundingBox())!
    const tableBox = (await table.boundingBox())!
    const toolbarBox = (await toolbar.boundingBox())!
    const nextBox = (await next.boundingBox())!
    expect(
      tableBox.y - (toolbarBox.y + toolbarBox.height),
    ).toBeGreaterThanOrEqual(16)
    expect(nextBox.y - (tableBox.y + tableBox.height)).toBeGreaterThanOrEqual(
      16,
    )
    expect(toolbarBox.x - cardBox.x - 1).toBeCloseTo(width < 1024 ? 16 : 24, 0)
    const enclosures = await table.evaluate((el) => {
      const bordered: Element[] = []
      for (
        let node: Element | null = el;
        node && node.id !== "workspace-main";
        node = node.parentElement
      ) {
        const style = getComputedStyle(node)
        if (
          [
            style.borderTopWidth,
            style.borderBottomWidth,
            style.borderLeftWidth,
            style.borderRightWidth,
          ].every((value) => parseFloat(value) > 0)
        )
          bordered.push(node)
      }
      const inner = bordered[0].getBoundingClientRect()
      const outer = bordered[1].getBoundingClientRect()
      const border = getComputedStyle(bordered[1])
      return {
        count: bordered.length,
        cardContainsToolbar: bordered[1].querySelector("form") !== null,
        tableContainsToolbar: bordered[0].querySelector("form") !== null,
        gaps: [
          inner.left - outer.left - parseFloat(border.borderLeftWidth),
          outer.right - inner.right - parseFloat(border.borderRightWidth),
        ],
      }
    })
    expect(enclosures.count).toBe(2)
    expect(enclosures.cardContainsToolbar).toBe(true)
    expect(enclosures.tableContainsToolbar).toBe(false)
    for (const gap of enclosures.gaps)
      expect(gap).toBeCloseTo(width < 1024 ? 16 : 24, 0)
    const scroll = await table.evaluate((el) => {
      el.scrollLeft = 200
      return {
        width: el.clientWidth,
        content: el.scrollWidth,
        left: el.scrollLeft,
      }
    })
    await expect(table).toHaveCSS("overflow-x", "auto")
    expect(scroll.content).toBeGreaterThan(scroll.width)
    expect(scroll.left).toBeGreaterThan(0)
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width)
    await next.click()
    await expect(page.locator("tbody tr")).toHaveCount(5)
    await table.evaluate((el) => {
      el.scrollLeft = 0
    })
    await expect(
      page.getByRole("link", { name: "分页策略 51", exact: true }),
    ).toBeVisible()
    await page.getByLabel("策略名称", { exact: true }).fill("分页策略 55")
    await page.getByRole("button", { name: "搜索", exact: true }).click()
    await expect(page.locator("tbody tr")).toHaveCount(1)
    await expect(
      page.getByRole("link", { name: "分页策略 55", exact: true }),
    ).toBeVisible()
    await expect(next).toBeDisabled()
  })
}

test("策略列表进入编辑并返回时保持正文标题和中性主题", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  const main = page.locator("#workspace-main")
  const header = page.locator('[data-slot="workspace-page-title"]')
  const readTheme = () =>
    main.evaluate((el) => ({
      background: getComputedStyle(el).backgroundColor,
      primary: getComputedStyle(
        el.querySelector('[data-presentation="strategy-list"]') ?? el,
      )
        .getPropertyValue("--primary")
        .trim(),
      mainPrimary: getComputedStyle(el).getPropertyValue("--primary").trim(),
      rootPrimary: getComputedStyle(document.documentElement)
        .getPropertyValue("--primary")
        .trim(),
    }))
  await expect(
    main.getByRole("heading", { level: 1, name: "投放策略", exact: true }),
  ).toHaveCSS("font-size", "22px")
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1)
  await expect(header).toHaveText("投放工作")
  await expect(header.getByRole("heading", { level: 1 })).toHaveCount(0)
  const listTheme = await readTheme()
  expect(listTheme.primary).toBe(listTheme.rootPrimary)
  expect(listTheme.mainPrimary).toBe(listTheme.rootPrimary)
  await page.getByRole("link", { name: "租户策略", exact: true }).click()
  await expect(page).toHaveURL(editUrl)
  await expect(
    main.getByRole("heading", {
      level: 1,
      name: "编辑投放策略",
      exact: true,
    }),
  ).toHaveCSS("font-size", "22px")
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1)
  await expect(header.getByRole("heading", { level: 1 })).toHaveCount(0)
  await expect(header).toHaveText("投放工作")
  await expect(main.locator('[data-presentation="strategy-list"]')).toHaveCount(
    0,
  )
  const editTheme = await readTheme()
  expect(editTheme.primary).toBe(editTheme.rootPrimary)
  expect(editTheme.mainPrimary).toBe(editTheme.rootPrimary)
  expect(editTheme).toEqual(listTheme)
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${A}/strategies/?$`))
  await expect(
    main.getByRole("heading", { level: 1, name: "投放策略", exact: true }),
  ).toHaveCSS("font-size", "22px")
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1)
  await expect(header).toHaveText("投放工作")
  expect(await readTheme()).toEqual(listTheme)
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("深色策略页主按钮保持中性色并具有可读文字和可见边界对比", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem("workbench-theme", "dark"),
  )
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.locator("html")).toHaveClass(/dark/)
  const button = page.getByRole("link", { name: "新建策略", exact: true })
  await expect(button).toBeVisible()
  await expect(button).toBeEnabled()
  const contrast = await button.evaluate((el) => {
    const canvas = document.createElement("canvas")
    canvas.width = canvas.height = 1
    const context = canvas.getContext("2d")!
    const rgb = (css: string) => {
      context.clearRect(0, 0, 1, 1)
      context.fillStyle = css
      context.fillRect(0, 0, 1, 1)
      return Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3)
    }
    const luminance = (values: number[]) =>
      values
        .map((v) => {
          const n = v / 255
          return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4
        })
        .reduce(
          (total, value, index) =>
            total + value * [0.2126, 0.7152, 0.0722][index],
          0,
        )
    const ratio = (a: number[], b: number[]) => {
      const x = luminance(a),
        y = luminance(b)
      return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
    }
    const style = getComputedStyle(el)
    const background = rgb(style.backgroundColor)
    const foreground = rgb(style.color)
    const main = rgb(
      getComputedStyle(document.querySelector("#workspace-main")!)
        .backgroundColor,
    )
    return {
      text: ratio(background, foreground),
      boundary: ratio(background, main),
      backgroundChroma: Math.max(...background) - Math.min(...background),
      foregroundChroma: Math.max(...foreground) - Math.min(...foreground),
    }
  })
  expect(contrast.text).toBeGreaterThanOrEqual(4.5)
  expect(contrast.boundary).toBeGreaterThanOrEqual(3)
  expect(contrast.backgroundChroma).toBeLessThanOrEqual(1)
  expect(contrast.foregroundChroma).toBeLessThanOrEqual(1)
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("策略205条列表默认可用、50/100服务端游标与字段对应", async ({ page }) => {
  const { requests } = await boundary(page, { count: 205 })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  const first = page.locator("tbody tr").first()
  await expect(first).toContainText("USD 100")
  await expect(first).toContainText("1.08 倍")
  await expect(first).toContainText("每组最多 10 条")
  await expect(first).toContainText("每个广告创意数量 2")
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(
    page.getByRole("link", { name: "分页策略 51", exact: true }),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  expect(
    requests
      .filter((r) => r.path.endsWith("/strategies"))
      .map((r) => [
        r.query.get("limit"),
        r.query.get("cursor"),
        r.query.get("active"),
      ]),
  ).toEqual([
    ["50", null, "true"],
    ["50", "50", "true"],
    ["100", null, "true"],
  ])
  await page.getByLabel("策略名称", { exact: true }).fill("%_")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("没有符合条件的记录", { exact: true }),
  ).toBeVisible()
  expect(
    requests
      .filter((r) => r.path.endsWith("/strategies"))
      .slice(-1)[0]
      .query.get("query"),
  ).toBe("%_")
})
test("只读成员能看版本和文案池，不能编辑复制或停用", async ({ page }) => {
  const { requests } = await boundary(page, { role: "viewer" })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(
    page.getByRole("link", { name: "新建策略", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: /^编辑$|复制为新策略|^停用$/ }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "查看版本", exact: true }).click()
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "查看版本", exact: true })
    .click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveAttribute(
    "readonly",
    "",
  )
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "查看文案", exact: true }).click()
  const sheet = page.getByRole("dialog")
  await expect(
    sheet.getByText("当前有效去重正文：100 条 · 英文", { exact: true }),
  ).toBeVisible()
  await expect(sheet.locator("li")).toHaveCount(100)
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("新建空表单无生产预算默认值，空租户和403明确区分", async ({ page }) => {
  await boundary(page, { empty: true })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.getByText("还没有投放策略", { exact: true })).toBeVisible()
  await page.getByRole("link", { name: "新建策略", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("")
  await expect(
    page.getByLabel("每个广告创意数量", { exact: true }),
  ).toHaveValue("1")
  await expect(
    page.getByRole("button", { name: "创建策略", exact: true }),
  ).toBeDisabled()
})
test("策略停用只PATCH本地可用性，旧版本保留且可恢复", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  await page.getByRole("button", { name: "停用", exact: true }).click()
  await expect(page.getByRole("dialog")).toContainText("已提交任务不受影响")
  await page.getByRole("button", { name: "确认", exact: true }).click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await page.getByRole("combobox", { name: "策略状态" }).click()
  await page.getByRole("option", { name: "已停用", exact: true }).click()
  await page.getByRole("link", { name: "租户策略", exact: true }).click()
  await expect(page.getByText("策略已停用", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
  expect(
    requests
      .filter((r) => r.method !== "GET")
      .map((r) => [r.method, r.path, r.body]),
  ).toEqual([["PATCH", `/api/tenants/${A}/strategies/${S}`, { active: false }]])
})
test("确定校验失败保留输入并标记字段，不进入未知回查", async ({ page }) => {
  const { requests } = await boundary(page, { mode: "failure" })
  await page.goto(editUrl)
  await page.getByLabel("每个广告创意数量", { exact: true }).fill("3")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(
    page.getByLabel("每个广告创意数量", { exact: true }),
  ).toHaveValue("3")
  await expect(
    page.getByLabel("每个广告创意数量", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true")
  expect(
    requests.filter((r) => r.path.includes("/strategy-save-requests/")),
  ).toHaveLength(0)
})
test("一个格式同时改变嘉书和网眼，批次编号自动追加且剧目 ID 必填", async ({
  page,
}) => {
  await boundary(page)
  await page.goto(editUrl)
  const template = page.getByLabel("广告名称格式", { exact: true })
  await expect(template).toHaveValue("[版权方＋剧名]-[剧目 ID]")
  await expect(
    page.getByLabel("Campaign 后缀模板", { exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "添加日期", exact: true }).click()
  await expect(template).toHaveValue("[版权方＋剧名]-[剧目 ID]-[日期]")
  const naming = page.getByRole("region", { name: "广告命名示例" })
  for (const [provider, base] of [
    ["嘉书", "jiashu-The Bond-106001"],
    ["网眼", "{b30008/s328302/c3}-The Bond-328302"],
  ]) {
    const campaign = `${base}-20260913-A7K2`
    await expect(
      naming.getByRole("region", { name: provider, exact: true }).locator("dd"),
    ).toHaveText([campaign, `${campaign}-g01`, `${campaign}-g01-sp1`])
  }
  await page
    .getByRole("button", { name: "添加版权方拼音", exact: true })
    .click()
  await expect(template).toHaveValue(
    "[版权方＋剧名]-[剧目 ID]-[日期]-[版权方拼音]",
  )
  await expect(
    page.getByRole("button", { name: "添加版权方拼音", exact: true }),
  ).toBeDisabled()
  for (const [provider, base, pinyin] of [
    ["嘉书", "jiashu-The Bond-106001", "jiashu"],
    ["网眼", "{b30008/s328302/c3}-The Bond-328302", "wangyan"],
  ]) {
    const campaign = `${base}-20260913-${pinyin}-A7K2`
    await expect(
      naming.getByRole("region", { name: provider, exact: true }).locator("dd"),
    ).toHaveText([campaign, `${campaign}-g01`, `${campaign}-g01-sp1`])
  }
  await template.fill("[版权方＋剧名]-固定文字-[剧目 ID]")
  await expect(
    naming
      .getByRole("region", { name: "网眼", exact: true })
      .locator("dd")
      .first(),
  ).toHaveText("{b30008/s328302/c3}-The Bond-固定文字-328302-A7K2")
  await template.fill("[版权方＋剧名]-[日期]")
  await expect(template).toHaveAttribute("aria-invalid", "true")
  await expect(
    page.getByText("请保留剧目 ID字段。", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
})
test("切租户先保护策略输入，确认后到新租户列表而非旧策略详情", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("321.00")
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("321.00")
  expect(new URL(page.url()).pathname).toContain(`/tenants/${A}/`)
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await page
    .getByRole("button", { name: "丢弃未保存修改", exact: true })
    .click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${B}/strategies/?$`))
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  expect(
    requests.some((r) => r.path === `/api/tenants/${B}/strategies/${S}`),
  ).toBe(false)
})
for (const path of [`/tenants/${A}/strategies/new`, editUrl])
  test(`登录恢复合法策略路由 ${path}`, async ({ page }) => {
    await boundary(page, { loggedOut: true })
    await page.goto(path)
    await expect(page).toHaveURL(/\/login$/)
    await page.getByLabel("账号", { exact: true }).fill("operator")
    await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
    await page.getByRole("button", { name: "登录", exact: true }).click()
    await expect(page).toHaveURL(path)
    await expect(page.getByLabel("日预算", { exact: true })).toBeVisible()
  })
test("策略403保留登录而不展示空列表", async ({ page }) => {
  await boundary(page, { deny: true })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByText("还没有投放策略", { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "strategy-test-token",
  )
})
test("历史版本始终只读，当前版本更新不改变旧版本预算", async ({ page }) => {
  const { records, versions } = await boundary(page)
  records[0].latest_version = 2
  records[0].version_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  records[0].config = { ...config, budget: "180.00" }
  versions.push({
    ...versions[0],
    id: records[0].version_id,
    number: 2,
    config: records[0].config,
  })
  await page.goto(`${editUrl}?version_id=${V}`)
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("100.00")
  await expect(page.getByLabel("日预算", { exact: true })).toHaveAttribute(
    "readonly",
    "",
  )
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
})
test("精确回查返回本次v2，即使服务器最新v3也不冒认其他版本", async ({
  page,
}) => {
  const { requests, records, versions } = await boundary(page, {
    mode: "unknown",
  })
  await page.route(
    "**/api/tenants/*/strategy-save-requests/*",
    async (route) => {
      records[0].latest_version = 3
      records[0].version_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
      records[0].config = { ...config, budget: "999.00" }
      if (!versions.some((v) => v.number === 3))
        versions.push({
          ...versions[0],
          id: records[0].version_id,
          number: 3,
          config: records[0].config,
        })
      await route.fallback()
    },
  )
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("101.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("101.00")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
  expect(
    requests.filter((r) => r.method === "POST" && r.path.endsWith("/versions")),
  ).toHaveLength(1)
})
test("延迟策略查询在切租户时取消，不把旧详情读入新租户", async ({ page }) => {
  const { requests } = await boundary(page)
  let release!: () => void, started!: () => void
  const hold = new Promise<void>((r) => {
      release = r
    }),
    start = new Promise<void>((r) => {
      started = r
    }),
    failed: string[] = []
  page.on("requestfailed", (r) => failed.push(r.url()))
  await page.route(`**/api/tenants/${A}/strategies/${S}`, async (route) => {
    started()
    await hold
    await route.fulfill({ json: original }).catch(() => {})
  })
  await page.goto(editUrl)
  await start
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${B}/strategies/?$`))
  release()
  await expect
    .poll(() =>
      failed.some((url) => url.includes(`/tenants/${A}/strategies/${S}`)),
    )
    .toBe(true)
  expect(
    requests.some((r) => r.path === `/api/tenants/${B}/strategies/${S}`),
  ).toBe(false)
})
test("切换BC保留当前租户策略，目录请求没有BC筛选", async ({ page }) => {
  const { requests } = await boundary(page)
  const BC1 = "7000000000000000001",
    BC2 = "7000000000000000002"
  await page.route("**/api/tenants/*/bcs*", (route) =>
    route.fulfill({
      json: {
        items: [
          { bc_id: BC1, name: "第一BC" },
          { bc_id: BC2, name: "第二BC" },
        ],
        next_cursor: null,
      },
    }),
  )
  await page.goto(`${editUrl}?bc_id=${BC1}`)
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("100.00")
  await page.getByRole("combobox", { name: "当前 BC" }).click()
  await page.getByRole("option", { name: /第二BC/ }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("100.00")
  expect(
    requests
      .filter((r) => r.path.includes("/strategies"))
      .every((r) => !r.query.has("bc_id")),
  ).toBe(true)
})
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 900, height: 900 },
  { width: 390, height: 844 },
])
  test(`策略编辑${viewport.width}px布局与固定操作区`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport)
    await boundary(page)
    await page.goto(editUrl)
    await expect(page.getByLabel("日预算", { exact: true })).toHaveValue(
      "100.00",
    )
    await expect(
      page.getByRole("region", { name: "结构与预算示例" }),
    ).toBeVisible()
    await page.getByLabel("每个广告创意数量", { exact: true }).fill("3")
    await page
      .getByRole("region", { name: "结构与预算示例" })
      .scrollIntoViewIfNeeded()
    const size = await page.evaluate(() => ({
      viewport: innerWidth,
      body: document.documentElement.scrollWidth,
    }))
    expect(size.body).toBeLessThanOrEqual(size.viewport)
    await page
      .getByRole("button", { name: "保存为新版本", exact: true })
      .scrollIntoViewIfNeeded()
    await expect(
      page.getByRole("button", { name: "保存为新版本", exact: true }),
    ).toBeInViewport()
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({
      path: testInfo.outputPath(`strategy-${viewport.width}.png`),
      fullPage: false,
    })
  })
test("后台检查版本变化保留本地配置，临时读取失败也不卸载表单", async ({
  page,
}) => {
  const { records } = await boundary(page)
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("130.00")
  records[0].latest_version = 2
  records[0].config = {
    ...config,
    budget: "120.00",
    cta_option_ids: ["other-existing-id"],
  }
  await page.getByRole("button", { name: "检查最新版本", exact: true }).click()
  await expect(
    page.getByText("服务器当前为 v2，本地输入已保留。"),
  ).toBeVisible()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("130.00")
  await expect(
    page.getByText("已有 ID：official-existing-id", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await page.route(`**/api/tenants/${A}/strategies/${S}`, (route) =>
    route.fulfill({ status: 500, json: { message: "读取暂时失败" } }),
  )
  await page.getByRole("button", { name: "检查最新版本", exact: true }).click()
  await expect(page.getByText("读取暂时失败", { exact: true })).toBeVisible({
    timeout: 10000,
  })
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("130.00")
})

test("策略列表与历史预算去尾零但编辑原文保留", async ({ page }) => {
  const api = await boundary(page)
  const budget = "9007199254740993123456.123400000000"
  api.records[0].config.budget = budget
  api.versions[0].config.budget = budget
  await page.goto(`/tenants/${A}/strategies`)
  await expect(
    page
      .getByRole("cell")
      .filter({ hasText: "USD 9007199254740993123456.1234 / 天" })
      .first(),
  ).toBeVisible()
  await page.getByRole("button", { name: "查看版本", exact: true }).click()
  await expect(
    page
      .getByRole("dialog")
      .getByRole("cell")
      .filter({ hasText: "USD 9007199254740993123456.1234 / 天" })
      .first(),
  ).toBeVisible()
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "查看版本", exact: true })
    .click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue(budget)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("策略固定列完整显示超长预算和ROAS且文字不覆盖相邻单元格", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const api = await boundary(page)
  const budget = "9007199254740993123456.123400000000"
  const roas = "9007199254740993123456.123456789012"
  for (const record of [api.records[0], api.versions[0]]) {
    record.config.budget = budget
    record.config.target_roas = roas
  }
  await page.goto(`/tenants/${A}/strategies`)
  for (const name of ["USD 9007199254740993123456.1234 / 天", `${roas} 倍`]) {
    const cell = page.getByRole("cell").filter({ hasText: name }).first()
    await expect(cell).toBeVisible()
    const bounds = await cell.evaluate((el) => {
      const box = el.getBoundingClientRect()
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
      const rects: DOMRect[] = []
      while (walker.nextNode()) {
        if (!walker.currentNode.textContent?.trim()) continue
        const range = document.createRange()
        range.selectNodeContents(walker.currentNode)
        rects.push(...Array.from(range.getClientRects()))
      }
      return {
        count: rects.length,
        overflow: Math.max(
          0,
          ...rects.flatMap((rect) => [
            box.left - rect.left,
            rect.right - box.right,
          ]),
        ),
        width: el.clientWidth,
        content: el.scrollWidth,
      }
    })
    expect(bounds.count).toBeGreaterThan(1)
    expect(bounds.overflow).toBeLessThanOrEqual(1)
    expect(bounds.content).toBeLessThanOrEqual(bounds.width)
  }
  expect(
    api.requests.filter((request) => request.method !== "GET"),
  ).toHaveLength(0)
})

test("空白新建策略默认 USD，币种禁止展开且保存仍提交 USD", async ({ page }) => {
  const { requests } = await boundary(page, { empty: true })
  await page.goto(`/tenants/${A}/strategies/new`)
  const currency = page.getByRole("combobox", {
    name: "预算币种",
    exact: true,
  })
  await expect(currency).toHaveText("USD")
  await expect(currency).toBeDisabled()
  await currency.click({ force: true })
  await expect(page.getByRole("listbox")).toHaveCount(0)
  await page.getByLabel("策略名称", { exact: true }).fill("空白新建策略")
  await page.getByLabel("日预算", { exact: true }).fill("100")
  await page.getByRole("combobox", { name: "竞价策略" }).click()
  await page.getByRole("option", { name: "目标 ROAS", exact: true }).click()
  await page.locator("#strategy-target_roas").fill("1.08")
  await page.getByRole("combobox", { name: "广告组数量规则" }).click()
  await page.getByRole("option", { name: "按素材数量", exact: true }).click()
  await page.getByLabel("每组最多素材数", { exact: true }).fill("10")
  await page.getByLabel("每个广告创意数量", { exact: true }).fill("2")
  await expect(currency).toHaveText("USD")
  await expect(
    page.getByRole("button", { name: "创建策略", exact: true }),
  ).toBeEnabled()
  await page.getByRole("button", { name: "创建策略", exact: true }).click()
  await expect(page).toHaveURL(
    /\/strategies\/66666666-6666-4666-8666-666666666666/,
  )
  const writes = requests.filter(
    (r) => r.method === "POST" && r.path.endsWith("/strategies"),
  )
  expect(writes).toHaveLength(1)
  expect(writes[0].body.config).toMatchObject({
    currency: "USD",
    budget: "100",
    target_roas: "1.08",
    max_materials_per_group: 10,
    creative_count: 2,
  })
})

test("统一命名使用中文字段并保存，恢复后仍为同一格式", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  const template = page.getByLabel("广告名称格式", { exact: true })
  await expect(template).toHaveValue("[版权方＋剧名]-[剧目 ID]")
  await template.fill("[剧目 ID]")
  await page
    .getByRole("button", { name: "添加版权方＋剧名", exact: true })
    .click()
  await expect(template).toHaveValue("[剧目 ID]-[版权方＋剧名]")
  const generic = page.getByRole("region", { name: "嘉书", exact: true })
  await expect(generic.locator("dd").first()).toHaveText(
    "106001-jiashu-The Bond-A7K2",
  )
  await template.fill("[版权方＋剧名]-[剧目 ID]-[日期")
  await expect(template).toHaveAttribute("aria-invalid", "true")
  await template.fill("[日期]-[版权方＋剧名]-[剧目 ID]")
  await expect(template).not.toHaveAttribute("aria-invalid", "true")
  await template.fill("[版权方＋剧名]-[剧目 ID]-[批次编号]")
  await expect(template).toHaveAttribute("aria-invalid", "true")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await template.fill("[版权方＋剧名]-[剧目 ID]-[剧目 ID]")
  await expect(template).toHaveAttribute("aria-invalid", "true")
  await template.fill("[版权方＋剧名]-[日期]-投放-[剧目 ID]")
  await expect(generic.locator("dd")).toHaveText([
    "jiashu-The Bond-20260913-投放-106001-A7K2",
    "jiashu-The Bond-20260913-投放-106001-A7K2-g01",
    "jiashu-The Bond-20260913-投放-106001-A7K2-g01-sp1",
  ])
  await expect(
    page
      .getByRole("region", { name: "网眼", exact: true })
      .locator("dd")
      .first(),
  ).toHaveText("{b30008/s328302/c3}-The Bond-20260913-投放-328302-A7K2")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const saved = requests.find(
    (row) => row.method === "POST" && row.path.endsWith("/versions"),
  )!
  expect(saved.body.config.campaign_name_template).toBe(
    "{provider_drama}-{YYYYMMDD}-投放-{drama_id}",
  )
  expect(saved.body.config).not.toHaveProperty("campaign_suffix")
  await page.goto(editUrl)
  await expect(page.getByLabel("广告名称格式", { exact: true })).toHaveValue(
    "[版权方＋剧名]-[日期]-投放-[剧目 ID]",
  )
})

for (const viewport of [
  { width: 1440, height: 1050 },
  { width: 390, height: 844 },
]) {
  test(`统一命名布局 ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await boundary(page)
    await page.goto(editUrl)
    const field = page.getByLabel("广告名称格式", { exact: true })
    await field.scrollIntoViewIfNeeded()
    await expect(field).toHaveValue("[版权方＋剧名]-[剧目 ID]")
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    await page.screenshot({
      path: `/tmp/tk-ada-unified-naming-${viewport.width}.png`,
      fullPage: true,
    })
  })
}

test("策略无 BC 时可保存自动定向最低年龄和语言", async ({ page }) => {
  const api = await boundary(page)
  await page.goto(editUrl)
  await expect(page.getByText("受众定向", { exact: true })).toBeVisible()
  await page.getByRole("combobox", { name: "国家范围", exact: true }).click()
  await page.getByRole("option", { name: "指定国家", exact: true }).click()
  await page.getByRole("checkbox", { name: "美国", exact: true }).check()
  await page.getByRole("checkbox", { name: "加拿大", exact: true }).check()
  await expect(
    page.getByRole("checkbox", { name: "中国", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("checkbox", { name: "印度", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("combobox", { name: "最低年龄", exact: true }).click()
  await page.getByRole("option", { name: "25 岁以上", exact: true }).click()
  await page.getByRole("checkbox", { name: "英语", exact: true }).check()
  await expect(
    page.getByRole("combobox", { name: "性别", exact: true }),
  ).toHaveCount(0)
  await expect(page.getByRole("checkbox", { name: /13.*17/ })).toHaveCount(0)
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const saved = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/versions"),
  )
  expect(saved?.body.config.targeting).toEqual({
    region_mode: "SELECTED",
    region_codes: ["CA", "US"],
    languages: ["en"],
    age_groups: ["AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"],
    gender: "GENDER_UNLIMITED",
  })
  await page.reload()
  await expect(
    page.getByRole("checkbox", { name: "美国", exact: true }),
  ).toBeChecked()
  await expect(
    page.getByRole("checkbox", { name: "加拿大", exact: true }),
  ).toBeChecked()
  expect(api.requests.some((r) => r.path.includes("/targeting"))).toBe(false)
})

test("策略默认值按业务决策顺序展示，隐藏无决策价值的安排项", async ({
  page,
}) => {
  await boundary(page, { empty: true })
  await page.goto(`/tenants/${A}/strategies/new`)
  const cardTitles = page.locator('form#strategy-form [data-slot="card-title"]')
  await expect(cardTitles).toHaveText([
    "基本信息",
    "搭建结构",
    "预算与出价",
    "创建状态",
    "受众定向",
    "文案与 CTA",
    "广告命名",
    "生成规则",
  ])
  await expect(
    page.getByRole("heading", { name: "广告组", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "广告", exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("combobox", { name: "预算策略" })).toHaveText(
    "系列预算",
  )
  await expect(page.getByRole("combobox", { name: "竞价策略" })).toHaveText(
    "最高价值",
  )
  await expect(
    page.getByRole("combobox", { name: "广告组数量规则" }),
  ).toHaveText("固定数量")
  await expect(page.getByLabel("广告组数量", { exact: true })).toHaveValue("1")
  await expect(page.getByLabel("广告组素材安排", { exact: true })).toHaveCount(
    0,
  )
  await expect(page.getByRole("combobox", { name: "广告数量规则" })).toHaveText(
    "按素材数量",
  )
  await expect(
    page.getByLabel("每个广告最多素材数", { exact: true }),
  ).toHaveValue("1")
  await expect(page.getByLabel("广告素材安排", { exact: true })).toHaveCount(0)
  await expect(page.getByText(/素材不足处理/)).toHaveCount(0)
})

test("固定广告结构摘要包含素材安排", async ({ page }) => {
  const api = await boundary(page)
  api.records[0].config.ad_generation_mode = "FIXED"
  api.records[0].config.ads_per_group = 2
  api.records[0].config.max_materials_per_ad = null
  api.records[0].config.ad_material_allocation = "SEQUENTIAL_AVERAGE"
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.locator("tbody tr").first()).toContainText(
    "固定 2 个广告 · 按顺序平均分配",
  )
})

test("切换数量规则和竞价策略会清除不相关字段", async ({ page }) => {
  await boundary(page, { empty: true })
  await page.goto(`/tenants/${A}/strategies/new`)
  const groupRule = page.getByRole("combobox", { name: "广告组数量规则" })
  await groupRule.click()
  await page.getByRole("option", { name: "按素材数量", exact: true }).click()
  await expect(page.getByLabel("每组最多素材数", { exact: true })).toBeVisible()
  await expect(page.getByLabel("广告组数量", { exact: true })).toHaveCount(0)
  await groupRule.click()
  await page.getByRole("option", { name: "固定数量", exact: true }).click()
  await page.getByLabel("广告组数量", { exact: true }).fill("2")
  await expect(page.getByLabel("广告组素材安排", { exact: true })).toBeVisible()
  await page.getByRole("combobox", { name: "广告组素材安排" }).click()
  await page
    .getByRole("option", { name: "按顺序平均分配", exact: true })
    .click()
  await groupRule.click()
  await page.getByRole("option", { name: "按素材数量", exact: true }).click()
  await expect(page.getByLabel("广告组素材安排", { exact: true })).toHaveCount(
    0,
  )
  await expect(page.getByLabel("广告组数量", { exact: true })).toHaveCount(0)

  const bidRule = page.getByRole("combobox", { name: "竞价策略" })
  await bidRule.click()
  await page.getByRole("option", { name: "目标 ROAS", exact: true }).click()
  await page.locator("#strategy-target_roas").fill("1.2")
  await bidRule.click()
  await page.getByRole("option", { name: "最高价值", exact: true }).click()
  await expect(page.locator("#strategy-target_roas")).toHaveCount(0)
})

test("规则切换后清理服务端互斥字段错误，仍可保存", async ({ page }) => {
  await boundary(page)
  let validationCalls = 0
  await page.route("**/api/tenants/*/strategies/validate", async (route) => {
    validationCalls += 1
    if (validationCalls === 1)
      return route.fulfill({
        json: {
          valid: false,
          errors: [{ field: "group_count", code: "group_count_invalid" }],
          scene_check_pending: true,
        },
      })
    return route.fulfill({
      json: { valid: true, errors: [], scene_check_pending: true },
    })
  })
  await page.goto(editUrl)
  const groupRule = page.getByRole("combobox", { name: "广告组数量规则" })
  await groupRule.click()
  await page.getByRole("option", { name: "固定数量", exact: true }).click()
  await page.getByLabel("日预算", { exact: true }).fill("101")
  await page.getByLabel("广告组数量", { exact: true }).fill("2")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText(/广告组数量：该字段未通过策略校验/)).toBeVisible()
  await groupRule.click()
  await page.getByRole("option", { name: "按素材数量", exact: true }).click()
  await expect(page.getByText(/广告组数量：/)).toHaveCount(0)
  await page.getByLabel("每组最多素材数", { exact: true }).fill("10")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  expect(validationCalls).toBe(2)
})

test("固定数量超过后端安全上限时显示字段错误并阻止保存", async ({ page }) => {
  await boundary(page)
  await page.goto(editUrl)
  const groupRule = page.getByRole("combobox", { name: "广告组数量规则" })
  await groupRule.click()
  await page.getByRole("option", { name: "固定数量", exact: true }).click()
  await page.getByLabel("广告组数量", { exact: true }).fill("101")
  await expect(
    page.getByText("请输入 1 至 100 的整数。", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
})

test("首次新建策略可搜索国家、全选和清空，不依赖账户目录", async ({ page }) => {
  const api = await boundary(page, { empty: true })
  await page.goto(`/tenants/${A}/strategies/new`)
  await page.getByRole("combobox", { name: "国家范围", exact: true }).click()
  await page.getByRole("option", { name: "指定国家", exact: true }).click()
  const search = page.getByRole("textbox", { name: "搜索国家", exact: true })
  await search.fill("JP")
  await expect(
    page.getByRole("checkbox", { name: "日本", exact: true }),
  ).toBeEnabled()
  await page.getByRole("checkbox", { name: "日本", exact: true }).check()
  await search.fill("美国")
  await page.getByRole("checkbox", { name: "美国", exact: true }).check()
  await search.fill("")
  await page.getByRole("button", { name: "全选国家", exact: true }).click()
  await expect(
    page.getByRole("checkbox", { name: "巴西", exact: true }),
  ).toBeChecked()
  await expect(
    page.getByRole("checkbox", { name: "日本", exact: true }),
  ).toBeChecked()
  await page.getByRole("button", { name: "清空国家", exact: true }).click()
  await expect(
    page.getByText("请至少选择一个国家。", { exact: true }).first(),
  ).toBeVisible()
  await expect(
    page.getByRole("checkbox", { name: "美国", exact: true }),
  ).not.toBeChecked()
  expect(api.requests.some((r) => r.path.includes("/targeting"))).toBe(false)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  await page.getByRole("checkbox", { name: "美国", exact: true }).check()
  await page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByText("受众定向", { exact: true }) })
    .screenshot({ path: "/tmp/tkada-strategy-countries.png" })
  await page.getByLabel("策略名称", { exact: true }).fill("首次国家策略")
  await page.getByLabel("日预算", { exact: true }).fill("100")
  await page.getByRole("button", { name: "创建策略", exact: true }).click()
  await expect(page.getByText("已保存 v1", { exact: true })).toBeVisible()
  const saved = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/strategies"),
  )
  expect(saved?.body.config.targeting.region_codes).toEqual(["US"])
})

test("目录外的已选国家保留并可移除", async ({ page }) => {
  const api = await boundary(page)
  api.records[0].config.targeting = {
    region_mode: "SELECTED",
    region_codes: ["US", "RU"],
  }
  await page.goto(editUrl)
  const historical = page.getByRole("checkbox", {
    name: "俄罗斯 不可用/待核实",
    exact: true,
  })
  await expect(historical).toBeChecked()
  // 移除目录外国家后该选项会立即消失，用 click 后断言移除，避免 uncheck 重找节点。
  await historical.click()
  await expect(historical).toHaveCount(0)
  await expect(
    page.getByRole("checkbox", { name: "美国", exact: true }),
  ).toBeChecked()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("只读策略可查看预置国家但不能更改选择", async ({ page }) => {
  const api = await boundary(page, { role: "viewer" })
  api.records[0].config.targeting = {
    region_mode: "SELECTED",
    region_codes: ["US"],
  }
  await page.goto(editUrl)
  await expect(
    page.getByRole("checkbox", { name: "美国", exact: true }),
  ).toBeChecked()
  await expect(
    page.getByRole("checkbox", { name: "美国", exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole("checkbox", { name: "加拿大", exact: true }),
  ).toBeDisabled()
  expect(api.requests.some((r) => r.path.includes("/targeting"))).toBe(false)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("结构示例读取服务器预算，试算失败后不显示旧广告数", async ({ page }) => {
  await boundary(page)
  await page.goto(editUrl)
  const example = page.getByRole("region", { name: "结构与预算示例" })
  await expect(example).toContainText("每基础广告文案版本数 K=2")
  await expect(page.locator("#strategy-creative_count-help")).toContainText(
    "K 为每基础广告的文案版本数",
  )
  await expect(example).toContainText("示例日预算合计：USD 100")
  let observed: any
  await page.route("**/strategies/structure-estimate", async (route) => {
    observed = route.request().postDataJSON()
    await route.fulfill({
      status: 422,
      json: { detail: "configuration_invalid" },
    })
  })
  await page.getByLabel("每个广告创意数量", { exact: true }).fill("3")
  await expect(example).toContainText("当前结构无法生成")
  await expect(example).not.toContainText("最终 46 个广告")
  expect(observed.unit_counts).toEqual([23])
  expect(observed.account_count).toBe(1)
  expect(observed.config.creative_count).toBe(3)
})

test("独立剧名和版权方标识可任意排列、预览并保存", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  const template = page.getByLabel("广告名称格式", { exact: true })
  await template.fill("[剧名]-[版权方标识]-[剧目 ID]-[日期]")
  await expect(template).not.toHaveAttribute("aria-invalid", "true")
  const wangyan = page.getByRole("region", { name: "网眼", exact: true })
  await expect(wangyan.locator("dd")).toHaveText([
    "The Bond-{b30008/s328302/c3}-328302-20260913-A7K2",
    "The Bond-{b30008/s328302/c3}-328302-20260913-A7K2-g01",
    "The Bond-{b30008/s328302/c3}-328302-20260913-A7K2-g01-sp1",
  ])
  await template.fill("[日期]_[剧目 ID]_[版权方标识]_[剧名]")
  await expect(wangyan.locator("dd").first()).toHaveText(
    "20260913_328302_{b30008/s328302/c3}_The Bond-A7K2",
  )
  await template.fill("[剧名]-[剧目 ID]")
  await expect(template).toHaveAttribute("aria-invalid", "true")
  await template.fill("[版权方＋剧名]-[剧名]-[剧目 ID]")
  await expect(template).toHaveAttribute("aria-invalid", "true")
  await template.fill("[剧名]-[版权方标识]-[剧目 ID]-[日期]")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  expect(
    requests.find((r) => r.method === "POST" && r.path.endsWith("/versions"))!
      .body.config.campaign_name_template,
  ).toBe("{drama_name}-{provider_label}-{drama_id}-{YYYYMMDD}")
  await page.goto(editUrl)
  await expect(template).toHaveValue("[剧名]-[版权方标识]-[剧目 ID]-[日期]")
})

test("工作台标签保留未保存策略和列表筛选，菜单重复打开不新增标签", async ({
  page,
}) => {
  await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  const search = page.getByPlaceholder("搜索策略名称")
  await search.fill("租户")
  await page.getByRole("link", { name: "租户策略", exact: true }).click()
  await page.getByLabel("日预算", { exact: true }).fill("321.00")
  await page
    .getByRole("navigation", { name: "投放准备" })
    .getByRole("link", { name: "投放策略", exact: true })
    .click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await expect(search).toHaveValue("租户")
  const tabs = page.getByRole("tablist", { name: "已打开的页面" })
  await expect(tabs.getByRole("tab")).toHaveCount(2)
  await tabs.getByRole("tab", { name: "投放策略 · 详情", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("321.00")
  await page
    .getByRole("navigation", { name: "投放准备" })
    .getByRole("link", { name: "投放策略", exact: true })
    .click()
  await expect(tabs.getByRole("tab")).toHaveCount(2)
  await page
    .getByRole("button", { name: "关闭标签：投放策略 · 详情", exact: true })
    .click()
  await expect(
    page.getByRole("dialog", { name: "有未保存的内容" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  await expect(tabs.getByRole("tab")).toHaveCount(2)
  await page
    .getByRole("button", { name: "关闭标签：投放策略 · 详情", exact: true })
    .click()
  await page.getByRole("button", { name: "丢弃并关闭", exact: true }).click()
  await expect(tabs.getByRole("tab")).toHaveCount(1)
  await page.getByRole("link", { name: "租户策略", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("100.00")
  await page.screenshot({
    path: test.info().outputPath("workspace-tabs-desktop.png"),
    fullPage: true,
  })
})

test("隐藏标签的未保存内容保护租户切换和刷新，确认切换后清除全部旧标签", async ({
  page,
}) => {
  await boundary(page)
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("321.00")
  await page
    .getByRole("navigation", { name: "投放准备" })
    .getByRole("link", { name: "投放策略", exact: true })
    .click()
  expect(
    await page.evaluate(
      () =>
        !window.dispatchEvent(new Event("beforeunload", { cancelable: true })),
    ),
  ).toBe(true)
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await expect(
    page.getByRole("dialog", { name: "有未保存的内容" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  await page.getByRole("tab", { name: "投放策略 · 详情", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("321.00")
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await page
    .getByRole("button", { name: "丢弃未保存修改", exact: true })
    .click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${B}/strategies`))
  await expect(
    page.getByRole("tablist", { name: "已打开的页面" }).getByRole("tab"),
  ).toHaveCount(1)
})

test("多张编辑表单的标签互不覆盖，关闭当前脏标签确认后切到相邻标签", async ({
  page,
}) => {
  await boundary(page)
  const errors: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  await page.goto(editUrl)
  await page.getByLabel("日预算", { exact: true }).fill("321.00")
  await page
    .getByRole("navigation", { name: "投放准备" })
    .getByRole("link", { name: "投放策略", exact: true })
    .click()
  await page.getByRole("link", { name: "新建策略", exact: true }).click()
  await page.getByLabel("日预算", { exact: true }).fill("789.00")
  await page.getByLabel("策略名称", { exact: true }).fill("独立新策略")
  const duplicates = await page.evaluate(() => {
    const ids = [...document.querySelectorAll("#workspace-main [id]")].map(
      (element) => element.id,
    )
    return ids.filter((id, index) => ids.indexOf(id) !== index)
  })
  expect(duplicates).toEqual([])
  await page.getByRole("tab", { name: "投放策略 · 详情", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("321.00")
  await page.getByRole("tab", { name: "投放策略 · 新建", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("789.00")
  await expect(page.getByLabel("策略名称", { exact: true })).toHaveValue(
    "独立新策略",
  )
  await page
    .getByRole("button", { name: "关闭标签：投放策略 · 新建", exact: true })
    .click()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("789.00")
  await page
    .getByRole("button", { name: "关闭标签：投放策略 · 新建", exact: true })
    .click()
  await page.getByRole("button", { name: "丢弃并关闭", exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${A}/strategies$`))
  await expect(
    page.getByRole("tab", { name: "投放策略 · 新建", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("tab", { name: "投放策略 · 详情", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("321.00")
  expect(errors).toEqual([])
})

test("旧精细年龄与性别条件必须明确修正才能保存", async ({ page }) => {
  const api = await boundary(page)
  api.records[0].config.targeting = {
    region_mode: "ALL_AVAILABLE",
    region_codes: [],
    languages: ["en"],
    age_groups: ["AGE_25_34"],
    gender: "GENDER_FEMALE",
  }
  await page.goto(editUrl)
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await expect(page.getByText(/旧年龄条件/).first()).toBeVisible()
  await page.getByRole("combobox", { name: "最低年龄", exact: true }).click()
  await page.getByRole("option", { name: "25 岁以上", exact: true }).click()
  await page.getByRole("button", { name: "改为不限性别", exact: true }).click()
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const saved = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/versions"),
  )
  expect(saved?.body.config.targeting).toEqual({
    region_mode: "ALL_AVAILABLE",
    region_codes: [],
    languages: ["en"],
    age_groups: ["AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"],
    gender: "GENDER_UNLIMITED",
  })
})

test("隐藏脏标签阻止切换BC，确认后清除旧标签并重建当前非素材页", async ({
  page,
}) => {
  await boundary(page)
  const firstBC = "7000000000000000001"
  const nextBC = "7000000000000000002"
  await page.route("**/api/tenants/*/bcs*", (route) =>
    route.fulfill({
      json: {
        items: [
          { bc_id: firstBC, name: "第一BC" },
          { bc_id: nextBC, name: "第二BC" },
        ],
        next_cursor: null,
      },
    }),
  )
  await page.goto(`${editUrl}?bc_id=${firstBC}`)
  await page.getByLabel("日预算", { exact: true }).fill("321.00")
  await page
    .getByRole("navigation", { name: "投放准备" })
    .getByRole("link", { name: "投放策略", exact: true })
    .click()
  await page.getByRole("combobox", { name: "当前 BC" }).click()
  await page.getByRole("option", { name: /第二BC/ }).click()
  const guard = page.getByRole("dialog", { name: "有未保存的内容" })
  await expect(guard).toBeVisible()
  await guard.getByRole("button", { name: "留在当前页", exact: true }).click()
  await expect(page).toHaveURL(
    (url) => url.searchParams.get("bc_id") === firstBC,
  )
  await page.getByRole("tab", { name: "投放策略 · 详情", exact: true }).click()
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("321.00")
  await page.getByRole("combobox", { name: "当前 BC" }).click()
  await page.getByRole("option", { name: /第二BC/ }).click()
  await guard
    .getByRole("button", { name: "丢弃未保存修改", exact: true })
    .click()
  await expect(page).toHaveURL(
    (url) => url.searchParams.get("bc_id") === nextBC,
  )
  await expect(page.getByLabel("日预算", { exact: true })).toHaveValue("100.00")
  await expect(
    page.getByRole("tablist", { name: "已打开的页面" }).getByRole("tab"),
  ).toHaveCount(1)
})
