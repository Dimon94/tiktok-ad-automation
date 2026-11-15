import { expect, type Page, type Request, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"
import { expectLineDrawn } from "./utils/trendChart"

// 启停自12d24fa3起直接提交冻结选择；这些详情回归不能再等待已移除的确认面板。
async function statusSubmission(page: Page) {
  await page.route("**/ad-management-requests", (route) =>
    route.fulfill({
      status: 202,
      json: {
        task_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        bc_id: BC_A,
        status: "PREPARING",
        counts: { selected: 1, prepared: 0, targets: 0 },
      },
    }),
  )
  return {
    request: page.waitForRequest((r) =>
      r.url().endsWith("/ad-management-requests"),
    ),
  }
}

async function expectPausedSubmission(page: Page, request: Promise<Request>) {
  expect((await request).postDataJSON()).toMatchObject({
    bc_id: BC_A,
    selection_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    mutation: { field: "status", mode: "set", value: "DISABLE" },
  })
  await expect(
    page.getByText("操作已提交。回读完成后本页自动更新状态，可继续操作。"),
  ).toBeVisible()
}

test.describe("ads detail", () => {
  for (const hourly of [false, true]) {
    test(`CPM 趋势切换并显示正确数值（${hourly ? "小时" : "日"}）`, async ({
      page,
    }) => {
      await adsBoundary(page)
      await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
      await page.getByRole("tab", { name: "系列", exact: true }).click()
      await page
        .getByRole("button", { name: "嘉书-总裁归来-测试", exact: true })
        .click()
      const sheet = page.getByRole("dialog")
      await sheet.getByRole("tab", { name: "趋势" }).click()
      if (hourly)
        await sheet.getByRole("button", { name: "按小时", exact: true }).click()
      const cpm = sheet.getByRole("button", {
        name: "千次展示成本 (CPM)",
        exact: true,
      })
      await cpm.click()
      await expect(cpm).toHaveAttribute("aria-pressed", "true")
      const chart = sheet.getByTestId("ads-trend-chart")
      await expectLineDrawn(chart)
      const dots = chart.locator(".recharts-line-dots circle")
      await expect(dots).toHaveCount(2)
      // SVG 的 y 值向下递增：CPM 从 2.50 降至 1.25，消耗则是 4 升至 6。
      expect(Number(await dots.first().getAttribute("cy"))).toBeLessThan(
        Number(await dots.last().getAttribute("cy")),
      )
      await dots.first().hover()
      await expect(chart.locator(".recharts-tooltip-wrapper")).toContainText(
        "2.50",
      )
    })
  }

  test("点名称打开详情，各页签有内容", async ({ page }) => {
    const trendUrls: string[] = []
    page.on("request", (r) => {
      if (r.url().includes("/ads/trend")) trendUrls.push(r.url())
    })
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告组" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByRole("tab", { name: "概览" })).toBeVisible()
    await expect(sheet.getByText("版权方")).toBeVisible()
    // 状态枚举显示中文，原值保留在悬停说明里。
    await expect(sheet.getByTitle("APPROVED")).toHaveText("已通过")
    // 页头动作文案与表格工具栏一致。
    const header = sheet.getByTestId("ads-detail-header")
    for (const name of ["启用", "暂停", "修改目标 ROAS", "修改预算"])
      await expect(
        header.getByRole("button", { name, exact: true }),
      ).toBeVisible()
    // 素材挂在广告下面，广告组详情不显示素材页签。
    await expect(sheet.getByRole("tab", { name: "素材" })).toHaveCount(0)
    await sheet.getByRole("tab", { name: "设置" }).click()
    await expect(
      sheet.getByRole("term").filter({ hasText: /^目标 ROAS$/ }),
    ).toBeVisible()
    await sheet.getByRole("tab", { name: "趋势" }).click()
    await expect(sheet.getByTestId("ads-trend-chart")).toBeVisible()
    await expectLineDrawn(sheet.getByTestId("ads-trend-chart"))
    // 对象趋势必须同时带账户与对象 ID，覆盖率才按该账户判断。
    const url = new URL(trendUrls.at(-1) ?? "")
    expect(url.searchParams.get("level")).toBe("adgroup")
    expect(url.searchParams.get("advertiser_ids")).toBe("adv-1")
    expect(url.searchParams.get("ids")).toBe("adgroup-1")
    await sheet.getByRole("tab", { name: "操作记录" }).click()
    await expect(sheet.getByText("ENABLE → DISABLE")).toBeVisible()
    // 原值缺失显示为"—"，不显示 null。
    await expect(sheet.getByText("— → 1.2")).toBeVisible()
    // 结果按真实 execution_result 显示中文，被拒绝用醒目样式。
    await expect(sheet.getByText("已执行", { exact: true })).toBeVisible()
    const rejected = sheet.getByText("被拒绝", { exact: true })
    await expect(rejected).toBeVisible()
    await expect(rejected).toHaveClass(/bg-destructive/)
  })

  test("详情内暂停提交该对象的冻结选择", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const submission = await statusSubmission(page)
    const freeze = page.waitForRequest((r) =>
      r.url().includes("/ad-selections"),
    )
    await page.getByRole("dialog").getByRole("button", { name: "暂停" }).click()
    expect((await freeze).postDataJSON()).toMatchObject({
      mode: "EXPLICIT",
      row_keys: [`${BC_A}-campaign-1`],
    })
    await expectPausedSubmission(page, submission.request)
  })

  test("广告详情显示本广告素材数据，只启停本广告的素材", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "素材" }).click()
    await expect(sheet.getByText("ep1.mp4")).toBeVisible()
    await expect(sheet.getByText("12.50")).toBeVisible()
    await sheet.getByRole("checkbox", { name: "选择 ep2.mp4" }).click()
    const submission = await statusSubmission(page)
    const freeze = page.waitForRequest((r) =>
      r.url().includes("/ads/ad/ad-1/material-selections"),
    )
    await sheet
      .getByRole("button", { name: "暂停", exact: true })
      .last()
      .click()
    const request = await freeze
    expect(request.postDataJSON()).toEqual({ ad_material_ids: ["m2"] })
    expect(new URL(request.url()).searchParams.get("advertiser_id")).toBe(
      "adv-1",
    )
    await expectPausedSubmission(page, submission.request)
  })

  for (const role of ["operator", "viewer"] as const) {
    test(`广告详情素材指标支持数值排序（${role}）`, async ({ page }) => {
      await adsBoundary(page, { role })
      const metrics = [
        ["spend", "消耗"],
        ["impressions", "展示量"],
        ["clicks", "点击量（目标页面）"],
        ["revenue_d0", "第 0 天 广告收益金额"],
        ["roas_d0", "第 0 天广告收益 ROAS"],
        ["revenue_total", "广告展示总价值"],
        ["roas_total", "广告收益 ROAS"],
      ]
      // 含小数、零、同值、NULL、空串、缺失字段和无效值，避免按显示文本排序。
      const values = ["3", "12.5", "0", null, "12.5", "", undefined, "NaN"]
      await page.route("**/ads/ad/ad-1/materials?*", (route) =>
        route.fulfill({
          json: {
            coverage: { status: "COMPLETE" },
            items: values.map((_, index) => ({
              use_ref: {
                ad_ref: {
                  tenant_id: TENANT,
                  advertiser_id: "adv-1",
                  kind: "ad",
                  remote_id: "ad-1",
                },
                platform_material_id: `video-m${index + 1}`,
                ad_material_id: `m${index + 1}`,
                material_type: "VIDEO",
              },
              name: `ep${index + 1}.mp4`,
              operation_status: "ENABLE",
              metric_buckets: [
                {
                  currency: "USD",
                  timezone: "UTC",
                  values: Object.fromEntries(
                    metrics.map(([key], metricIndex) => [
                      key,
                      values[(index + metricIndex) % values.length],
                    ]),
                  ),
                },
              ],
            })),
          },
        }),
      )
      await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
      await page.getByRole("tab", { name: "广告", exact: true }).click()
      await page
        .getByRole("button", { name: /嘉书-总裁归来/ })
        .first()
        .click()
      const sheet = page.getByRole("dialog")
      await sheet.getByRole("tab", { name: "素材" }).click()
      const names = sheet.locator("tbody tr td[title]")
      // 各指标把同组测试数值错开，确保切换指标后确实读取了对应字段。
      const orderedNames = (groups: number[][], offset: number) =>
        groups.flatMap((ids) =>
          ids
            .map(
              (id) => ((id - 1 - offset + values.length) % values.length) + 1,
            )
            .sort((a, b) => a - b)
            .map((id) => `ep${id}.mp4`),
        )
      // 同值与缺失值各自按原始素材顺序排列，错位后也保持稳定。
      const descending = [[2, 5], [1], [3], [4, 6, 7, 8]]
      const ascending = [[3], [1], [2, 5], [4, 6, 7, 8]]
      await expect(names).toHaveText(orderedNames(descending, 0))
      if (role === "operator")
        await sheet.getByRole("checkbox", { name: "选择 ep2.mp4" }).check()
      for (const [metricIndex, [key, label]] of metrics.entries()) {
        const head = sheet.getByRole("columnheader").filter({
          has: page.getByRole("button", { name: new RegExp(`^${label}，`) }),
        })
        const button = head.getByRole("button")
        // 首列已默认按消耗降序；换到其他指标时首次点击也为降序。
        if (key !== "spend") await button.click()
        await expect(head).toHaveAttribute("aria-sort", "descending")
        await expect(names).toHaveText(orderedNames(descending, metricIndex))
        await button.click()
        await expect(head).toHaveAttribute("aria-sort", "ascending")
        await expect(names).toHaveText(orderedNames(ascending, metricIndex))
        await button.click()
        await expect(names).toHaveText(orderedNames(descending, metricIndex))
      }
      if (role === "operator") {
        await expect(
          sheet.getByRole("checkbox", { name: "选择 ep2.mp4" }),
        ).toBeChecked()
        const freeze = page.waitForRequest((r) =>
          r.url().includes("/ads/ad/ad-1/material-selections"),
        )
        await sheet
          .getByRole("button", { name: "暂停", exact: true })
          .last()
          .click()
        expect((await freeze).postDataJSON()).toEqual({
          ad_material_ids: ["m2"],
        })
      } else {
        await expect(sheet.getByRole("checkbox")).toHaveCount(0)
      }
    })
  }

  test("切 BC 关闭详情", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    await expect(page.getByRole("dialog")).toBeVisible()
    await page.keyboard.press("Escape")
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    await expect(page.getByRole("dialog")).toHaveCount(0)
  })

  test("只读成员详情无操作按钮", async ({ page }) => {
    await adsBoundary(page, { role: "viewer" })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    await expect(
      page.getByRole("dialog").getByRole("tab", { name: "概览" }),
    ).toBeVisible()
    await expect(
      page.getByRole("dialog").getByRole("button", { name: "暂停" }),
    ).toHaveCount(0)
  })

  test("账户详情显示余额与时区，不请求对象详情接口", async ({ page }) => {
    const detailCalls: string[] = []
    page.on("request", (r) => {
      if (/\/ads\/(campaign|adgroup|ad|creative)\//.test(r.url()))
        detailCalls.push(r.url())
    })
    await adsBoundary(page, { balance: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("button", { name: "甲账户", exact: true }).click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByText("余额")).toBeVisible()
    await expect(sheet.getByText("12.50")).toBeVisible()
    await expect(sheet.getByText("时区")).toBeVisible()
    await expect(sheet.getByRole("tab", { name: "设置" })).toHaveCount(0)
    expect(detailCalls).toHaveLength(0)
  })

  test("剧详情点关联系列：与系列维度详情一致、可操作并可返回", async ({
    page,
  }) => {
    const queries: URL[] = []
    page.on("request", (r) => {
      const url = new URL(r.url())
      if (url.pathname.endsWith(`/tenants/${TENANT}/ads`)) queries.push(url)
    })
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "剧" }).click()
    await page.getByRole("button", { name: "总裁归来", exact: true }).click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByText("关联系列")).toBeVisible()
    // 关联系列显示系列名，ID 与账户放在下一行。
    const campaign = sheet.getByRole("button", {
      name: "嘉书-总裁归来-测试",
      exact: true,
    })
    await expect(campaign).toBeVisible()
    await expect(sheet.getByText("campaign-1 · 账户 甲账户")).toBeVisible()
    const detail = page.waitForRequest((r) =>
      r.url().includes("/ads/campaign/campaign-1"),
    )
    await campaign.click()
    await detail
    // 关联对象按账户 + ID 查询真实报表行，零消耗也能找到。
    const linked = queries.at(-1)!
    expect(linked.searchParams.get("dimension")).toBe("campaign")
    expect(linked.searchParams.get("ids")).toBe("campaign-1")
    expect(linked.searchParams.get("advertiser_id")).toBe("adv-1")
    expect(linked.searchParams.get("include_empty")).toBe("true")
    const header = sheet.getByTestId("ads-detail-header")
    for (const name of ["启用", "暂停", "修改目标 ROAS", "修改预算"])
      await expect(
        header.getByRole("button", { name, exact: true }),
      ).toBeVisible()
    await expect(sheet.getByRole("tab", { name: "设置" })).toBeVisible()
    await expect(sheet.getByRole("tab", { name: "操作记录" })).toBeVisible()
    // 返回回到剧详情。
    await sheet.getByRole("button", { name: "返回" }).click()
    await expect(sheet.getByText("关联系列")).toBeVisible()
    await expect(sheet.getByRole("button", { name: "返回" })).toHaveCount(0)
    // 再进系列详情发起暂停：冻结的是关联查询快照里的这一行，提交按系列层级执行。
    await campaign.click()
    const submission = await statusSubmission(page)
    const freeze = page.waitForRequest((r) =>
      r.url().includes("/ad-selections"),
    )
    await header.getByRole("button", { name: "暂停", exact: true }).click()
    expect((await freeze).postDataJSON()).toMatchObject({
      snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      mode: "EXPLICIT",
      row_keys: [`${BC_A}-campaign-1`],
    })
    await expectPausedSubmission(page, submission.request)
  })

  test("层级路径可多层跳转并逐层返回", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告组" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    const path = sheet.getByText("层级路径").locator("..")
    await path.getByRole("button", { name: "嘉书-总裁归来-测试" }).click()
    await expect(
      sheet.getByTestId("ads-detail-header").getByText("系列"),
    ).toBeVisible()
    await sheet
      .getByText("层级路径")
      .locator("..")
      .getByRole("button", { name: "甲账户" })
      .click()
    await expect(sheet.getByText("余额")).toBeVisible()
    await sheet.getByRole("button", { name: "返回" }).click()
    await expect(
      sheet.getByTestId("ads-detail-header").getByText("系列"),
    ).toBeVisible()
    await sheet.getByRole("button", { name: "返回" }).click()
    await expect(
      sheet.getByTestId("ads-detail-header").getByText("广告组"),
    ).toBeVisible()
    await expect(sheet.getByRole("button", { name: "返回" })).toHaveCount(0)
  })

  test("剧维度跳转到该剧关联的系列，筛选标签显示剧名", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "剧" }).click()
    const query = page.waitForRequest((r) => {
      const url = new URL(r.url())
      return (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("dimension") === "campaign"
      )
    })
    await page.getByRole("button", { name: "查看 总裁归来 的下级" }).click()
    const url = new URL((await query).url())
    expect(url.searchParams.get("ids")).toBe("campaign-1")
    // 系列可能分布在多个账户：不限定账户。
    expect(url.searchParams.get("advertiser_id")).toBeNull()
    await expect(
      page.getByRole("tab", { name: "系列", exact: true }),
    ).toHaveAttribute("data-state", "active")
    await expect(page.getByText("剧：总裁归来")).toBeVisible()
    await page.getByRole("button", { name: "取消筛选 剧：总裁归来" }).click()
    await expect(page.getByText("剧：总裁归来")).toHaveCount(0)
  })

  test("素材详情可预览成片，按视频与使用位置账户读取", async ({ page }) => {
    await adsBoundary(page)
    await page.route("**/materials/account-video-preview{,?*}", (route) =>
      route.fulfill({
        headers: { "Access-Control-Allow-Origin": "*" },
        json: {
          bc_id: BC_A,
          url: "https://video.test/preview?signature=report-preview",
          advertiser_id: "adv-1",
          video_id: "vid-1",
          width: 1080,
          height: 1920,
          duration: 12.5,
          format: "mp4",
        },
      }),
    )
    // 假的视频地址保持挂起，不让播放器因加载失败进入错误状态（与素材库用例一致）。
    await page.route("https://video.test/**", () => {})
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "素材" }).click()
    await page.getByRole("button", { name: "素材-测试", exact: true }).click()
    const sheet = page.getByRole("dialog")
    // 打开详情不自动读取，点击才读取。
    await expect(sheet.locator("video")).toHaveCount(0)
    const preview = page.waitForRequest((r) =>
      r.url().includes("/materials/account-video-preview"),
    )
    await sheet.getByRole("button", { name: "预览成片" }).click()
    const url = new URL((await preview).url())
    expect(url.searchParams.get("video_id")).toBe("vid-1")
    expect(url.searchParams.get("advertiser_ids")).toBe("adv-1")
    await expect(sheet.getByLabel("素材成片预览")).toHaveAttribute(
      "src",
      /report-preview/,
    )
    await expect(sheet.getByText(/来源账户 adv-1/)).toBeVisible()
  })

  test("广告详情的素材页签可逐个预览成片，按本广告账户读取", async ({
    page,
  }) => {
    await adsBoundary(page)
    await page.route("**/materials/account-video-preview{,?*}", (route) => {
      const url = new URL(route.request().url())
      return route.fulfill({
        headers: { "Access-Control-Allow-Origin": "*" },
        json: {
          bc_id: BC_A,
          url: `https://video.test/${url.searchParams.get("video_id")}`,
          advertiser_id: url.searchParams.get("advertiser_ids"),
          video_id: url.searchParams.get("video_id"),
          width: 1080,
          height: 1920,
          duration: 12.5,
          format: "mp4",
        },
      })
    })
    await page.route("https://video.test/**", () => {})
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "素材" }).click()
    await expect(sheet.locator("video")).toHaveCount(0)
    const preview = page.waitForRequest((r) =>
      r.url().includes("/materials/account-video-preview"),
    )
    await sheet.getByRole("button", { name: "预览 ep2.mp4" }).click()
    const url = new URL((await preview).url())
    expect(url.searchParams.get("advertiser_ids")).toBe("adv-1")
    const video = url.searchParams.get("video_id")!
    await expect(sheet.getByText("成片：ep2.mp4")).toBeVisible()
    await expect(sheet.getByLabel("素材成片预览")).toHaveAttribute(
      "src",
      new RegExp(video),
    )
    await sheet.getByRole("button", { name: "收起" }).click()
    await expect(sheet.locator("video")).toHaveCount(0)
  })

  test("素材成片地址失效时提示重新读取", async ({ page }) => {
    await adsBoundary(page)
    await page.route("**/materials/account-video-preview{,?*}", (route) =>
      route.fulfill({
        headers: { "Access-Control-Allow-Origin": "*" },
        json: {
          bc_id: BC_A,
          url: "https://video.test/expired",
          advertiser_id: "adv-1",
          video_id: "vid-1",
          width: 1080,
          height: 1920,
          duration: 12.5,
          format: "mp4",
        },
      }),
    )
    await page.route("https://video.test/**", (route) => route.abort())
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "素材" }).click()
    await page.getByRole("button", { name: "素材-测试", exact: true }).click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("button", { name: "预览成片" }).click()
    await expect(
      sheet.getByText("视频加载失败，预览地址可能已过期，请重新读取。"),
    ).toBeVisible()
    await expect(sheet.locator("video")).toHaveCount(0)
    await expect(sheet.getByRole("button", { name: "重新读取" })).toBeVisible()
  })

  for (const [timezone, zone, first] of [
    ["Asia/Hong_Kong", "Asia/Hong_Kong", "09:00"],
    ["MIXED", "UTC", "01:00"],
  ]) {
    test(`小时趋势按账户时区显示（${timezone}）`, async ({ page }) => {
      await adsBoundary(page, { trendTimezone: timezone })
      await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
      await page.getByRole("tab", { name: "系列" }).click()
      await page
        .getByRole("button", { name: /嘉书-总裁归来/ })
        .first()
        .click()
      const sheet = page.getByRole("dialog")
      await sheet.getByRole("tab", { name: "趋势" }).click()
      const hourly = page.waitForRequest((r) =>
        r.url().includes("granularity=hour"),
      )
      await sheet.getByRole("button", { name: "按小时" }).click()
      await hourly
      await expect(
        sheet.getByRole("columnheader", { name: `时间（${zone}）` }),
      ).toBeVisible()
      await expect(
        sheet.getByRole("cell", { name: first, exact: true }),
      ).toBeVisible()
    })
  }
  test("小时数据按需拉取期间提示并自动刷新", async ({ page }) => {
    await adsBoundary(page, { hourSyncingCalls: 1 })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "趋势" }).click()
    await sheet.getByRole("button", { name: "按小时" }).click()
    await expect(
      sheet.getByText("正在拉取小时数据，完成后自动更新"),
    ).toBeVisible()
    // 轮询拿到数据后提示消失、小时点出现，无需用户手动刷新。
    await expect(
      sheet.getByRole("columnheader", { name: "时间（UTC）" }),
    ).toBeVisible({ timeout: 10_000 })
    await expect(
      sheet.getByText("正在拉取小时数据，完成后自动更新"),
    ).toHaveCount(0)
  })
})

for (const complete of [true, false]) {
  test(`轮播说明保留顺序和曲目且不显示单元金额（完整=${complete}）`, async ({
    page,
  }) => {
    await adsBoundary(page)
    await page.route("**/ads/ad/ad-1/materials?*", (route) =>
      route.fulfill({
        json: {
          items: [],
          metric_scope: "CREATIVE",
          creative_rows: [
            {
              row_key: '["carousel", "adv-1", "native-2"]',
              display: { name: "轮播创意 native-2", material_type: "CAROUSEL" },
              metric_buckets: [
                {
                  currency: "USD",
                  values: { spend: "90", revenue_total: "20" },
                  availability: { spend: "PARTIAL" },
                },
              ],
              coverage: {
                status: "INCOMPLETE",
                reason: "composite_creative_not_allocated_to_images",
                assets: [
                  {
                    creative_id: "native-2",
                    assets: [
                      { platform_material_id: "second" },
                      { platform_material_id: "shared" },
                    ],
                  },
                ],
              },
            },
          ],
          carousel_linkage_complete: complete,
          carousel_creatives: [
            {
              advertiser_id: "adv-1",
              ad_id: "ad-1",
              remote_creative_id: null,
              ordered_image_ids: ["shared", "second"],
              music_id: "song-one",
            },
            {
              advertiser_id: "adv-1",
              ad_id: "ad-1",
              remote_creative_id: "native-2",
              ordered_image_ids: ["second", "shared"],
              music_id: "song-two",
            },
          ],
          frozen_creative_links: [
            {
              submission_id: "submission",
              unit_id: "unit",
              group_id: "group",
              planned_ad_id: "planned",
            },
          ],
        },
      }),
    )
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "素材" }).click()
    await expect(sheet.getByText("图片轮播", { exact: true })).toBeVisible()
    await expect(sheet.getByText(/USD · 消耗 90 · 收入 20/)).toHaveCount(1)
    await expect(sheet.getByText("部分数据", { exact: true })).toBeVisible()
    await expect(
      sheet.getByText("图片顺序：shared → second", { exact: true }),
    ).toBeVisible()
    await expect(
      sheet.getByText("图片顺序：second → shared", { exact: true }),
    ).toBeVisible()
    await expect(
      sheet.getByText("曲目：song-one", { exact: true }),
    ).toBeVisible()
    await expect(
      sheet.getByText("曲目：song-two", { exact: true }),
    ).toBeVisible()
    await expect(
      sheet.getByText("平台创意 ID：未返回", { exact: true }),
    ).toBeVisible()
    await expect(
      sheet.getByText(/每个轮播创意展示一份消耗与收入/),
    ).toBeVisible()
    await expect(sheet.getByText(/金额不分配到单张图片/)).toBeVisible()
    await expect(sheet.getByRole("columnheader", { name: "消耗" })).toHaveCount(
      0,
    )
    await expect(sheet.getByRole("checkbox")).toHaveCount(0)
    await expect(
      sheet.getByRole("link", { name: "查看冻结创意与投放任务" }),
    ).toHaveAttribute(
      "href",
      new RegExp(`/tenants/${TENANT}/build-tasks/submission`),
    )
    await expect(sheet.getByText(/冻结内容为提交时的原始意图/)).toBeVisible()
    await expect(sheet.getByText(/轮播关联不完整/)).toHaveCount(
      complete ? 0 : 1,
    )
  })
}

for (const [kind, explanation] of [
  ["UNMAPPED_CREATIVE", "创意身份尚未关联，金额已保留。"],
  ["UNATTRIBUTED", "广告组广告金额与创意金额的有符号差额，未分配给任何素材。"],
  ["CAROUSEL", "完整轮播创意金额，不拆分到单张图片。"],
] as const) {
  test(`素材 ${kind} 保留金额说明且不可执行视频操作`, async ({ page }) => {
    await adsBoundary(page, { materialViewOnly: kind })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "素材", exact: true }).click()
    await expect(
      page.getByRole("checkbox", { name: "选择 素材-测试" }),
    ).toBeDisabled()
    await page.getByRole("button", { name: "素材-测试", exact: true }).click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByText(explanation, { exact: true })).toBeVisible()
    await expect(
      sheet.getByRole("button", { name: "预览成片", exact: true }),
    ).toHaveCount(0)
    await expect(
      sheet.getByRole("button", { name: "暂停", exact: true }),
    ).toHaveCount(0)
  })
}

for (const branch of ["empty", "uses", "carousel", "complete-empty"] as const) {
  test(`响应级素材覆盖证据独立显示（${branch}）`, async ({ page }) => {
    await adsBoundary(page)
    await page.route("**/ads/ad/ad-1/materials?*", (route) =>
      route.fulfill({
        json: {
          items:
            branch === "uses"
              ? [
                  {
                    name: "current-video.mp4",
                    operation_status: "ENABLE",
                    use_ref: {
                      ad_ref: {
                        advertiser_id: "adv-1",
                        remote_id: "ad-1",
                        kind: "ad",
                      },
                      platform_material_id: "vid-current",
                      ad_material_id: "am-current",
                      material_type: "VIDEO",
                    },
                    metric_buckets: [],
                  },
                ]
              : [],
          creative_rows: [],
          coverage:
            branch === "complete-empty"
              ? { status: "COMPLETE" }
              : {
                  status: "INCOMPLETE",
                  unallocated_note: "广告组存在未归属金额，无法分配到本广告。",
                },
          carousel_creatives:
            branch === "carousel"
              ? [
                  {
                    advertiser_id: "adv-1",
                    ad_id: "ad-1",
                    remote_creative_id: "cr-carousel",
                    ordered_image_ids: ["img-1", "img-2"],
                    music_id: "song",
                  },
                ]
              : [],
          carousel_linkage_complete: true,
        },
      }),
    )
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "素材" }).click()
    if (branch === "complete-empty") {
      await expect(
        sheet.getByText("所选日期暂无创意报表记录，也无当前素材使用位置。"),
      ).toBeVisible()
      await expect(sheet.getByTestId("ad-material-coverage")).toHaveCount(0)
    } else {
      const warning = sheet.getByTestId("ad-material-coverage")
      await expect(warning).toContainText(
        "素材报表覆盖不完整，当前金额无法完整归属。",
      )
      await expect(warning).toContainText(
        "广告组存在未归属金额，无法分配到本广告。",
      )
      await expect(sheet.getByText(/消耗 0|收入 0/)).toHaveCount(0)
      if (branch === "uses")
        await expect(sheet.getByText("current-video.mp4")).toBeVisible()
      if (branch === "carousel")
        await expect(sheet.getByText("图片轮播", { exact: true })).toBeVisible()
    }
  })
}
