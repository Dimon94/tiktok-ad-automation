import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

test("区分冻结汇总和页指标时点，并如实显示投影等待", async ({ page }) => {
  await adsBoundary(page, { projectionStatus: "BUILDING" })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByTestId("report-query-times")).toContainText(
    "顺序和汇总固定于",
  )
  await expect(
    page.getByText("素材数据正在整理，尚未完成的金额暂不显示。"),
  ).toBeVisible()
})

test("首次素材投影未完成不能显示没有匹配广告", async ({ page }) => {
  await adsBoundary(page, {
    projectionStatus: "BUILDING",
    emptyRows: true,
    total: 0,
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "素材", exact: true }).click()
  await expect(
    page.getByText("素材数据正在整理，完成后显示查询结果。", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("没有匹配的广告", { exact: true })).toHaveCount(0)
})

test("空闲首屏自动重建快照的间隔不少于60秒", async ({ page }) => {
  await page.clock.install()
  const options = { lastSuccess: "2026-10-01T05:00:00Z" }
  await adsBoundary(page, options)
  let queries = 0
  page.on("request", (request) => {
    if (request.url().includes(`/api/tenants/${TENANT}/ads?`)) queries += 1
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await expect(
    page.getByText("嘉书-总裁归来-测试", { exact: true }),
  ).toBeVisible()
  const initial = queries
  options.lastSuccess = "2026-10-01T05:01:00Z"
  await page.clock.fastForward(30_000)
  await expect(page.getByText("有新数据，点击加载")).toBeVisible()
  expect(queries).toBe(initial)
  await page.clock.fastForward(31_000)
  await expect.poll(() => queries).toBe(initial + 1)
})

test("隐藏页停止轮询，勾选期间不自动替换快照", async ({ page }) => {
  await page.clock.install()
  const options = { lastSuccess: "2026-10-01T05:00:00Z" }
  await adsBoundary(page, options)
  let queries = 0
  let polls = 0
  page.on("request", (request) => {
    if (request.url().includes(`/api/tenants/${TENANT}/ads?`)) queries += 1
    if (request.url().includes("/ad-sync-status?")) polls += 1
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("checkbox", { name: "选择 甲账户" }).check()
  const initial = queries
  options.lastSuccess = "2026-10-01T05:01:00Z"
  await page.clock.fastForward(61_000)
  await expect(page.getByText("有新数据，点击加载")).toBeVisible()
  expect(queries).toBe(initial)
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    })
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    })
    document.dispatchEvent(new Event("visibilitychange"))
  })
  const beforeHidden = polls
  await page.clock.fastForward(120_000)
  expect(polls).toBe(beforeHidden)
  expect(queries).toBe(initial)
})

test("报表状态提示且不再拼接 TikTok 后台链接", async ({ page }) => {
  await adsBoundary(page)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
  // 状态收敛成一行结论，不再逐账户铺开，也不使用红色覆盖告警框。
  await expect(page.getByText(/数据更新到 13:00（账户本地）/)).toBeVisible()
  await expect(page.getByText("数据覆盖", { exact: true })).toHaveCount(0)
  await expect(page.getByText(/^\d{19} ·/)).toHaveCount(0)
  await expect(page.getByRole("link", { name: "TikTok 后台" })).toHaveCount(0)
})

test("翻页沿用同一报表快照", async ({ page }) => {
  await adsBoundary(page, { paged: true })
  const requests: string[] = []
  page.on("request", (request) => {
    if (request.url().includes(`/api/tenants/${TENANT}/ads?`))
      requests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await expect(page.getByRole("button", { name: "下一页" })).toBeEnabled()
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(
    page.getByText("嘉书-第二页-测试", { exact: true }),
  ).toBeVisible()
  const pageTwo = new URL(requests.at(-1) ?? "http://localhost")
  expect(pageTwo.searchParams.get("cursor")).toBe("cursor-1")
  expect(pageTwo.searchParams.get("snapshot_id")).toBe(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  )
})

test("详情沿用快照并读取 operation 状态", async ({ page }) => {
  await adsBoundary(page)
  const detailRequests: string[] = []
  page.on("request", (request) => {
    if (request.url().includes("/ads/campaign/campaign-1?"))
      detailRequests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  // 点名称打开详情；账户行没有对象引用，因此在系列维度验证。
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page
    .getByRole("button", { name: "嘉书-总裁归来-测试", exact: true })
    .click()
  await expect(page.getByText("PAUSED", { exact: true })).toBeVisible()
  expect(
    new URL(detailRequests.at(-1) ?? "http://localhost").searchParams.get(
      "snapshot_id",
    ),
  ).toBe("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
})

test("混币种桶不静默合并", async ({ page }) => {
  await adsBoundary(page, { mixedBuckets: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  // 汇总卡片保留单行写法；表格单元格按币种逐行显示。
  await expect(
    page.getByText("USD 100.00 / CNY 100.00", { exact: true }),
  ).toHaveCount(1)
  const spendCell = page.locator('tbody td[data-metric="spend"]').first()
  await expect(spendCell.getByText("USD 100.00", { exact: true })).toBeVisible()
  await expect(spendCell.getByText("CNY 100.00", { exact: true })).toBeVisible()
  await expect(page.getByText("多口径", { exact: true })).toHaveCount(0)
})

test("新回收列排序、账户本地更新时间与缺失日期", async ({ page }) => {
  await adsBoundary(page, { incomplete: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByText(/数据更新到 13:00（账户本地）/)).toBeVisible()
  await expect(
    page.getByText("1 个账户有日期缺失", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("完整数据自 2026-10-05 起", { exact: true }),
  ).toBeVisible()
  // 表头文字与下方数值左边缘对齐，不能被排序按钮内边距额外推开。
  // 量的是首个文字节点的位置，按钮外框本身总是贴着表头内边距。
  const textLeft = (selector: string) =>
    page
      .locator(selector)
      .first()
      .evaluate((element) => {
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
        const range = document.createRange()
        range.selectNodeContents(walker.nextNode() as Text)
        return range.getBoundingClientRect().left
      })
  const headLeft = await textLeft('thead th[data-metric="spend"]')
  const cellLeft = await textLeft('tbody td[data-metric="spend"]')
  expect(Math.abs(headLeft - cellLeft)).toBeLessThan(1)
  await expect(page.getByText(/以下日期数据不完整/)).toHaveCount(0)
  await page.getByRole("button", { name: "同步明细", exact: true }).click()
  await expect(page.getByText(/以下日期数据不完整：2026-10-04/)).toBeVisible()
  await page.keyboard.press("Escape")
  const request = page.waitForRequest(
    (r) => new URL(r.url()).searchParams.get("sort_by") === "roas_d30",
  )
  await page
    .getByRole("button", {
      name: "第 29 天广告收入 ROAS，点击排序",
      exact: true,
    })
    .click()
  expect(
    new URL((await request).url()).searchParams.get("sort_direction"),
  ).toBe("desc")
  await page.getByRole("tab", { name: "素材", exact: true }).click()
  // 素材报表不提供的天数列直接隐藏；其余无值统一显示“—”，原因放在悬停说明。
  await expect(page.locator('td[title="平台未提供该指标"]')).toHaveCount(0)
  await expect(
    page.locator('td[title="暂无数据，同步完成后显示"]').first(),
  ).toHaveText("—")
  await expect(
    page.getByRole("cell", { name: "平台未提供", exact: true }),
  ).toHaveCount(0)
})

for (const [status, message] of [
  ["SYNC_DISABLED", "报表同步未开启"],
  ["API_BINDING_REQUIRED", "该 BC 未接入 API，无法同步报表"],
] as const) {
  test(`刷新显示 ${status} 且使用当前筛选账户`, async ({ page }) => {
    await adsBoundary(page, { refreshStatus: status })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByRole("checkbox", { name: "选择 甲账户" }),
    ).toBeVisible()
    const request = page.waitForRequest((r) =>
      r.url().includes("/ad-sync-refresh"),
    )
    await page.getByRole("button", { name: "刷新", exact: true }).click()
    expect((await request).postDataJSON()).toEqual({ advertiser_ids: [] })
    await expect(page.getByText(message, { exact: true })).toBeVisible()
  })
}

test("账户对账提示保留广告汇总", async ({ page }) => {
  await adsBoundary(page, { reconciliation: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "账户", exact: true }).click()
  await expect(page.getByLabel("账户报表：USD 101.00")).toBeVisible()
  await expect(page.getByRole("cell", { name: /100.00/ }).first()).toBeVisible()
})

test("同步发布后清除第二页快照和选择并显示放缓", async ({ page }) => {
  await page.clock.install()
  const options = {
    paged: true,
    slowed: true,
    lastSuccess: "2026-10-05T05:00:00Z",
  }
  await adsBoundary(page, options)
  const requests: string[] = []
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith(`/tenants/${TENANT}/ads`))
      requests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(
    page.getByText("嘉书-第二页-测试", { exact: true }),
  ).toBeVisible()
  await page.getByRole("checkbox").last().check()
  await expect(page.getByText("已选 1 条", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "刷新", exact: true }).click()
  await expect(page.getByText("已加入刷新队列")).toBeVisible()
  options.lastSuccess = "2026-10-05T05:01:00Z"
  await page.clock.fastForward(31_000)
  await expect(page.getByText(/数据更新到 13:01/)).toBeVisible()
  await expect(page.getByText("嘉书-第二页-测试", { exact: true })).toHaveCount(
    0,
  )
  await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
  const latest = new URL(requests.at(-1)!)
  expect(latest.searchParams.get("snapshot_id")).toBeNull()
  expect(latest.searchParams.get("cursor")).toBeNull()
})

test("额度放缓状态可见", async ({ page }) => {
  await adsBoundary(page, { slowed: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByText("历史补数暂缓", { exact: true })).toBeVisible()
})

test("账户余额单独显示币种和共享资金池", async ({ page }) => {
  await adsBoundary(page, { balance: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "账户", exact: true }).click()
  await expect(
    page.getByRole("cell", { name: "CNY 12.50（共享资金池）", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "余额，点击排序", exact: true }),
  ).toBeVisible()
})

test("后台发布不打断第二页选择，用户可选择加载最新数据", async ({ page }) => {
  await page.clock.install()
  const options = { paged: true, lastSuccess: "2026-10-05T05:00:00Z" }
  await adsBoundary(page, options)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page.getByRole("button", { name: "下一页" }).click()
  await page.getByRole("checkbox").last().check()
  options.lastSuccess = "2026-10-05T05:01:00Z"
  await page.clock.fastForward(31_000)
  await expect(
    page.getByText("嘉书-第二页-测试", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("已选 1 条", { exact: true })).toBeVisible()
  await page
    .getByRole("button", { name: "有新数据，点击加载", exact: true })
    .click()
  await expect(page.getByText("嘉书-第二页-测试", { exact: true })).toHaveCount(
    0,
  )
  await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
})

test("空闲页面后台发布自动更新汇总且同版本不循环刷新", async ({ page }) => {
  await page.clock.install()
  const options = { lastSuccess: "2026-10-05T05:00:00Z", reportSpend: "100" }
  await adsBoundary(page, options)
  const requests: string[] = []
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith(`/tenants/${TENANT}/ads`))
      requests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByTestId("summary-spend")).toContainText("100.00")
  const before = requests.length
  options.lastSuccess = "2026-10-05T05:01:00Z"
  options.reportSpend = "240"
  await page.clock.fastForward(31_000)
  await expect(page.getByTestId("summary-spend")).toContainText("100.00")
  await page.clock.fastForward(31_000)
  await expect(page.getByTestId("summary-spend")).toContainText("240.00")
  expect(requests.length).toBe(before + 1)
  expect(new URL(requests.at(-1)!).searchParams.get("snapshot_id")).toBeNull()
  await page.clock.fastForward(31_000)
  expect(requests.length).toBe(before + 1)
  await expect(
    page.getByRole("button", { name: "有新数据，点击加载" }),
  ).toHaveCount(0)
})

test("第一页勾选保留汇总，取消选择后自动加载已发布数据", async ({ page }) => {
  await page.clock.install()
  const options = { lastSuccess: "2026-10-05T05:00:00Z", reportSpend: "100" }
  await adsBoundary(page, options)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  const checkbox = page.getByRole("checkbox", { name: "选择 甲账户" })
  await checkbox.check()
  options.lastSuccess = "2026-10-05T05:01:00Z"
  options.reportSpend = "240"
  await page.clock.fastForward(31_000)
  await expect(checkbox).toBeChecked()
  await expect(page.getByTestId("summary-spend")).toContainText("100.00")
  await expect(
    page.getByRole("button", { name: "有新数据，点击加载" }),
  ).toBeVisible()
  await checkbox.uncheck()
  await page.clock.fastForward(31_000)
  await expect(page.getByTestId("summary-spend")).toContainText("240.00")
})

test("覆盖时间取最旧账户，混时区明确本地日期及超时账户数", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-18T06:00:00Z") })
  await adsBoundary(page, {
    syncAccounts: [
      {
        advertiser_id: "adv-1",
        timezone: "Asia/Hong_Kong",
        local_today: "2026-10-18",
        last_success_at: "2026-10-18T05:59:00Z",
      },
      {
        advertiser_id: "adv-2",
        timezone: "Etc/GMT",
        local_today: "2026-10-18",
        last_success_at: "2026-10-18T05:30:00Z",
      },
      {
        advertiser_id: "adv-3",
        timezone: "Etc/GMT+8",
        local_today: "2026-10-17",
        last_success_at: null,
      },
    ],
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  const oldest = await page.evaluate(() =>
    new Date("2026-10-18T05:30:00Z").toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }),
  )
  await expect(
    page.getByText(`最旧有效数据更新到 ${oldest}（浏览器本地，账户时区不同）`, {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByText("1 个账户数据超过 20 分钟", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("查询日期按各账户本地日", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("1 个账户当地尚未到所选日期", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("1 个账户同步中", { exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "同步明细" }).click()
  await expect(
    page.getByText("账户当地今天为 2026-10-17，尚未到查询日期"),
  ).toBeVisible()
})

for (const busy of ["详情", "第二页"] as const) {
  test(`后台发布保留正在查看的${busy}，返回空闲页面后自动更新`, async ({
    page,
  }) => {
    await page.clock.install()
    const options = {
      paged: true,
      lastSuccess: "2026-10-05T05:00:00Z",
      reportSpend: "100",
    }
    await adsBoundary(page, options)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    if (busy === "详情")
      await page
        .getByRole("button", { name: "嘉书-总裁归来-测试", exact: true })
        .click()
    else await page.getByRole("button", { name: "下一页" }).click()
    // 先确认已进入“正在查看”的状态，再推进时钟；否则慢机器上第二页/详情
    // 尚未加载完成，页面仍算空闲，会提前自动更新。
    if (busy === "详情") await expect(page.getByRole("dialog")).toBeVisible()
    else
      await expect(
        page.getByText("嘉书-第二页-测试", { exact: true }),
      ).toBeVisible()
    options.lastSuccess = "2026-10-05T05:01:00Z"
    options.reportSpend = "240"
    await page.clock.fastForward(31_000)
    await expect(page.getByTestId("summary-spend")).toContainText("100.00")
    if (busy === "详情") {
      await expect(page.getByRole("dialog")).toBeVisible()
      await page.keyboard.press("Escape")
    } else {
      await expect(
        page.getByText("嘉书-第二页-测试", { exact: true }),
      ).toBeVisible()
      await page.getByRole("button", { name: "上一页" }).click()
    }
    await page.clock.fastForward(31_000)
    await expect(page.getByTestId("summary-spend")).toContainText("240.00")
  })
}

test("下钻指定账户后覆盖状态仅统计所选账户", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-18T06:00:00Z") })
  await adsBoundary(page, {
    syncAccounts: [
      {
        advertiser_id: "adv-1",
        timezone: "Asia/Hong_Kong",
        local_today: "2026-10-18",
        last_success_at: "2026-10-18T05:59:00Z",
      },
      {
        advertiser_id: "adv-2",
        timezone: "Asia/Hong_Kong",
        local_today: "2026-10-18",
        last_success_at: "2026-10-18T05:30:00Z",
      },
    ],
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(
    page.getByText("最旧有效数据更新到 13:30（账户本地）", { exact: true }),
  ).toBeVisible()
  await page
    .getByRole("button", { name: "查看 甲账户 的下级", exact: true })
    .click()
  await expect(
    page.getByText("最旧有效数据更新到 13:59（账户本地）", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("1 个账户数据超过 20 分钟", { exact: true }),
  ).toHaveCount(0)
})

test("接口版本不变时数据超过20分钟仍自动显示超时提示", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-18T06:00:00Z") })
  await adsBoundary(page, { lastSuccess: "2026-10-18T05:40:01Z" })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(
    page.getByText("最旧有效数据更新到 13:40（账户本地）", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("1 个账户数据超过 20 分钟", { exact: true }),
  ).toHaveCount(0)
  await page.clock.fastForward(31_000)
  await expect(
    page.getByText("1 个账户数据超过 20 分钟", { exact: true }),
  ).toBeVisible()
})

for (const missing of [false, true]) {
  test(`未来账户保留空事实，部分覆盖${missing ? "仍提示真实日期缺口" : "不误报同步失败"}`, async ({
    page,
  }) => {
    await page.clock.install({ time: new Date("2026-10-18T06:00:00Z") })
    await adsBoundary(page, {
      syncAccounts: [
        {
          advertiser_id: "adv-1",
          timezone: "Asia/Hong_Kong",
          local_today: "2026-10-18",
          last_success_at: "2026-10-18T05:59:00Z",
          incomplete_dates: missing ? ["2026-10-18"] : [],
        },
        {
          advertiser_id: "adv-2",
          timezone: "Etc/GMT+8",
          local_today: "2026-10-17",
          last_success_at: null,
        },
      ],
    })
    await page.route("**/api/**", async (route) => {
      if (
        !new URL(route.request().url()).pathname.endsWith(
          `/tenants/${TENANT}/ads`,
        )
      )
        return route.fallback()
      return route.fulfill({
        json: {
          snapshot: {
            snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            expires_at: "2099-01-01T00:00:00Z",
          },
          items: [],
          total: 0,
          summary: {
            buckets: [
              {
                currency: "USD",
                values: { spend: null },
                availability: { spend: "MISSING" },
              },
            ],
          },
          coverage: { status: "INCOMPLETE" },
        },
      })
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByText("1 个账户当地尚未到所选日期", { exact: true }),
    ).toBeVisible()
    await expect(
      page.getByText("当前范围部分数据仍在同步", { exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByText("部分数据读取失败，请刷新重试", { exact: true }),
    ).toHaveCount(0)
    await expect(page.getByTestId("summary-spend")).toContainText("—")
    await expect(page.getByTestId("summary-spend")).not.toContainText("0.00")
    await expect(
      page.getByText("1 个账户有日期缺失", { exact: true }),
    ).toHaveCount(missing ? 1 : 0)
  })
}

const degradedHealthCases = [
  {
    name: "覆盖尚未完成不能借分钟水位消音",
    health: { issues: ["COVERAGE_PENDING"] },
    labels: ["该 BC 目录与素材关联尚未核验完成"],
  },
  {
    name: "覆盖过期保留未确认告警",
    health: { issues: ["COVERAGE_STALE"] },
    labels: ["该 BC 目录与素材关联核验已过期，完整性待确认"],
  },
  {
    name: "覆盖查询失败与同步新鲜度分开",
    health: { issues: ["COVERAGE_QUERY_FAILED"] },
    labels: ["该 BC 目录与素材关联核验失败，完整性待确认"],
  },
  {
    name: "具体异常计数与数据时间分开着色",
    health: {
      stale_ad_accounts: 2,
      stale_material_accounts: 3,
      missing_directory_ads: 167,
      unlinked_material_posts: 9,
      pending_actions: 1,
      issues: ["MONITOR_STALE"],
    },
    labels: [
      "该 BC 广告 2 个账户超时",
      "该 BC 素材 3 个账户超时",
      "该 BC 目录 167 条待补齐或刷新",
      "该 BC 素材 9 项待关联",
      "该 BC 动作 1 项积压",
      "该 BC 健康监测已过期，当前同步状态待确认",
    ],
  },
  {
    name: "零计数保留筛选过期告警",
    health: { issues: ["ACCOUNT_SCREEN_STALE"] },
    labels: ["该 BC 账户筛选已过期，活跃账户覆盖待确认"],
  },
  {
    name: "零计数保留监测尚未完成告警",
    health: { issues: ["MONITOR_PENDING"] },
    labels: ["该 BC 健康监测尚未完成，当前同步状态待确认"],
  },
  {
    name: "零计数保留监测失败告警",
    health: { issues: ["MONITOR_QUERY_FAILED"] },
    labels: ["该 BC 健康监测读取失败，当前同步状态待确认"],
  },
  {
    name: "未知降级原因不能消音",
    health: { issues: ["FUTURE_ISSUE"] },
    labels: ["该 BC 同步健康异常，原因待确认"],
  },
  {
    name: "缺失降级原因不能消音",
    health: { issues: [] },
    labels: ["该 BC 同步健康异常，原因待确认"],
  },
]

for (const scenario of degradedHealthCases) {
  test(`同步健康：${scenario.name}`, async ({ page }) => {
    await page.clock.install({ time: new Date("2026-10-18T06:00:00Z") })
    await adsBoundary(page)
    await page.route("**/ad-sync-status?*", async (route) =>
      route.fulfill({
        json: {
          accounts: [
            {
              advertiser_id: "adv-1",
              timezone: "Asia/Hong_Kong",
              local_today: "2026-10-18",
              last_success_at: "2026-10-18T05:59:00Z",
              incomplete_dates: [],
              failed_dates: [],
            },
          ],
          plan: { enabled: true, disabled_reason: null, slowed: false },
          health: {
            status: "DEGRADED",
            stale_ad_accounts: 0,
            stale_material_accounts: 0,
            missing_directory_ads: 0,
            unlinked_material_posts: 0,
            pending_actions: 0,
            ...scenario.health,
          },
        },
      }),
    )
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    for (const text of scenario.labels) {
      const warning = page.getByText(text, { exact: true })
      await expect(warning).toBeVisible()
      await expect(warning).toHaveClass(/text-destructive/)
    }
    await expect(
      page.getByText("最旧有效数据更新到 13:59（账户本地）", { exact: true }),
    ).toHaveClass(/text-muted-foreground/)
    await expect(
      page.getByText("该 BC 实时同步存在延迟或关联缺口", { exact: true }),
    ).toHaveCount(0)
  })
}

test("同步健康：独立覆盖观测时间不会随页面时钟续期", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-18T06:00:00Z") })
  await adsBoundary(page)
  await page.route("**/ad-sync-status?*", async (route) =>
    route.fulfill({
      json: {
        accounts: [],
        plan: { enabled: true, disabled_reason: null, slowed: false },
        health: {
          status: "HEALTHY",
          observed_at: "2026-10-18T06:00:00Z",
          coverage_status: "COMPLETE",
          coverage_observed_at: "2026-10-18T05:59:00Z",
          coverage_completed_at: "2026-10-18T05:59:30Z",
          issues: [],
        },
      },
    }),
  )
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  const observation = page.getByText(/目录与素材关联核验于/)
  await expect(observation).toBeVisible()
  const original = await observation.textContent()
  await page.clock.fastForward(5 * 60 * 1000)
  await expect(
    page.getByText("该 BC 目录与素材关联核验已过期，完整性待确认", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(observation).toHaveText(original!)
})

test("慢查询显示加载状态、保留结果提示并在完成后恢复", async ({ page }) => {
  await adsBoundary(page)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByRole("button", { name: "应用筛选" })).toBeEnabled()
  await expect(page.getByText(/数据更新到 13:00（账户本地）/)).toBeVisible()
  await expect(
    page.getByText("正在查询广告报表…", { exact: true }),
  ).toHaveCount(0)
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(`**/api/tenants/${TENANT}/ads?**`, async (route) => {
    if (decodeURIComponent(route.request().url()).includes("慢查询"))
      await pending
    await route.fallback()
  })
  await page.getByRole("textbox", { name: "搜索广告" }).fill("慢查询")
  await page.getByRole("button", { name: "应用筛选" }).click()
  await expect(page.getByRole("button", { name: "应用筛选" })).toBeDisabled()
  await expect(
    page.getByText("正在查询广告报表…", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("下方暂时显示上一次查询结果，完成后自动更新。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByText("查询耗时较长，仍在加载，请稍候…", { exact: true }),
  ).toBeVisible({ timeout: 12000 })
  release()
  await expect(page.getByRole("button", { name: "应用筛选" })).toBeEnabled()
  await expect(
    page.getByText("查询耗时较长，仍在加载，请稍候…", { exact: true }),
  ).toHaveCount(0)
})

test("空结果和首次加载提示显示在表头下方的列表内", async ({ page }) => {
  await adsBoundary(page)
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(`**/api/tenants/${TENANT}/ads?**`, async (route) => {
    await pending
    await route.fulfill({
      json: {
        snapshot: {
          snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          filters: {},
          publication_versions: {},
        },
        items: [],
        total: 0,
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  const table = page.locator('table[data-slot="table"]')
  await expect(
    table.locator("tbody").getByText("正在加载当前报表…"),
  ).toBeVisible()
  release()
  const empty = table.locator("tbody td").filter({ hasText: "没有匹配的广告" })
  await expect(empty).toBeVisible()
  const header = await table.locator("thead").boundingBox()
  const cell = await empty.boundingBox()
  expect(Math.abs(cell!.y - (header!.y + header!.height))).toBeLessThan(2)
})
