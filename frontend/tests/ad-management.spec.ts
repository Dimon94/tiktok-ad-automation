import { expect, test } from "@playwright/test"
import {
  parsePastedRows,
  renameRuleFrom,
} from "../src/features/ad-management/renameRules"
import {
  type AdsBoundaryOptions,
  adsBoundary,
  BC_A,
  TENANT,
} from "./utils/adsBoundary"

const selectionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
const previewId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
const taskId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
const ruleTaskId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

const CAMPAIGN_REF = {
  tenant_id: TENANT,
  advertiser_id: "adv-1",
  kind: "campaign",
  remote_id: "campaign-1",
}
const GROUP_REF = {
  tenant_id: TENANT,
  advertiser_id: "adv-1",
  kind: "adgroup",
  remote_id: "group-zero",
}
const PARENT_REF = {
  tenant_id: TENANT,
  advertiser_id: "adv-1",
  kind: "campaign",
  remote_id: "campaign-paused",
}

const route = {
  tenant_id: TENANT,
  bc_id: BC_A,
  connection_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  channel: "OFFICIAL_API",
  authorization_revision: 1,
  binding_revision: 1,
  adapter_contract_revision: "test-v1",
}

async function managementBoundary(
  page: Parameters<typeof adsBoundary>[0],
  options: {
    viewer?: boolean
    reportOptions?: AdsBoundaryOptions
    unsupported?: boolean
    blockedParent?: boolean
    previewError?: { status: number; code: string; message: string }
  } = {},
) {
  await adsBoundary(page, options.reportOptions)
  const directBodies: Array<Record<string, unknown>> = []
  const previewBodies: Array<Record<string, unknown>> = []
  const listQueries: Array<Record<string, string>> = []
  const renameSelectionBodies: Array<Record<string, unknown>> = []
  await page.route("**/api/**", async (handler) => {
    const request = handler.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (
      request.method() === "POST" &&
      path.endsWith("/ad-management-requests")
    ) {
      directBodies.push(request.postDataJSON())
      return handler.fulfill({
        status: 202,
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "PREPARING",
          counts: { selected: 1, prepared: 0, targets: 0 },
        },
      })
    }
    if (options.viewer && path === "/api/me/tenants")
      return handler.fulfill({
        json: {
          items: [
            {
              id: TENANT,
              name: "报表租户",
              active: true,
              role: "viewer",
              default_bc_id: BC_A,
            },
          ],
          next_cursor: null,
          total: 1,
        },
      })
    // 表格改名解析：第一行命中 campaign-1，第二行在本 BC 目录里找不到。
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-rename-selections`)
    ) {
      renameSelectionBodies.push(request.postDataJSON())
      return handler.fulfill({
        json: {
          selection: {
            selection_id: selectionId,
            snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            refs: [CAMPAIGN_REF],
            material_uses: [],
            membership_digest: "m".repeat(64),
            expires_at: "2099-01-01T00:00:00Z",
          },
          problems: [
            {
              campaign_id: "1799999999999999999",
              new_name: "x",
              reason: "not_found",
            },
          ],
        },
      })
    }
    if (path.endsWith(`/tenants/${TENANT}/ad-selections`))
      return handler.fulfill({
        json: {
          selection_id: selectionId,
          snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          refs: [
            {
              tenant_id: TENANT,
              advertiser_id: "adv-1",
              kind: "campaign",
              remote_id: "campaign-1",
            },
          ],
          material_uses: [],
          membership_digest: "m".repeat(64),
          expires_at: "2099-01-01T00:00:00Z",
        },
      })
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-previews`)
    ) {
      const body = request.postDataJSON() as {
        mutation?: {
          field?: string
          excluded_refs?: unknown[]
          include_parents?: unknown[]
        }
      }
      previewBodies.push(body)
      if (options.previewError)
        return handler.fulfill({
          status: options.previewError.status,
          json: {
            code: options.previewError.code,
            message: options.previewError.message,
            retryable: false,
          },
        })
      const excluded = body.mutation?.excluded_refs ?? []
      const parents = body.mutation?.include_parents ?? []
      // 默认选中的系列是可修改项；不可用、暂停上级两种场景各自追加对应行。
      // 改名预览返回新旧名称，并提示改名后剧名发生变化。
      const items: Array<Record<string, unknown>> = [
        body.mutation?.field === "name"
          ? {
              ref: CAMPAIGN_REF,
              name: "嘉书-总裁归来-测试",
              original_value: "嘉书-总裁归来-测试",
              final_value: "嘉书-剧A-1015",
              execution_result: "PENDING",
              reason: null,
              drama_name: "剧A",
              provider_label: "嘉书",
              drama_changed: true,
            }
          : {
              ref: CAMPAIGN_REF,
              name: "嘉书-总裁归来-测试",
              original_value: "20",
              final_value: "25",
              execution_result: "PENDING",
              reason: null,
            },
      ]
      if (options.unsupported && excluded.length === 0)
        items.push({
          ref: GROUP_REF,
          name: "预算为零的广告组",
          original_value: null,
          final_value: null,
          execution_result: "UNSUPPORTED",
          reason: "configuration_invalid",
        })
      if (options.unsupported && excluded.length > 0)
        items.push({
          ref: GROUP_REF,
          execution_result: "UNSUPPORTED",
          reason: "excluded_ref",
        })
      if (options.blockedParent)
        items.push({
          ref: PARENT_REF,
          name: "暂停中的上级系列",
          final_value: "ENABLE",
          execution_result: parents.length ? "PENDING" : "NO_CHANGE",
          reason: parents.length ? null : "parent_disabled",
        })
      return handler.fulfill({
        status: 201,
        json: {
          preview_id: previewId,
          digest: "p".repeat(64),
          created_at: "2026-10-01T00:00:00Z",
          expires_at: "2099-01-01T00:05:00Z",
          bc_id: BC_A,
          route,
          items,
          counts: {
            selected: 1,
            targets: items.filter((i) => i.execution_result === "PENDING")
              .length,
            linked: 0,
            unsupported: items.filter(
              (i) => i.execution_result === "UNSUPPORTED",
            ).length,
          },
        },
      })
    }
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-tasks`)
    )
      return handler.fulfill({
        status: 202,
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "QUEUED",
          counts: { selected: 1, targets: 1, linked: 0, unsupported: 0 },
        },
      })
    if (
      request.method() === "POST" &&
      path.endsWith(`/ad-management-tasks/${taskId}/refresh`)
    )
      return handler.fulfill({
        status: 202,
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "RUNNING",
          counts: { selected: 1, targets: 1, refresh_pending: 1 },
        },
      })
    if (request.method() === "GET" && path.endsWith("/ad-management-tasks")) {
      const query = new URL(request.url()).searchParams
      listQueries.push(Object.fromEntries(query))
      const rows = [
        {
          task_id: taskId,
          bc_id: BC_A,
          status: "QUEUED",
          counts: { selected: 1, targets: 1, linked: 0, unsupported: 0 },
          created_at: "2026-10-14T02:30:00Z",
          actor_name: "投放同学",
          mutation: { field: "status", mode: "set", value: "DISABLE" },
          source: { kind: "manual" },
          target_kinds: { campaign: 1 },
        },
        {
          task_id: ruleTaskId,
          bc_id: BC_A,
          status: "PARTIAL",
          counts: { selected: 3, targets: 3, accepted: 2, rejected: 1 },
          created_at: "2026-10-14T01:00:00Z",
          actor_name: "投放同学",
          mutation: { field: "budget", mode: "increase_percent", value: "20" },
          source: { kind: "rule", rule_name: "自动加预算" },
          target_kinds: { adgroup: 3 },
        },
      ].filter(
        (row) => query.get("source") !== "rule" || row.source.kind === "rule",
      )
      // 仅返回当前页，翻页后不应保留首页记录。
      const page = query.get("cursor")
        ? [
            {
              ...rows[0],
              task_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
              created_at: "2026-10-13T08:00:00Z",
              mutation: { field: "roas", mode: "set", value: "1.5" },
            },
          ]
        : rows
      return handler.fulfill({
        json: {
          items: page,
          next_cursor: query.get("cursor") ? null : "next-page",
          total: 51,
        },
      })
    }
    if (
      request.method() === "GET" &&
      path.endsWith(`/ad-management-tasks/${taskId}`)
    )
      return handler.fulfill({
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "QUEUED",
          counts: {
            selected: 1,
            targets: 1,
            linked: 0,
            unsupported: 0,
            refresh_pending: 1,
          },
          items: [
            {
              ref: {
                tenant_id: TENANT,
                advertiser_id: "adv-1",
                kind: "campaign",
                remote_id: "campaign-1",
              },
              execution_result: "ACCEPTED",
              delivery_status: "DELIVERED",
              observation_state: "REFRESH_PENDING",
              attempts: [
                {
                  attempt: 1,
                  request_at: "2026-10-01T00:00:00Z",
                  outcome: "ACCEPTED",
                  request_id: "req-1",
                },
              ],
            },
          ],
        },
      })
    return handler.fallback()
  })
  return { directBodies, previewBodies, listQueries, renameSelectionBodies }
}

test.describe("广告管理工作台", () => {
  test("任务列表显示状态且 viewer 隐藏写入口", async ({ page }) => {
    await managementBoundary(page, { viewer: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await expect(
      page.getByRole("button", { name: "批量改名", exact: true }),
    ).toHaveCount(0)
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    // 只读成员只看到计数和取消选择，不出现任何写入动作。
    await expect(page.getByText("已选 1 条")).toBeVisible()
    await expect(page.getByRole("button", { name: "取消选择" })).toBeVisible()
    for (const name of [
      "启用",
      "暂停",
      "修改目标 ROAS",
      "修改预算",
      "批量改名",
      "按表格改名",
    ])
      await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
        0,
      )
    await page.goto(`/tenants/${TENANT}/ad-management-tasks?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "操作记录" })).toBeVisible()
    await expect(page.getByText("排队中")).toBeVisible()
    await page
      .getByRole("link", { name: `查看明细 ${taskId}`, exact: true })
      .click()
    await expect(
      page.getByText("当前为只读成员，隐藏取消、重试和恢复入口。"),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "取消未发送项" }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: /刷新重试/ })).toHaveCount(0)
  })

  test("操作记录复用服务端分页，翻页替换、刷新当前页，筛选和条数重置首页", async ({
    page,
  }) => {
    const { listQueries } = await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ad-management-tasks?bc_id=${BC_A}`)
    const manual = page.getByRole("row", { name: /暂停/ })
    await expect(manual).toContainText("系列 1")
    await expect(manual).toContainText("人工 · 投放同学")
    await expect(manual).toContainText("排队中")
    const byRule = page.getByRole("row", { name: /预算 提高 20%/ })
    await expect(byRule).toContainText("广告组 3")
    await expect(byRule).toContainText("自动规则 · 自动加预算")
    await expect(byRule).toContainText("部分完成")
    await expect(byRule).toContainText("成功 2")
    await expect(byRule).toContainText("失败 1")
    await expect(page.getByText("共 51 条 · 第 1 / 2 页")).toBeVisible()
    await expect(page.getByRole("button", { name: "上一页" })).toBeDisabled()
    await expect(page.getByRole("button", { name: "加载更多" })).toHaveCount(0)

    await page.getByRole("button", { name: "下一页" }).click()
    await expect(
      page.getByRole("row", { name: /目标 ROAS 设为 1.5/ }),
    ).toBeVisible()
    await expect(manual).toHaveCount(0)
    await expect(byRule).toHaveCount(0)
    await expect(page.getByText("共 51 条 · 第 2 / 2 页")).toBeVisible()
    await expect(page.getByRole("button", { name: "下一页" })).toBeDisabled()
    expect(listQueries[listQueries.length - 1]?.cursor).toBe("next-page")
    const beforeRefresh = listQueries.length
    await page.getByRole("button", { name: "刷新", exact: true }).click()
    await expect.poll(() => listQueries.length).toBe(beforeRefresh + 1)
    expect(listQueries.at(-1)?.cursor).toBe("next-page")
    await page.getByRole("button", { name: "上一页" }).click()
    await expect(manual).toBeVisible()
    await expect(
      page.getByRole("row", { name: /目标 ROAS 设为 1.5/ }),
    ).toHaveCount(0)
    await page.getByRole("button", { name: "下一页" }).click()
    await expect(page.getByText("共 51 条 · 第 2 / 2 页")).toBeVisible()

    await page.getByRole("combobox", { name: "每页条数" }).click()
    await page.getByRole("option", { name: "100 条" }).click()
    await expect(page.getByText("共 51 条 · 第 1 / 1 页")).toBeVisible()
    expect(listQueries.at(-1)?.limit).toBe("100")
    expect(listQueries.at(-1)?.cursor).toBeUndefined()
    await page.getByRole("combobox", { name: "每页条数" }).click()
    await page.getByRole("option", { name: "50 条" }).click()
    await page.getByRole("button", { name: "下一页" }).click()
    await expect(page.getByText("共 51 条 · 第 2 / 2 页")).toBeVisible()

    await page.getByRole("combobox", { name: "来源" }).click()
    await page.getByRole("option", { name: "自动规则" }).click()
    await expect(page).toHaveURL(/source=rule/)
    await expect(page.getByRole("row", { name: /暂停/ })).toHaveCount(0)
    await expect(page.getByRole("row", { name: /预算 提高 20%/ })).toBeVisible()
    expect(listQueries[listQueries.length - 1]).toMatchObject({
      bc_id: BC_A,
      source: "rule",
    })
    expect(listQueries[listQueries.length - 1]?.cursor).toBeUndefined()
    await expect(page.getByRole("button", { name: "上一页" })).toBeDisabled()
    // 浏览器回退也重置页游标，不恢复另一筛选范围的第二页。
    await page.goBack()
    await expect(manual).toBeVisible()
    await expect(page.getByRole("button", { name: "上一页" })).toBeDisabled()
  })

  test("账户维度不提供修改目标 ROAS", async ({ page }) => {
    await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("checkbox").nth(1).check()
    for (const name of ["启用", "暂停", "修改预算"])
      await expect(
        page.getByRole("button", { name, exact: true }),
      ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "修改目标 ROAS" }),
    ).toHaveCount(0)
  })

  test("取消选择恢复列设置，选择全部匹配结果在工具栏", async ({ page }) => {
    await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await expect(page.getByRole("button", { name: "列设置" })).toBeVisible()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "选择全部匹配结果" }).click()
    await expect(page.getByText(/已选全部 \d+ 条匹配结果/)).toBeVisible()
    await expect(
      page.getByRole("button", { name: "选择全部匹配结果" }),
    ).toBeDisabled()
    await page.getByRole("button", { name: "取消选择" }).click()
    await expect(page.getByRole("button", { name: "列设置" })).toBeVisible()
    await expect(
      page.getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" }),
    ).not.toBeChecked()
  })
})

test.describe("parsePastedRows", () => {
  test("忽略表头，支持 Tab 与逗号分隔", () => {
    expect(
      parsePastedRows(
        "推广系列ID\t新名称\n1700000000000000001\t嘉书-剧A\n\n1700000000000000002,网眼-剧B,第二季\r\n",
      ),
    ).toEqual({
      rows: [
        { campaignId: "1700000000000000001", newName: "嘉书-剧A" },
        { campaignId: "1700000000000000002", newName: "网眼-剧B,第二季" },
      ],
      errors: [],
    })
  })

  test("首行是数字 ID 时不当表头，缺少新名称的行报错", () => {
    expect(
      parsePastedRows("1700000000000000001,剧A\n1700000000000000002"),
    ).toEqual({
      rows: [{ campaignId: "1700000000000000001", newName: "剧A" }],
      errors: ["第 2 行缺少推广系列 ID 或新名称"],
    })
  })

  test("超过 500 行返回错误", () => {
    const text = Array.from(
      { length: 501 },
      (_, index) =>
        `17000000000000${String(index).padStart(5, "0")},名称${index}`,
    ).join("\n")
    expect(parsePastedRows(text).errors).toEqual([
      "单次最多 500 行，请分批粘贴",
    ])
  })

  test("格式异常的首行不当表头，交给服务器校验 ID", () => {
    expect(parsePastedRows("1.7E+18\t剧A\n１７００\t剧B")).toEqual({
      rows: [
        { campaignId: "1.7E+18", newName: "剧A" },
        { campaignId: "１７００", newName: "剧B" },
      ],
      errors: [],
    })
  })

  test("首列含空格或全角逗号时提示改用 Tab 或逗号分隔", () => {
    expect(
      parsePastedRows("1700000000000000001 剧A\n1700000000000000002，剧B")
        .errors,
    ).toEqual([
      "第 1 行未分出两列，请用 Tab 或逗号分隔推广系列 ID 和新名称",
      "第 2 行未分出两列，请用 Tab 或逗号分隔推广系列 ID 和新名称",
    ])
  })

  test("同一 ID 粘贴多次时以第一行的新名称为准", () => {
    expect(
      renameRuleFrom({
        kind: "explicit",
        find: "",
        replaceWith: "",
        prefix: "",
        suffix: "",
        text: "1700000000000000001\t剧A\n1700000000000000001\t剧B",
      }),
    ).toEqual({
      kind: "explicit",
      names: { "1700000000000000001": "剧A" },
    })
  })
})

for (const paged of [false, true]) {
  test(`启用回读更新当前目录，冻结报表、分页、筛选与勾选保持到明确重查（${paged ? "第二页" : "首页与详情"}）`, async ({
    page,
  }) => {
    await page.clock.install()
    const reportOptions: AdsBoundaryOptions = {
      paged,
      campaignOperationStatus: "DISABLE",
    }
    await managementBoundary(page, { reportOptions })
    let version = "before"
    await page.route("**/ad-sync-status?*", (route) =>
      route.fulfill({
        json: {
          accounts: [],
          publication_version: version,
          plan: { enabled: true, bc_report_supported: true, slowed: false },
        },
      }),
    )
    const queries: URLSearchParams[] = []
    page.on("request", (req) => {
      const url = new URL(req.url())
      if (url.pathname.endsWith(`/tenants/${TENANT}/ads`))
        queries.push(url.searchParams)
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page.getByRole("textbox", { name: "搜索广告" }).fill("总裁归来")
    await page.getByRole("button", { name: "应用筛选" }).click()
    if (paged)
      await page.getByRole("button", { name: "下一页", exact: true }).click()
    const row = page.getByRole("row", {
      name: paged ? /嘉书-第二页-测试/ : /嘉书-总裁归来-测试/,
    })
    await expect(row.getByText("已暂停", { exact: true })).toBeVisible()
    await row.getByRole("checkbox").check()
    await page.getByRole("button", { name: "启用", exact: true }).click()
    await expect(
      page.getByRole("link", { name: "查看操作记录", exact: true }),
    ).toBeVisible()
    // 提交受理尚未等于成功，继续显示真实旧状态；后续用户的勾选必须保留。
    await expect(row.getByText("已暂停", { exact: true })).toBeVisible()
    await row.getByRole("checkbox").check()
    {
      await page.route("**/ads/campaign/campaign-1?*", (route) =>
        route.fulfill({
          json: {
            statuses: {
              operation: reportOptions.campaignOperationStatus,
              review: "APPROVED",
            },
            configuration: {},
          },
        }),
      )
      await row
        .getByRole("button", { name: paged ? "嘉书-第二页-测试" : "嘉书-总裁归来-测试", exact: true })
        .click()
      await expect(
        page
          .getByTestId("ads-detail-header")
          .getByText("已暂停", { exact: true }),
      ).toBeVisible()
    }
    const beforePublication = queries.length
    const heldQuery = queries.at(-1)?.toString()
    reportOptions.campaignOperationStatus = "ENABLE"
    version = "directory-published"
    await page.clock.fastForward(3_000)
    {
      await expect(
        page
          .getByTestId("ads-detail-header")
          .getByText("已开启", { exact: true }),
      ).toBeVisible()
      await expect(page.getByTitle("ENABLE", { exact: true })).toHaveText(
        "已开启",
      )
      await page
        .getByRole("dialog")
        .getByRole("button", { name: "Close", exact: true })
        .click()
    }
    await expect(row.getByText("已暂停", { exact: true })).toBeVisible()
    expect(queries).toHaveLength(beforePublication)
    await expect(row.getByRole("checkbox")).toBeChecked()
    await expect(page.getByRole("textbox", { name: "搜索广告" })).toHaveValue(
      "总裁归来",
    )
    expect(queries.at(-1)?.toString()).toBe(heldQuery)
    expect(queries.at(-1)?.get("cursor")).toBe(paged ? "cursor-1" : null)

    expect(queries.at(-1)?.get("query")).toBe("总裁归来")
    await page.getByRole("button", { name: "有新数据，点击加载", exact: true }).click()
    await expect(page.getByRole("row", { name: /嘉书-总裁归来-测试/ }).getByText("已开启", { exact: true })).toBeVisible()
    expect(queries.at(-1)?.get("snapshot_id")).toBeNull()
    expect(queries.at(-1)?.get("cursor")).toBeNull()
    await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
  })
}

async function selectCampaign(page: Parameters<typeof adsBoundary>[0]) {
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page.getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" }).check()
}

test("启停点击直接受理，不发预览请求、不再确认", async ({ page }) => {
  const boundary = await managementBoundary(page)
  await selectCampaign(page)
  await page.getByRole("button", { name: "暂停", exact: true }).click()
  await expect.poll(() => boundary.directBodies.length).toBe(1)
  expect(boundary.previewBodies).toEqual([])
  expect(boundary.directBodies[0]).toMatchObject({
    selection_id: selectionId,
    bc_id: BC_A,
    mutation: { field: "status", mode: "set", value: "DISABLE" },
  })
  await expect(page.getByRole("heading", { name: "批量暂停" })).toHaveCount(0)
  await expect(
    page.getByRole("link", { name: "查看操作记录", exact: true }),
  ).toBeVisible()
})

test("数值只提交参数，准备由后台完成", async ({ page }) => {
  const boundary = await managementBoundary(page)
  await selectCampaign(page)
  await page.getByRole("button", { name: "修改预算", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "提交管理任务" }),
  ).toBeDisabled()
  await page.getByRole("textbox", { name: "管理新值" }).fill("100")
  await page.getByRole("button", { name: "提交管理任务" }).click()
  await expect.poll(() => boundary.directBodies.length).toBe(1)
  expect(boundary.previewBodies).toEqual([])
  expect(boundary.directBodies[0]?.mutation).toMatchObject({
    field: "budget",
    mode: "set",
    value: "100",
  })
})

test("提交失败重试保留幂等键", async ({ page }) => {
  await managementBoundary(page)
  const bodies: Record<string, unknown>[] = []
  await page.route("**/ad-management-requests", (handler) => {
    bodies.push(handler.request().postDataJSON())
    return handler.fulfill(
      bodies.length === 1
        ? { status: 503, json: { detail: "暂时不可用" } }
        : {
            status: 202,
            json: {
              task_id: taskId,
              bc_id: BC_A,
              status: "PREPARING",
              counts: {},
            },
          },
    )
  })
  await selectCampaign(page)
  await page.getByRole("button", { name: "暂停", exact: true }).click()
  await expect.poll(() => bodies.length).toBe(1)
  await page.getByRole("button", { name: "重试提交" }).click()
  await expect.poll(() => bodies.length).toBe(2)
  expect(bodies[0]?.idempotency_key).toBe(bodies[1]?.idempotency_key)
})

test("未勾选也只有一个改名入口，规则改名需勾选、表格粘贴可直接提交", async ({
  page,
}) => {
  const boundary = await managementBoundary(page)
  const selections: string[] = []
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith("/ad-selections"))
      selections.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(
    page.getByRole("button", { name: "批量改名", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "批量改名", exact: true }),
  ).toHaveCount(1)
  await expect(
    page.getByRole("button", { name: "按表格改名", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "批量改名", exact: true }).click()
  await expect(
    page.getByRole("tab", { name: "表格粘贴", exact: true }),
  ).toHaveAttribute("data-state", "active")
  await page.getByRole("tab", { name: "查找替换", exact: true }).click()
  await page.getByRole("textbox", { name: "查找", exact: true }).fill("测试")
  await expect(
    page.getByText(
      "请先在列表勾选要改名的推广系列，或切换到“表格粘贴”按 ID 改名。",
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "提交管理任务" }),
  ).toBeDisabled()
  await page.getByRole("tab", { name: "前缀后缀", exact: true }).click()
  await page.getByRole("textbox", { name: "前缀", exact: true }).fill("【停】")
  await expect(
    page.getByRole("button", { name: "提交管理任务" }),
  ).toBeDisabled()
  await page.getByRole("tab", { name: "表格粘贴", exact: true }).click()
  await page
    .getByRole("textbox", { name: "粘贴推广系列 ID 和新名称" })
    .fill("1700000000000000001\t新名称")
  await page.getByRole("button", { name: "提交管理任务" }).click()
  await expect.poll(() => boundary.directBodies.length).toBe(1)
  expect(selections).toEqual([])
  expect(boundary.renameSelectionBodies).toHaveLength(1)
  expect(boundary.renameSelectionBodies[0]).toMatchObject({
    bc_id: BC_A,
    rows: [{ campaign_id: "1700000000000000001", new_name: "新名称" }],
  })
})

for (const kind of ["replace", "affix"] as const) {
  test(`勾选后统一改名入口默认查找替换，${kind}使用勾选范围`, async ({
    page,
  }) => {
    const boundary = await managementBoundary(page)
    await selectCampaign(page)
    await expect(
      page.getByRole("button", { name: "批量改名", exact: true }),
    ).toHaveCount(1)
    await expect(
      page.getByRole("button", { name: "按表格改名", exact: true }),
    ).toHaveCount(0)
    await page.getByRole("button", { name: "批量改名", exact: true }).click()
    await expect(
      page.getByRole("tab", { name: "查找替换", exact: true }),
    ).toHaveAttribute("data-state", "active")
    if (kind === "replace") {
      await page
        .getByRole("textbox", { name: "查找", exact: true })
        .fill("测试")
      await page
        .getByRole("textbox", { name: "替换为", exact: true })
        .fill("放量")
    } else {
      await page.getByRole("tab", { name: "前缀后缀", exact: true }).click()
      await page
        .getByRole("textbox", { name: "前缀", exact: true })
        .fill("【停】")
    }
    await page.getByRole("button", { name: "提交管理任务" }).click()
    await expect.poll(() => boundary.directBodies.length).toBe(1)
    expect(boundary.renameSelectionBodies).toEqual([])
    expect(boundary.directBodies[0]).toMatchObject({
      bc_id: BC_A,
      selection_id: selectionId,
      mutation: {
        field: "name",
        mode: "set",
        rename:
          kind === "replace"
            ? { kind, find: "测试", replace_with: "放量" }
            : { kind, prefix: "【停】", suffix: "" },
      },
    })
  })
}

test("勾选后可切换表格改名，按粘贴ID解析且异常行随任务保存", async ({
  page,
}) => {
  const boundary = await managementBoundary(page)
  await selectCampaign(page)
  await page.getByRole("button", { name: "批量改名", exact: true }).click()
  await page.getByRole("tab", { name: "表格粘贴", exact: true }).click()
  await page
    .getByRole("textbox", { name: "粘贴推广系列 ID 和新名称" })
    .fill("1700000000000000001\t新名称\n1799999999999999999\tx")
  await page.getByRole("button", { name: "提交管理任务" }).click()
  await expect.poll(() => boundary.directBodies.length).toBe(1)
  expect(boundary.renameSelectionBodies).toHaveLength(1)
  expect(boundary.previewBodies).toEqual([])
  expect(boundary.directBodies[0]?.row_problems).toEqual([
    { campaign_id: "1799999999999999999", new_name: "x", reason: "not_found" },
  ])
})

test("任务明细分页并显示中文准备失败原因", async ({ page }) => {
  await managementBoundary(page)
  const offsets: number[] = []
  await page.route(
    `**/api/tenants/${TENANT}/ad-management-tasks/${taskId}?*`,
    (handler) => {
      const offset = Number(
        new URL(handler.request().url()).searchParams.get("offset"),
      )
      offsets.push(offset)
      return handler.fulfill({
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "FAILED",
          counts: { selected: 10748, prepared: 200, targets: 200 },
          items_total: 200,
          items_offset: offset,
          next_items_offset: offset === 0 ? 100 : null,
          preparation_error:
            "后台准备未完成：当前权限、连接或冻结参数已不可用，请检查后重试。",
          items: [],
        },
      })
    },
  )
  await page.goto(
    `/tenants/${TENANT}/ad-management-tasks/${taskId}?bc_id=${BC_A}`,
  )
  await expect(page.getByText("准备 200 / 10748")).toBeVisible()
  await expect(page.getByText(/后台准备未完成/)).toBeVisible()
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect.poll(() => offsets.at(-1)).toBe(100)
  await expect(
    page.getByRole("button", { name: "下一页", exact: true }),
  ).toBeDisabled()
})

test("UNKNOWN只读核查异步显示观察匹配，受理仍为未知", async ({ page }) => {
  await managementBoundary(page)
  let reads = 0
  await page.route(
    `**/api/tenants/${TENANT}/ad-management-tasks/${taskId}?*`,
    (handler) => {
      reads += 1
      return handler.fulfill({
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "NEEDS_REVIEW",
          counts: { selected: 1, unknown: 1 },
          items_total: 1,
          items: [
            {
              ref: CAMPAIGN_REF,
              execution_result: "UNKNOWN",
              observation_state:
                reads === 1 ? "RECONCILE_REQUESTED" : "OBSERVED_MATCH",
            },
          ],
        },
      })
    },
  )
  await page.goto(
    `/tenants/${TENANT}/ad-management-tasks/${taskId}?bc_id=${BC_A}`,
  )
  await expect(
    page.getByRole("cell", { name: "等待平台只读核查", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("cell", { name: "观察值符合目标，受理仍未知", exact: true }),
  ).toBeVisible({ timeout: 12000 })
  await expect(
    page.getByRole("cell", { name: "结果待核查", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("cell", { name: "平台已接收", exact: true }),
  ).toHaveCount(0)
})
