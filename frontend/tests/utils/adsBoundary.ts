import type { Page } from "@playwright/test"
import METRIC_CATALOG from "../fixtures/metricCatalog.json" with {
  type: "json",
}

export const TENANT = "11111111-1111-4111-8111-111111111111"
// BC 使用路由接受的十进制字符串，覆盖大于 JS 安全整数的真实 ID 格式。
export const BC_A = "90071992547409931111"
export const BC_B = "90071992547409932222"
export const ADVERTISER = "adv-1"
const token = "ads-workspace-test-token"

export type AdsBoundaryOptions = {
  emptyRows?: boolean
  projectionStatus?: "CURRENT" | "STALE" | "BUILDING"
  lastSuccess?: string
  reportSpend?: string
  syncAccounts?: Array<{
    advertiser_id: string
    timezone: string
    local_today: string
    last_success_at: string | null
    incomplete_dates?: string[]
    failed_dates?: string[]
  }>
  slowed?: boolean
  delayedA?: boolean
  /** 让某个维度的报表响应延迟，用于验证切换维度时不显示上一维度的行。 */
  delayedDimension?: string
  paged?: boolean
  /** 分页摘要使用的冻结匹配总数。 */
  total?: number
  campaignOperationStatus?: "ENABLE" | "DISABLE"
  mixedBuckets?: boolean
  refreshStatus?: "QUEUED" | "SYNC_DISABLED" | "API_BINDING_REQUIRED"
  incomplete?: boolean
  reconciliation?: boolean
  balance?: boolean
  /** 消耗列标为 PARTIAL，但行覆盖仍为 COMPLETE（成员账户平台返回 NULL 的情形）。 */
  partialSpend?: boolean
  /** 记录趋势请求次数 */
  trendCalls?: { count: number }
  /** 只改 /api/me/tenants 返回的成员角色，用于只读成员用例。 */
  role?: "viewer" | "operator"
  /** 趋势响应的 timezone：单一账户时区（IANA）或 MIXED。 */
  trendTimezone?: string
  /** 前 N 次小时趋势响应仍在按需拉取（syncing=true 且无数据点）。 */
  hourSyncingCalls?: number
}

// 详情接口的配置取自线上实测的 TikTok 原始对象形态。
const DETAIL_CONFIGURATION: Record<string, Record<string, unknown>> = {
  campaign: {
    budget: "50",
    budget_mode: "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
    objective_type: "APP_PROMOTION",
    app_promotion_type: "MINIS",
    campaign_type: "REGULAR_CAMPAIGN",
    budget_optimize_on: false,
    smart_plus_adgroup_mode: "MULTIPLE",
    create_time: "2026-09-30 08:00:00",
    modify_time: "2026-10-01 08:00:00",
  },
  adgroup: {
    roas_bid: "1.1",
    bid_type: "BID_TYPE_NO_BID",
    deep_bid_type: "VO_MIN_ROAS",
    optimization_goal: "VALUE",
    optimization_event: "IMPRESSION_LEVEL_AD_REVENUE",
    vbo_window: "ZERO_DAY",
    billing_event: "OCPM",
    budget: "30",
    budget_mode: "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
    min_budget: null,
    schedule_type: "SCHEDULE_FROM_NOW",
    schedule_start_time: "2026-09-30 08:00:00",
    schedule_end_time: null,
    dayparting: null,
    placement_type: "PLACEMENT_TYPE_NORMAL",
    placements: ["PLACEMENT_TIKTOK"],
    targeting_spec: {
      gender: "GENDER_UNLIMITED",
      age_groups: ["AGE_18_24", "AGE_25_34"],
      location_ids: ["6252001"],
      spc_audience_age: "OVER_EIGHTEEN",
      brand_safety_type: "STANDARD_INVENTORY",
    },
    click_attribution_window: "SEVEN_DAYS",
    view_attribution_window: "ONE_DAY",
    engaged_view_attribution_window: "SEVEN_DAYS",
    attribution_event_count: "EVERY",
    minis_id: "minis-1",
    promotion_type: "MINI_APP",
    comment_disabled: false,
    share_disabled: false,
    video_download_disabled: true,
    create_time: "2026-09-30 08:00:00",
    modify_time: "2026-10-01 08:00:00",
  },
  ad: {
    ad_text_list: [{ ad_text: "总裁归来，今晚上线" }],
    landing_page_url_list: [{ landing_page_url: "https://example.com/lp" }],
    creative_list: [
      {
        creative_info: {
          ad_format: "SINGLE_VIDEO",
          video_info: { file_name: "ep1.mp4", video_id: "v-1" },
        },
      },
    ],
    ad_configuration: { identity_type: "CUSTOMIZED_USER", identity_id: "id-1" },
    create_time: "2026-09-30 08:00:00",
    modify_time: "2026-10-01 08:00:00",
  },
}
const DETAIL_IDS: Record<string, string> = {
  campaign: "campaign-1",
  adgroup: "adgroup-1",
  ad: "ad-1",
}

export async function adsBoundary(
  page: Page,
  options: AdsBoundaryOptions = {},
) {
  let hourCalls = 0
  await page.addInitScript(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (request.method() === "OPTIONS")
      return route.fulfill({ status: 204, headers })
    if (path === "/api/users/me")
      return route.fulfill({
        headers,
        json: {
          id: "44444444-4444-4444-8444-444444444444",
          username: "ads-test",
          full_name: "报表测试",
          is_active: true,
          is_superuser: false,
        },
      })
    if (path === "/api/me/tenants")
      return route.fulfill({
        headers,
        json: {
          items: [
            {
              id: TENANT,
              name: "报表租户",
              active: true,
              role: options.role ?? "tenant_admin",
              default_bc_id: BC_A,
            },
          ],
          next_cursor: null,
          total: 1,
        },
      })
    if (path.endsWith(`/tenants/${TENANT}/bcs`)) {
      return route.fulfill({
        headers,
        json: {
          items: [
            { bc_id: BC_A, name: "甲 BC" },
            { bc_id: BC_B, name: "乙 BC" },
          ],
          next_cursor: null,
          total: 2,
        },
      })
    }
    if (path.endsWith(`/tenants/${TENANT}/ads/metric-catalog`))
      return route.fulfill({ headers, json: METRIC_CATALOG })
    if (path.endsWith(`/tenants/${TENANT}/ads`)) {
      const bc = url.searchParams.get("bc_id")
      if (bc === BC_A && options.delayedA)
        await new Promise((resolve) => setTimeout(resolve, 700))
      if (
        options.delayedDimension &&
        url.searchParams.get("dimension") === options.delayedDimension
      )
        await new Promise((resolve) => setTimeout(resolve, 1500))
      const secondPage = Boolean(url.searchParams.get("cursor"))
      const name = secondPage
        ? "嘉书-第二页-测试"
        : bc === BC_B
          ? "乙-其他剧-测试"
          : "嘉书-总裁归来-测试"
      const dimension = url.searchParams.get("dimension")
      const display =
        dimension === "drama"
          ? { name: "总裁归来", drama_name: "总裁归来", provider: "嘉书" }
          : dimension === "account"
            ? {
                name: bc === BC_B ? "乙账户" : "甲账户",
                account_name: bc === BC_B ? "乙账户" : "甲账户",
                account_id: ADVERTISER,
                advertiser_id: ADVERTISER,
                remote_id: ADVERTISER,
                account_status: "STATUS_ENABLE",
              }
            : dimension === "adgroup"
              ? {
                  name,
                  account_name: "甲账户",
                  account_id: ADVERTISER,
                  campaign_name: "嘉书-总裁归来-测试",
                  campaign_id: "campaign-1",
                  adgroup_name: name,
                  adgroup_id: "adgroup-1",
                  advertiser_id: ADVERTISER,
                  remote_id: "adgroup-1",
                  operation_status: "ENABLE",
                  secondary_status: "ADGROUP_STATUS_DELIVERY_OK",
                }
              : dimension === "ad"
                ? {
                    name,
                    account_name: "甲账户",
                    account_id: ADVERTISER,
                    campaign_name: "嘉书-总裁归来-测试",
                    campaign_id: "campaign-1",
                    adgroup_name: "嘉书-总裁归来-测试",
                    adgroup_id: "adgroup-1",
                    ad_name: name,
                    ad_id: "ad-1",
                    advertiser_id: ADVERTISER,
                    remote_id: "ad-1",
                    operation_status: "ENABLE",
                    secondary_status: "AD_STATUS_DELIVERY_OK",
                  }
                : dimension === "material"
                  ? {
                      name: "素材-测试",
                      material_name: "素材-测试",
                      main_material_id: "material-1",
                      account_id: ADVERTISER,
                      account_name: "甲账户",
                      advertiser_id: ADVERTISER,
                    }
                  : {
                      name,
                      campaign_name: name,
                      account_name: "甲账户",
                      account_id: ADVERTISER,
                      campaign_id: "campaign-1",
                      drama_name: "总裁归来",
                      advertiser_id: ADVERTISER,
                      remote_id: "campaign-1",
                      operation_status:
                        options.campaignOperationStatus ?? "DISABLE",
                      secondary_status:
                        options.campaignOperationStatus === "ENABLE"
                          ? "CAMPAIGN_STATUS_ENABLE"
                          : "CAMPAIGN_STATUS_DISABLE",
                      campaign_budget: "50",
                    }
      return route.fulfill({
        headers,
        json: {
          snapshot: {
            snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            snapshot_at: "2026-10-01T05:00:00Z",
            expires_at: "2099-01-01T00:00:00Z",
            filters: {
              dimension: dimension ?? "campaign",
              start_date: "2026-10-01",
              end_date: "2026-10-01",
            },
            publication_versions: {},
          },
          metrics_observed_at: "2026-10-01T05:05:00Z",
          ordering_basis: "snapshot",
          metrics_coverage: { status: options.projectionStatus ?? "CURRENT" },
          items: [
            {
              row_key: `${bc}-campaign-1`,
              display: {
                ...display,
                ...(options.balance
                  ? {
                      balance: "12.50",
                      balance_currency: "CNY",
                      balance_scope: "PORTFOLIO",
                      balance_updated_at: "2026-10-12T00:00:00Z",
                    }
                  : {}),
                ...(options.reconciliation
                  ? { reconciliation: "DIFF", account_report_spend: "101" }
                  : {}),
              },
              // 账户行在后端裁剪了列表引用，只保留 display.account_id。
              refs:
                dimension === "account"
                  ? []
                  : [
                      {
                        tenant_id: TENANT,
                        advertiser_id: ADVERTISER,
                        kind: "campaign",
                        remote_id: "campaign-1",
                      },
                    ],
              // 素材行带一个使用位置，详情的成片预览据此选视频与候选账户。
              material_uses:
                dimension === "material"
                  ? [
                      {
                        ad_ref: {
                          tenant_id: TENANT,
                          advertiser_id: ADVERTISER,
                          kind: "ad",
                          remote_id: "ad-1",
                        },
                        platform_material_id: "vid-1",
                        ad_material_id: "am-1",
                        material_type: "VIDEO",
                      },
                    ]
                  : [],
              metric_buckets: [
                {
                  currency: "USD",
                  timezone: "UTC",
                  attribution: "IAA",
                  values: {
                    spend: options.reportSpend ?? "100",
                    revenue_d0: "120",
                    revenue_total: "150",
                    ad_impression_events: "50",
                    impressions: "1000",
                    clicks: "20",
                    roas_total: "1.5",
                    roas_d0: "1.2",
                    roas_d3: dimension === "material" ? null : "1.3",
                    roas_d7: "1.4",
                    roas_d14: "1.5",
                    roas_d30: dimension === "material" ? null : "1.6",
                    cost_per_ad_impression_event: "2",
                    ctr: "0.02",
                  },
                  availability: {
                    spend: options.partialSpend ? "PARTIAL" : "AVAILABLE",
                    revenue_d0: "AVAILABLE",
                    revenue_total: "AVAILABLE",
                    ad_impression_events: "AVAILABLE",
                    impressions: "AVAILABLE",
                    clicks: "AVAILABLE",
                    roas_total: "AVAILABLE",
                    roas_d0: "AVAILABLE",
                    roas_d3:
                      dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                    roas_d7: "AVAILABLE",
                    roas_d14: "AVAILABLE",
                    roas_d30:
                      dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                    cost_per_ad_impression_event: "AVAILABLE",
                    ctr: "AVAILABLE",
                  },
                },
                ...(options.mixedBuckets
                  ? [
                      {
                        currency: "CNY",
                        timezone: "Asia/Shanghai",
                        attribution: "IAA",
                        values: {
                          spend: options.reportSpend ?? "100",
                          revenue_d0: "120",
                          revenue_total: "150",
                          ad_impression_events: "50",
                          impressions: "1000",
                          clicks: "20",
                          roas_total: "1.5",
                          roas_d0: "1.2",
                          roas_d3: dimension === "material" ? null : "1.3",
                          roas_d7: "1.4",
                          roas_d14: "1.5",
                          roas_d30: dimension === "material" ? null : "1.6",
                          cost_per_ad_impression_event: "2",
                          ctr: "0.02",
                        },
                        availability: {
                          spend: "AVAILABLE",
                          revenue_d0: "AVAILABLE",
                          revenue_total: "AVAILABLE",
                          ad_impression_events: "AVAILABLE",
                          impressions: "AVAILABLE",
                          clicks: "AVAILABLE",
                          roas_total: "AVAILABLE",
                          roas_d0: "AVAILABLE",
                          roas_d3:
                            dimension === "material"
                              ? "UNSUPPORTED"
                              : "AVAILABLE",
                          roas_d7: "AVAILABLE",
                          roas_d14: "AVAILABLE",
                          roas_d30:
                            dimension === "material"
                              ? "UNSUPPORTED"
                              : "AVAILABLE",
                          cost_per_ad_impression_event: "AVAILABLE",
                          ctr: "AVAILABLE",
                        },
                      },
                    ]
                  : []),
              ],
              capabilities: { can_manage: true },
              coverage: {
                status: "COMPLETE",
                updated_at: "2026-10-01T00:00:00Z",
              },
            },
          ].filter(() => !options.emptyRows),
          total: options.total ?? 1,
          summary: {
            row_count: 1,
            incomplete_advertiser_count: options.incomplete ? 2 : 0,
            incomplete_dates: options.incomplete ? ["2026-10-04"] : [],
            buckets: [
              {
                currency: "USD",
                timezone: "UTC",
                attribution: "IAA",
                values: {
                  spend: options.reportSpend ?? "100",
                  revenue_d0: "120",
                  revenue_total: "150",
                  ad_impression_events: "50",
                  impressions: "1000",
                  clicks: "20",
                  roas_total: "1.5",
                  roas_d0: "1.2",
                  roas_d3: dimension === "material" ? null : "1.3",
                  roas_d7: "1.4",
                  roas_d14: "1.5",
                  roas_d30: dimension === "material" ? null : "1.6",
                  cost_per_ad_impression_event: "2",
                  ctr: "0.02",
                  cpm: "0.1",
                  cpc: "5",
                },
                availability: {
                  spend: "AVAILABLE",
                  revenue_d0: "AVAILABLE",
                  revenue_total: "AVAILABLE",
                  ad_impression_events: "AVAILABLE",
                  impressions: "AVAILABLE",
                  clicks: "AVAILABLE",
                  roas_total: "AVAILABLE",
                  roas_d0: "AVAILABLE",
                  roas_d3:
                    dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                  roas_d7: "AVAILABLE",
                  roas_d14: "AVAILABLE",
                  roas_d30:
                    dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                  cost_per_ad_impression_event: "AVAILABLE",
                  ctr: "AVAILABLE",
                  cpm: "AVAILABLE",
                  cpc: "AVAILABLE",
                },
              },
              ...(options.mixedBuckets
                ? [
                    {
                      currency: "CNY",
                      timezone: "Asia/Shanghai",
                      attribution: "IAA",
                      values: {
                        spend: options.reportSpend ?? "100",
                        revenue_d0: "120",
                        revenue_total: "150",
                        ad_impression_events: "50",
                        impressions: "1000",
                        clicks: "20",
                        roas_total: "1.5",
                        roas_d0: "1.2",
                        roas_d3: dimension === "material" ? null : "1.3",
                        roas_d7: "1.4",
                        roas_d14: "1.5",
                        roas_d30: dimension === "material" ? null : "1.6",
                        cost_per_ad_impression_event: "2",
                        ctr: "0.02",
                        cpm: "0.1",
                        cpc: "5",
                      },
                      availability: {
                        spend: "AVAILABLE",
                        revenue_d0: "AVAILABLE",
                        revenue_total: "AVAILABLE",
                        ad_impression_events: "AVAILABLE",
                        impressions: "AVAILABLE",
                        clicks: "AVAILABLE",
                        roas_total: "AVAILABLE",
                        roas_d0: "AVAILABLE",
                        roas_d3:
                          dimension === "material"
                            ? "UNSUPPORTED"
                            : "AVAILABLE",
                        roas_d7: "AVAILABLE",
                        roas_d14: "AVAILABLE",
                        roas_d30:
                          dimension === "material"
                            ? "UNSUPPORTED"
                            : "AVAILABLE",
                        cost_per_ad_impression_event: "AVAILABLE",
                        ctr: "AVAILABLE",
                        cpm: "AVAILABLE",
                        cpc: "AVAILABLE",
                      },
                    },
                  ]
                : []),
            ],
          },
          coverage: {
            status: "COMPLETE",
            updated_at: "2026-10-01T00:00:00Z",
          },
          next_cursor:
            options.paged &&
            !secondPage &&
            (options.total === undefined ||
              Number(url.searchParams.get("limit") ?? 50) < options.total)
              ? "cursor-1"
              : null,
        },
      })
    }
    // 广告详情的素材：本广告里每个素材的数据，以及只针对本广告的启停选择。
    if (path.endsWith(`/tenants/${TENANT}/ads/ad/ad-1/materials`))
      return route.fulfill({
        headers,
        json: {
          coverage: { status: "COMPLETE" },
          items: ["m1", "m2"].map((id, index) => ({
            use_ref: {
              ad_ref: {
                tenant_id: TENANT,
                advertiser_id: ADVERTISER,
                kind: "ad",
                remote_id: "ad-1",
              },
              platform_material_id: `video-${id}`,
              ad_material_id: id,
              material_type: "VIDEO",
            },
            name: `ep${index + 1}.mp4`,
            operation_status: "ENABLE",
            post_ids: [`post-${id}`],
            shared: false,
            metric_buckets: [
              {
                currency: "USD",
                timezone: "UTC",
                values: { spend: index ? "3.00" : "12.50" },
                availability: { spend: "AVAILABLE" },
              },
            ],
          })),
        },
      })
    if (path.endsWith(`/tenants/${TENANT}/ads/ad/ad-1/material-selections`)) {
      const body = request.postDataJSON() as { ad_material_ids: string[] }
      return route.fulfill({
        headers,
        json: {
          selection_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          refs: [],
          material_uses: body.ad_material_ids.map((id) => ({
            ad_ref: {
              tenant_id: TENANT,
              advertiser_id: ADVERTISER,
              kind: "ad",
              remote_id: "ad-1",
            },
            platform_material_id: `video-${id}`,
            ad_material_id: id,
            material_type: "VIDEO",
          })),
          membership_digest: "m".repeat(64),
          expires_at: "2099-01-01T00:00:00Z",
        },
      })
    }
    const detailMatch = /\/ads\/(campaign|adgroup|ad)\/([^/]+)$/.exec(path)
    if (detailMatch && DETAIL_IDS[detailMatch[1]] === detailMatch[2]) {
      const kind = detailMatch[1]
      return route.fulfill({
        headers,
        json: {
          ref: {
            tenant_id: TENANT,
            advertiser_id: ADVERTISER,
            kind,
            remote_id: detailMatch[2],
          },
          name: "嘉书-总裁归来-测试",
          ad_type: "VIDEO",
          configuration: DETAIL_CONFIGURATION[kind],
          statuses:
            kind === "campaign"
              ? { operation: "PAUSED", review: "APPROVED" }
              : { operation: "ENABLE", review: "APPROVED" },
          parent:
            kind === "campaign"
              ? null
              : {
                  ref: {
                    tenant_id: TENANT,
                    advertiser_id: ADVERTISER,
                    kind: "campaign",
                    remote_id: "campaign-1",
                  },
                  operation_status: "DISABLE",
                  reasons: ["operation_status:DISABLE"],
                },
          materials:
            kind === "ad"
              ? [
                  {
                    use_ref: {
                      ad_ref: {
                        tenant_id: TENANT,
                        advertiser_id: ADVERTISER,
                        kind: "ad",
                        remote_id: "ad-1",
                      },
                      platform_material_id: "material-1",
                      ad_material_id: null,
                      material_type: "VIDEO",
                    },
                    name: "ep1.mp4",
                    operation_status: "ENABLE",
                    complete: true,
                    external: false,
                  },
                ]
              : [],
          operation_history: [
            {
              task_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
              created_at: "2026-10-01T03:00:00Z",
              field: "status",
              mode: "set",
              original_value: "ENABLE",
              final_value: "DISABLE",
              execution_result: "ACCEPTED",
              reason: null,
            },
            {
              task_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
              created_at: "2026-09-30T03:00:00Z",
              field: "roas",
              mode: "set",
              original_value: null,
              final_value: "1.2",
              execution_result: "REJECTED",
              reason: "rejected",
            },
          ],
          observed_at: "2026-10-01T00:00:00Z",
        },
      })
    }
    if (path.endsWith(`/tenants/${TENANT}/ad-selections`)) {
      const body = request.postDataJSON() as { row_keys?: string[] }
      return route.fulfill({
        headers,
        json: {
          selection_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          refs: (body.row_keys ?? []).map(() => ({
            tenant_id: TENANT,
            advertiser_id: ADVERTISER,
            kind: "campaign",
            remote_id: "campaign-1",
          })),
          material_uses: [],
          membership_digest: "m".repeat(64),
          expires_at: "2099-01-01T00:00:00Z",
        },
      })
    }
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-previews`)
    )
      return route.fulfill({
        status: 201,
        headers,
        json: {
          preview_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          digest: "p".repeat(64),
          created_at: "2026-10-01T00:00:00Z",
          expires_at: "2099-01-01T00:05:00Z",
          bc_id: BC_A,
          route: {
            tenant_id: TENANT,
            bc_id: BC_A,
            connection_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            channel: "OFFICIAL_API",
            authorization_revision: 1,
            binding_revision: 1,
            adapter_contract_revision: "test-v1",
          },
          items: [],
          counts: { selected: 1, targets: 1, linked: 0, unsupported: 0 },
        },
      })
    if (path.endsWith("/ad-sync-status"))
      return route.fulfill({
        headers,
        json: {
          accounts: options.syncAccounts ?? [
            {
              advertiser_id: ADVERTISER,
              timezone: "Asia/Hong_Kong",
              local_today: new Date().toLocaleDateString("en-CA", {
                timeZone: "Asia/Hong_Kong",
              }),
              last_success_at: options.lastSuccess ?? "2026-10-05T05:00:00Z",
              incomplete_dates: options.incomplete ? ["2026-10-04"] : [],
              failed_dates: [],
            },
          ],
          complete_from: options.incomplete ? "2026-10-05" : "2026-09-01",
          publication_version: options.lastSuccess ?? "version-1",
          plan: {
            enabled: true,
            disabled_reason: null,
            next_hot_at: null,
            slowed: options.slowed ?? false,
          },
        },
      })
    if (path.endsWith("/ad-sync-refresh"))
      return route.fulfill({
        headers,
        json: {
          queued: 3,
          accounts: 1,
          status: options.refreshStatus ?? "QUEUED",
        },
      })
    if (path.endsWith("/report-views")) {
      if (request.method() === "POST") {
        return route.fulfill({
          headers,
          json: {
            id: "view-1",
            name: "广告报表当前筛选",
            filters: {
              dimension: "account",
              start_date: "2026-10-01",
              end_date: "2026-10-01",
            },
            columns: ["name", "status", "spend", "roas_d0"],
            created_at: "2026-10-12T00:00:00Z",
          },
        })
      }
      return route.fulfill({ headers, json: [] })
    }
    // 导出：创建后先返回排队，再次查询即完成，下载返回 CSV 文本。
    if (path.endsWith(`/tenants/${TENANT}/report-exports`))
      return route.fulfill({
        headers,
        json: { id: "export-1", status: "QUEUED", coverage: {} },
      })
    if (path.endsWith("/report-exports/export-1"))
      return route.fulfill({
        headers,
        json: { id: "export-1", status: "COMPLETE", coverage: {} },
      })
    if (path.endsWith("/report-exports/export-1/download"))
      return route.fulfill({
        headers: { ...headers, "Content-Type": "text/csv" },
        body: "名称,消耗\n甲账户,100.00\n",
      })
    if (path.endsWith(`/tenants/${TENANT}/ads/trend`)) {
      if (options.trendCalls) options.trendCalls.count++
      // 小时点是 UTC 的 ISO 分钟串，与后端一致。
      const hour = url.searchParams.get("granularity") === "hour"
      const timezone = options.trendTimezone ?? "UTC"
      const syncing = hour && (options.hourSyncingCalls ?? 0) > hourCalls++
      if (syncing)
        return route.fulfill({
          headers,
          json: {
            level: url.searchParams.get("level") ?? "account",
            granularity: "hour",
            timezone: "UTC",
            points: [],
            syncing: true,
          },
        })
      return route.fulfill({
        headers,
        json: {
          level: url.searchParams.get("level") ?? "account",
          granularity: hour ? "hour" : "day",
          timezone,
          points: [
            [hour ? "2026-10-01T01:00Z" : "2026-10-01", "4.00"],
            [hour ? "2026-10-01T02:00Z" : "2026-10-02", "6.00"],
          ].map(([at, spend], index) => ({
            at,
            buckets: [
              {
                currency: "USD",
                timezone: timezone === "MIXED" ? "UTC" : timezone,
                attribution: "IAA",
                // CPM 与消耗走势相反，验证切换指标后实际读取了 CPM。
                values: { spend, cpm: index ? "1.25" : "2.50" },
              },
            ],
            coverage: "COMPLETE",
          })),
        },
      })
    }
    return route.fulfill({
      headers,
      status: 404,
      json: { detail: "Unexpected ads test API request" },
    })
  })
}
