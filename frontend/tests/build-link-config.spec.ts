import { expect, test } from "@playwright/test"
import { BC, buildsBoundary, pickInputs, T } from "./utils/buildsBoundary"

test("搭建页还原紧凑布局，侧栏收窄且宽屏不留大块空白", async ({
  page,
}, testInfo) => {
  await buildsBoundary(page, { linkKind: "duiba" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  await expect(
    page
      .getByRole("region", { name: "自动取链参数" })
      .getByText("起播集 2（应用默认）"),
  ).toBeVisible()
  for (const width of [1440, 1920]) {
    await page.setViewportSize({ width, height: 900 })
    const sidebar = page.locator('[data-slot="sidebar-container"]')
    const config = page.getByRole("region", { name: "投放配置", exact: true })
    const steps = page.getByRole("list", { name: "搭建步骤" })
    const operations = page.getByRole("region", { name: "搭建操作" })
    await expect
      .poll(async () => Math.round((await sidebar.boundingBox())!.width))
      .toBe(194)
    const bounds = (await config.boundingBox())!
    expect(bounds.height).toBeLessThanOrEqual(230)
    expect(width - bounds.x - bounds.width).toBeLessThanOrEqual(24)
    expect((await steps.boundingBox())!.height).toBeLessThanOrEqual(26)
    expect((await operations.boundingBox())!.height).toBeLessThanOrEqual(70)
    await expect(
      page.getByRole("button", { name: "下一步：准备链接与素材" }),
    ).toBeInViewport()
    await page.screenshot({
      path: testInfo.outputPath(`compact-build-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
  }
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath("compact-build-390.png"),
    fullPage: true,
    animations: "disabled",
  })
})

test("取链参数默认只读展示来源，调整后只提交填写的字段", async ({ page }) => {
  const api = await buildsBoundary(page, { linkKind: "duiba" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  const card = page.getByRole("region", { name: "自动取链参数" })
  await expect(card.getByText("起播集 2（应用默认）")).toBeVisible()
  await expect(card.getByText("卡点集：按剧免费集数")).toBeVisible()
  await expect(card.getByLabel("卡点集")).toHaveCount(0)
  await card.getByRole("button", { name: "修改" }).click()
  await card.getByLabel("卡点集").fill("5")
  await card.getByLabel("起播集").fill("")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect
    .poll(
      () =>
        api.requests.find(
          (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
        )?.body.link_config,
    )
    .toEqual({ card_point_episode: 5 })
})

test("容量无需设置取链参数", async ({ page }) => {
  await buildsBoundary(page, { linkKind: "rongliang" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  await expect(page.getByText("参数与版权方后台一致，无需设置")).toBeVisible()
  await expect(page.getByRole("button", { name: "修改" })).toHaveCount(0)
})

test("刚刚好付费应用未设置支付模板时提示去连接设置", async ({ page }) => {
  await buildsBoundary(page, { linkKind: "gangganhao-paid-no-template" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  await expect(
    page.getByText(
      "付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置",
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存草稿", exact: true }),
  ).toBeEnabled()
  await expect(
    page.getByRole("button", { name: "下一步：准备链接与素材" }),
  ).toBeDisabled()
})

test("明略默认起播集为 1，调整后沿用搭建草稿参数", async ({ page }) => {
  const api = await buildsBoundary(page, { linkKind: "minglue" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  const card = page.getByRole("region", { name: "自动取链参数" })
  await expect(card.getByText("起播集 1（系统默认）")).toBeVisible()
  await card.getByRole("button", { name: "修改" }).click()
  await card.getByLabel("起播集").fill("3")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect
    .poll(
      () =>
        api.requests.find(
          (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
        )?.body.link_config,
    )
    .toEqual({ episode: 3 })
})
