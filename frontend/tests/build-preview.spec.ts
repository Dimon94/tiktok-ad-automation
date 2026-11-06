import { expect, test } from "@playwright/test"
import {
  decimalError,
  normalizeDecimal,
} from "../src/features/strategies/validation"
import { BC, buildsBoundary, P, T } from "./utils/buildsBoundary"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

test("Decimal 指数格式精确展开且保留十进制输入校验规则", () => {
  for (const [value, expected] of [
    ["0E-12", "0"],
    ["0.000E+12", "0"],
    ["1E+3", "1000"],
    ["1.2300e+2", "123"],
    ["1.23E-10", "0.000000000123"],
    ["9.0071992547409931234561234E+21", "9007199254740993123456.1234"],
    ["9007199254740993123456.123400000000", "9007199254740993123456.1234"],
    ["000100.0000", "100"],
  ]) {
    expect(normalizeDecimal(value)).toBe(expected)
  }
  for (const value of ["0", "0E-12", "1E3", "1e-12", "-1", "0.0000000000001"]) {
    expect(decimalError(value)).toBeDefined()
  }
  for (const value of [
    "0.000000000001",
    "9007199254740993123456.123400000000",
  ]) {
    expect(decimalError(value)).toBeUndefined()
  }
})

for (const [budget, expected] of [
  ["0E-12", "0"],
  ["9.0071992547409931234561234E+21", "9007199254740993123456.1234"],
  ["1.23E-10", "0.000000000123"],
]) {
  test(`预览预算将 Decimal ${budget} 显示为精确十进制金额`, async ({
    page,
  }) => {
    const api = await buildsBoundary(page)
    api.preview.daily_budget_sum = budget
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    await expect(
      page.getByText(`系列预算合计 USD ${expected}`, { exact: true }),
    ).toBeVisible()
  })
}

test("部分素材被跳过仍可搭建，详情显示冻结文件及原因", async ({ page }) => {
  const api = await buildsBoundary(page, { skippedMaterials: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("已跳过 1 条不可用素材，其余素材继续搭建。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "创建并立即启用" }),
  ).toBeEnabled()
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await expect(
    page.getByText("冻结失败视频-CL6-6.mp4", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("来源账户没有当前可用的视频副本", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("第 1 组 · 9 份素材 · 2 条 SP 创意", { exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("整部剧未匹配到素材时列出剧名，其余剧仍可提交", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.preview.blocked_count = 9
  api.preview.excluded_dramas = [
    {
      drama_id: "00000000-0000-4000-8000-0000000000e1",
      title: "Fake Heiress: Five Princes Back Her Up",
      reason_codes: ["materials_missing"],
    },
  ]
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText("1 部剧本次不会搭建")).toBeVisible()
  await expect(
    page.getByText(
      "Fake Heiress: Five Princes Back Her Up（未匹配到可用素材）",
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "创建并立即启用" }),
  ).toBeEnabled()
})

test("全部素材不可用时不提示其余素材继续搭建", async ({ page }) => {
  const api = await buildsBoundary(page, { skippedMaterials: true })
  api.preview.campaign_count = 0
  api.preview.adgroup_count = 0
  api.preview.ad_count = 0
  api.preview.daily_budget_sum = "0E-12"
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText(
      "已跳过 1 条不可用素材；当前没有可提交组合，请补充可用素材或处理其他阻断问题。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "创建并立即启用" }),
  ).toBeDisabled()
})

for (const viewer of [false, true]) {
  test(`历史提交配置跨会话只读，viewer=${viewer}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewer ? 390 : 1440, height: 900 })
    const api = await buildsBoundary(page, { viewer, blocked: true })
    api.preview.submission_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    await expect(
      page.getByRole("heading", { name: "提交配置", exact: true }),
    ).toBeVisible()
    await expect(page.getByText(/本次任务提交时的配置 · 只读/)).toBeVisible()
    await expect(page.getByRole("list", { name: "搭建步骤" })).toHaveCount(0)
    await expect(
      page.getByRole("button", { name: /创建并立即启用|返回调整/ }),
    ).toHaveCount(0)
    await expectWorkspaceLayout(page)
    await page.screenshot({
      path: testInfo.outputPath("submitted-config.png"),
      fullPage: true,
    })
    await page.getByRole("tab", { name: "输入问题", exact: true }).click()
    await expect(page.getByText("未解析剧名", { exact: true })).toBeVisible()
    await expect(page.getByRole("button", { name: "返回修正" })).toHaveCount(0)
    await page.reload()
    await expect(
      page.getByRole("heading", { name: "提交配置", exact: true }),
    ).toBeVisible()
    await page.getByRole("link", { name: "返回任务详情" }).click()
    await expect(page).toHaveURL(
      new RegExp(`/build-tasks/${api.preview.submission_id}`),
    )
    expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  })
}

test("冻结预览两剧三账户实际六Campaign，金额使用后端字符串，明细按需读取", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("6 Campaign · 18 Ad Group · 36 Ad", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("系列预算合计 USD 600", { exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter((r) => r.path.includes("/build-units/")),
  ).toHaveLength(0)
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await expect(
    page.getByText("https://example.com/real-frozen-link", { exact: true }),
  ).toBeVisible()
  await page
    .getByText("第 1 组 · 10 份素材 · 2 条 SP 创意", { exact: true })
    .click()
  await expect(
    page.getByText("真实冻结正文 1", { exact: true }).first(),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.path.includes("/build-units/") && r.path.endsWith("/groups"),
    ),
  ).toHaveLength(1)
})

test("预览摘要说明预算层级、竞价策略与素材分配口径", async ({ page }) => {
  await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("系列预算合计 USD 600", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText(/广告组按素材数量/).first()).toBeVisible()
  await expect(page.getByText(/去重素材 23 个/).first()).toBeVisible()
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(page.getByText(/每个广告 1 个素材/).first()).toBeVisible()
})
test("部分阻断组合和输入问题分别计数，不按已加载页估算", async ({ page }) => {
  await buildsBoundary(page, { blocked: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("4 Campaign · 12 Ad Group · 24 Ad", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("系列预算合计 USD 400", { exact: true }),
  ).toBeVisible()
  await page.getByRole("tab", { name: "排除组合", exact: true }).click()
  await expect(
    page.getByText("currency_mismatch", { exact: true }),
  ).toHaveCount(2)
  await page.getByRole("tab", { name: "输入问题", exact: true }).click()
  await expect(page.getByText("未解析剧名", { exact: true })).toBeVisible()
})
test("过期预览不可执行创建，保留返回调整", async ({ page }) => {
  await buildsBoundary(page, { previewStatus: "OBSOLETE" })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText(/草稿已更新，当前预览已过期/)).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "返回调整", exact: true }),
  ).toBeEnabled()
})
test("预览生成进度随服务端组合推进，冻结后移除转圈并展示最终统计", async ({
  page,
}, testInfo) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  api.preview.created_at = new Date(Date.now() - 90_000).toISOString()
  api.preview.generation_progress = {
    phase: "units",
    completed_units: 175,
    total_units: 200,
    updated_at: new Date().toISOString(),
  }
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toBeVisible()
  await expect(
    page.getByText("正在检查剧目与账户组合", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("175 / 200 个组合", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("progressbar")).toHaveAttribute(
    "aria-valuenow",
    "87.5",
  )
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await expectWorkspaceLayout(page)
    await page.screenshot({
      path: testInfo.outputPath(`preview-progress-${width}.png`),
      fullPage: true,
    })
  }
  api.preview.generation_progress.completed_units = 190
  await expect(
    page.getByText("190 / 200 个组合", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("progressbar")).toHaveAttribute(
    "aria-valuenow",
    "95",
  )
  api.preview.status = "FROZEN"
  api.preview.generation_progress.phase = "complete"
  api.preview.generation_progress.completed_units = 200
  await expect(
    page.getByText("6 Campaign · 18 Ad Group · 36 Ad", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toHaveCount(0)
  await expect(page.getByRole("progressbar")).toHaveCount(0)
})

test("预览总数未知时仅显示阶段和真实已处理数，耗时持续更新", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  const now = new Date("2026-09-17T02:00:00Z")
  // 只固定业务读取的时间，保留路由和轮询定时器正常运行。
  await page.clock.setFixedTime(now)
  api.preview.created_at = "2026-09-17T01:59:50Z"
  api.preview.generation_progress = {
    phase: "dramas",
    completed_units: 12,
    total_units: null,
    updated_at: now.toISOString(),
  }
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toBeVisible()
  await expect(
    page.getByText("正在整理剧目与素材", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("已处理 12 个组合 · 总数待确认", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("progressbar")).toHaveCount(0)
  await expect(page.getByText("已用时 10 秒", { exact: true })).toBeVisible()
  await page.clock.setFixedTime(new Date(now.getTime() + 1000))
  await expect(page.getByText("已用时 11 秒", { exact: true })).toBeVisible()
})

test("组合处理完仍在汇总时不显示100%或允许提交", async ({ page }) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  api.preview.generation_progress.phase = "digest"
  api.preview.generation_progress.completed_units = 6
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("正在汇总并冻结预览", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("6 / 6 个组合", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toBeVisible()
  await expect(page.getByRole("progressbar")).toHaveCount(0)
  await expect(page.getByText("100%", { exact: true })).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
})

test("预览长期未推进提示核实，刷新仅查询同一预览并清除恢复后的提示", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  const now = new Date("2026-09-17T02:00:00Z")
  // 只固定业务读取的时间，保留路由和轮询定时器正常运行。
  await page.clock.setFixedTime(now)
  api.preview.created_at = "2026-09-17T01:59:00Z"
  api.preview.generation_progress.updated_at = "2026-09-17T01:59:31Z"
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toBeVisible()
  await expect(page.getByText("进度暂未更新", { exact: true })).toHaveCount(0)
  await page.clock.setFixedTime(new Date(now.getTime() + 2000))
  await expect(page.getByText("进度暂未更新", { exact: true })).toBeVisible()
  api.preview.generation_progress.updated_at = "2026-09-17T02:00:02Z"
  api.preview.generation_progress.completed_units = 1
  await page.getByRole("button", { name: "重新查询进度", exact: true }).click()
  await expect(page.getByText("进度暂未更新", { exact: true })).toHaveCount(0)
  await expect(page.getByText("1 / 6 个组合", { exact: true })).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("预览网络失败保留最后进度并提供查询恢复，不误报生成失败", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  api.preview.generation_progress.phase = "units"
  api.preview.generation_progress.completed_units = 2
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText("2 / 6 个组合", { exact: true })).toBeVisible()
  let offline = true
  await page.route(`**/api/tenants/${T}/build-previews/${P}`, (route) =>
    offline ? route.abort("failed") : route.fallback(),
  )
  await page.getByRole("button", { name: "刷新预览状态" }).click()
  await expect(
    page.getByText("暂时无法获取最新进度", { exact: true }),
  ).toBeVisible({ timeout: 10000 })
  await expect(page.getByText("2 / 6 个组合", { exact: true })).toBeVisible()
  await expect(page.getByText(/预览未完成：/)).toHaveCount(0)
  offline = false
  api.preview.generation_progress.completed_units = 3
  await page.getByRole("button", { name: "重新查询进度", exact: true }).click()
  await expect(page.getByText("3 / 6 个组合", { exact: true })).toBeVisible()
  await expect(
    page.getByText("暂时无法获取最新进度", { exact: true }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("零组合汇总仍保持生成中，失败后停止转圈并展示原因", async ({ page }) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  api.preview.generation_progress.phase = "digest"
  api.preview.generation_progress.total_units = 0
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText("0 / 0 个组合", { exact: true })).toBeVisible()
  await expect(page.getByRole("progressbar")).toHaveCount(0)
  api.preview.status = "FAILED"
  api.preview.error_code = "preview_naming_outdated"
  await expect(page.getByText(/命名规则已更新，请重试生成预览/)).toBeVisible()
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
})
test("生成期间权限失效显示权限提示，不误报网络故障", async ({ page }) => {
  const api = await buildsBoundary(page, { previewStatus: "BUILDING" })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toBeVisible()
  await page.route(`**/api/tenants/${T}/build-previews/${P}`, (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "刷新预览状态" }).click()
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByText(/请检查网络/)).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
for (const width of [1440, 900, 390])
  test(`预览 ${width}px 不产生页面横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await buildsBoundary(page)
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    await expect(
      page.getByRole("heading", { name: "搭建预览", exact: true }),
    ).toBeVisible()
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
  })
test("未知生成预览只查同草稿同revision", async ({ page }) => {
  const { D } = await import("./utils/buildsBoundary"),
    api = await buildsBoundary(page, {
      previewUnknown: true,
      miniSelected: true,
    })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "生成搭建预览", exact: true }).click()
  await page.getByRole("button", { name: "查询原预览结果" }).click()
  await expect(
    page.getByRole("heading", { name: "搭建预览", exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/previews"),
    ),
  ).toHaveLength(1)
  expect(
    api.requests.some(
      (r) => r.method === "GET" && r.path.endsWith("/previews/1"),
    ),
  ).toBe(true)
})

test("默认剧目汇总来自服务端，打开单剧才读取其账户组合", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("tab", { name: "剧目汇总", exact: true }),
  ).toHaveAttribute("data-state", "active")
  expect(api.requests.filter((r) => r.path.endsWith("/units"))).toHaveLength(0)
  await page.getByRole("button", { name: "查看账户组合" }).first().click()
  await expect
    .poll(() => api.requests.filter((r) => r.path.endsWith("/units")).length)
    .toBe(1)
  expect(
    api.requests.find((r) => r.path.endsWith("/units"))!.query.get("drama_id"),
  ).toBeTruthy()
})

for (const width of [1440, 900, 390])
  test(`三步搭建 ${width}px 浏览器验收`, async ({ page }) => {
    const { pickInputs } = await import("./utils/buildsBoundary")
    await page.setViewportSize({ width, height: 900 })
    await buildsBoundary(page, { miniSelected: true })
    await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
    await pickInputs(page)
    await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(0)
    await expectWorkspaceLayout(page)
    await page.evaluate(() => window.scrollTo(0, 0))
    const screenshots = process.env.BUILD_SCREENSHOT_DIR
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-build-input-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
    await page
      .getByRole("button", { name: "下一步：准备链接与素材", exact: true })
      .click()
    await expect(
      page.getByRole("heading", { name: "准备与调整", exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "查看与调整素材" }).first(),
    ).toBeVisible()
    // 页面变短后鼠标可能停在主按钮上；视觉验收检查默认样式。
    await page.mouse.move(0, 0)
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-build-preparation-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
    await page
      .getByRole("button", { name: "生成搭建预览", exact: true })
      .click()
    await expect(
      page.getByRole("tab", { name: "剧目汇总", exact: true }),
    ).toHaveAttribute("data-state", "active")
    await expect(
      page.getByRole("button", { name: "查看账户组合" }).first(),
    ).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-build-preview-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
  })

for (const suffix of [
  "builds/new",
  `build-drafts/33333333-3333-4333-8333-333333333333`,
  `build-previews/${P}`,
  "build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
])
  test(`登录回跳保留搭建URL ${suffix}`, async ({ page }) => {
    await buildsBoundary(page, { loggedOut: true })
    const target = `/tenants/${T}/${suffix}?bc_id=${BC}`
    await page.goto(target)
    await expect(page).toHaveURL(/\/login$/)
    await page.getByLabel("账号", { exact: true }).fill("operator")
    await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
    await page.getByRole("button", { name: "登录", exact: true }).click()
    await expect(page).toHaveURL(target)
  })

test("冻结组合按50/100服务端游标分页，不逐行读取详情", async ({ page }) => {
  const api = await buildsBoundary(page, { unitCount: 131 })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(page.getByRole("button", { name: "查看冻结详情" })).toHaveCount(
    50,
  )
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect
    .poll(() => api.requests.filter((r) => r.path.endsWith("/units")).length)
    .toBe(2)
  expect(
    api.requests
      .filter((r) => r.path.endsWith("/units"))[1]
      .query.get("cursor"),
  ).toBe("50")
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.getByRole("button", { name: "查看冻结详情" })).toHaveCount(
    100,
  )
  const last = api.requests
    .filter((r) => r.path.endsWith("/units"))
    .slice(-1)[0]
  expect(last.query.get("limit")).toBe("100")
  expect(last.query.get("cursor")).toBeNull()
  expect(
    api.requests.filter((r) => r.path.includes("/build-units/")),
  ).toHaveLength(0)
})

test("明确三级数量后创建并立即启用，仅提交一次并读取真实任务摘要", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", {
      name: "创建并立即启用 6 个 Campaign / 18 个 Ad Group / 36 条 Ad",
      exact: true,
    }),
  ).toBeEnabled()
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(page).toHaveURL(
    /\/build-tasks\/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/,
  )
  await expect(
    page.getByRole("heading", { name: "任务 batch-real", exact: true }),
  ).toBeVisible()
  await expect(page.getByText("排队中", { exact: true })).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/submit"),
    ),
  ).toHaveLength(1)
  expect(
    api.requests.some(
      (r) => r.method === "GET" && r.path.includes("/submissions/"),
    ),
  ).toBe(true)
})

test("提交响应丢失后刷新只回查原请求，返回预览不能再次提交", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { submitUnknown: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  const requestId = api.requests.find((r) => r.path.endsWith("/submit"))!.body
    .request_id
  await page.reload()
  await page.getByRole("button", { name: "查询原提交结果" }).click()
  await expect(
    page.getByRole("heading", { name: "任务 batch-real", exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) =>
        r.method === "GET" &&
        r.path.endsWith(`/submission-requests/${requestId}`),
    ),
  ).toHaveLength(1)
  await page.getByRole("link", { name: "查看提交配置" }).click()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  await page.getByRole("link", { name: "返回任务详情" }).click()
  await expect(
    page.getByRole("heading", { name: "任务 batch-real", exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/submit"),
    ),
  ).toHaveLength(1)
})

for (const status of [408, 429] as const) {
  test(`${status} 提交响应仍保留原 request ledger，明确回查前不允许重复提交`, async ({
    page,
  }) => {
    const api = await buildsBoundary(page, { submitStatus: status })
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    await page.getByRole("button", { name: /创建并立即启用/ }).click()
    await expect(
      page.getByRole("button", { name: "查询原提交结果" }),
    ).toBeEnabled()
    await expect(
      page.getByRole("button", { name: /创建并立即启用/ }),
    ).toBeDisabled()
    expect(
      await page.evaluate(
        ({ T, BC, P }) =>
          sessionStorage.getItem(`build-submit:${T}:${BC}:${P}`),
        { T, BC, P },
      ),
    ).toBeTruthy()
    expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(
      1,
    )
  })
}

test("原提交404保留同request且切BC先守卫，不把未找到当未创建", async ({
  page,
}) => {
  const api = await buildsBoundary(page, {
    submitUnknown: true,
    lookup404: true,
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await page.getByRole("button", { name: "查询原提交结果" }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await expect(
    page.getByRole("heading", { name: "提交结果尚待核实" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page).toHaveURL(new RegExp(`/build-previews/${P}`))
  await page.getByRole("button", { name: "查询原提交结果" }).click()
  await expect
    .poll(
      () =>
        api.requests.filter((r) => r.path.includes("/submission-requests/"))
          .length,
    )
    .toBe(2)
  const lookups = api.requests.filter((r) =>
    r.path.includes("/submission-requests/"),
  )
  expect(lookups[0].path).toBe(lookups[1].path)
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/submit"),
    ),
  ).toHaveLength(1)
})

test("提交403保留登录并隐藏提交，viewer只读预览", async ({ page }) => {
  const api = await buildsBoundary(page, { submitDenied: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
  await expect(page.getByText(/你没有执行此操作的权限/).first()).toBeVisible()
  expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(1)
})

test("viewer和零可执行组合不能提交", async ({ page }) => {
  const api = await buildsBoundary(page, { viewer: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("heading", { name: "搭建预览", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(0)
})

test("零可执行组合保持创建禁用", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.preview.campaign_count = 0
  api.preview.adgroup_count = 0
  api.preview.ad_count = 0
  api.preview.daily_budget_sum = "0.00"
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(0)
})

test("预览后台403保留原提交核实入口并阻止新写入", async ({ page }) => {
  await buildsBoundary(page, { submitUnknown: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  await page.route(`**/api/tenants/${T}/build-previews/${P}`, (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "刷新预览状态" }).click()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
})

test("冻结预算保持长Decimal有效位并只移除小数尾零", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.preview.daily_budget_sum = "9007199254740993123456.123400000000"
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("系列预算合计 USD 9007199254740993123456.1234", {
      exact: true,
    }),
  ).toBeVisible()
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(
    page.getByText("系列日预算 USD 100", { exact: true }).first(),
  ).toBeVisible()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await expect(
    page.getByText("系列日预算 USD 100 · 目标 ROAS 1.08", { exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("BC mismatch preserves tenant identity and directs to the frozen BC", async ({
  page,
}) => {
  const { BC2 } = await import("./utils/buildsBoundary")
  await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC2}`)
  await expect(
    page.getByRole("heading", { name: "当前 BC 与预览不一致" }),
  ).toBeVisible()
  await expect(page.getByText("尚未接入租户", { exact: true })).toHaveCount(0)
  await expect(
    page.getByText("尚未连接 TikTok BC", { exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("link", { name: "切换到资源所属 BC", exact: true })
    .click()
  await expect(page).toHaveURL(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("6 Campaign · 18 Ad Group · 36 Ad", { exact: true }),
  ).toBeVisible()
})

test("旧版未完成预览显示中文重建提示并禁止提交", async ({ page }) => {
  const api = await buildsBoundary(page, {
    previewStatus: "FAILED",
    previewError: "preview_naming_outdated",
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText(/命名规则已更新，请重试生成预览/)).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("失败预览可在原页重试且不创建广告", async ({ page }) => {
  const api = await buildsBoundary(page, {
    previewStatus: "FAILED",
    previewError: "naming_context_missing",
  })
  const retriedId = "99999999-9999-4999-8999-999999999999"
  await page.route(`**/build-previews/${retriedId}`, (route) =>
    route.fulfill({
      json: {
        ...api.preview,
        preview_id: retriedId,
        draft_revision: 2,
        status: "BUILDING",
        error_code: null,
      },
    }),
  )
  let retries = 0
  await page.route("**/build-drafts/*/previews", async (route) => {
    expect(route.request().method()).toBe("POST")
    expect(route.request().postDataJSON()).toEqual({ expected_revision: 1 })
    retries += 1
    api.preview.status = "BUILDING"
    api.preview.error_code = null
    await route.fulfill({ status: 202, json: { preview_id: retriedId } })
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: "重试生成预览", exact: true }).click()
  await expect(
    page.getByRole("status", { name: "正在生成搭建预览" }),
  ).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`/build-previews/${retriedId}`))
  expect(retries).toBe(1)
  expect(api.requests.some((r) => r.path.endsWith("/submit"))).toBe(false)
})

test("V2 大分组展示准确总数并分页读取完整广告创意", async ({ page }) => {
  await buildsBoundary(page)
  const calls: Array<{ cursor: string | null; limit: string | null }> = []
  await page.route(
    /\/build-units\/[^/]+\/groups(?:\/[^?]*)?(?:\?.*)?$/,
    async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith("/ads")) {
        calls.push({
          cursor: url.searchParams.get("cursor"),
          limit: url.searchParams.get("limit"),
        })
        const start = url.searchParams.get("cursor") ? 3 : 1
        return route.fulfill({
          json: {
            total: 2000,
            next_cursor: start === 1 ? "second-page" : null,
            items: [start, start + 1].map((n) => ({
              ad_id: `ad-${n}`,
              base_ad_no: n,
              creative_no: 1,
              name: `完整广告 ${n}`,
              copy_id: "copy",
              text: `正文 ${n}`,
              cta_option_ids: [],
              material_ids: [`${n}-1`, `${n}-2`, `${n}-3`],
              creative_units: [
                {
                  unit_id: `creative-${n}`,
                  kind: "CAROUSEL",
                  generation: 1,
                  content_revision: 1,
                  music_revision: 1,
                  content_digest: "digest",
                  music_id: "fixed-song",
                  music_snapshot: {},
                  members: [1, 2, 3].map((i) => ({
                    material_id: `${n}-${i}`,
                    file_name: `广告${n}-图片${i}.jpg`,
                    content_digest: "digest",
                  })),
                },
              ],
            })),
          },
        })
      }
      return route.fulfill({
        json: {
          total: 1,
          next_cursor: null,
          items: [
            {
              contract_version: 2,
              group_id: "large-group",
              group_no: 1,
              name: "大分组",
              material_count: 10000,
              creative_unit_count: 2000,
              ad_count: 2000,
              sample_material_ids: ["sample-1", "sample-2"],
              material_ids: null,
              ads: null,
            },
          ],
        },
      })
    },
  )
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await page
    .getByText("第 1 组 · 10000 份素材 · 2000 个完整创意 · 2000 条 SP 创意", {
      exact: true,
    })
    .click()
  await expect(
    page.getByText("素材 ID 样例（最多 6 项）：sample-1、sample-2", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(page.getByText("广告1-图片3.jpg", { exact: true })).toBeVisible()
  await expect(
    page
      .getByText("创意 1 · 轮播，3 张图片，配乐 fixed-song", { exact: true })
      .first(),
  ).toBeVisible()
  await page.getByRole("button", { name: "下一页广告", exact: true }).click()
  await expect(page.getByText("广告3-图片3.jpg", { exact: true })).toBeVisible()
  await expect(page.getByText("广告1-图片3.jpg", { exact: true })).toHaveCount(
    0,
  )
  expect(calls).toEqual([
    { cursor: null, limit: "2" },
    { cursor: "second-page", limit: "2" },
  ])
  await page.getByRole("button", { name: "上一页广告", exact: true }).click()
  await expect(page.getByText("广告1-图片3.jpg", { exact: true })).toBeVisible()
})

test("续建预览列出复用和新增范围，并直接进入原任务", async ({ page }) => {
  const api = await buildsBoundary(page)
  const source = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  Object.assign(api.preview, {
    can_refresh_continuation: true,
    continuation_source_ids: [source],
    continuation_unit_count: 1,
    reused_campaign_count: 1,
    reused_adgroup_count: 0,
    reused_ad_count: 0,
    new_campaign_count: 0,
    new_adgroup_count: 1,
    new_ad_count: 1,
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("复用 1 个系列 · 0 个广告组 · 0 条广告", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("新建 0 个系列 · 1 个广告组 · 1 条广告", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "查看原搭建任务", exact: true }),
  ).toHaveAttribute("href", `/tenants/${T}/build-tasks/${source}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: /^继续搭建并立即启用/ }),
  ).toBeEnabled()
  await expect(
    page.getByText("仅新增对象将创建并立即启用，复用对象保持原状态。", {
      exact: true,
    }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("冻结旧预览可以显式刷新续建计划，无需新建草稿", async ({ page }) => {
  const api = await buildsBoundary(page)
  Object.assign(api.preview, {
    can_refresh_continuation: true,
    continuation_source_ids: [],
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: "刷新续建预览", exact: true }).click()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.method === "POST" && r.path.endsWith("/previews"),
        ).length,
    )
    .toBe(1)
  const request = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/previews"),
  )!
  expect(request.body).toEqual({
    expected_revision: 1,
    refresh_continuation: true,
  })
  expect(api.requests.some((r) => r.path.endsWith("/submit"))).toBe(false)
})

test("原任务结果待核实的组合不能继续提交", async ({ page }) => {
  const api = await buildsBoundary(page)
  Object.assign(api.preview, {
    can_refresh_continuation: true,
    continuation_blocked_count: 1,
    continuation_unit_count: 0,
    campaign_count: 0,
    adgroup_count: 0,
    ad_count: 0,
    blocked_count: 1,
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText(
      "有 1 个组合暂不能续建，请查看排除原因并处理原任务后刷新预览。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /^创建并立即启用/ }),
  ).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "刷新续建预览", exact: true }),
  ).toBeEnabled()
})
