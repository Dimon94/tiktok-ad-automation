import { expect, type Page, test } from "@playwright/test"

// Intercept only the external API boundary. Every test renders the real app/router.
// The token/user below are synthetic and never sent to a live server.
const user = {
  id: "00000000-0000-4000-8000-000000000001",
  username: "workspace-test",
  full_name: "测试管理员",
  is_active: true,
  is_superuser: true,
}
const token = "workspace-ui-test-token"
// 未接入租户时的全局入口，按“投放准备 → 广告投放 → 设置”分组排序。
const navigation = [
  "素材库",
  "投放策略",
  "广告搭建",
  "搭建任务",
  "自动规则",
  "账户与授权",
  "版权方连接",
]

async function apiBoundary(
  page: Page,
  options: {
    authenticated?: boolean
    admin?: boolean
    meStatus?: number
    usersStatus?: number
    createStatus?: number
  } = {},
) {
  if (options.authenticated !== false) {
    await page.addInitScript(
      (value) => localStorage.setItem("access_token", value),
      token,
    )
  }
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === "OPTIONS")
      return route.fulfill({
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "*",
          "Access-Control-Allow-Methods": "*",
        },
      })
    const headers = { "Access-Control-Allow-Origin": "*" }
    if (path.endsWith("/login/access-token"))
      return route.fulfill({
        json: { access_token: token, token_type: "bearer" },
        headers,
      })
    if (path.endsWith("/me/tenants"))
      return route.fulfill({ json: { items: [], next_cursor: null }, headers })
    if (path.endsWith("/users/me"))
      return route.fulfill({
        status: options.meStatus ?? 200,
        json: options.meStatus
          ? { detail: "Forbidden" }
          : { ...user, is_superuser: options.admin ?? false },
        headers,
      })
    if (path.endsWith("/users/") && request.method() === "POST")
      return route.fulfill({
        status: options.createStatus ?? 200,
        json: options.createStatus
          ? { detail: "Forbidden" }
          : { ...user, ...request.postDataJSON() },
        headers,
      })
    if (path.endsWith("/users/"))
      return route.fulfill({
        status: options.usersStatus ?? 200,
        json: options.usersStatus
          ? { detail: "Forbidden" }
          : { data: [user], count: 1 },
        headers,
      })
    return route.fulfill({
      status: 404,
      json: { detail: "Unexpected test API request" },
      headers,
    })
  })
}

async function expectNoOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
}

test("退出登录清理应用提交账本并保留登录回跳", async ({ page }) => {
  await apiBoundary(page)
  await page.goto("/")
  await page.evaluate(() => {
    sessionStorage.setItem("build-submit:tenant:bc:preview", "pending")
    sessionStorage.setItem(
      "submission-recovery:tenant:bc:submission",
      "pending",
    )
    sessionStorage.setItem("strategy-save-pending:tenant:strategy", "pending")
    sessionStorage.setItem("workspace-login-return", "/settings")
    sessionStorage.setItem("unrelated-session", "keep")
  })
  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "退出登录" }).click()
  await page.waitForURL("/login")
  await expect
    .poll(() =>
      page.evaluate(() => ({
        build: sessionStorage.getItem("build-submit:tenant:bc:preview"),
        recovery: sessionStorage.getItem(
          "submission-recovery:tenant:bc:submission",
        ),
        strategy: sessionStorage.getItem(
          "strategy-save-pending:tenant:strategy",
        ),
        returnTo: sessionStorage.getItem("workspace-login-return"),
        unrelated: sessionStorage.getItem("unrelated-session"),
      })),
    )
    .toEqual({
      build: null,
      recovery: null,
      strategy: null,
      returnTo: "/settings",
      unrelated: "keep",
    })
})

test("workspace navigation keeps one 22px content title and a stable neutral shell", async ({
  page,
}) => {
  await page.setViewportSize({ width: 2304, height: 1080 })
  await apiBoundary(page)
  await page.goto("/")
  const main = page.locator("#workspace-main")
  const header = page.locator('[data-slot="workspace-page-title"]')
  let background: string | undefined
  for (const title of ["素材库", "投放策略", "账户与授权"]) {
    await page.getByRole("link", { name: title, exact: true }).click()
    const heading = main.getByRole("heading", { level: 1 })
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1)
    await expect(heading).toHaveText(title)
    await expect(heading).toHaveCSS("font-size", "22px")
    await expect(header).toHaveText("投放工作")
    await expect(
      page.locator("header").getByRole("heading", { level: 1 }),
    ).toHaveCount(0)
    const colors = await main.evaluate((el) => ({
      background: getComputedStyle(el).backgroundColor,
      header: getComputedStyle(el.previousElementSibling!).backgroundColor,
      primary: getComputedStyle(el).getPropertyValue("--primary").trim(),
      rootPrimary: getComputedStyle(document.documentElement)
        .getPropertyValue("--primary")
        .trim(),
    }))
    expect(colors.background).not.toBe(colors.header)
    expect(colors.primary).toBe(colors.rootPrimary)
    background ??= colors.background
    expect(colors.background).toBe(background)
    await expectNoOverflow(page)
  }
})

test("unauthenticated entry and obsolete signup lead to the Chinese login", async ({
  page,
}) => {
  await apiBoundary(page, { authenticated: false })
  await page.goto("/")
  await expect(page).toHaveURL("/login")
  await expect(page.getByRole("heading", { name: "登录 TK-ADA" })).toBeVisible()
  await expect(page.getByRole("link", { name: /注册|Sign up/i })).toHaveCount(0)
  await page.goto("/signup")
  await expect(page).toHaveURL("/login")
  await expect(page.locator('input[autocomplete="username"]')).toBeVisible()
  await page.getByRole("button", { name: "登录", exact: true }).click()
  await expect(page.getByLabel("账号", { exact: true })).toHaveAttribute(
    "aria-invalid",
    "true",
  )
  await expect(
    page.getByText("账号需为 3–64 位字母、数字、下划线、点或短横线"),
  ).toBeVisible()
})

test("login submits credentials then reads the authenticated user with a bearer token", async ({
  page,
}) => {
  await apiBoundary(page, { authenticated: false })
  await page.goto("/login")
  await page.getByLabel("账号", { exact: true }).fill(user.username)
  await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
  const loginRequest = page.waitForRequest((request) =>
    request.url().includes("/login/access-token"),
  )
  const meRequest = page.waitForRequest((request) =>
    request.url().includes("/users/me"),
  )
  await page.getByRole("button", { name: "登录", exact: true }).click()
  const login = await loginRequest
  expect(new URL(login.url()).pathname).toBe("/api/login/access-token")
  expect(new URLSearchParams(login.postData()!).get("username")).toBe(
    user.username,
  )
  const me = await meRequest
  expect(new URL(me.url()).pathname).toBe("/api/users/me")
  expect(me.headers().authorization).toBe(`Bearer ${token}`)
  await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
})

for (const width of [1440, 1280]) {
  test(`authenticated empty shell has ordered navigation and no demo data at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await apiBoundary(page)
    await page.goto("/")
    await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
    await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
    await expect(
      page.getByRole("link", { name: "DSH 管理台", exact: true }),
    ).toHaveCount(0)
    const labels = await page
      .getByRole("navigation")
      .getByRole("link")
      .allTextContents()
    expect(labels).toEqual(navigation)
    await expect(page.getByRole("button", { name: "新建搭建" })).toBeDisabled()
    await expect(
      page.getByRole("link", { name: /Items|Dashboard|注册|Sign up/i }),
    ).toHaveCount(0)
    await expect(page.getByText(/Demo BC|账户 A|DEMO-|ROI/)).toHaveCount(0)
    await page.getByRole("link", { name: "素材库", exact: true }).click()
    await expect(
      page.getByRole("heading", { name: "素材库", exact: true }),
    ).toBeVisible()
    await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
    await expectNoOverflow(page)
    await page.screenshot({
      path: test.info().outputPath(`workspace-${width}.png`),
      fullPage: true,
    })
  })
}

test("mobile navigation sheet is labelled, keyboard accessible, and closes after navigation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await apiBoundary(page)
  await page.goto("/")
  await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
  const trigger = page.getByRole("button", { name: "切换导航" })
  await trigger.focus()
  await page.keyboard.press("Enter")
  const dialog = page.getByRole("dialog", { name: "TK-ADA 导航" })
  await expect(dialog).toBeVisible()
  const accountLink = dialog.getByRole("link", { name: "账户与授权" })
  await accountLink.focus()
  await page.keyboard.press("Enter")
  await expect(dialog).not.toBeVisible()
  await expect(page.getByRole("heading", { name: "账户与授权" })).toBeVisible()
  await expectNoOverflow(page)
  await page.screenshot({
    path: test.info().outputPath("workspace-mobile.png"),
    fullPage: true,
  })
})

test("mobile login fits one column and supports keyboard submission", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await apiBoundary(page, { authenticated: false })
  await page.goto("/login")
  await page.getByLabel("账号", { exact: true }).fill(user.username)
  await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
  await page.screenshot({
    path: test.info().outputPath("login-mobile.png"),
    fullPage: true,
  })
  await expectNoOverflow(page)
  await page.keyboard.press("Enter")
  await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
})

test("403 user query retains the shell and login token with readable permission feedback", async ({
  page,
}) => {
  await apiBoundary(page, { meStatus: 403 })
  await page.goto("/")
  await expect(page.getByRole("alert").getByText("无操作权限")).toBeVisible()
  await expect(
    page.getByText("你没有执行此操作的权限。请联系管理员检查角色和租户权限。"),
  ).toBeVisible()
  await expect(page).toHaveURL("/")
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
  await expect(page.getByRole("navigation", { name: "广告投放" })).toBeVisible()
})

test("403 list query stays inside authenticated platform management", async ({
  page,
}) => {
  await apiBoundary(page, { admin: true, usersStatus: 403 })
  await page.goto("/admin")
  await expect(
    page.getByRole("link", { name: "DSH 管理台", exact: true }),
  ).toHaveAttribute("href", "/api/admin/dsh/entry")
  await expect(page.getByText("无法读取用户列表")).toBeVisible()
  await expect(
    page.getByRole("alert").getByText("无操作权限", { exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId("user-menu")).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})

test("admin creation remains available and a 403 mutation does not sign out", async ({
  page,
}) => {
  await apiBoundary(page, { admin: true, createStatus: 403 })
  await page.goto("/admin")
  await page.getByRole("button", { name: "Add User" }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("账号").fill("new-user")
  await dialog.getByLabel("Set Password").fill("synthetic-password")
  await dialog.getByLabel("Confirm Password").fill("synthetic-password")
  await dialog.getByRole("button", { name: "Save", exact: true }).click()
  await expect(
    page
      .getByText("你没有执行此操作的权限。请联系管理员检查角色和租户权限。")
      .first(),
  ).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
  await expect(page).toHaveURL("/admin")
})

test("ordinary users cannot enter platform management", async ({ page }) => {
  await apiBoundary(page, { admin: false })
  await page.goto("/admin")
  await expect(
    page.getByText("仅平台管理员可管理用户。请联系管理员获取权限。"),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "Add User" })).toHaveCount(0)
  await expect(
    page.getByRole("link", { name: "平台管理", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("link", { name: "DSH 管理台", exact: true }),
  ).toHaveCount(0)
})

test("401 clears an expired session and returns to login", async ({ page }) => {
  // Set the fixture once, so redirects cannot re-inject an expired token.
  await apiBoundary(page, { authenticated: false, meStatus: 401 })
  await page.goto("/login")
  await page.evaluate(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.goto("/")
  await expect(page).toHaveURL("/login")
  await expect(page.getByText("登录已过期，请重新登录。")).toBeVisible()
  expect(
    await page.evaluate(() => localStorage.getItem("access_token")),
  ).toBeNull()
})

test("administrator can create an account through the retained authenticated form", async ({
  page,
}) => {
  await apiBoundary(page, { admin: true })
  await page.goto("/admin")
  await page.getByRole("button", { name: "Add User" }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("账号").fill("new-user")
  await dialog.getByLabel("Set Password").fill("synthetic-password")
  await dialog.getByLabel("Confirm Password").fill("synthetic-password")
  const created = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname.endsWith("/users/"),
  )
  await dialog.getByRole("button", { name: "Save", exact: true }).click()
  const request = await created
  expect(request.headers().authorization).toBe(`Bearer ${token}`)
  expect(request.postDataJSON()).not.toHaveProperty("email")
  expect(request.postDataJSON()).not.toHaveProperty("confirm_password")
  expect(request.postDataJSON()).toMatchObject({
    username: "new-user",
    password: "synthetic-password",
  })
  await expect(page.getByText("User created successfully")).toBeVisible()
  await expect(dialog).not.toBeVisible()
})

test("the removed Items example has no public route", async ({ page }) => {
  await apiBoundary(page)
  await page.goto("/items")
  await expect(page.getByTestId("not-found")).toBeVisible()
  await expect(page.getByRole("button", { name: "Add Item" })).toHaveCount(0)
})

for (const width of [900, 1023, 1024]) {
  test(`sidebar switches between Sheet and desktop navigation at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await apiBoundary(page)
    await page.goto("/")
    await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
    const trigger = page.getByRole("button", { name: "切换导航" })
    const desktopSidebar = page.locator('[data-slot="sidebar-container"]')
    const dialog = page.getByRole("dialog", { name: "TK-ADA 导航" })

    if (width < 1024) {
      await expect(desktopSidebar).toHaveCount(0)
      await expect(
        page.getByRole("navigation", { name: "广告投放" }),
      ).toHaveCount(0)
      await trigger.focus()
      await page.keyboard.press("Enter")
      await expect(dialog).toBeVisible()
      await page.keyboard.press("Escape")
      await expect(dialog).not.toBeVisible()
      await expect(trigger).toBeFocused()
      await page.keyboard.press("Enter")
      await expect(dialog).toBeVisible()
      await dialog.getByRole("link", { name: "账户与授权" }).focus()
      await page.keyboard.press("Enter")
      await expect(dialog).not.toBeVisible()
      await expect(desktopSidebar).toHaveCount(0)
    } else {
      await expect(desktopSidebar).toBeVisible()
      await expect(
        page.getByRole("navigation", { name: "广告投放" }),
      ).toBeVisible()
      await trigger.focus()
      await page.keyboard.press("Enter")
      await expect(
        page.locator('[data-slot="sidebar"][data-state="collapsed"]'),
      ).toBeVisible()
      await expect(dialog).toHaveCount(0)
      await page.keyboard.press("Enter")
      await expect(
        page.locator('[data-slot="sidebar"][data-state="expanded"]'),
      ).toBeVisible()
      await desktopSidebar.getByRole("link", { name: "账户与授权" }).focus()
      await page.keyboard.press("Enter")
      await expect(desktopSidebar).toBeVisible()
    }
    await expect(
      page.getByRole("heading", { name: "账户与授权" }),
    ).toBeVisible()
  })
}

test("unauthenticated internal links resume the original tenant BC and tab after login", async ({
  page,
}) => {
  await apiBoundary(page, { authenticated: false })
  const target =
    "/tenants/11111111-1111-4111-8111-111111111111/accounts?tab=connections&bc_id=7000000000000000001"
  await page.goto(target)
  await expect(page).toHaveURL(/\/login$/)
  await page.getByLabel("账号", { exact: true }).fill("member")
  await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
  await page.getByRole("button", { name: "登录", exact: true }).click()
  await expect(page).toHaveURL(target)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  expect(
    await page.evaluate(() => sessionStorage.getItem("workspace-login-return")),
  ).toBeNull()
})

for (const target of [
  "https://example.com/",
  "//example.com/",
  "/\\example.com/",
  "/api/users/",
])
  test(`login rejects unsafe stored return ${target}`, async ({ page }) => {
    await apiBoundary(page, { authenticated: false })
    await page.addInitScript(
      (value) => sessionStorage.setItem("workspace-login-return", value),
      target,
    )
    await page.goto("/login")
    await page.getByLabel("账号", { exact: true }).fill("member")
    await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
    await page.getByRole("button", { name: "登录", exact: true }).click()
    await expect(page).toHaveURL("/")
    await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
  })

test("页面标签随菜单打开、去重、历史切换与关闭，窄屏没有横向溢出", async ({
  page,
}) => {
  await apiBoundary(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto("/materials")
  const tabs = page.getByRole("tablist", { name: "已打开的页面" })
  await expect(
    tabs.getByRole("tab", { name: "素材库", exact: true }),
  ).toHaveAttribute("aria-selected", "true")
  // 窄屏通过真实侧栏进入其他功能。
  await page.getByRole("button", { name: "切换导航" }).click()
  await expect(
    page
      .getByRole("dialog", { name: "TK-ADA 导航" })
      .getByRole("link", { name: "投放策略", exact: true }),
  ).toHaveAttribute("data-slot", "sidebar-menu-button")
  await page
    .getByRole("dialog", { name: "TK-ADA 导航" })
    .getByRole("link", { name: "投放策略", exact: true })
    .click()
  await expect(tabs.getByRole("tab")).toHaveCount(2)
  await page.getByRole("tab", { name: "素材库", exact: true }).click()
  await expect(page).toHaveURL("/materials")
  await page.goBack()
  await expect(page).toHaveURL("/strategies")
  await expect(
    tabs.getByRole("tab", { name: "投放策略", exact: true }),
  ).toHaveAttribute("aria-selected", "true")
  await expect(tabs.getByRole("tab")).toHaveCount(2)
  await page
    .getByRole("button", { name: "关闭标签：投放策略", exact: true })
    .click()
  await expect(page).toHaveURL("/materials")
  await expect(tabs.getByRole("tab")).toHaveCount(1)
  await expect(
    page.getByRole("button", { name: "关闭标签：素材库", exact: true }),
  ).toBeDisabled()
  await expectNoOverflow(page)
  await page.screenshot({
    path: test.info().outputPath("workspace-tabs-mobile.png"),
    fullPage: true,
  })
})
