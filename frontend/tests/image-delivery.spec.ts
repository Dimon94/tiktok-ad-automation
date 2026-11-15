import { expect, test, type Page } from "@playwright/test"

const T = "d0d0d0d0-0000-4000-8000-000000000065"
const BC = "7400000000000000101"
const D = "d0d0d0d0-0000-4000-8000-0000000002d3"
const path = (section: string) => `/tenants/${T}/${section}?bc_id=${BC}`
async function go(page: Page, destination: string) {
  await page.evaluate((hash) => {
    window.location.hash = hash
  }, `#${destination}`)
}

test("合成图片批次：规则、单图配乐修正、V2冻结、模拟执行、UNKNOWN核查与广告级报表", async ({
  page,
}) => {
  test.setTimeout(120_000)
  const errors: string[] = [],
    requests: string[] = []
  page.on("pageerror", (e) => errors.push(e.message))
  page.on("request", (r) => requests.push(r.url()))
  await page.goto("/demo/index.html#/login")
  await page.getByRole("button", { name: "自由体验" }).click()
  await expect(page).toHaveURL(/#\/tenants\//)
  await expect(page.getByText("体验版 · 演示数据")).toBeVisible()
  await go(page, path(`build-drafts/${D}`))
  await expect(page.getByText("整批图片规则", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeDisabled()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "应用并生成", exact: true }),
  ).toBeEnabled()
  await page.getByRole("button", { name: "应用并生成", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "查看与调整轮播" }).first(),
  ).toBeEnabled()
  await page.getByRole("button", { name: "查看与调整轮播" }).first().click()
  const card = page
    .locator("[data-slot=card]")
    .filter({ hasText: "创意 13 · 1 张" })
  await card.getByRole("button", { name: "换音乐", exact: true }).click()
  await page.getByRole("textbox", { name: "搜索音乐" }).fill("晨光")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByRole("button", { name: /晨光序曲/ }).click()
  await page.getByRole("button", { name: "使用这首音乐", exact: true }).click()
  await expect(card.getByText(/晨光序曲/)).toBeVisible()
  await page.getByRole("button", { name: "完成", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeEnabled()
  await page.getByRole("button", { name: "生成搭建预览", exact: true }).click()
  await expect(page).toHaveURL(/build-previews/)
  await expect(
    page.getByText(/合成演示 · 4 个系列 \/ 共享广告组 · 8 条广告/),
  ).toBeVisible()
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(
    page.getByText(/将在提交后、发送前核验目标账户的原选曲目/).first(),
  ).toBeVisible()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await page
    .getByText("第 1 组 · 61 份素材 · 13 个完整创意 · 3 条 SP 创意", {
      exact: true,
    })
    .click()
  await expect(page.getByText("模拟图片共享组", { exact: true })).toBeVisible()
  await expect(page.getByText(/创意 1 · 轮播，5 张图片/).first()).toBeVisible()
  await page.getByRole("button", { name: "下一页广告", exact: true }).click()
  await expect(page.getByText(/轮播，1 张图片，配乐/)).toBeVisible()
  await page.getByRole("button", { name: "关闭", exact: true }).click()
  await page.getByRole("button", { name: /^创建并立即启用/ }).click()
  await expect(page).toHaveURL(/build-tasks\//)
  await expect(
    page.getByRole("button", { name: "核查待核实项（1）", exact: true }),
  ).toBeVisible({ timeout: 25_000 })
  await expect(page.getByRole("button", { name: /重试失败步骤/ })).toHaveCount(
    0,
  )
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(page.getByRole("button", { name: /核查待核实项/ })).toHaveCount(
    0,
    { timeout: 15_000 },
  )
  await go(page, path("ads"))
  await page.getByRole("tab", { name: "广告", exact: true }).click()
  await expect(page.getByText(/模拟图片广告/).first()).toBeVisible()
  await expect(page.getByText("图片轮播 · 广告级数据").first()).toBeVisible()
  await page
    .getByRole("button", { name: /模拟图片广告/ })
    .first()
    .click()
  await page.getByRole("tab", { name: "素材", exact: true }).click()
  await expect(
    page.getByText(/每个轮播创意展示一份消耗与收入，金额不分配到单张图片/),
  ).toBeVisible()
  await expect(page.getByText(/轮播 1 · 5 张图片/)).toBeVisible()
  await expect(
    page.getByRole("link", { name: "查看冻结创意与投放任务" }),
  ).toBeVisible()
  await page.screenshot({
    path: "test-results/image-delivery-report.png",
    fullPage: true,
  })
  expect(errors).toEqual([])
  expect(requests.filter((url) => url.includes("/api/"))).toEqual([])
})

test("图片上传入口独立于35图轮播上限并开始合成分片上传", async ({ page }) => {
  test.setTimeout(60_000)
  const requests: string[] = []
  page.on("request", (request) => requests.push(request.url()))
  await page.goto("/demo/index.html#/login")
  await page.getByRole("button", { name: "自由体验" }).click()
  await expect(page).toHaveURL(/#\/tenants\//)
  await go(page, path("materials"))
  await page.getByRole("button", { name: "批量上传", exact: true }).click()
  const files = Array.from({ length: 40 }, (_, i) => ({
    name: `合成上传_${i + 1}.png`,
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6rWQAAAAASUVORK5CYII=",
      "base64",
    ),
  }))
  await page.getByLabel("选择本地素材").setInputFiles(files)
  await expect(
    page.getByRole("button", { name: "开始上传 40 个文件", exact: true }),
  ).toBeEnabled()
  await expect(page.locator("#material-files")).toHaveAttribute(
    "accept",
    /image\/png/,
  )
  await page
    .getByRole("button", { name: "开始上传 40 个文件", exact: true })
    .click()
  await expect(page.getByText(/合成上传_1.png/).first()).toBeVisible()
  await expect(page.getByText(/平台可用 40/).first()).toBeVisible({
    timeout: 30_000,
  })
  expect(
    requests.filter(
      (url) => url.includes("/api/") || url.includes("demo-storage.invalid"),
    ),
  ).toEqual([])
})
