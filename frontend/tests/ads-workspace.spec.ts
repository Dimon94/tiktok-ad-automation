import { expect, test } from "@playwright/test"
import {
  ADVERTISER,
  adsBoundary,
  BC_A,
  BC_B,
  TENANT,
} from "./utils/adsBoundary"

test.describe("ads workspace", () => {
  test("分页显示页码，切换每页条数保留筛选并清除旧游标", async ({ page }) => {
    await adsBoundary(page, { paged: true, total: 75 })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("textbox", { name: "搜索广告" }).fill("嘉书")
    await page.getByRole("button", { name: "应用筛选" }).click()
    await expect(
      page.getByText("共 75 条 · 第 1 / 2 页", { exact: true }),
    ).toBeVisible()
    const pager = page.locator("div.border-t").filter({
      has: page.getByRole("combobox", { name: "每页条数" }),
    })
    await pager.screenshot({
      path: test.info().outputPath("report-pagination-desktop.png"),
    })
    await expect(
      page.getByRole("button", { name: "上一页", exact: true }),
    ).toBeDisabled()
    await page.getByRole("button", { name: "下一页", exact: true }).click()
    await expect(
      page.getByText("共 75 条 · 第 2 / 2 页", { exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "下一页", exact: true }),
    ).toBeDisabled()
    await page.getByRole("combobox", { name: "每页条数" }).click()
    const request = page.waitForRequest((value) => {
      const url = new URL(value.url())
      return (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("limit") === "100"
      )
    })
    await page.getByRole("option", { name: "100 条", exact: true }).click()
    const url = new URL((await request).url())
    expect(url.searchParams.get("cursor")).toBeNull()
    expect(url.searchParams.get("snapshot_id")).toBeNull()
    expect(url.searchParams.get("query")).toBe("嘉书")
    expect(url.searchParams.get("bc_id")).toBe(BC_A)
    await expect(
      page.getByText("共 75 条 · 第 1 / 1 页", { exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "上一页", exact: true }),
    ).toBeDisabled()
    await expect(
      page.getByRole("button", { name: "下一页", exact: true }),
    ).toBeDisabled()
    await page.getByRole("button", { name: "应用筛选" }).click()
    await expect(
      page.getByRole("combobox", { name: "每页条数" }),
    ).toContainText("100 条")
  })

  test("窄屏分页条数、页码和按钮均在视口内", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 640 })
    await adsBoundary(page, { total: 75, paged: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByText("共 75 条 · 第 1 / 2 页", { exact: true }),
    ).toBeVisible()
    const pager = page.locator("div.border-t").filter({
      has: page.getByRole("combobox", { name: "每页条数" }),
    })
    await pager.scrollIntoViewIfNeeded()
    for (const control of [
      page.getByRole("combobox", { name: "每页条数" }),
      page.getByText("共 75 条 · 第 1 / 2 页", { exact: true }),
      page.getByRole("button", { name: "上一页", exact: true }),
      page.getByRole("button", { name: "下一页", exact: true }),
    ]) {
      const box = await control.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.x).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width).toBeLessThanOrEqual(390)
    }
    await pager.screenshot({
      path: test.info().outputPath("report-pagination-mobile.png"),
    })
  })

  test("外部广告六维切换保留筛选", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
    await expect(page.getByRole("combobox", { name: "当前 BC" })).toContainText(
      "甲 BC",
    )
    await page.getByRole("textbox", { name: "搜索广告" }).fill("嘉书 总裁归来")
    await page.getByRole("button", { name: "应用筛选" }).click()
    await page.getByRole("tab", { name: "剧" }).click()
    // 剧名单元格里还有跳转到关联系列的按钮，按名称按钮定位。
    await expect(
      page.getByRole("button", { name: "总裁归来", exact: true }),
    ).toBeVisible()
    for (const column of [
      "消耗",
      "广告展示总价值",
      "广告收益 ROAS",
      "第 0 天 广告收益金额",
      "第 0 天广告收益 ROAS",
      "第 2 天广告收益 ROAS",
      "第 6 天广告收益 ROAS",
      "第 13 天广告收益 ROAS",
      "第 29 天广告收入 ROAS",
      "展示量",
      "点击量（目标页面）",
      "点击率（目标页面）",
      "千次展示成本 (CPM)",
      "平均点击成本（目标页面）",
      "广告展示事件总数",
      "单次广告展示事件成本",
    ]) {
      await expect(
        page.getByRole("columnheader", {
          // 默认按消耗降序，消耗列表头显示当前方向。
          name: column === "消耗" ? "消耗，当前降序" : `${column}，点击排序`,
          exact: true,
        }),
      ).toBeVisible()
    }
    await expect(
      page.getByRole("columnheader", { name: "来源", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("cell", { name: "1.20", exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("cell", { name: "外部广告", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("cell", { name: "目录未同步", exact: true }),
    ).toHaveCount(0)
  })

  test("切BC忽略迟到响应", async ({ page }) => {
    await adsBoundary(page, { delayedA: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("combobox", { name: "当前 BC" })).toContainText(
      "甲 BC",
    )
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    await expect(page.getByText("乙账户", { exact: true })).toBeVisible()
    await page.waitForTimeout(900)
    await expect(page.getByText("甲账户", { exact: true })).toHaveCount(0)
  })

  test("按账户到素材逐级跳转并固定账户", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)

    const navigate = async (name: string, dimension: string, id: string) => {
      const request = page.waitForRequest((value) => {
        const url = new URL(value.url())
        return (
          value.method() === "GET" &&
          url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
          url.searchParams.get("dimension") === dimension
        )
      })
      await page
        .locator("tbody tr")
        .first()
        .getByRole("button", { name: `查看 ${name} 的下级`, exact: true })
        .click()
      const url = new URL((await request).url())
      expect(url.searchParams.get("ids")).toBe(id)
      expect(url.searchParams.get("advertiser_id")).toBe(ADVERTISER)
      const labels: Record<string, string> = {
        campaign: "系列",
        adgroup: "广告组",
        ad: "广告",
        material: "素材",
      }
      await expect(
        page.getByRole("tab", { name: labels[dimension], exact: true }),
      ).toHaveAttribute("data-state", "active")
    }

    await navigate("甲账户", "campaign", ADVERTISER)
    await navigate("嘉书-总裁归来-测试", "adgroup", "campaign-1")
    await navigate("嘉书-总裁归来-测试", "ad", "adgroup-1")
    // 广告是最末级：素材在广告详情里看本广告数据，不再跳到素材维度。
    await expect(
      page
        .locator("tbody tr")
        .first()
        .getByRole("button", { name: /的下级$/ }),
    ).toHaveCount(0)
  })

  test("点击上级路径按该上级筛选", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    const row = page.locator("tbody tr").first()
    await expect(row.getByText("ad-1", { exact: true })).toBeVisible()
    const request = page.waitForRequest((value) => {
      const url = new URL(value.url())
      return (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("dimension") === "campaign"
      )
    })
    await row.getByRole("button", { name: "甲账户", exact: true }).click()
    const url = new URL((await request).url())
    expect(url.searchParams.get("ids")).toBe(ADVERTISER)
    expect(url.searchParams.get("advertiser_id")).toBe(ADVERTISER)
    await expect(
      page.getByRole("tab", { name: "系列", exact: true }),
    ).toHaveAttribute("data-state", "active")
  })

  test("相同筛选应用也清空选择", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("checkbox", { name: "选择 甲账户" }).check()
    await expect(page.getByText("已选 1 条", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: "应用筛选" }).click()
    await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
  })

  test("从第二页切 BC 不携带旧快照", async ({ page }) => {
    await adsBoundary(page, { paged: true })
    const bcBRequests: string[] = []
    page.on("request", (request) => {
      const url = new URL(request.url())
      if (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("bc_id") === BC_B
      )
        bcBRequests.push(request.url())
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page.getByRole("button", { name: "下一页" }).click()
    await expect(
      page.getByText("嘉书-第二页-测试", { exact: true }),
    ).toBeVisible()
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    // 切 BC 会重新进入工作台，回到默认账户维度。
    await expect(page.getByText("乙账户", { exact: true })).toBeVisible()
    const bcB = new URL(bcBRequests.at(-1) ?? "http://localhost")
    expect(bcB.searchParams.get("cursor")).toBeNull()
    expect(bcB.searchParams.get("snapshot_id")).toBeNull()
  })

  test("默认账户维度，横向滚动后勾选框和名称列仍固定", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByRole("tab", { name: "账户", selected: true }),
    ).toBeVisible()
    const container = page.locator('[data-slot="table-container"]')
    // 新增趋势区域后表格可能在视口下方，滚动到视图内再测量
    await container.scrollIntoViewIfNeeded()
    const nameCell = page.locator('[data-sticky="name"]').first()
    const before = await nameCell.boundingBox()
    await container.evaluate((el) => el.scrollTo({ left: 800 }))
    const after = await nameCell.boundingBox()
    expect(after?.x).toBe(before?.x)
    await expect(
      page.locator('[data-sticky="select"]').first(),
    ).toBeInViewport()
  })

  test("不再出现详情与 TikTok 后台列", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("columnheader", { name: "详情" })).toHaveCount(
      0,
    )
    await expect(page.getByRole("link", { name: "TikTok 后台" })).toHaveCount(0)
    await expect(
      page.getByRole("columnheader", { name: "数据状态" }),
    ).toHaveCount(0)
  })

  test("素材维度显示创意日报提供的收入窗口", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "素材" }).click()
    await expect(
      page.getByRole("columnheader", { name: /第 2 天/ }),
    ).toBeVisible()
    await expect(
      page.getByRole("columnheader", { name: /广告展示总价值/ }),
    ).toBeVisible()
  })

  test("状态显示中文徽标，原始枚举放在悬停说明", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByText("正常", { exact: true })).toHaveAttribute(
      "title",
      "STATUS_ENABLE",
    )
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await expect(page.getByText("已暂停", { exact: true })).toBeVisible()
    // 系列维度带预算配置列，账户维度带余额列。
    await expect(
      page.getByRole("columnheader", { name: /^预算/ }),
    ).toBeVisible()
    await expect(
      page.getByRole("cell", { name: "50.00", exact: true }),
    ).toBeVisible()
  })

  test("列为部分同步时照常显示数值并给出标记", async ({ page }) => {
    await adsBoundary(page, { partialSpend: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    const row = page.locator("tbody tr").first()
    await expect(row.getByText("100.00", { exact: true })).toBeVisible()
    await expect(row.getByLabel("部分账户数据未同步完整")).toBeVisible()
    // 行覆盖完整时名称旁不出现不完整警示。
    await expect(row.getByLabel("数据不完整")).toHaveCount(0)
  })

  test("回收窗口未结束的天数列表头标灰", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    // 默认日期为今天，第 N 天窗口全部未结束；总值列不受影响。
    await expect(
      page.locator('th[data-recovery-open="true"]').first(),
    ).toBeVisible()
    await expect(page.locator('th[data-metric="roas_d30"]')).toHaveAttribute(
      "data-recovery-open",
      "true",
    )
    await expect(
      page.locator('th[data-metric="roas_total"]'),
    ).not.toHaveAttribute("data-recovery-open", "true")
  })

  test("汇总卡片使用官方名并提示不完整账户", async ({ page }) => {
    await adsBoundary(page, { incomplete: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    // 顶部只放 6 张紧凑小卡片，第 N 天 ROAS 不进汇总。
    for (const [key, name] of [
      ["spend", "消耗"],
      ["revenue_total", "广告展示总价值"],
      ["roas_total", "广告收益 ROAS"],
      ["impressions", "展示量"],
      ["ctr", "点击率（目标页面）"],
      ["cpm", "千次展示成本"],
    ])
      await expect(page.getByTestId(`summary-${key}`)).toContainText(name)
    await expect(page.getByTestId("summary-roas_d0")).toHaveCount(0)
    await expect(page.getByText("2 个账户数据不完整")).toBeVisible()
  })

  test("切换维度加载中不显示上一维度的行", async ({ page }) => {
    await adsBoundary(page, { delayedDimension: "material" })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "剧", exact: true }).click()
    await expect(
      page.locator("tbody tr").first().getByText("总裁归来", { exact: true }),
    ).toBeVisible()
    await page.getByRole("tab", { name: "素材", exact: true }).click()
    // 素材响应尚未返回：只显示加载提示，不能残留剧维度的行。
    await expect(page.getByText("正在加载当前报表…")).toBeVisible()
    // 加载提示已归入tbody；唯一行是提示，不能保留上一维度的业务行。
    await expect(page.locator("tbody tr")).toHaveCount(1)
    await expect(page.locator("tbody")).toContainText("正在加载当前报表…")
    await expect(
      page.locator("tbody").getByText("总裁归来", { exact: true }),
    ).toHaveCount(0)
  })

  test("页面不再显示账户趋势，也不请求趋势", async ({ page }) => {
    const trendCalls = { count: 0 }
    await adsBoundary(page, { trendCalls })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.locator("tbody tr").first()).toBeVisible()
    await expect(page.getByRole("button", { name: /账户趋势/ })).toHaveCount(0)
    expect(trendCalls.count).toBe(0)
  })

  test("下钻后显示筛选标签，可逐级取消", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page
      .locator("tbody tr")
      .first()
      .getByRole("button", { name: "查看 甲账户 的下级", exact: true })
      .click()
    await expect(page.getByText("账户：甲账户", { exact: true })).toBeVisible()
    await page
      .locator("tbody tr")
      .first()
      .getByRole("button", {
        name: "查看 嘉书-总裁归来-测试 的下级",
        exact: true,
      })
      .click()
    await expect(
      page.getByText("系列：嘉书-总裁归来-测试", { exact: true }),
    ).toBeVisible()
    // 取消系列只保留账户筛选。
    const parentCleared = page.waitForRequest((value) => {
      const url = new URL(value.url())
      return (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("ids") === null &&
        url.searchParams.get("advertiser_id") === ADVERTISER
      )
    })
    await page
      .getByRole("button", { name: "取消筛选 系列：嘉书-总裁归来-测试" })
      .click()
    await parentCleared
    await expect(page.getByText("账户：甲账户", { exact: true })).toBeVisible()
    // 取消账户回到全部账户。
    const allCleared = page.waitForRequest((value) => {
      const url = new URL(value.url())
      return (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("advertiser_id") === null
      )
    })
    await page.getByRole("button", { name: "取消筛选 账户：甲账户" }).click()
    await allCleared
    await expect(page.getByText("当前筛选")).toHaveCount(0)
  })

  test("导出生成后自动下载 CSV", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.locator("tbody tr").first()).toBeVisible()
    const download = page.waitForEvent("download")
    await page.getByRole("button", { name: "导出", exact: true }).click()
    expect((await download).suggestedFilename()).toMatch(
      /^广告报表-账户-\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$/,
    )
  })

  test("广告的上级路径标出层级", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    const row = page.locator("tbody tr").first()
    for (const kind of ["账户", "系列", "广告组"])
      await expect(row.getByText(kind, { exact: true })).toBeVisible()
  })

  test("隐藏列在刷新后保持", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("button", { name: "列设置" }).click()
    await page
      .getByRole("menuitemcheckbox", { name: "千次展示成本 (CPM)" })
      .click()
    await page.keyboard.press("Escape")
    await expect(
      page.getByRole("columnheader", { name: /千次展示成本/ }),
    ).toHaveCount(0)

    // 验证 localStorage 键包含用户 ID
    const storageKey = await page.evaluate(() => {
      const keys = Object.keys(localStorage).filter((k) =>
        k.startsWith("ads-columns:"),
      )
      return keys.length > 0 ? keys[0] : null
    })
    expect(storageKey).toContain(TENANT)
    expect(storageKey).toContain("44444444-4444-4444-8444-444444444444") // adsBoundary /api/users/me 返回的 user.id

    await page.reload()
    await expect(
      page.getByRole("columnheader", { name: /千次展示成本/ }),
    ).toHaveCount(0)
  })

  test("默认只查有数据对象，勾选后查询包含无数据对象", async ({ page }) => {
    await adsBoundary(page)
    const first = page.waitForRequest((r) =>
      new URL(r.url()).pathname.endsWith(`/tenants/${TENANT}/ads`),
    )
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    // 默认口径不传参数，由后端按“只列有数据对象”处理。
    expect(
      new URL((await first).url()).searchParams.get("include_empty"),
    ).toBeNull()
    await page.getByRole("checkbox", { name: "显示无数据对象" }).click()
    const request = page.waitForRequest(
      (r) =>
        new URL(r.url()).pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        new URL(r.url()).searchParams.get("include_empty") === "true",
    )
    await page.getByRole("button", { name: "应用筛选" }).click()
    const url = new URL((await request).url())
    expect(url.searchParams.get("snapshot_id")).toBeNull()
  })

  test("保存筛选提交字典列键", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("columnheader", { name: /消耗/ })).toBeVisible()
    const request = page.waitForRequest(
      (r) =>
        new URL(r.url()).pathname.endsWith("/report-views") &&
        r.method() === "POST",
    )
    await page.getByRole("button", { name: "保存筛选" }).click()
    const body = (await request).postDataJSON()
    expect(body.columns).toContain("roas_d0")
    expect(body.columns).not.toContain("status_raw")
  })

  test("表头固定，垂直滚动后位置不变", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    const container = page.locator('[data-slot="table-container"]')
    await container.scrollIntoViewIfNeeded()
    const header = page.locator('[data-sticky="name"]').first()
    const before = (await header.boundingBox())?.y
    await container.evaluate((el) => el.scrollTo({ top: 200 }))
    const after = (await header.boundingBox())?.y
    expect(after).toBe(before)
    // 分页按钮在滚动区域外可见
    await expect(page.getByRole("button", { name: "下一页" })).toBeVisible()
  })

  test("矮屏下表格滚动区仍保留最小高度", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 600 })
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    const container = page.locator('[data-slot="table-container"]')
    await expect(container).toBeVisible()
    // 28rem = 448px；视口高度减去页头后不足时不能把表格压成一两行。
    expect(
      await container.evaluate((el) => getComputedStyle(el).minHeight),
    ).toBe("448px")
  })

  test("首屏加载中不显示空结果提示", async ({ page }) => {
    await adsBoundary(page, { delayedA: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByText("正在加载当前报表…", { exact: true }),
    ).toBeVisible()
    // 用即时计数而不是自动重试断言：加载结束后提示自然消失，重试会掩盖问题。
    expect(
      await page.getByText("没有匹配的广告", { exact: true }).count(),
    ).toBe(0)
    await expect(page.getByText("甲账户", { exact: true })).toBeVisible()
  })
})
