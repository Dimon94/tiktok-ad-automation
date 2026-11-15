import { expect, type Page, test } from "@playwright/test"
import { BC, buildsBoundary, C, D, DR, P, T } from "./utils/buildsBoundary"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

test.use({ timezoneId: "Asia/Shanghai" })
const ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
const G = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
const U = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
const E = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
const counts = (c: number, g: number, a: number) => ({
  campaign_count: c,
  adgroup_count: g,
  ad_count: a,
})
async function boundary(
  page: Page,
  options: {
    viewer?: boolean
    count?: number
    denied?: boolean
    delayedUnits?: boolean
    longLabels?: boolean
  } = {},
) {
  await buildsBoundary(page, { viewer: options.viewer })
  const requests: { path: string; query: URLSearchParams; method: string }[] =
    []
  let releaseUnits = () => {}
  const unitsWait = new Promise<void>((resolve) => {
    releaseUnits = resolve
  })
  let newest = false
  const summary = {
    execution_route: {
      connection_id: C,
      connection_name: "原搭建连接",
      channel: "OFFICIAL_API",
      bc_id: BC,
    },
    recovery_mode: "ORIGINAL_READ",
    error_code: null,
    submission_id: ID,
    preview_id: P,
    draft_id: D,
    batch_short_id: "B0909-01",
    bc_id: BC,
    status: "NEEDS_REVIEW",
    expanded: true,
    currency: "USD",
    daily_budget_sum: "100.00",
    actor_name: "真实提交人",
    provider_name: "真实版权方",
    strategy_label: "测试策略 v2",
    planned: counts(1, 1, 2),
    submitted: counts(1, 1, 2),
    succeeded: counts(1, 1, 1),
    failed: counts(0, 0, 0),
    unknown: counts(0, 0, 1),
    pending: counts(0, 0, 0),
    excluded: counts(0, 0, 0),
    stage_counts: { "MATERIAL:SUCCEEDED": 2, "READBACK:UNKNOWN": 1 },
    excluded_unit_count: 0,
    drama_count: 1,
    account_count: 1,
    created_at: "2026-09-09T01:00:00Z",
    updated_at: "2026-09-09T01:05:00Z",
    recovery: {
      can_retry: false,
      can_reconcile: true,
      retryable_step_count: 0,
      reconcilable_step_count: 1,
      reasons: [] as string[],
    },
  }
  const step = {
    error_message: null as string | null,
    remote_code: null as number | null,
    step_id: E,
    unit_id: U,
    kind: "AD",
    group_id: G,
    planned_ad_id: E,
    material_id: null,
    status: "UNKNOWN",
    remote_id: null,
    error_code: "result_unknown",
    operation_status: null,
    review_status: null,
    mismatch: false,
    checked_at: null,
    title: "真实剧目",
    advertiser_id: "90071992547409931235",
    group_no: 1,
    creative_no: 2,
  }
  await page.route("**/api/tenants/*/submissions**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname
    expect(req.headers().authorization).toBe("Bearer build-test-token")
    requests.push({ path, query: url.searchParams, method: req.method() })
    if (options.denied)
      return route.fulfill({ status: 403, json: { code: "action_forbidden" } })
    const paged = (rows: unknown[]) => {
      const n = Number(url.searchParams.get("cursor") || 0),
        limit = Number(url.searchParams.get("limit") || 50)
      return route.fulfill({
        json: {
          items: rows.slice(n, n + limit),
          next_cursor: rows.length > n + limit ? String(n + limit) : null,
        },
      })
    }
    if (path.endsWith("/submissions") && url.searchParams.get("bc_id") !== BC)
      return paged([])
    if (path.endsWith("/submissions"))
      return paged([
        ...(newest
          ? [
              {
                ...summary,
                submission_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                batch_short_id: "最新任务",
              },
            ]
          : []),
        ...Array.from({ length: options.count ?? 1 }, (_, i) => ({
          ...summary,
          submission_id:
            i === 0
              ? ID
              : `bbbbbbbb-bbbb-4bbb-8bbb-${String(i).padStart(12, "0")}`,
          batch_short_id: `B0909-${i + 1}`,
        })),
      ])
    if (/\/submissions\/[a-f0-9-]{36}$/.test(path))
      return route.fulfill({
        json: { ...summary, submission_id: path.split("/").pop() },
      })
    if (path.endsWith("/units") && options.delayedUnits) await unitsWait
    if (path.endsWith("/units"))
      return paged([
        {
          unit_id: U,
          drama_id: DR,
          title: "真实剧目",
          advertiser_id: step.advertiser_id,
          account_name: options.longLabels
            ? "{LemonShow Minis}-031541-example-TT-p2"
            : "真实账户",
          campaign_name: "真实Campaign",
          result_status: summary.status,
          disposition: "INCLUDED",
          reason_codes: [],
          expanded: true,
          group_count: 1,
          ad_count: 2,
          succeeded_group_count: 1,
          succeeded_ad_count: summary.succeeded.ad_count,
          material_count: 2,
          ready_material_count: 2,
          campaign_step: {
            ...step,
            kind: "CAMPAIGN",
            status: "SUCCEEDED",
            remote_id: "campaign-real",
            operation_status: "ENABLE",
            review_status: "PENDING",
            checked_at: "2026-09-09T01:04:00Z",
          },
        },
      ])
    if (path.endsWith("/groups"))
      return paged([
        {
          group_id: G,
          unit_id: U,
          group_no: 1,
          name: "真实素材组",
          material_count: 2,
          ad_count: 2,
          step: {
            ...step,
            kind: "ADGROUP",
            status: "SUCCEEDED",
            remote_id: "group-real",
            operation_status: "ENABLE",
          },
        },
      ])
    if (path.endsWith("/materials"))
      return paged([
        {
          material_id: E,
          position: 1,
          file_name: "真实剧目-素材.mp4",
          preview_available: true,
          video_id: "target-video-real",
          image_id: "target-cover-real",
          step: { ...step, kind: "MATERIAL", status: "SUCCEEDED" },
        },
      ])
    if (path.endsWith("/ads"))
      return paged([
        {
          planned_ad_id: E,
          group_id: G,
          creative_no: 2,
          name: "SP2",
          text: "冻结真实广告正文",
          cta_option_ids: ["real-cta"],
          step,
        },
      ])
    if (path.endsWith("/steps")) return paged([step])
    if (path.endsWith("/excluded")) return paged([])
    if (path.endsWith("/events"))
      return paged([
        {
          evidence_id: E,
          step_id: E,
          unit_id: U,
          kind: "AD",
          attempt: 1,
          conclusion: "UNKNOWN",
          title: "真实剧目",
          advertiser_id: step.advertiser_id,
          account_name: "真实账户",
          observed_at: "2026-09-09T01:05:00Z",
        },
      ])
    throw new Error(`Unexpected task boundary ${path}`)
  })
  return {
    requests,
    summary,
    step,
    releaseUnits,
    setNewest: () => {
      newest = true
    },
  }
}

test("核实补建显示完成和新远端对象，保留原尝试审计", async ({ page }) => {
  const api = await boundary(page)
  Object.assign(api.summary, {
    status: "COMPLETED",
    corrected_ad_count: 2,
    succeeded: counts(1, 1, 2),
    unknown: counts(0, 0, 0),
    stage_counts: { "READBACK:VERIFIED_REPLACEMENT": 2 },
    recovery: {
      can_retry: false,
      can_reconcile: false,
      retryable_step_count: 0,
      reconcilable_step_count: 0,
      reasons: [],
    },
  })
  Object.assign(api.step, {
    correction: {
      correction_id: E,
      status: "VERIFIED_REPLACEMENT",
      source_step_id: E,
      remote_id: "replacement-ad",
      remote_adgroup_id: "replacement-group",
      original_adgroup_id: "group-real",
      operation_status: "ENABLE",
      review_status: "PENDING",
      checked_at: "2026-09-15T01:00:00Z",
    },
  })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await expect(
    page.getByText("已完成（含2条补建）", { exact: true }),
  ).toBeVisible()
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues`)
  await expect(
    page.getByText("已完成（含2条补建）", { exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId("count-ad-unknown")).toHaveText("0")
  await expect(page.getByText("replacement-ad", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await expect(
    page.getByText("原尝试结果：结果待核实", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("补建已核实，原尝试保留用于审计。", { exact: true }),
  ).toBeVisible()
  expect(api.requests.every((request) => request.method === "GET")).toBe(true)
})

test("任务列表默认50并显示真实提交范围，只有详情操作", async ({ page }) => {
  const api = await boundary(page, { count: 131 })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await expect(
    page.getByText("查看创建进度并处理异常", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "查看详情", exact: true }),
  ).toHaveCount(50)
  expect(
    api.requests
      .filter((r) => r.path.endsWith("/submissions"))[0]
      .query.get("limit"),
  ).toBe("50")
  await expect(page.getByRole("checkbox")).toHaveCount(0)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect
    .poll(() =>
      api.requests
        .filter((r) => r.path.endsWith("/submissions"))
        .some((r) => r.query.get("cursor") === "50"),
    )
    .toBe(true)
})

test("任务详情三级对象守恒，按需展开真实素材组和SP正文", async ({ page }) => {
  const api = await boundary(page)
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByRole("heading", { name: "任务 B0909-01", exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("1")
  await expect(page.getByTestId("count-ad-unknown")).toHaveText("1")
  expect(api.requests.filter((r) => r.path.endsWith("/groups"))).toHaveLength(0)
  await page.getByRole("button", { name: "查看明细", exact: true }).click()
  await expect(page.getByText("真实素材组", { exact: true })).toBeVisible()
  expect(api.requests.filter((r) => r.path.endsWith("/ads"))).toHaveLength(0)
  await page.getByRole("button", { name: "查看 SP 创意", exact: true }).click()
  await expect(
    page.getByText("冻结真实广告正文", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /再次启用|停止广告|激活/ }),
  ).toHaveCount(0)
})

test("切换列表筛选总是回首页，返回曾访问筛选不复用旧页", async ({ page }) => {
  const api = await boundary(page, { count: 131 })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await page.getByRole("tab", { name: "需要处理", exact: true }).click()
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect
    .poll(() =>
      api.requests
        .filter(
          (r) =>
            r.path.endsWith("/submissions") &&
            r.query.get("status_group") === "attention",
        )
        .slice(-1)[0]
        ?.query.get("cursor"),
    )
    .toBe("50")
  await page.getByRole("tab", { name: "全部", exact: true }).click()
  await page.getByRole("tab", { name: "需要处理", exact: true }).click()
  await expect(page.getByText(/第 1(?: \/ \d+)? 页/)).toBeVisible()
  expect(
    api.requests
      .filter(
        (r) =>
          r.path.endsWith("/submissions") &&
          r.query.get("status_group") === "attention",
      )
      .slice(-1)[0]
      ?.query.get("cursor"),
  ).toBeNull()
})

test("任务由进行中到完成时刷新已显示单位详情，不能只更新摘要", async ({
  page,
}) => {
  const api = await boundary(page)
  api.summary.status = "RUNNING"
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: "查看明细", exact: true }),
  ).toBeVisible()
  const initial = api.requests.filter((r) => r.path.endsWith("/units")).length
  api.summary.status = "COMPLETED"
  api.summary.succeeded = counts(1, 1, 2)
  api.summary.unknown = counts(0, 0, 0)
  api.summary.updated_at = "2026-09-09T01:06:00Z"
  await expect(page.getByText("已完成", { exact: true }).first()).toBeVisible()
  await expect
    .poll(() => api.requests.filter((r) => r.path.endsWith("/units")).length)
    .toBeGreaterThan(initial)
})

test("列表进入详情再返回保留已提交筛选和当前页", async ({ page }) => {
  const api = await boundary(page, { count: 131 })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await page.getByLabel("搜索任务", { exact: true }).fill("Moon_%")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  await page
    .getByRole("link", { name: "查看详情", exact: true })
    .first()
    .click()
  await expect(
    page.getByRole("heading", { name: "任务 B0909-01", exact: true }),
  ).toBeVisible()
  await page.getByRole("link", { name: "返回任务列表" }).click()
  await expect(page.getByLabel("搜索任务", { exact: true })).toHaveValue(
    "Moon_%",
  )
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  expect(
    api.requests
      .filter(
        (r) => r.path.endsWith("/submissions") && r.query.get("limit") === "50",
      )
      .slice(-1)[0]
      .query.get("cursor"),
  ).toBe("50")
})

test("列表100条服务端页长与日期半开范围，完整长BC不转数字", async ({
  page,
}) => {
  const api = await boundary(page, { count: 131 })
  await page.goto(
    `/tenants/${T}/build-tasks?bc_id=${BC}&range=custom&from=2026-09-01&to=2026-09-09`,
  )
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(
    page.getByRole("link", { name: "查看详情", exact: true }),
  ).toHaveCount(100)
  const req = api.requests
    .filter(
      (r) => r.path.endsWith("/submissions") && r.query.get("limit") === "100",
    )
    .slice(-1)[0]
  expect(req.query.get("bc_id")).toBe(BC)
  expect(req.query.get("created_from")).toBe("2026-08-31T16:00:00.000Z")
  expect(req.query.get("created_to")).toBe("2026-09-09T16:00:00.000Z")
  expect(req.query.get("cursor")).toBeNull()
})

test("列表新任务只提示，不插入正在浏览的第二页", async ({ page }) => {
  await page.clock.install()
  const api = await boundary(page, { count: 131 })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  const first = await page
    .getByRole("link", { name: /B0909-/ })
    .first()
    .textContent()
  await page.clock.runFor(100)
  api.setNewest()
  await page.clock.fastForward(31000)
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) =>
            r.path.endsWith("/submissions") && r.query.get("limit") === "1",
        ).length,
    )
    .toBeGreaterThan(1)
  await expect(
    page.getByText("有新任务，刷新查看。", { exact: true }),
  ).toBeVisible()
  expect(
    await page
      .getByRole("link", { name: /B0909-/ })
      .first()
      .textContent(),
  ).toBe(first)
  await page.getByRole("button", { name: "刷新至首页" }).click()
  await expect(page.getByText(/第 1(?: \/ \d+)? 页/)).toBeVisible()
  await expect(
    page.getByRole("link", { name: "最新任务", exact: true }),
  ).toBeVisible()
})

test("素材核查预算耗尽明确停止自动重试，不提示重复上传", async ({ page }) => {
  const api = await boundary(page)
  api.step.kind = "MATERIAL"
  api.step.error_code = "material_reconciliation_budget_exhausted"
  await page.goto(
    `/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues&result=UNKNOWN`,
  )
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await expect(
    page
      .getByRole("dialog", { name: "结果与处理建议" })
      .getByText(
        "本轮素材核查已停止自动重试，结果仍未知；请核查原任务，勿重复上传。",
        { exact: true },
      ),
  ).toBeVisible()
  expect(api.requests.every((r) => r.method === "GET")).toBe(true)
  // 未知的素材核查结果只保留原冻结任务，不能重新规划广告或扩大素材集合。
  expect(api.requests.some((r) => r.path.endsWith("/ads"))).toBe(false)
})

test("异常定位与操作记录只读按需查询，并保留平台ENABLE的真实含义", async ({
  page,
}) => {
  const api = await boundary(page)
  await page.goto(
    `/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues&result=UNKNOWN`,
  )
  await expect(
    page.getByRole("button", { name: "查看原因", exact: true }),
  ).toBeVisible()
  const req = api.requests.find((r) => r.path.endsWith("/steps"))!
  expect(req.query.get("result")).toBe("UNKNOWN")
  expect(req.query.get("limit")).toBe("50")
  expect(api.requests.filter((r) => r.path.endsWith("/events"))).toHaveLength(0)
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await expect(page.getByRole("dialog", { name: "结果与处理建议" })).toHaveCSS(
    "max-width",
    "1024px",
  )
  await expect(
    page.getByRole("heading", { name: "结果与处理建议" }),
  ).toBeVisible()
  expect(
    api.requests.find((r) => r.path.endsWith("/events"))?.query.get("step_id"),
  ).toBe(E)
  await page.keyboard.press("Escape")
  await page.getByRole("tab", { name: "搭建明细", exact: true }).click()
  await page.getByRole("button", { name: "查看明细", exact: true }).click()
  await expect(page.getByRole("dialog", { name: /搭建明细$/ })).toHaveCSS(
    "max-width",
    "1024px",
  )
  await expect(
    page
      .getByRole("region", { name: "广告系列详情" })
      .getByText("操作状态：已启用（ENABLE）", { exact: true }),
  ).toBeVisible()
  await expect(
    page
      .getByRole("region", { name: "广告系列详情" })
      .getByText("审核/投放状态：PENDING", { exact: true }),
  ).toBeVisible()
  expect(api.requests.every((r) => r.method === "GET")).toBe(true)
})

test("广告前置失败时直接显示广告组的平台错误文案、错误码和请求编号", async ({
  page,
}) => {
  const api = await boundary(page)
  api.step.status = "FAILED"
  api.step.error_code = "dependency_failed"
  await page.route("**/api/tenants/*/submissions/*/events*", async (route) => {
    expect(new URL(route.request().url()).searchParams.get("step_id")).toBe(E)
    expect(route.request().method()).toBe("GET")
    await route.fulfill({
      json: {
        items: [
          {
            evidence_id: E,
            step_id: G,
            unit_id: U,
            kind: "ADGROUP",
            attempt: 1,
            conclusion: "REMOTE_REJECTED",
            title: "真实剧目",
            advertiser_id: api.step.advertiser_id,
            account_name: "真实账户",
            observed_at: "2026-09-09T01:05:00Z",
            remote_code: 40002,
            remote_message:
              "Invalid value for field targeting_optimization_mode: MANUAL",
            request_id: "platform-request-40002",
          },
        ],
        next_cursor: null,
      },
    })
  })
  await page.goto(
    `/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues&result=FAILED`,
  )
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "结果与处理建议" })
  await expect(
    dialog.getByText(
      "Invalid value for field targeting_optimization_mode: MANUAL",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    dialog.getByText("TikTok 错误码：40002", { exact: true }),
  ).toBeVisible()
  await expect(
    dialog.getByText("platform-request-40002", { exact: true }),
  ).toBeVisible()
  expect(api.requests.every((request) => request.method === "GET")).toBe(true)
})

test("组内素材分页展示实际目标VID，点击原件才请求短期预览", async ({
  page,
}) => {
  const api = await boundary(page)
  let previews = 0
  await page.route(`**/api/tenants/${T}/materials/${E}/preview`, (route) => {
    previews++
    expect(new URL(route.request().url()).searchParams.has("bc_id")).toBe(false)
    return route.fulfill({
      json: {
        url: "https://storage.example.com/task-preview.mp4",
        expires_in: 300,
      },
    })
  })
  await page.route("https://storage.example.com/task-preview.mp4", (route) =>
    route.fulfill({
      body: "isolated-video-boundary",
      contentType: "video/mp4",
    }),
  )
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看明细", exact: true }).click()
  await page.getByRole("button", { name: "查看素材", exact: true }).click()
  await expect(page.getByText(/目标视频 ID：target-video-real/)).toBeVisible()
  expect(previews).toBe(0)
  expect(
    api.requests.find((r) => r.path.endsWith("/materials"))?.query.get("limit"),
  ).toBe("50")
  await page.getByRole("button", { name: "预览原件", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "素材原件预览" }),
  ).toBeVisible()
  expect(previews).toBe(1)
  await expect(page.locator("video")).toHaveAttribute(
    "src",
    "https://storage.example.com/task-preview.mp4",
  )
})

for (const viewer of [true, false])
  test(`任务页面403保留登录，viewer=${viewer}`, async ({ page }) => {
    await boundary(page, { viewer, denied: true })
    await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
    await expect(page.getByText(/当前角色无权执行此操作/).first()).toBeVisible()
    await expect(
      page.getByRole("link", { name: "新建搭建", exact: true }),
    ).toHaveCount(0)
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe("build-test-token")
  })

test("viewer没有新建和恢复动作，仍可读取任务所有已知结果", async ({ page }) => {
  await boundary(page, { viewer: true })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await expect(
    page.getByRole("link", { name: "查看详情", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "新建搭建", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("link", { name: "查看详情", exact: true }).click()
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("1")
  await expect(
    page.getByRole("button", { name: /重试失败|核查待核实/ }),
  ).toHaveCount(0)
})

for (const width of [1440, 900, 390])
  test(`任务工作区 ${width}px 表格有界滚动与键盘侧栏`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await boundary(page, { count: 131 })
    await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
    await expect(
      page.getByRole("link", { name: "查看详情", exact: true }),
    ).toHaveCount(50)
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    const box = page.locator('[data-slot="table-container"]').first()
    expect(
      await box.evaluate(
        (e) =>
          e.scrollHeight > e.clientHeight &&
          e.clientHeight <= window.innerHeight * 0.61,
      ),
    ).toBe(true)
    expect(
      await page
        .getByRole("columnheader", { name: "任务", exact: true })
        .evaluate((e) => getComputedStyle(e).position),
    ).toBe("sticky")
    const screenshots = process.env.TASK_SCREENSHOT_DIR
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-submission-list-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
    await page
      .getByRole("link", { name: "查看详情", exact: true })
      .first()
      .click()
    await expect(
      page.getByRole("heading", { name: "任务 B0909-01", exact: true }),
    ).toBeVisible()
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    await page.getByRole("button", { name: "查看明细", exact: true }).focus()
    await page.keyboard.press("Enter")
    await expect(
      page.getByRole("heading", { name: "真实剧目 · 搭建明细", exact: true }),
    ).toBeVisible()
    await page.keyboard.press("Escape")
    await expect(
      page.getByRole("button", { name: "查看明细", exact: true }),
    ).toBeFocused()
    if (screenshots) {
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.screenshot({
        path: `${screenshots}/task-6-submission-detail-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
    }
  })

test("读取详情时切BC取消旧请求并到新BC列表，不带旧任务ID", async ({ page }) => {
  const { BC2 } = await import("./utils/buildsBoundary"),
    api = await boundary(page, { delayedUnits: true })
  const failed: string[] = []
  page.on("requestfailed", (r) => failed.push(r.url()))
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect
    .poll(() => api.requests.some((r) => r.path.endsWith("/units")))
    .toBe(true)
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await expect(page).toHaveURL(
    (url) =>
      url.pathname === `/tenants/${T}/build-tasks` &&
      url.searchParams.get("bc_id") === BC2,
  )
  await expect
    .poll(() => failed.some((url) => url.includes(`/submissions/${ID}/units`)))
    .toBe(true)
  api.releaseUnits()
  await expect(
    page.getByRole("heading", { name: "搭建任务", exact: true }),
  ).toBeVisible()
  expect(
    api.requests
      .filter((r) => r.path.endsWith("/submissions"))
      .slice(-1)[0]
      .query.get("bc_id"),
  ).toBe(BC2)
})

test("首次无任务与筛选无结果使用不同空态", async ({ page }) => {
  await boundary(page, { count: 0 })
  await page.goto(`/tenants/${T}/build-tasks?bc_id=${BC}`)
  await expect(page.getByText("还没有搭建任务", { exact: true })).toBeVisible()
  await expect(page.getByRole("link", { name: "新建搭建" })).toBeVisible()
  await page.getByRole("textbox", { name: "搜索任务" }).fill("不存在")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("没有符合筛选的任务", { exact: true }),
  ).toBeVisible()
})

test("异常深链更改筛选后刷新保留新的结果条件", async ({ page }) => {
  const api = await boundary(page)
  await page.goto(
    `/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues&result=UNKNOWN`,
  )
  await page.getByRole("combobox", { name: "创建结果筛选" }).click()
  await page.getByRole("option", { name: "确定失败", exact: true }).click()
  await expect
    .poll(() =>
      api.requests
        .filter((r) => r.path.endsWith("/steps"))
        .slice(-1)[0]
        ?.query.get("result"),
    )
    .toBe("FAILED")
  await page.reload()
  await expect(page.getByRole("combobox", { name: "创建结果筛选" })).toHaveText(
    "确定失败",
  )
  expect(
    api.requests
      .filter((r) => r.path.endsWith("/steps"))
      .slice(-1)[0]
      ?.query.get("result"),
  ).toBe("FAILED")
})

test("Campaign成功但Ad仍未知时组合保持待核实", async ({ page }) => {
  await boundary(page)
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  const row = page.getByRole("row").filter({ hasText: "真实剧目" })
  await expect(row.getByText("待核实", { exact: true })).toBeVisible()
  await expect(row.getByText("已成功", { exact: true })).toHaveCount(0)
  await expect(page.getByTestId("count-ad-unknown")).toHaveText("1")
  await expect(row.getByText("广告系列 1 / 1", { exact: true })).toBeVisible()
})

test("刷新失败保留已加载任务结果并标明读取失败", async ({ page }) => {
  await boundary(page)
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(page.getByText("真实剧目", { exact: true })).toBeVisible()
  await page.route("**/api/tenants/*/submissions/**", (route) =>
    route.fulfill({ status: 503, json: { code: "temporarily_unavailable" } }),
  )
  await page.getByRole("button", { name: "刷新任务结果", exact: true }).click()
  await expect(
    page.getByText("请求未完成", { exact: true }).first(),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "任务 B0909-01", exact: true }),
  ).toBeVisible()
  await expect(page.getByText("真实剧目", { exact: true })).toBeVisible()
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("1")
})

test("操作记录独立服务端50/100分页，不预先读取后续证据", async ({ page }) => {
  await boundary(page)
  const reads: URLSearchParams[] = []
  await page.route("**/api/tenants/*/submissions/*/events*", async (route) => {
    const query = new URL(route.request().url()).searchParams
    reads.push(query)
    const offset = Number(query.get("cursor") || 0),
      limit = Number(query.get("limit"))
    await route.fulfill({
      json: {
        items: Array.from(
          { length: Math.min(limit, 111 - offset) },
          (_, i) => ({
            evidence_id: `event-${offset + i + 1}`,
            step_id: E,
            unit_id: U,
            kind: "AD",
            attempt: offset + i + 1,
            conclusion: "UNKNOWN",
            observed_at: "2026-09-09T01:05:00Z",
          }),
        ),
        next_cursor: offset + limit < 111 ? String(offset + limit) : null,
      },
    })
  })
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=events`)
  await expect(page.getByRole("table").last().getByRole("row")).toHaveCount(51)
  expect(reads).toHaveLength(1)
  expect(reads[0].get("limit")).toBe("50")
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText(/第 2(?: \/ \d+)? 页/)).toBeVisible()
  expect(reads[1].get("cursor")).toBe("50")
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.getByRole("table").last().getByRole("row")).toHaveCount(101)
  expect(reads.slice(-1)[0].get("limit")).toBe("100")
  expect(reads.slice(-1)[0].get("cursor")).toBeNull()
})

test("已读详情刷新403清除受限结果并保留登录", async ({ page }) => {
  await boundary(page)
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(page.getByText("真实剧目", { exact: true })).toBeVisible()
  await page.route("**/api/tenants/*/submissions/**", (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "刷新任务结果", exact: true }).click()
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByText("真实剧目", { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
})

const RECOVERY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
async function recoveryBoundary(
  page: Page,
  mode?: "lost" | "missing" | "forbidden" | "unauthorized" | "no-candidates",
) {
  const api = await boundary(page)
  let requestId = ""
  const calls: { path: string; method: string }[] = []
  const progress = {
    recovery_id: RECOVERY,
    request_id: "",
    submission_id: ID,
    kind: "RECONCILE",
    state: "QUEUED",
    scheduled_count: 0,
    reason_code: null,
  }
  const original = () => ({
    ...progress,
    state: "QUEUED",
    scheduled_count: 0,
    reason_code: null,
  })
  await page.route("**/api/tenants/*/submission**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    if (!/\/(retry|reconcile)$|\/submission-recover/.test(path))
      return route.fallback()
    expect(request.headers().authorization).toBe("Bearer build-test-token")
    calls.push({ path, method: request.method() })
    if (request.method() === "POST") {
      expect(Object.keys(request.postDataJSON())).toEqual(["request_id"])
      requestId = request.postDataJSON().request_id
      expect(requestId).toMatch(/^[0-9a-f-]{36}$/)
      progress.request_id = requestId
      progress.kind = path.endsWith("/retry") ? "RETRY" : "RECONCILE"
      if (mode === "unauthorized")
        return route.fulfill({
          status: 401,
          json: { code: "not_authenticated" },
        })
      if (mode === "forbidden")
        return route.fulfill({
          status: 403,
          json: { code: "action_forbidden" },
        })
      if (mode === "no-candidates") {
        api.summary.recovery.can_reconcile = false
        api.summary.recovery.reconcilable_step_count = 0
        return route.fulfill({
          status: 409,
          json: { code: "recovery_no_candidates" },
        })
      }
      if (mode === "lost" || mode === "missing") return route.abort("failed")
      return route.fulfill({ status: 202, json: original() })
    }
    if (path.includes("/submission-recovery-requests/")) {
      expect(path.split("/").pop()).toBe(requestId)
      if (mode === "missing")
        return route.fulfill({
          status: 404,
          json: { code: "resource_not_found" },
        })
      return route.fulfill({ json: original() })
    }
    expect(path.split("/").pop()).toBe(RECOVERY)
    return route.fulfill({ json: progress })
  })
  return { ...api, progress, calls }
}

test("UNKNOWN只核查，QUEUED零回执与当前扫描调度进度分开", async ({ page }) => {
  const api = await recoveryBoundary(page)
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(page.getByRole("button", { name: /重试失败步骤/ })).toHaveCount(
    0,
  )
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(
    page.getByText("已安排核查，等待扫描。", { exact: true }),
  ).toBeVisible()
  expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(1)
  expect(api.calls[0].path).toMatch(/\/reconcile$/)
  await expect
    .poll(() =>
      api.calls.some((c) => c.path.includes("/submission-recoveries/")),
    )
    .toBe(true)
  api.progress.state = "COMPLETED"
  api.progress.scheduled_count = 1
  api.summary.succeeded = counts(1, 1, 2)
  api.summary.unknown = counts(0, 0, 0)
  api.summary.status = "COMPLETED"
  api.summary.recovery.can_reconcile = false
  await page.getByRole("button", { name: "刷新恢复进度", exact: true }).click()
  await expect(
    page.getByText("扫描完成，已安排 1 个步骤。实际执行结果以任务统计为准。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("2")
  expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(1)
})

test("恢复响应丢失刷新后只查同请求和当前进度，不再POST", async ({ page }) => {
  const api = await recoveryBoundary(page, "lost")
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(
    page.getByText("恢复请求结果尚未确认。", { exact: true }),
  ).toBeVisible()
  await page.reload()
  await page
    .getByRole("button", { name: "确认恢复请求结果", exact: true })
    .click()
  await expect(
    page.getByText("已安排核查，等待扫描。", { exact: true }),
  ).toBeVisible()
  await expect
    .poll(() =>
      api.calls.some((c) => c.path.includes("/submission-recoveries/")),
    )
    .toBe(true)
  expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(1)
  expect(
    api.calls.filter((c) => c.path.includes("/submission-recovery-requests/")),
  ).toHaveLength(1)
})

test("恢复403隐藏写入动作且保留已读取结果和登录", async ({ page }) => {
  await recoveryBoundary(page, "forbidden")
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(
    page.getByText("当前角色无权执行恢复操作。", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /核查待核实项|重试失败步骤/ }),
  ).toHaveCount(0)
  await expect(page.getByText("真实剧目", { exact: true })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
})

test("恢复原请求404继续同键核实，切BC可取消且不删除原记录", async ({
  page,
}) => {
  const { BC2 } = await import("./utils/buildsBoundary")
  const api = await recoveryBoundary(page, "missing")
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await page
    .getByRole("button", { name: "确认恢复请求结果", exact: true })
    .click()
  await expect(
    page.getByText("尚未查到原请求，不能据此重新安排。请继续核实。", {
      exact: true,
    }),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await expect(
    page.getByRole("heading", { name: "有未保存的内容", exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  expect(new URL(page.url()).searchParams.get("bc_id")).toBe(BC)
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await page
    .getByRole("button", { name: "丢弃未保存修改", exact: true })
    .click()
  await expect(page).toHaveURL((url) => url.searchParams.get("bc_id") === BC2)
  expect(
    await page.evaluate(
      ({ T, BC, ID }) =>
        JSON.parse(
          sessionStorage.getItem(`submission-recovery:${T}:${BC}:${ID}`) ||
            "null",
        )?.requestId,
      { T, BC, ID },
    ),
  ).toBe(api.progress.request_id)
  expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(1)
})

test("失败和未知同时存在，重试仅使用服务端允许的失败步骤", async ({ page }) => {
  const api = await recoveryBoundary(page)
  api.summary.recovery.can_retry = true
  api.summary.recovery.retryable_step_count = 3
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: "核查待核实项（1）", exact: true }),
  ).toBeVisible()
  await page
    .getByRole("button", { name: "重试失败步骤（3）", exact: true })
    .click()
  await expect(
    page.getByText("已安排重试，等待扫描。", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "核查待核实项（1）", exact: true }),
  ).toBeDisabled()
  expect(
    api.calls.filter((c) => c.method === "POST").map((c) => c.path),
  ).toEqual([`/api/tenants/${T}/submissions/${ID}/retry`])
  await page.getByRole("button", { name: "查看明细", exact: true }).click()
  await expect(page.getByText("campaign-real", { exact: true })).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("1")
  api.progress.state = "FAILED"
  api.progress.scheduled_count = 2
  await page.getByRole("button", { name: "刷新恢复进度", exact: true }).click()
  await expect(
    page.getByText("扫描未完成，已安排 2 个步骤；已安排的工作保持现状。", {
      exact: true,
    }),
  ).toBeVisible()
})

test("恢复无候选409刷新服务端能力，不模拟安排成功", async ({ page }) => {
  const api = await recoveryBoundary(page, "no-candidates")
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(
    page.getByText("当前已没有符合条件的步骤，已重新读取任务结果。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: /核查待核实项/ })).toHaveCount(
    0,
  )
  await expect(
    page.getByText("已安排核查，等待扫描。", { exact: true }),
  ).toHaveCount(0)
  expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(1)
})

test("恢复401失效登录并保留合法任务回跳", async ({ page }) => {
  await recoveryBoundary(page, "unauthorized")
  const dialogs: string[] = []
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.type())
    void dialog.dismiss()
  })
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(page).toHaveURL(/\/login/)
  expect(
    await page.evaluate(() => localStorage.getItem("access_token")),
  ).toBeNull()
  expect(dialogs).toEqual([])
})

test("原恢复回查401允许登录且保留尚未知的原请求编号", async ({ page }) => {
  const api = await recoveryBoundary(page, "lost")
  const dialogs: string[] = []
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.type())
    void dialog.dismiss()
  })
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "核查待核实项（1）", exact: true })
    .click()
  await expect(
    page.getByText("恢复请求结果尚未确认。", { exact: true }),
  ).toBeVisible()
  await page.route("**/api/tenants/*/submission-recovery-requests/*", (route) =>
    route.fulfill({ status: 401, json: { code: "not_authenticated" } }),
  )
  await page
    .getByRole("button", { name: "确认恢复请求结果", exact: true })
    .click()
  await expect(page).toHaveURL(/\/login/)
  expect(dialogs).toEqual([])
  expect(
    await page.evaluate(
      ({ T, BC, ID }) =>
        JSON.parse(
          sessionStorage.getItem(`submission-recovery:${T}:${BC}:${ID}`) ||
            "null",
        )?.requestId,
      { T, BC, ID },
    ),
  ).toBe(api.progress.request_id)
})

for (const [code, label] of [
  ["cover_pending", "正在准备目标账户的视频封面"],
  ["cover_result_unknown", "封面上传结果待核实，请核查原任务，勿重复上传"],
  ["cover_permission_unverified", "当前连接的封面读写权限尚未核实"],
  ["cover_video_changed", "目标视频或连接已变化，请重新准备当前素材"],
  ["cover_evidence_stale", "封面核实已过期，需要重新读取平台结果"],
]) {
  test(`封面状态 ${code} 显示中文原因`, async ({ page }) => {
    const api = await boundary(page)
    api.step.error_code = code
    api.step.kind = "MATERIAL"
    await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues`)
    await expect(page.getByText(label, { exact: true })).toBeVisible()
    await expect(page.getByText(code, { exact: true })).toBeVisible()
  })
}

test("完成任务不展示空恢复提示并保留预算大数精度", async ({ page }) => {
  const api = await boundary(page)
  api.summary.status = "COMPLETED"
  api.summary.recovery_mode = "BLOCKED"
  api.summary.daily_budget_sum = "9007199254740993123456.123400000000"
  api.summary.recovery = {
    can_retry: false,
    can_reconcile: false,
    retryable_step_count: 0,
    reconcilable_step_count: 0,
    reasons: ["recovery_no_candidates"],
  }
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(page.getByText("任务连接需要检查", { exact: true })).toHaveCount(
    0,
  )
  await expect(
    page.getByText("当前没有需要重试或核查的步骤。", { exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByText("recovery_no_candidates", { exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByText(/配置日预算合计 USD 9007199254740993123456\.1234/),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /重试失败步骤|核查待核实项/ }),
  ).toHaveCount(0)
})

for (const width of [1920, 1440, 390]) {
  test(`完成任务 ${width}px 优先呈现结果与明细，技术信息按需展开`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const api = await boundary(page, { longLabels: true })
    Object.assign(api.summary, {
      status: "COMPLETED",
      recovery_mode: "BLOCKED",
      provider_name: "测试版权方 · 海外短剧素材服务连接",
      strategy_label: "IAA 目标回收策略 v3",
      succeeded: counts(1, 1, 2),
      unknown: counts(0, 0, 0),
      stage_counts: { "MATERIAL:SUCCEEDED": 2 },
      recovery: {
        can_retry: false,
        can_reconcile: false,
        reasons: ["recovery_no_candidates"],
      },
    })
    await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
    await expect(page.getByRole("button", { name: "查看明细" })).toBeVisible()
    await expect(
      page.getByText("任务连接需要检查", { exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("region", { name: "任务恢复操作" }),
    ).toHaveCount(0)
    await expect(page.getByRole("region", { name: "执行连接" })).toHaveCount(0)
    await expect(page.getByRole("tab", { name: /排除项/ })).toHaveCount(0)
    await expect(page.getByText("真实Campaign", { exact: true })).toHaveCount(0)
    await expect(page.getByTestId("count-ad-succeeded")).not.toBeVisible()
    await expectWorkspaceLayout(page)
    const metricRows = await page
      .locator('[aria-label="创建数量摘要"] dd')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getBoundingClientRect().y),
      )
    expect(Math.max(...metricRows) - Math.min(...metricRows)).toBeLessThan(1)
    if (width >= 1440) {
      const box = await page
        .getByRole("button", { name: "查看明细" })
        .boundingBox()
      expect(box!.y + box!.height).toBeLessThan(700)
    }
    await page.screenshot({
      path: testInfo.outputPath(`completed-${width}.png`),
      fullPage: true,
    })
    await page.getByText("完整统计与执行阶段", { exact: true }).click()
    await expect(page.getByTestId("count-ad-succeeded")).toHaveText("2")
    await expect(page.getByTestId("count-ad-succeeded")).toBeVisible()
    await page.getByRole("button", { name: "技术详情", exact: true }).click()
    await expect(page.getByRole("region", { name: "执行连接" })).toBeVisible()
    await expect(page.getByText("原搭建连接", { exact: true })).toBeVisible()
    expect(api.requests.every((r) => r.method === "GET")).toBe(true)
  })
}

test("无核查能力但可以重试时，不误报授权失效", async ({ page }) => {
  const api = await boundary(page)
  api.summary.status = "FAILED"
  api.summary.recovery_mode = "BLOCKED"
  api.summary.recovery = {
    can_retry: true,
    can_reconcile: false,
    retryable_step_count: 1,
    reconcilable_step_count: 0,
    reasons: [],
  }
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: "重试失败步骤（1）" }),
  ).toBeVisible()
  await expect(page.getByText("任务连接需要检查", { exact: true })).toHaveCount(
    0,
  )
  expect(api.requests.every((r) => r.method === "GET")).toBe(true)
})

test("真实权限阻断保留具体原因与连接检查入口", async ({ page }) => {
  const api = await boundary(page)
  api.summary.recovery_mode = "BLOCKED"
  api.summary.recovery = {
    can_retry: false,
    can_reconcile: false,
    retryable_step_count: 0,
    reconcilable_step_count: 0,
    reasons: ["account_access_denied"],
  }
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByText("当前账户缺少搭建权限", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /重试失败步骤|核查待核实项/ }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "技术详情", exact: true }).click()
  const authorizationLink = page.getByRole("link", { name: "查看连接与授权" })
  await expect(authorizationLink).toBeVisible()
  const target = new URL(
    (await authorizationLink.getAttribute("href"))!,
    page.url(),
  )
  expect(target.pathname).toBe(`/tenants/${T}/accounts`)
  expect(target.searchParams.get("bc_id")).toBe(BC)
  expect(target.searchParams.get("tab")).toBe("connections")
  expect(api.requests.every((r) => r.method === "GET")).toBe(true)
})

test("操作记录先呈现中文与业务对象，原始事件和编号可展开", async ({ page }) => {
  const api = await boundary(page)
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=events`)
  await expect(
    page.getByText("广告 · 结果待核实", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("真实账户", { exact: true })).toBeVisible()
  await expect(page.getByText("步骤编号", { exact: true })).not.toBeVisible()
  await page.getByText("技术记录", { exact: true }).click()
  await expect(page.getByText("步骤编号", { exact: true })).toBeVisible()
  await expect(
    page.getByText("UNKNOWN · 尝试序号 1", { exact: true }),
  ).toBeVisible()
  expect(api.requests.every((r) => r.method === "GET")).toBe(true)
})

test("续建任务展示复用计数并保留来源入口", async ({ page }) => {
  const api = await boundary(page)
  const source = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  Object.assign(api.summary, {
    continuation_source_ids: [source],
    reused: counts(1, 0, 0),
  })
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByText(
      "已复用 1 个系列 · 0 个广告组 · 0 条广告；成功数量包含这些已有对象。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "查看原搭建任务", exact: true }),
  ).toHaveAttribute("href", `/tenants/${T}/build-tasks/${source}?bc_id=${BC}`)
})

test("旧任务说明已由新版接续并提供新任务入口", async ({ page }) => {
  const api = await boundary(page)
  const newer = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  Object.assign(api.summary, { continued_by_submission_ids: [newer] })
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}`)
  await expect(
    page.getByText(
      "这份任务的部分范围已由新预览接续，已接续范围不会在此重复创建。历史结果保留。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "查看续建任务", exact: true }),
  ).toHaveAttribute("href", `/tenants/${T}/build-tasks/${newer}?bc_id=${BC}`)
})


test("失败列表直接显示平台具体原因，无需打开操作记录", async ({ page }) => {
  const api = await boundary(page)
  api.step.status = "FAILED"
  api.step.error_code = "tiktok_business_error"
  api.step.error_message = "Invalid landing page URL. Please check and try again."
  api.step.remote_code = 40002
  await page.goto(`/tenants/${T}/build-tasks/${ID}?bc_id=${BC}&tab=issues&result=FAILED`)
  await expect(page.getByText(/Invalid landing page URL.*TikTok 40002/)).toBeVisible()
  expect(api.requests.filter((r) => r.path.endsWith("/events"))).toHaveLength(0)
})
