import { expect, test } from "@playwright/test"

const tenant = "11111111-1111-4111-8111-111111111111"
const optimizer = "22222222-2222-4222-8222-222222222222"
const sessionTenants = [
  tenant,
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
]

// 登录态与租户列表的 transport double；用例里后注册的路由优先于这里的兜底。
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("access_token", "ai-workbench-test-token"),
  )
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === "/api/users/me")
      return route.fulfill({
        json: {
          id: "44444444-4444-4444-8444-444444444444",
          username: "ai-test",
          full_name: "AI 测试",
          is_active: true,
          is_superuser: true,
        },
      })
    if (path === "/api/me/tenants")
      return route.fulfill({
        json: {
          items: sessionTenants.map((id, index) => ({
            id,
            name: `租户 ${index + 1}`,
            active: true,
            role: "tenant_admin",
            default_bc_id: "bc-a",
          })),
          next_cursor: null,
          total: sessionTenants.length,
        },
      })
    return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } })
  })
})

test("AI 每日报告使用 transport double 展示冻结证据和待确认状态", async ({
  page,
}) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: optimizer,
            name: "自动投手",
            active: true,
            version: 2,
            bc_id: "bc-a",
            advertiser_ids: ["acc-1"],
            strategy_version_id: "strategy-2",
            optimization_mode: "AUTO",
            rule_ids: [],
          },
        ],
      },
    }),
  )
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}/daily-report**`,
    (route) =>
      route.fulfill({
        json: {
          business_date: "2026-11-12",
          optimization_mode: "AUTO",
          target_progress: {
            planned: 1,
            completed: 1,
            pending: 0,
            failed: 0,
            unknown: 0,
          },
          schedule_rows: [
            {
              id: "plan-1",
              title: "剧集 A",
              status: "COMPLETE",
              order_materials: true,
            },
          ],
          material: { requested: 1, not_requested: 0, pending: 0, matched: 1 },
          rule_actions: [],
          pending_proposals: [{ id: "proposal-1", status: "PENDING" }],
          frozen_evidence_ids: ["evidence-1"],
          next_trigger: null,
        },
      }),
  )
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("每日报告")
  await expect(page.getByTestId("ai-daily-report")).toContainText("剧集 A")
  await expect(page.getByTestId("ai-daily-report")).toContainText("待确认 1 条")
})

test("切换投手后每日报告请求保持租户边界", async ({ page }) => {
  const requested: string[] = []
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: optimizer,
            name: "手动投手",
            active: true,
            version: 1,
            bc_id: "bc-a",
            advertiser_ids: ["acc-1"],
            strategy_version_id: "strategy-1",
            optimization_mode: "MANUAL",
            rule_ids: [],
          },
        ],
      },
    }),
  )
  await page.route(
    "**/api/tenants/*/ai-optimizers/*/daily-report**",
    async (route) => {
      requested.push(route.request().url())
      await route.fulfill({
        json: {
          target_progress: {},
          schedule_rows: [],
          material: {},
          rule_actions: [],
          pending_proposals: [],
          frozen_evidence_ids: [],
        },
      })
    },
  )
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  await expect.poll(() => requested.length).toBeGreaterThan(0)
  expect(
    requested.every((url) => url.includes(`/api/tenants/${tenant}/`)),
  ).toBeTruthy()
})

test("双租户双投手切换保留 AUTO/MANUAL、下单路径和回读边界", async ({
  page,
}) => {
  const tenantA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  const tenantB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
  const optimizerA = "aaaaaaaa-0000-4000-8000-aaaaaaaaaaaa"
  const optimizerAManual = "aaaaaaaa-0001-4000-8000-aaaaaaaaaaaa"
  const optimizerB = "bbbbbbbb-0000-4000-8000-bbbbbbbbbbbb"
  const reportRequests: string[] = []
  const proposalRequests: string[] = []
  const versionRequests: Array<Record<string, unknown>> = []
  const optimizerPayload = (
    id: string,
    mode: "AUTO" | "MANUAL",
    name: string,
  ) => ({
    id,
    name,
    active: true,
    version: mode === "AUTO" ? 2 : 3,
    bc_id: id.startsWith("bbbb") ? "bc-b" : "bc-a",
    advertiser_ids: [id.startsWith("bbbb") ? "acc-b" : "acc-a"],
    strategy_version_id: `strategy-${id}`,
    optimization_mode: mode,
    rule_ids: [id.startsWith("bbbb") ? "rule-b-account" : "rule-a-account"],
  })
  await page.route("**/api/tenants/*/ai-optimizers?**", async (route) => {
    const url = route.request().url()
    if (url.includes(`/tenants/${tenantA}/`)) {
      await route.fulfill({
        json: {
          items: [
            optimizerPayload(optimizerA, "AUTO", "自动投手 A"),
            optimizerPayload(optimizerAManual, "MANUAL", "手动投手 A"),
          ],
        },
      })
      return
    }
    if (url.includes(`/tenants/${tenantB}/`)) {
      await route.fulfill({
        json: {
          items: [optimizerPayload(optimizerB, "MANUAL", "手动投手 B")],
        },
      })
      return
    }
    await route.fulfill({
      status: 404,
      json: { detail: "cross-tenant optimizer lookup" },
    })
  })
  await page.route(
    "**/api/tenants/*/ai-optimizers/*/daily-report**",
    async (route) => {
      const url = route.request().url()
      reportRequests.push(url)
      const tenantMatch = url.match(
        /\/api\/tenants\/([^/]+)\/ai-optimizers\/([^/]+)\//,
      )
      const reportTenant = tenantMatch?.[1]
      const reportOptimizer = tenantMatch?.[2]
      const valid =
        (reportTenant === tenantA &&
          [optimizerA, optimizerAManual].includes(reportOptimizer ?? "")) ||
        (reportTenant === tenantB && reportOptimizer === optimizerB)
      if (!valid) {
        await route.fulfill({
          status: 404,
          json: { detail: "cross-tenant report lookup" },
        })
        return
      }
      await route.fulfill({
        json: {
          business_date: "2026-11-12",
          optimization_mode: reportOptimizer === optimizerA ? "AUTO" : "MANUAL",
          target_progress: {
            planned: 2,
            completed: 1,
            partial: 1,
            pending: 0,
            failed: 0,
            unknown: 0,
          },
          schedule_rows: [
            {
              id: `${reportOptimizer}-ordered`,
              title: "需下单剧",
              status: "PARTIAL",
              order_materials: true,
            },
            {
              id: `${reportOptimizer}-existing`,
              title: "已有素材剧",
              status: "COMPLETE",
              order_materials: false,
            },
          ],
          build_outcomes: [
            {
              id: `${reportOptimizer}-build`,
              status: "PARTIAL",
              submission_id: `${reportOptimizer}-submission`,
              readback: { matched: 1, source: "tkada" },
            },
          ],
          material: {
            requested: 1,
            not_requested: 1,
            pending: 0,
            matched: 1,
            unknown: 0,
          },
          rule_actions: [
            {
              rule_id:
                reportOptimizer === optimizerB
                  ? "rule-b-account"
                  : "rule-a-account",
            },
          ],
          pending_proposals: [
            { id: `${reportOptimizer}-proposal`, status: "PENDING" },
          ],
          frozen_evidence_ids: [`${reportOptimizer}-evidence`],
          next_trigger: null,
        },
      })
    },
  )
  await page.route(
    `**/api/tenants/${tenantA}/ai-optimizers/${optimizerAManual}`,
    async (route) => {
      if (route.request().method() !== "PATCH") return route.continue()
      const body = route.request().postDataJSON() as Record<string, unknown>
      versionRequests.push(body)
      await route.fulfill({
        json: {
          ...optimizerPayload(optimizerAManual, "MANUAL", "手动投手 A"),
          version: 4,
          request_id: body.request_id,
        },
      })
    },
  )
  await page.route(
    `**/api/tenants/${tenantA}/ai-optimizers/${optimizerAManual}/proposals**`,
    async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          json: [
            {
              id: `${optimizerAManual}-proposal`,
              status: "PENDING",
              title: "复盘建议",
            },
          ],
        })
        return
      }
      proposalRequests.push(route.request().url())
      await route.fulfill({
        json: {
          id: `${optimizerAManual}-proposal`,
          status: "ACCEPTED",
          published_version_id: "strategy-next",
        },
      })
    },
  )

  await page.goto(`/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerA}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("自动优化")
  await expect(page.getByTestId("ai-daily-report")).toContainText("部分完成")
  await expect(page.getByTestId("ai-daily-report")).toContainText("需下单剧")
  await page.goto(
    `/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerAManual}`,
  )
  await expect(page.getByTestId("ai-daily-report")).toContainText("人工确认")
  await expect(page.getByTestId("ai-daily-report")).toContainText("部分完成")
  await page.goto(
    `/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerAManual}&view=review`,
  )
  await expect(page.getByTestId("ai-review-view")).toContainText("复盘建议")
  await page.getByRole("button", { name: "接受" }).click()
  await expect.poll(() => proposalRequests.length).toBe(1)
  expect(proposalRequests[0]).toContain(
    `/proposals/${optimizerAManual}-proposal/accept`,
  )

  await page.goto(`/tenants/${tenantB}/ai-workbench?optimizer_id=${optimizerB}`)
  // 投手名称显示在左侧列表；报告卡片只承载模式与冻结证据。
  await expect(page.getByTestId("ai-workbench-shell")).toContainText(
    "手动投手 B",
  )
  await expect(page.getByTestId("ai-daily-report")).toContainText("人工确认")
  await expect.poll(() => reportRequests.length).toBeGreaterThanOrEqual(3)
  expect(
    reportRequests.every(
      (url) =>
        url.includes("/api/tenants/") &&
        !url.includes(`/tenants/${tenantA}/ai-optimizers/${optimizerB}/`),
    ),
  ).toBeTruthy()
  expect(
    reportRequests.some((url) =>
      url.includes(`/tenants/${tenantB}/ai-optimizers/${optimizerB}/`),
    ),
  ).toBeTruthy()
})

const configuredOptimizer = (
  id = optimizer,
  name = "投手 A",
  active = true,
) => ({
  id,
  name,
  active,
  version: 1,
  bc_id: "bc-a",
  advertiser_ids: ["acc-1"],
  strategy_version_id: "version-1",
  optimization_mode: "MANUAL",
  daily_spend: "100",
  target_roas: "1.2",
  priority: "BALANCED",
  rule_ids: ["rule-1"],
  timezone: "Asia/Shanghai",
  review_time: "13:00",
  authorization_limits: {},
})

async function choose(
  page: import("@playwright/test").Page,
  label: string,
  name: string,
) {
  await page.getByRole("combobox", { name: label, exact: true }).click()
  await page.getByRole("option", { name, exact: true }).click()
}

test("空租户可创建、编辑、暂停和恢复投手，所有业务请求带 Bearer", async ({
  page,
}) => {
  let items: ReturnType<typeof configuredOptimizer>[] = []
  const writes: Array<Record<string, unknown>> = []
  const authenticated: string[] = []
  await page.route(`**/api/tenants/${tenant}/**`, async (route) => {
    const request = route.request()
    expect(request.headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    authenticated.push(new URL(request.url()).pathname)
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path.endsWith("/bcs"))
      return route.fulfill({
        json: { items: [{ bc_id: "bc-a", name: "星屿" }], next_cursor: null },
      })
    if (path.endsWith("/accounts"))
      return route.fulfill({
        json: {
          items: [
            {
              advertiser_id: "acc-1",
              bc_id: "bc-a",
              name: "账户一",
              can_build: true,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith("/strategies"))
      return route.fulfill({
        json: {
          items: [
            {
              id: "strategy-1",
              name: "初始策略",
              version_id: "version-1",
              latest_version: 1,
              active: true,
            },
            {
              id: "strategy-2",
              name: "新策略",
              version_id: "version-2",
              latest_version: 2,
              active: true,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith("/automation-rules"))
      return route.fulfill({
        json: { items: [{ id: "rule-1", name: "控损规则" }] },
      })
    if (path.endsWith("/ai-optimizers") && method === "GET")
      return route.fulfill({ json: { items, next_cursor: null } })
    if (
      (path.endsWith("/ai-optimizers") && method === "POST") ||
      method === "PATCH"
    ) {
      const body = request.postDataJSON()
      writes.push(body)
      items = [
        {
          ...(items[0] ?? configuredOptimizer()),
          ...body,
          version: writes.length,
        },
      ]
      return route.fulfill({ json: items[0] })
    }
    if (path.endsWith("/daily-report"))
      return route.fulfill({
        json: { target_progress: {}, material: {}, schedule_rows: [] },
      })
    return route.fulfill({ json: { items: [], total: 0 } })
  })
  await page.goto(`/tenants/${tenant}/ai-workbench`)
  await page.getByRole("button", { name: "创建投手", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("投手名称").fill("第一位投手")
  await choose(page, "BC", "星屿 · bc-a")
  await dialog.getByRole("button", { name: "选择管理账户" }).click()
  const accounts = page.getByRole("dialog", {
    name: "选择管理账户",
    exact: true,
  })
  await accounts.getByRole("checkbox", { name: /账户一/ }).check()
  await accounts.getByRole("button", { name: "确定选择", exact: true }).click()
  await dialog.getByLabel("日消耗目标").fill("150")
  await dialog.getByLabel("目标 ROAS").fill("1.3")
  await choose(page, "绑定策略版本", "初始策略 · v1")
  await dialog.getByRole("button", { name: "选择自动规则" }).click()
  const rules = page.getByRole("dialog", { name: "选择绑定规则", exact: true })
  await rules.getByLabel("控损规则").check()
  await rules.getByRole("button", { name: "确定选择", exact: true }).click()
  await dialog.getByRole("button", { name: "创建投手", exact: true }).click()
  await expect(dialog).not.toBeVisible()
  expect(writes[0]).toMatchObject({
    name: "第一位投手",
    advertiser_ids: ["acc-1"],
    strategy_version_id: "version-1",
    rule_ids: ["rule-1"],
    timezone: "Asia/Shanghai",
    review_time: "13:00",
  })
  expect(writes[0]).not.toHaveProperty("authorization_limits")
  expect(writes[0]).not.toHaveProperty("material_config")
  await page.getByRole("button", { name: "编辑投手" }).click()
  await dialog.getByLabel("投手名称").fill("已编辑投手")
  await choose(page, "绑定策略版本", "新策略 · v2")
  await choose(page, "优化模式", "自动优化")
  await dialog.getByLabel("每日复盘时间").fill("15:30")
  await dialog.getByRole("button", { name: "保存投手" }).click()
  await expect(dialog).not.toBeVisible()
  expect(writes[1]).toMatchObject({
    name: "已编辑投手",
    strategy_version_id: "version-2",
    optimization_mode: "AUTO",
    review_time: "15:30",
  })
  await page.getByRole("button", { name: "暂停投手" }).click()
  await expect(page.getByRole("button", { name: "恢复投手" })).toBeVisible()
  await expect(page.getByTestId("ai-optimizer-select")).toHaveValue(optimizer)
  expect(writes[2]).toMatchObject({ active: false })
  await page.getByRole("button", { name: "恢复投手" }).click()
  await expect(page.getByRole("button", { name: "暂停投手" })).toBeVisible()
  expect(writes[3]).toMatchObject({ active: true })
  expect(authenticated.some((path) => path.endsWith("/accounts"))).toBeTruthy()
})

test("提案展示前后值与授权证据，决定错误可见，复盘可触发并回读状态", async ({
  page,
}) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({ json: { items: [configuredOptimizer()] } }),
  )
  const proposal = {
    id: "proposal-1",
    kind: "strategy",
    status: "PENDING",
    before: { budget: 100 },
    after: { budget: 120 },
    evidence: { evidence_id: "frozen-123" },
    authorization: { advertiser_ids: ["acc-1"], within_scope: true },
    base_optimizer_version: 1,
  }
  await page.route(
    `**/ai-optimizers/${optimizer}/proposals**`,
    async (route) => {
      expect(route.request().headers().authorization).toBe(
        "Bearer ai-workbench-test-token",
      )
      if (route.request().method() === "POST")
        return route.fulfill({
          status: 409,
          json: { detail: "版本已变更，请重新复盘" },
        })
      return route.fulfill({ json: [proposal] })
    },
  )
  let reviews = 0
  await page.route(`**/ai-optimizers/${optimizer}/reviews`, async (route) => {
    expect(route.request().headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    if (route.request().method() === "GET")
      return route.fulfill({ json: { runtime_ready: true, items: [] } })
    expect(route.request().postDataJSON().request_id).toBeTruthy()
    reviews += 1
    return route.fulfill({
      json: { task: { id: "review-1", status: "QUEUED" }, proposals: [] },
    })
  })
  await page.route(`**/ai-optimizers/${optimizer}/reviews/review-1`, (route) =>
    route.fulfill({ json: { id: "review-1", status: "COMPLETE" } }),
  )
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`,
  )
  await expect(page.getByTestId("ai-review-view")).toContainText("frozen-123")
  await expect(page.getByTestId("ai-review-view")).toContainText(
    '"budget": 120',
  )
  await expect(page.getByTestId("ai-review-view")).toContainText(
    "授权与影响范围",
  )
  await page.getByRole("button", { name: "接受", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("版本已变更")
  await page.getByRole("button", { name: "发起复盘" }).click()
  await expect(page.getByRole("status")).toContainText("COMPLETE")
  expect(reviews).toBe(1)
})

test("切换投手丢弃旧提案和编辑草稿，已暂停投手仍可管理", async ({ page }) => {
  const second = "33333333-3333-4333-8333-333333333333"
  let releaseOld: (() => void) | undefined
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          configuredOptimizer(),
          configuredOptimizer(second, "暂停投手 B", false),
        ],
      },
    }),
  )
  await page.route(
    `**/ai-optimizers/${optimizer}/proposals**`,
    async (route) => {
      await new Promise<void>((resolve) => {
        releaseOld = resolve
      })
      await route
        .fulfill({
          json: [{ id: "old", status: "PENDING", title: "旧投手建议" }],
        })
        .catch(() => {})
    },
  )
  await page.route(`**/ai-optimizers/${second}/proposals**`, (route) =>
    route.fulfill({
      json: [{ id: "new", status: "REJECTED", title: "新投手历史" }],
    }),
  )
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`,
  )
  await expect.poll(() => !!releaseOld).toBeTruthy()
  await page.getByTestId("ai-optimizer-select").selectOption(second)
  await expect(page.getByTestId("ai-review-view")).toContainText("新投手历史")
  releaseOld?.()
  await expect(page.getByTestId("ai-review-view")).not.toContainText(
    "旧投手建议",
  )
  await expect(
    page.getByRole("button", { name: "接受", exact: true }),
  ).toHaveCount(0)
  await page.getByTestId("ai-nav-overview").click()
  await expect(page.getByRole("button", { name: "恢复投手" })).toBeVisible()
})

test("排期保存保留行 ID、冻结行只读，输入不中断，TSV 和日期切换可用", async ({
  page,
}) => {
  // 此场景在次日切到空排期；固定起始日，避免真实日期变化直接命中空数据分支。
  await page.clock.setFixedTime(new Date("2026-11-12T04:00:00Z"))
  const value = configuredOptimizer()
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({ json: { items: [value] } }),
  )
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}`,
    (route) => route.fulfill({ json: value }),
  )
  const saved: Array<Record<string, unknown>> = []
  await page.route(`**/ai-optimizers/${optimizer}/daily-plans**`, (route) => {
    const request = route.request()
    expect(request.headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    const date = new URL(request.url()).searchParams.get("business_date")
    const row = {
      id: "row-frozen",
      plan_id: "plan-1",
      position: 0,
      read_only: true,
      business_date: date,
      optimizer_id: optimizer,
      title: "已执行剧",
      scheduled_time: "09:00",
      provider_connection_id: "provider-1",
      application_id: "app-1",
      external_drama_id: "0012",
      promotion_url: "",
      order_materials: true,
    }
    if (request.method() === "GET")
      return route.fulfill({
        json: { revision: 1, rows: date === "2026-11-13" ? [] : [row] },
      })
    const body = request.postDataJSON()
    saved.push(body)
    return route.fulfill({
      json: {
        revision: 2,
        rows: body.rows.map(
          (entry: Record<string, unknown>, index: number) => ({
            ...entry,
            id: entry.id ?? `new-${index}`,
            read_only: index === 0,
          }),
        ),
      },
    })
  })
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=schedule`,
  )
  await expect(page.getByTestId("plan-cell-0-1")).toHaveValue("已执行剧")
  await expect(page.getByTestId("plan-cell-0-1")).toBeDisabled()
  await expect(
    page.getByTestId("drama-plan-table").locator("tbody tr"),
  ).toHaveCount(10)
  const input = page.getByTestId("plan-cell-1-1")
  await input.pressSequentially("New drama")
  await expect(input).toHaveValue("New drama")
  await expect(input).toBeFocused()
  await page.getByTestId("plan-cell-1-0").evaluate((element) => {
    const data = new DataTransfer()
    data.setData("text/plain", "10:00\t新剧\tprovider-1\tapp-1\t\t0013\t否")
    element.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: data, bubbles: true }),
    )
  })
  await expect(input).toHaveValue("新剧")
  await page.getByRole("button", { name: "撤销" }).click()
  await expect(input).toHaveValue("New drama")
  await input.press("Tab")
  await expect(page.getByTestId("plan-cell-1-2")).toBeFocused()
  await input.press("Enter")
  await expect(page.getByTestId("plan-cell-2-1")).toBeFocused()
  await page.getByRole("button", { name: "保存剧单" }).click()
  await expect.poll(() => saved.length).toBe(1)
  const rows = saved[0].rows as Array<Record<string, unknown>>
  expect(rows[0].id).toBe("row-frozen")
  expect(
    rows.every(
      (row) =>
        !("read_only" in row) && !("plan_id" in row) && !("position" in row),
    ),
  ).toBeTruthy()
  await page.getByTestId("plan-date").fill("2026-11-13")
  await expect(page.getByTestId("plan-cell-0-1")).toHaveValue("")
  await expect(page.getByRole("button", { name: "撤销" })).toBeDisabled()
})

for (const width of [390, 1024, 1440]) {
  test(`工作台在 ${width}px 保持容器宽度`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
      route.fulfill({ json: { items: [configuredOptimizer()] } }),
    )
    await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
    await expect(page.getByTestId("ai-optimizer-config")).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    )
    expect(overflow).toBe(false)
  })
}

test("复盘历史支持失败重试、取消排队任务和服务未配置提示", async ({ page }) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({ json: { items: [configuredOptimizer()] } }),
  )
  await page.route(`**/ai-optimizers/${optimizer}/proposals**`, (route) =>
    route.fulfill({ json: [] }),
  )
  let status = "FAILED"
  const actions: string[] = []
  await page.route(`**/ai-optimizers/${optimizer}/reviews**`, async (route) => {
    const path = new URL(route.request().url()).pathname
    expect(route.request().headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    if (path.endsWith("/reviews"))
      return route.fulfill({
        json: {
          runtime_ready: true,
          items: [
            {
              id: "historical-task",
              status,
              result: { summary: "上次服务暂时不可用" },
            },
          ],
        },
      })
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({})
      actions.push(path.split("/").at(-1)!)
      status = path.endsWith("/retry") ? "PENDING" : "CANCELLED"
    }
    return route.fulfill({ json: { id: "historical-task", status } })
  })
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`,
  )
  await expect(page.getByRole("status")).toContainText("上次服务暂时不可用")
  await page.getByRole("button", { name: "重试复盘", exact: true }).click()
  await page.getByRole("button", { name: "取消复盘", exact: true }).click()
  await expect(page.getByRole("status")).toContainText("CANCELLED")
  expect(actions).toEqual(["retry", "cancel"])
  await page.route(`**/ai-optimizers/${optimizer}/reviews`, (route) =>
    route.fulfill({ json: { runtime_ready: false, items: [] } }),
  )
  await page.reload()
  await expect(
    page.getByText("复盘服务尚未配置，暂不能发起复盘。"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "发起复盘", exact: true }),
  ).toBeDisabled()
})

test("租户管理员看不到 AI 入口且直接地址不能加载工作台数据", async ({
  page,
}) => {
  let aiRequests = 0
  await page.route("**/api/users/me", (route) =>
    route.fulfill({
      json: {
        id: "44444444-4444-4444-8444-444444444444",
        username: "tenant-admin",
        is_active: true,
        is_superuser: false,
      },
    }),
  )
  await page.route("**/api/tenants/*/ai-optimizers**", (route) => {
    aiRequests++
    return route.fulfill({ json: { items: [] } })
  })
  await page.goto(`/tenants/${tenant}/ai-workbench`)
  await expect(
    page.getByText("AI 投放工作台目前仅对平台管理员开放。"),
  ).toBeVisible()
  await expect(
    page.locator(`a[href="/tenants/${tenant}/ai-workbench"]`),
  ).toHaveCount(0)
  expect(aiRequests).toBe(0)
})

test("绑定策略入口读取实际绑定版本，不加载租户策略列表", async ({ page }) => {
  const listRequests: string[] = []
  await page.route(`**/api/tenants/${tenant}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith(`/ai-optimizers/${optimizer}`))
      return route.fulfill({
        json: { strategy_version_id: "bound-v2", rule_ids: [] },
      })
    if (path.endsWith("/strategy-versions/bound-v2"))
      return route.fulfill({
        json: { id: "bound-v2", strategy_id: "bound-strategy", number: 2 },
      })
    if (path.endsWith("/strategies/bound-strategy"))
      return route.fulfill({
        json: { id: "bound-strategy", name: "实际绑定策略", latest_version: 5 },
      })
    if (path.endsWith("/strategies")) listRequests.push(path)
    return route.fulfill({
      json: { items: [{ id: "unbound", name: "其他策略" }], total: 1 },
    })
  })
  await page.goto(
    `/tenants/${tenant}/strategies?ai_optimizer_id=${optimizer}&scope=bound&version_id=stale-version`,
  )
  await expect(
    page.getByRole("heading", { name: "实际绑定策略" }),
  ).toBeVisible()
  await expect(page.getByTestId("strategy-bound-context")).toContainText("v2")
  await expect(page.getByText("其他策略", { exact: true })).toHaveCount(0)
  await expect(
    page.getByRole("link", { name: "查看绑定版本" }),
  ).toHaveAttribute("href", /version_id=bound-v2/)
  expect(listRequests).toEqual([])
})

test("绑定规则入口使用实际绑定，隐藏租户统计和其他规则", async ({ page }) => {
  const { automationBoundary } = await import("./utils/automationBoundary")
  await automationBoundary(page)
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}`,
    (route) => route.fulfill({ json: { rule_ids: ["rule-1"] } }),
  )
  await page.goto(
    `/tenants/${tenant}/automation-rules?ai_optimizer_id=${optimizer}&scope=bound&rule_ids=rule-2&tab=runs`,
  )
  await expect(page.getByRole("row", { name: /剧级止损/ })).toBeVisible()
  await expect(page.getByRole("row", { name: /跑量加预算/ })).toHaveCount(0)
  await expect(page.getByRole("region", { name: "近 7 天概览" })).toHaveCount(0)
  await expect(page.getByRole("tab", { name: "执行记录" })).toHaveCount(0)
})

test("无绑定规则时不回退到租户规则", async ({ page }) => {
  const { automationBoundary } = await import("./utils/automationBoundary")
  await automationBoundary(page)
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}`,
    (route) => route.fulfill({ json: { rule_ids: [] } }),
  )
  await page.goto(
    `/tenants/${tenant}/automation-rules?ai_optimizer_id=${optimizer}&scope=bound&rule_ids=rule-1`,
  )
  await expect(page.getByRole("textbox", { name: "规则名称" })).toBeVisible()
  await expect(page.getByRole("row", { name: /剧级止损/ })).toHaveCount(0)
})

for (const width of [390, 1440]) {
  test(`管理范围在 ${width}px 支持搜索批选、取消与 BC 联动，不铺账户 ID`, async ({
    page,
  }) => {
    const ids = Array.from({ length: 50 }, (_, n) => `account-long-id-${n}`)
    const writes: Array<Record<string, unknown>> = []
    const item = {
      ...configuredOptimizer(),
      advertiser_ids: ids,
      rule_ids: ["rule-1"],
    }
    await page.route(`**/api/tenants/${tenant}/**`, async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname
      if (request.method() === "PATCH") {
        writes.push(request.postDataJSON())
        return route.fulfill({ json: { ...item, ...request.postDataJSON() } })
      }
      if (path.endsWith("/ai-optimizers"))
        return route.fulfill({ json: { items: [item] } })
      if (path.endsWith("/bcs"))
        return route.fulfill({
          json: {
            items: [
              { bc_id: "bc-a", name: "星屿" },
              { bc_id: "bc-b", name: "另一个BC" },
            ],
          },
        })
      if (path.endsWith("/strategies"))
        return route.fulfill({ json: { items: [] } })
      if (path.endsWith("/automation-rules"))
        return route.fulfill({
          json: {
            items: Array.from({ length: 55 }, (_, n) => ({
              id: `rule-${n + 1}`,
              name: `控损规则 ${n + 1}`,
            })),
          },
        })
      if (path.endsWith("/accounts")) {
        const bc = url.searchParams.get("bc_id")!
        const q = url.searchParams.get("query")
        const tail = url.searchParams.get("cursor") === "tail"
        const account = (id: string, name: string, extra = {}) => ({
          advertiser_id: id,
          bc_id: bc,
          name,
          can_build: true,
          availability: "available",
          ...extra,
        })
        return route.fulfill({
          json: {
            items:
              bc === "bc-b"
                ? [account("b-1", "B账户")]
                : q
                  ? [account("match-1", "搜索命中")]
                  : tail
                    ? [account("tail-1", "尾页账户")]
                    : [
                        account(ids[0], "原绑定账户"),
                        account("a-2", "新增账户"),
                        account("blocked", "冲突账户", {
                          ownership_conflict: true,
                        }),
                      ],
            total: bc === "bc-b" || q ? 1 : 4,
            next_cursor: bc === "bc-a" && !q && !tail ? "tail" : null,
          },
        })
      }
      return route.fulfill({
        json: {
          items: [],
          target_progress: {},
          material: {},
          schedule_rows: [],
        },
      })
    })
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
    await expect(page.getByTestId("ai-inherited-context")).toContainText(
      "50 个管理账户",
    )
    await expect(page.getByTestId("ai-workbench-shell")).not.toContainText(
      ids[0],
    )
    await page.getByRole("button", { name: "编辑投手" }).click()
    const editor = page.getByRole("dialog", { name: "编辑投手", exact: true })
    const scope = editor
      .locator("fieldset")
      .filter({ has: page.locator("legend", { hasText: "管理范围" }) })
    await expect(
      scope.getByRole("combobox", { name: "BC", exact: true }),
    ).toBeVisible()
    await scope.getByRole("button", { name: "已选择 50 个账户" }).click()
    const accounts = page.getByRole("dialog", {
      name: "选择管理账户",
      exact: true,
    })
    await expect(
      accounts.getByRole("checkbox", { name: /原绑定账户/ }),
    ).toBeChecked()
    await accounts.getByRole("button", { name: "清空勾选" }).click()
    await accounts.getByRole("button", { name: "取消", exact: true }).click()
    await expect(
      scope.getByRole("button", { name: "已选择 50 个账户" }),
    ).toBeVisible()
    await scope.getByRole("button", { name: "已选择 50 个账户" }).click()
    await accounts.getByRole("button", { name: "清空勾选" }).click()
    await expect(
      accounts.getByRole("checkbox", { name: /冲突账户/ }),
    ).toBeDisabled()
    await accounts.getByRole("button", { name: "全选筛选结果" }).click()
    await expect(accounts.getByRole("status")).toHaveText("已选 3 个账户")
    await accounts.getByLabel("搜索账户", { exact: true }).fill("命中")
    await accounts.getByRole("button", { name: "搜索", exact: true }).click()
    await accounts.getByRole("checkbox", { name: "全选本页" }).check()
    await expect(accounts.getByRole("status")).toHaveText("已选 4 个账户")
    await expect(
      accounts.getByRole("button", { name: "确定选择" }),
    ).toBeInViewport()
    expect(
      await accounts.evaluate((el) => el.scrollWidth <= el.clientWidth),
    ).toBe(true)
    await accounts.getByRole("button", { name: "确定选择" }).click()
    await editor.getByRole("button", { name: "已绑定 1 条规则" }).click()
    const rules = page.getByRole("dialog", {
      name: "选择绑定规则",
      exact: true,
    })
    await rules.getByRole("button", { name: "清空勾选" }).click()
    await rules.getByLabel("搜索规则").fill("控损规则 5")
    await rules.getByRole("button", { name: "全选筛选结果" }).click()
    await expect(rules.getByRole("status")).toHaveText("已选 11 条规则")
    await rules.getByRole("button", { name: "取消", exact: true }).click()
    await expect(
      editor.getByRole("button", { name: "已绑定 1 条规则" }),
    ).toBeVisible()
    await editor.getByRole("button", { name: "已绑定 1 条规则" }).click()
    await rules.getByRole("button", { name: "清空勾选" }).click()
    await rules.getByRole("button", { name: "全选筛选结果" }).click()
    await expect(rules.getByRole("status")).toHaveText("已选 55 条规则")
    await rules.getByRole("button", { name: "下一页" }).click()
    await expect(rules.getByLabel("控损规则 55", { exact: true })).toBeChecked()
    await expect(
      rules.getByRole("button", { name: "确定选择" }),
    ).toBeInViewport()
    await rules.getByRole("button", { name: "确定选择" }).click()
    await editor.getByRole("button", { name: "保存投手" }).click()
    await expect(editor).not.toBeVisible()
    expect(writes[0].advertiser_ids).toEqual([
      ids[0],
      "a-2",
      "tail-1",
      "match-1",
    ])
    expect(writes[0].rule_ids).toHaveLength(55)
    await page.getByRole("button", { name: "编辑投手" }).click()
    await choose(page, "BC", "另一个BC · bc-b")
    await expect(
      scope.getByRole("button", { name: "选择管理账户" }),
    ).toBeVisible()
    await scope.getByRole("button", { name: "选择管理账户" }).click()
    await expect(
      accounts.getByRole("checkbox", { name: /B账户/ }),
    ).toBeVisible()
    await expect(
      accounts.getByRole("checkbox", { name: /原绑定账户/ }),
    ).toHaveCount(0)
    await accounts.getByRole("checkbox", { name: "全选本页" }).check()
    await accounts.getByRole("button", { name: "确定选择" }).click()
    await editor.getByRole("button", { name: "保存投手" }).click()
    await expect(editor).not.toBeVisible()
    expect(writes[1]).toMatchObject({ bc_id: "bc-b", advertiser_ids: ["b-1"] })
  })
}

test("工作台业务用语和数值易读，搭建与规则证据范围明确", async ({ page }) => {
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}/occurrences`,
    (route) => route.fulfill({ json: [] }),
  )
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...configuredOptimizer(),
            daily_spend: "1000.000000000000",
            target_roas: "1.000000000000",
          },
        ],
      },
    }),
  )
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  const overview = page.getByTestId("ai-optimizer-overview")
  await expect(overview).toContainText("人工确认")
  await expect(overview).toContainText("平衡")
  await expect(overview).toContainText("1,000")
  await expect(overview).not.toContainText(".000000")
  await expect(page.getByTestId("ai-optimizer-config")).not.toContainText(
    ".000000",
  )
  await expect(page.getByTestId("ai-optimizer-list")).not.toContainText(
    "MANUAL",
  )
  await expect(page.getByTestId("ai-nav-schedule")).toHaveText("剧单排")
  await page.getByTestId("ai-nav-execution").click()
  await expect(
    page.getByRole("heading", { name: "剧单搭建执行记录" }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "本投手自动规则运行证据" }),
  ).toBeVisible()
  await expect(
    page.getByText("当日暂无归属于当前投手的规则运行证据。"),
  ).toBeVisible()
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}/daily-report`,
    (route) =>
      route.fulfill({ status: 503, json: { message: "证据服务暂不可用" } }),
  )
  await page.reload()
  await expect(page.getByRole("alert")).toContainText("规则证据读取失败")
  await expect(
    page.getByText("当日暂无归属于当前投手的规则运行证据。"),
  ).toHaveCount(0)
})

test("知识库独立导航、搜索与详情，待确认合并到复盘并更新数量", async ({ page }) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers`, (route) => route.fulfill({ json: [] }))
  const body = "这是政策正文，仅详情展示。"
  await page.route(`**/api/tenants/${tenant}/ai-knowledge`, (route) => route.fulfill({ json: [
    { id: "knowledge-1", title: "新剧补贴", body, scope: "PUBLIC", version: 1, status: "CONFIRMED", source: "政策原文", source_verified: false },
    { id: "knowledge-2", title: "短剧变现", body: "变现链路正文", scope: "PUBLIC", version: 2, status: "CONFIRMED" },
  ] }))
  // 知识库无需先创建或选择投手。
  await page.goto(`/tenants/${tenant}/ai-workbench?view=knowledge`)
  const library = page.getByTestId("ai-knowledge-view")
  await expect(library).toContainText("新剧补贴")
  await expect(library).not.toContainText(body)
  await expect(page.getByTestId("ai-nav-pending")).toHaveCount(0)
  await expect(page.getByTestId("ai-inherited-context")).toHaveCount(0)
  await page.getByLabel("搜索知识").fill("新剧")
  await expect(library).not.toContainText("短剧变现")
  await page.getByRole("button", { name: "查看详情：新剧补贴" }).click()
  await expect(page.getByRole("dialog")).toContainText(body)
  await expect(page.getByRole("dialog")).toContainText("政策原文")
  await expect(page.getByRole("dialog")).toContainText("来源待核实")
  await page.keyboard.press("Escape")
  await page.getByLabel("搜索知识").fill("不存在")
  await expect(library).toContainText("没有匹配的知识")

  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/reviews`, (route) => route.fulfill({ json: { runtime_ready: false, items: [] } }))
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/proposals`, (route) => route.fulfill({ json: [
    { id: "proposal-1", title: "待确认调整", status: "PENDING" },
    { id: "proposal-2", title: "已处理调整", status: "ACCEPTED" },
  ] }))
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/proposals/proposal-1/accept`, (route) => route.fulfill({ json: { id: "proposal-1", title: "待确认调整", status: "ACCEPTED" } }))
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`)
  const reviews = page.getByTestId("ai-review-view")
  await expect(reviews).not.toContainText("新剧补贴")
  await page.getByRole("tab", { name: "待确认（1）" }).click()
  await expect(reviews).toContainText("待确认调整")
  await expect(reviews).not.toContainText("已处理调整")
  await page.getByRole("button", { name: "接受", exact: true }).click()
  await expect(page.getByRole("tab", { name: "待确认（0）" })).toBeVisible()
  await expect(reviews).toContainText("暂无待确认建议")
  await page.getByRole("tab", { name: "已处理", exact: true }).click()
  await expect(reviews).toContainText("已处理调整")
  await expect(reviews).toContainText("待确认调整")
})

test("发起复盘等待时给出反馈，失败可见且重试后显示任务", async ({ page }) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers**`, (route) => route.fulfill({ json: { items: [], next_cursor: null, total: 0 } }))
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/proposals`, (route) => route.fulfill({ json: [] }))
  let release = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  let attempts = 0
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/reviews`, async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: { runtime_ready: true, items: [] } })
    attempts += 1
    if (attempts === 1) {
      await gate
      return route.fulfill({ status: 422, json: { code: "ai_review_input_too_large", message: "复盘证据超过单轮容量，暂未创建任务。" } })
    }
    return route.fulfill({ json: { task: { id: "review-created", status: "PENDING" }, proposals: [] } })
  })
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`)
  await page.getByRole("button", { name: "发起复盘", exact: true }).click()
  await expect(page.getByRole("button", { name: "正在准备复盘证据…" })).toBeDisabled()
  await expect(page.getByTestId("ai-review-view")).toContainText("创建复盘任务，请稍候")
  release()
  await expect(page.getByRole("alert")).toContainText("发起复盘失败：复盘证据超过单轮容量")
  await page.getByRole("button", { name: "发起复盘", exact: true }).click()
  await expect(page.getByTestId("ai-review-view")).toContainText("review-created")
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("后台准备复盘持续轮询，准备期间不能重复发起且可以取消", async ({ page }) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers**`, (route) => route.fulfill({ json: { items: [], next_cursor: null, total: 0 } }))
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/proposals`, (route) => route.fulfill({ json: [] }))
  let polls = 0
  const task = { id: 'preparing-review', status: 'PREPARING', preparation: { total_accounts: 50, completed_accounts: 23, failed_accounts: 1 }, result: {} }
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/reviews`, (route) => route.fulfill({ json: { runtime_ready: true, items: [task] } }))
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/reviews/preparing-review`, (route) => { polls++; return route.fulfill({ json: task }) })
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`)
  await expect(page.getByTestId('ai-review-view')).toContainText('PREPARING')
  await expect(page.getByTestId('ai-review-view')).toContainText('账户数据准备 23 / 50')
  await expect(page.getByTestId('ai-review-view')).toContainText('1 个账户准备失败')
  await expect(page.getByRole('button', { name: '发起复盘', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '取消复盘', exact: true })).toBeVisible()
  await expect.poll(() => polls).toBeGreaterThan(0)
})
