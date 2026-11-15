import {
  type APIRequestContext,
  expect,
  type Page,
  test,
} from "@playwright/test"

type Scenario = {
  tenant_id: string
  bc_id: string
  username: string
  password: string
  accounts: string[]
  other_tenant_id: string
  other_bc_id: string
  // 后端按 ACCEPTANCE_SCALE（compact 默认 / full）推导的期望数量。
  expected: {
    scale: string
    strategy_name: string
    campaigns: number
    adgroups: number
    ads: number
    daily_budget: number
    excluded: {
      campaign_count: number
      adgroup_count: number
      ad_count: number
    }
    excluded_budget: number
  }
}
const budgetPattern = (value: number) => new RegExp(`^${value}(?:\\.0+)?$`)
async function seed(
  request: APIRequestContext,
  partial = false,
  providerKind: "jiashu" | "wangyan" = "jiashu",
): Promise<Scenario> {
  const response = await request.post("/__acceptance__/scenario", {
    data: { partial_currency: partial, provider_kind: providerKind },
  })
  expect(response.ok()).toBeTruthy()
  return response.json()
}
async function login(page: Page, scope: Scenario) {
  await page.goto(`/tenants/${scope.tenant_id}/builds/new?bc_id=${scope.bc_id}`)
  await page.getByLabel("账号", { exact: true }).fill(scope.username)
  await page.getByLabel("密码", { exact: true }).fill(scope.password)
  await page.getByRole("button", { name: "登录", exact: true }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toBeVisible()
}
async function inputs(
  page: Page,
  scope: Scenario,
  providerKind: "jiashu" | "wangyan" = "jiashu",
) {
  await page.getByRole("combobox", { name: "版权方连接", exact: true }).click()
  await page
    .getByRole("option")
    .filter({ hasText: `${providerKind} acceptance` })
    .click()
  await page.getByRole("combobox", { name: "推广应用", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "Acceptance Minis" }).click()
  await page.getByRole("combobox", { name: "投放策略", exact: true }).click()
  await page
    .getByRole("option")
    .filter({ hasText: scope.expected.strategy_name })
    .click()
  await page
    .getByLabel("剧目名称", { exact: true })
    .fill("The Bond\nHidden Promise")
  await page
    .getByLabel("广告账户", { exact: true })
    .fill(scope.accounts.join("\n"))
}
type Progress = { ready: boolean; state: Record<string, unknown> }
async function pumpUntil(
  request: APIRequestContext,
  scope: Scenario,
  phase: "preparation" | "preview" | "execution",
  predicate: () => Promise<Progress>,
) {
  // Execution includes all target materials, source covers and their real
  // delayed verification reads. Match the backend acceptance execution budget;
  // the existing 600-second test deadline still bounds the complete scenario.
  const timeout = phase === "execution" ? 360_000 : 180_000
  const startedAt = performance.now()
  let lastProgressAt = startedAt
  let pumpCalls = 0
  let pumpDurationMs = 0
  let state = "No business state read"
  let diagnostics = "No task delivery completed"
  try {
    await expect
      .poll(
        async () => {
          const pumpStartedAt = performance.now()
          const response = await request.post("/__acceptance__/pump", {
            data: { tenant_id: scope.tenant_id },
            timeout: 120_000,
          })
          pumpCalls += 1
          pumpDurationMs += performance.now() - pumpStartedAt
          expect(response.ok()).toBeTruthy()
          diagnostics = (await response.json()).diagnostics
          const progress = await predicate()
          const nextState = JSON.stringify(progress.state)
          if (nextState !== state) {
            state = nextState
            lastProgressAt = performance.now()
          }
          return progress.ready
        },
        // pump 已连续执行最多 10 秒；与后端 drive_until 一样仅让出 100ms，
        // 避免每轮额外停工 1 秒挤占真实业务和延迟回读的执行预算。
        { timeout, intervals: [100] },
      )
      .toBeTruthy()
    console.info(
      `Acceptance ${phase} completed in ${((performance.now() - startedAt) / 1000).toFixed(1)}s (${pumpCalls} pumps, ${(pumpDurationMs / 1000).toFixed(1)}s in delivery/diagnostics): ${state}`,
    )
  } catch (error) {
    console.error(
      `Acceptance ${phase} after ${((performance.now() - startedAt) / 1000).toFixed(1)}s; last reported state change ${((performance.now() - lastProgressAt) / 1000).toFixed(1)}s ago: ${state}; task diagnostics: ${diagnostics}`,
    )
    throw error
  }
}
async function prepare(
  page: Page,
  request: APIRequestContext,
  scope: Scenario,
  providerKind: "jiashu" | "wangyan" = "jiashu",
) {
  await inputs(page, scope, providerKind)
  await page
    .getByRole("button", { name: "下一步：准备链接与素材", exact: true })
    .click()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  const draftId = /build-drafts\/([^?]+)/.exec(new URL(page.url()).pathname)![1]
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  await pumpUntil(request, scope, "preparation", async () => {
    const response = await request.get(
      `/api/tenants/${scope.tenant_id}/build-drafts/${draftId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    const data = await response.json()
    return {
      ready: data.status === "READY" || data.status === "BLOCKED",
      state: { status: data.status },
    }
  })
  await page.getByRole("button", { name: "刷新准备结果", exact: true }).click()
  return draftId
}
async function freeze(page: Page, request: APIRequestContext, scope: Scenario) {
  await page.getByRole("button", { name: "生成搭建预览", exact: true }).click()
  await expect(page).toHaveURL(/build-previews\//)
  const previewId = /build-previews\/([^?]+)/.exec(
    new URL(page.url()).pathname,
  )![1]
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  let summary: Record<string, unknown> = {}
  await pumpUntil(request, scope, "preview", async () => {
    const response = await request.get(
      `/api/tenants/${scope.tenant_id}/build-previews/${previewId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    summary = await response.json()
    return {
      ready: summary.status === "FROZEN",
      state: { status: summary.status },
    }
  })
  await page.getByRole("button", { name: "刷新预览状态", exact: true }).click()
  return { previewId, summary }
}

test("真实 API 两剧多户：按规模冻结系列/广告组/广告与预算，提交并完成后台任务", async ({
  page,
  request,
}, testInfo) => {
  const scope = await seed(request)
  await login(page, scope)
  await prepare(page, request, scope)
  const { summary } = await freeze(page, request, scope)
  expect([
    summary.campaign_count,
    summary.adgroup_count,
    summary.ad_count,
  ]).toEqual([
    scope.expected.campaigns,
    scope.expected.adgroups,
    scope.expected.ads,
  ])
  expect(summary.daily_budget_sum).toMatch(
    budgetPattern(scope.expected.daily_budget),
  )
  for (const width of [1440, 1280, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(
      page.getByRole("heading", { name: "搭建预览", exact: true }),
    ).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy()
    await page.screenshot({
      path: testInfo.outputPath(`preview-${width}.png`),
      fullPage: true,
    })
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(page).toHaveURL(/build-tasks\//)
  const submissionId = /build-tasks\/([^?]+)/.exec(
    new URL(page.url()).pathname,
  )![1]
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  let task: any
  await pumpUntil(request, scope, "execution", async () => {
    const response = await request.get(
      `/api/tenants/${scope.tenant_id}/submissions/${submissionId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    task = await response.json()
    return {
      ready: !["QUEUED", "RUNNING"].includes(task.status),
      state: {
        status: task.status,
        stage_counts: task.stage_counts,
        succeeded: task.succeeded,
        failed: task.failed,
        unknown: task.unknown,
      },
    }
  })
  expect(task.succeeded).toEqual({
    campaign_count: scope.expected.campaigns,
    adgroup_count: scope.expected.adgroups,
    ad_count: scope.expected.ads,
  })
  expect(task.daily_budget_sum).toMatch(
    budgetPattern(scope.expected.daily_budget),
  )
  await page.reload()
  await expect(
    page.getByRole("heading", {
      name: `任务 ${task.batch_short_id}`,
      exact: true,
    }),
  ).toBeVisible()
  await page.screenshot({
    path: testInfo.outputPath("task-completed-1440.png"),
    fullPage: true,
  })
})

test("真实草稿编辑使旧预览失效；丢失回执按原请求恢复并只读回看", async ({
  page,
  request,
}) => {
  const scope = await seed(request)
  await login(page, scope)
  await prepare(page, request, scope)
  const old = await freeze(page, request, scope)
  const oldURL = page.url()
  await page.getByRole("button", { name: "返回调整", exact: true }).click()
  await page.getByRole("button", { name: "返回输入", exact: true }).click()
  await expect(page.getByLabel("广告账户", { exact: true })).toHaveValue(
    scope.accounts.join("\n"),
  )
  await page
    .getByLabel("广告账户", { exact: true })
    .fill([...scope.accounts, scope.accounts[0]].join("\n"))
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  await page.goto(oldURL)
  await expect(page.getByText(/草稿已更新，当前预览已过期/)).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const obsolete = await request.get(
    `/api/tenants/${scope.tenant_id}/build-previews/${old.previewId}`,
    { headers: { Authorization: `Bearer ${token}` } },
  )
  expect((await obsolete.json()).status).toBe("OBSOLETE")
  await page.getByRole("button", { name: "返回调整", exact: true }).click()
  // 返回的是准备页；重新解析已保存草稿，再冻结新预览。
  await page
    .getByRole("button", { name: "解析并准备", exact: true })
    .click()
  const draftId = /build-drafts\/([^?]+)/.exec(new URL(page.url()).pathname)![1]
  await pumpUntil(request, scope, "preparation", async () => {
    const response = await request.get(
      `/api/tenants/${scope.tenant_id}/build-drafts/${draftId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    const draft = await response.json()
    return { ready: draft.status === "READY", state: { status: draft.status } }
  })
  await page.getByRole("button", { name: "刷新准备结果", exact: true }).click()
  const current = await freeze(page, request, scope)
  expect(current.previewId).not.toBe(old.previewId)
  expect(current.summary.campaign_count).toBe(scope.expected.campaigns)
  await request.post("/__acceptance__/lose-submission-response")
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果", exact: true }),
  ).toBeVisible()
  await page
    .getByRole("button", { name: "查询原提交结果", exact: true })
    .click()
  await expect(page).toHaveURL(/build-tasks\//)
  // 真实服务端已经记录关联任务，刷新预览应只读回看，不能再次提交。
  await page.goto(
    `/tenants/${scope.tenant_id}/build-previews/${current.previewId}?bc_id=${scope.bc_id}`,
  )
  await expect(
    page.getByRole("heading", { name: "提交配置", exact: true }),
  ).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole("heading", { name: "提交配置", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  await page.getByRole("link", { name: "返回任务详情", exact: true }).click()
  await expect(page).toHaveURL(/build-tasks\//)
  const evidence = await (
    await request.get(`/__acceptance__/evidence/${scope.tenant_id}`)
  ).json()
  const posts = evidence.requests.filter(
    (entry: { method: string; path: string }) =>
      entry.method === "POST" && entry.path.endsWith("/submit"),
  )
  expect(posts).toHaveLength(1)
  expect(posts[0].status).toBe(202)
  expect(posts[0].delivery_lost).toBe(true)
  expect(evidence.submission_count).toBe(1)
  expect(
    evidence.requests.some(
      (entry: { method: string; path: string }) =>
        entry.method === "GET" &&
        entry.path.endsWith(`/submission-requests/${posts[0].request_id}`),
    ),
  ).toBeTruthy()
  // The accepted task continues through the real worker graph after recovery.
  const submissionId = /build-tasks\/([^?]+)/.exec(
    new URL(page.url()).pathname,
  )![1]
  await pumpUntil(request, scope, "execution", async () => {
    const response = await request.get(
      `/api/tenants/${scope.tenant_id}/submissions/${submissionId}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    const task = await response.json()
    return {
      ready: task.succeeded.ad_count === scope.expected.ads,
      state: {
        status: task.status,
        stage_counts: task.stage_counts,
        succeeded: task.succeeded,
        failed: task.failed,
        unknown: task.unknown,
      },
    }
  })
})

test("真实币种差异排除两组合；普通投手访问另一租户保留登录且不泄漏数据", async ({
  page,
  request,
}, testInfo) => {
  const scope = await seed(request, true)
  await login(page, scope)
  await prepare(page, request, scope)
  const { summary } = await freeze(page, request, scope)
  expect(summary.blocked_count).toBe(scope.expected.excluded.campaign_count)
  expect([
    summary.campaign_count,
    summary.adgroup_count,
    summary.ad_count,
  ]).toEqual([
    scope.expected.campaigns - scope.expected.excluded.campaign_count,
    scope.expected.adgroups - scope.expected.excluded.adgroup_count,
    scope.expected.ads - scope.expected.excluded.ad_count,
  ])
  expect(summary.daily_budget_sum).toMatch(
    budgetPattern(scope.expected.excluded_budget),
  )
  await page.getByRole("tab", { name: "排除组合", exact: true }).click()
  await expect(
    page.getByText("账户币种与策略不一致", { exact: true }),
  ).toHaveCount(scope.expected.excluded.campaign_count)
  await page.screenshot({
    path: testInfo.outputPath("partial-excluded.png"),
    fullPage: true,
  })
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(page).toHaveURL(/build-tasks\//)
  const submissionId = /build-tasks\/([^?]+)/.exec(
    new URL(page.url()).pathname,
  )![1]
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const response = await request.get(
    `/api/tenants/${scope.tenant_id}/submissions/${submissionId}`,
    { headers: { Authorization: `Bearer ${token}` } },
  )
  const task = await response.json()
  expect(task.submitted).toEqual({
    campaign_count:
      scope.expected.campaigns - scope.expected.excluded.campaign_count,
    adgroup_count:
      scope.expected.adgroups - scope.expected.excluded.adgroup_count,
    ad_count: scope.expected.ads - scope.expected.excluded.ad_count,
  })
  expect(task.excluded).toEqual(scope.expected.excluded)
  const forbidden = await request.get(
    `/api/tenants/${scope.other_tenant_id}/submissions/${submissionId}`,
    { headers: { Authorization: `Bearer ${token}` } },
  )
  expect(forbidden.status()).toBe(403)
  await page.goto(
    `/tenants/${scope.other_tenant_id}/build-tasks?bc_id=${scope.other_bc_id}`,
  )
  await expect(
    page.getByText(/无法进入此租户|没有权限|无权访问/).first(),
  ).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
  expect(page.url()).not.toContain("/login")
})

test("真实 API 网眼入口：正式归因、完整账户编号与按规模冻结", async ({
  page,
  request,
}, testInfo) => {
  const scope = await seed(request, false, "wangyan")
  const before = await (
    await request.get(`/__acceptance__/evidence/${scope.tenant_id}`)
  ).json()
  await login(page, scope)
  await prepare(page, request, scope, "wangyan")
  const { previewId, summary } = await freeze(page, request, scope)
  expect([
    summary.campaign_count,
    summary.adgroup_count,
    summary.ad_count,
  ]).toEqual([
    scope.expected.campaigns,
    scope.expected.adgroups,
    scope.expected.ads,
  ])
  expect(summary.blocked_count).toBe(0)
  expect(summary.daily_budget_sum).toMatch(
    budgetPattern(scope.expected.daily_budget),
  )
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const response = await request.get(
    `/api/tenants/${scope.tenant_id}/build-previews/${previewId}/units?limit=50`,
    {
      headers: { Authorization: `Bearer ${token}` },
    },
  )
  expect(response.ok()).toBeTruthy()
  const units = await response.json()
  expect(units.next_cursor).toBeNull()
  expect(units.items).toHaveLength(scope.expected.campaigns)
  expect(
    [
      ...new Set(
        units.items.map((u: { advertiser_id: string }) => u.advertiser_id),
      ),
    ].sort(),
  ).toEqual([...scope.accounts].sort())
  for (const unit of units.items) {
    expect(unit.advertiser_id).toMatch(/^\d{20}$/)
    expect(unit.campaign_name).toMatch(
      /^\{b(?:71|72)\/s[1-9]\d*\/c1\}-(?:The Bond|Hidden Promise)/,
    )
  }
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(
    page.getByText(scope.accounts[0], { exact: true }).first(),
  ).toBeVisible()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  const frozenLink = page
    .getByRole("dialog")
    .getByText(/^https:\/\/www\.tiktok\.com\/minis\/acceptance\?/)
    .first()
  await expect(frozenLink).toBeVisible()
  // 查询参数顺序不属于链接合同；分别核实小程序与版权方链接编号。
  const linkUrl = new URL((await frozenLink.textContent())!)
  expect(linkUrl.searchParams.get("minis_id")).toBe("acceptance-minis")
  expect(linkUrl.searchParams.get("link_id")).toMatch(/^\d+$/)
  await page.screenshot({
    path: testInfo.outputPath("wangyan-frozen-detail.png"),
    fullPage: true,
    animations: "disabled",
  })
  const evidence = await (
    await request.get(`/__acceptance__/evidence/${scope.tenant_id}`)
  ).json()
  expect(evidence.submission_count).toBe(0)
  expect(evidence.smart_posts).toBe(before.smart_posts)
  expect(
    (evidence.sdk_calls["provider:/api/distribute_admin/promote/link/create"] ||
      0) -
      (before.sdk_calls["provider:/api/distribute_admin/promote/link/create"] ||
        0),
  ).toBe(2)
})
