import { expect, type Page, test } from "@playwright/test"
import { automationBoundary, TENANT } from "./utils/automationBoundary"

const BC = "90071992547409931111"
const PROVIDER = "22222222-2222-4222-8222-222222222222"
const values = {
  spend: "2000",
  revenue: "1600",
  provider_share: "128",
  net_revenue: "1472",
  rebate: "120",
  business_revenue: "1592",
  compensation: "100",
  subsidy_comic: "800",
  subsidy_other: "500",
  subsidy_existing: "400",
  platform_roas: "0.8",
  business_roas: "0.796",
  scenario_revenue: "1592",
  scenario_roas: "0.796",
}

async function boundary(
  page: Page,
  role: "tenant_admin" | "viewer" | "operator" = "tenant_admin",
) {
  await automationBoundary(page, { role })
  const queries: Record<string, unknown>[] = []
  const rates: Record<string, unknown>[] = []
  const evidence: Record<string, unknown>[] = []
  await page.route("**/api/**/business/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    const body =
      route.request().method() === "GET" ? {} : route.request().postDataJSON()
    const reply = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data })
    if (path.endsWith("/options"))
      return reply({
        end_date: "2026-11-03",
        accounts: [
          { id: "acc-1", name: "投放账户", currency: "USD", timezone: "UTC" },
        ],
        providers: [{ id: PROVIDER, name: "嘉书" }],
      })
    if (path.endsWith("/query")) {
      queries.push(body)
      const scenario = body.scenario ?? "base"
      const extra =
        scenario === "comic"
          ? 900
          : scenario === "other"
            ? 600
            : scenario === "existing"
              ? 500
              : 0
      const output = {
        ...values,
        scenario_revenue: String(1592 + extra),
        scenario_roas: String((1592 + extra) / 2000),
      }
      return reply({
        rows: [
          {
            key: "drama-1",
            name:
              body.dimension === "provider"
                ? "嘉书"
                : body.dimension === "campaign"
                  ? "测试系列"
                  : "测试剧",
            provider_name: "嘉书",
            provider_id: PROVIDER,
            advertiser_id: "acc-1",
            campaign_id: "campaign-1",
            drama_id: PROVIDER,
            values: output,
          },
        ],
        totals: output,
        total: 1,
        run_ids: [PROVIDER],
        currency: "USD",
        currencies: ["USD"],
        end_date: "2026-11-03",
        updated_at: "2026-11-04T01:00:00Z",
        issues: [],
        missing_dates: [],
        pending: false,
      })
    }
    if (path.endsWith("/rates")) {
      if (route.request().method() === "POST") {
        rates.push(body)
        return reply({ id: PROVIDER, revision: 1, ...body })
      }
      return reply([])
    }
    if (path.endsWith("/evidence")) {
      if (route.request().method() === "PUT") {
        evidence.push(body)
        return reply({ saved: true })
      }
      return reply({ bid: null, eligible: null, source: "" })
    }
    if (path.endsWith("/rebuild")) return reply({ run_ids: [PROVIDER] }, 202)
    if (path.endsWith("/runs")) return reply([])
    if (path.endsWith("/exports"))
      return reply(
        { id: PROVIDER, status: "QUEUED", expires_at: "2026-11-05T00:00:00Z" },
        202,
      )
    if (path.endsWith(`/exports/${PROVIDER}`))
      return reply({
        id: PROVIDER,
        status: "COMPLETE",
        expires_at: "2026-11-05T00:00:00Z",
      })
    if (path.endsWith("/download"))
      return route.fulfill({
        body: "名称,消耗\n测试剧,2000",
        contentType: "text/csv",
      })
    return reply({ detail: path }, 404)
  })
  return { queries, rates, evidence }
}

test("经营看板五卡与三个补贴情景独立展示", async ({ page }) => {
  const calls = await boundary(page)
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await expect(
    page.getByRole("heading", { name: "经营看板", exact: true }),
  ).toBeVisible()
  for (const label of [
    "平台消耗",
    "平台收入",
    "平台 ROAS",
    "经营收入",
    "经营 ROAS",
  ])
    await expect(
      page.getByTestId("business-cards").getByText(label, { exact: true }),
    ).toBeVisible()
  for (const label of ["新开漫剧预计", "新开其他剧预计", "在投闭环预计"])
    await expect(page.getByRole("columnheader", { name: label })).toBeVisible()
  await page.getByRole("combobox", { name: "经营口径" }).click()
  await page.getByRole("option", { name: "含预计：新开漫剧" }).click()
  await expect(page.getByTestId("business-cards")).toContainText("2,492.00")
  expect(calls.queries.at(-1)?.scenario).toBe("comic")
  await expect(page.getByRole("listbox")).toHaveCount(0)
  await page.screenshot({
    path: test.info().outputPath("dashboard-desktop.png"),
    fullPage: true,
  })
  await page.getByRole("radio", { name: "广告系列", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "测试系列", exact: true }),
  ).toBeVisible()
  await page.getByRole("radio", { name: "版权方", exact: true }).click()
  await expect.poll(() => calls.queries.at(-1)?.dimension).toBe("provider")
})

test("返点默认0且按生效日保存，小屏保持页面可用", async ({ page }) => {
  const calls = await boundary(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  await expect(
    page.getByRole("spinbutton", { name: "其他返点比例（%）" }),
  ).toHaveValue("0")
  await page.getByRole("spinbutton", { name: "其他返点比例（%）" }).fill("10.8")
  await page.getByLabel("生效日期", { exact: true }).fill("2026-10-10")
  await page.getByRole("button", { name: "保存比例", exact: true }).click()
  await expect.poll(() => calls.rates.length).toBe(1)
  expect(calls.rates[0]).toMatchObject({
    kind: "rebate",
    scope_id: BC,
    effective_date: "2026-10-10",
    rate: "0.108",
  })
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true)
  await page.screenshot({
    path: test.info().outputPath("dashboard-mobile.png"),
    fullPage: true,
  })
})

for (const role of ["viewer", "operator"] as const) {
  test(`${role}不能查看经营看板或导出`, async ({ page }) => {
    await boundary(page, role)
    const requests: string[] = []
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname.startsWith("/api/") &&
        request.url().includes("/business/")
      )
        requests.push(request.url())
    })
    await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
    await expect(page.getByText("仅管理员可以查看经营报表。")).toBeVisible()
    await expect(
      page.getByRole("link", { name: "经营看板", exact: true }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: "导出 CSV" })).toHaveCount(0)
    await expect(page.getByText("2,000.00", { exact: true })).toHaveCount(0)
    expect(requests).toEqual([])
  })
}

test("系列赔付按指定当地日登记历史依据", async ({ page }) => {
  const calls = await boundary(page)
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("radio", { name: "广告系列", exact: true }).click()
  await page.getByRole("button", { name: "赔付依据", exact: true }).click()
  await page.getByLabel("核算日期", { exact: true }).fill("2026-11-02")
  await page.getByLabel("当日最低 ROAS 出价", { exact: true }).fill("1.06")
  await page.getByRole("combobox", { name: "当日核验依据" }).click()
  await page.getByRole("option", { name: "已核实适用且无排除操作" }).click()
  await page
    .getByLabel("依据说明", { exact: true })
    .fill("测试历史配置与操作核验记录")
  await page.getByRole("button", { name: "保存依据", exact: true }).click()
  await expect.poll(() => calls.evidence.length).toBe(1)
  expect(calls.evidence[0]).toMatchObject({
    bc_id: BC,
    advertiser_id: "acc-1",
    campaign_id: "campaign-1",
    stat_date: "2026-11-02",
    bid: "1.06",
    eligible: true,
    source: "测试历史配置与操作核验记录",
  })
})

test("版权方连接中独立保存抽成，不触发凭据验证", async ({ page }) => {
  const calls = await boundary(page)
  const verifications: string[] = []
  await page.route("**/api/**/providers/connections**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith("/verify")) {
      verifications.push(path)
      return route.fulfill({ status: 500 })
    }
    return route.fulfill({
      json: {
        items: [
          {
            id: PROVIDER,
            display_name: "嘉书连接",
            kind: "jiashu",
            status: "active",
            error_code: null,
            verified_at: null,
          },
        ],
        total: 1,
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${TENANT}/providers?bc_id=${BC}`)
  await page.getByRole("button", { name: "抽成设置", exact: true }).click()
  await expect(
    page.getByRole("spinbutton", { name: "版权方抽成比例（%）" }),
  ).toHaveValue("0")
  await page.getByRole("spinbutton", { name: "版权方抽成比例（%）" }).fill("8")
  await page.getByLabel("生效日期", { exact: true }).fill("2026-10-01")
  await page.getByRole("button", { name: "保存比例", exact: true }).click()
  await expect.poll(() => calls.rates.length).toBe(1)
  expect(calls.rates[0]).toMatchObject({
    kind: "provider",
    scope_id: PROVIDER,
    effective_date: "2026-10-01",
    rate: "0.08",
  })
  expect(verifications).toEqual([])
})
