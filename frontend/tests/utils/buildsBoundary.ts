import { expect, type Page } from "@playwright/test"
import type { LinkConfigSchema } from "../../src/client"
export const T = "11111111-1111-4111-8111-111111111111",
  T2 = "22222222-2222-4222-8222-222222222222",
  D = "33333333-3333-4333-8333-333333333333",
  P = "44444444-4444-4444-8444-444444444444",
  S = "55555555-5555-4555-8555-555555555555",
  V = "66666666-6666-4666-8666-666666666666",
  C = "77777777-7777-4777-8777-777777777777",
  DR = "88888888-8888-4888-8888-888888888888",
  BC = "90071992547409931234",
  BC2 = "90071992547409939999"
export const config = {
  budget: "100.00",
  currency: "USD",
  budget_strategy: "SERIES",
  bid_strategy: "TARGET_ROAS",
  targeting: {
    region_mode: "ALL_AVAILABLE",
    region_codes: [],
    languages: [],
    age_groups: [],
    gender: "GENDER_UNLIMITED",
  },
  group_generation_mode: "BY_MATERIAL",
  group_count: null,
  group_material_allocation: null,
  max_materials_per_group: 10,
  ad_generation_mode: "FIXED",
  ads_per_group: 1,
  ad_material_allocation: "SHARED",
  max_materials_per_ad: null,
  target_roas: "1.08",
  creative_count: 2,
  copy_pool_version: S,
  cta_option_ids: [],
  campaign_name_template: "{provider_drama}-{drama_id}",
}
const intField = (name: string, label: string, extra = {}) => ({
  name,
  label,
  type: "integer" as const,
  minimum: 1,
  required: false,
  options: [],
  ...extra,
})
const linkSchemas: Record<string, LinkConfigSchema> = {
  wangyan: {
    provider_kind: "wangyan",
    fields: [
      intField("episode", "起播集", {
        default: 1,
        source: "application_default",
      }),
    ],
    defaults: { episode: 1 },
    application_defaults: {},
    schema_version: 2,
  },
  minglue: {
    provider_kind: "minglue",
    fields: [
      intField("episode", "起播集", {
        default: 1,
        source: "application_default",
      }),
    ],
    defaults: { episode: 1 },
    application_defaults: {},
    schema_version: 2,
  },
  duiba: {
    provider_kind: "duiba",
    fields: [
      intField("episode", "起播集", {
        default: 1,
        source: "application_default",
      }),
      intField("card_point_episode", "卡点集", {
        source: "per_drama",
        rule: "按剧免费集数",
      }),
    ],
    defaults: { episode: 1 },
    application_defaults: { episode: 2 },
    schema_version: 2,
  },
  rongliang: {
    provider_kind: "rongliang",
    fields: [],
    defaults: {},
    application_defaults: {},
    schema_version: 2,
  },
  "gangganhao-paid-no-template": {
    provider_kind: "gangganhao",
    fields: [
      intField("free_episode_count", "免费集数", {
        source: "per_drama",
        rule: "按剧免费集数，缺失时为 3",
        minimum: 0,
      }),
      intField("episode_seq", "跳转集", {
        default: 1,
        source: "application_default",
      }),
      {
        ...intField("payment_template_id", "支付模板", {
          source: "application_default",
          required: true,
        }),
        type: "select" as const,
        options: [{ value: "7", label: "周卡" }],
      },
    ],
    defaults: { episode_seq: 1 },
    application_defaults: {},
    schema_version: 2,
  },
} as unknown as Record<string, LinkConfigSchema>
export async function buildsBoundary(
  page: Page,
  options: {
    viewer?: boolean
    admin?: boolean
    loggedOut?: boolean
    candidate?: "drama" | "account"
    inputCount?: number
    unitCount?: number
    submitUnknown?: boolean
    submitStatus?: 408 | 429
    submitDenied?: boolean
    deny?: boolean
    createUnknown?: boolean
    lookup404?: boolean
    prepareUnknown?: boolean
    previewUnknown?: boolean
    conflict?: boolean
    groupUnknown?: boolean
    updateUnknown?: boolean
    miniUnknown?: boolean
    delayedInput?: boolean
    previewStatus?: string
    previewError?: string
    blocked?: boolean
    empty?: boolean
    waitingAccounts?: boolean
    channel?: "OFFICIAL_API" | "OFFICIAL_MCP"
    missingRoute?: boolean
    executionConnectionId?: string | null
    historicalRead?: boolean
    historicalReadLost?: boolean
    historicalReadState?: "UNKNOWN" | "BLOCKED" | "RUNNING"
    defaultConnectionId?: string
    draftList?: boolean
    miniSelected?: boolean
    skippedMaterials?: boolean
    linkKind?:
      | "minglue"
      | "wangyan"
      | "duiba"
      | "rongliang"
      | "gangganhao-paid-no-template"
  } = {},
) {
  const submissionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
  const historicalStepId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
  let historicalRequestId: string | undefined
  let historicalReadId = S
  let historicalReadCount = 0
  const mutations = new Map<string, { draft_id: string; revision: number }>()
  const requests: {
      path: string
      method: string
      body: any
      query: URLSearchParams
    }[] = [],
    originals = {
      drama: Array.from(
        { length: options.inputCount || 2 },
        (_, i) => `完整剧名${i + 1}`,
      ),
      account: ["90071992547409936666", "广告户乙", "广告户丙"],
    }
  const summary = {
    targeting: {
      region_mode: "ALL_AVAILABLE",
      region_codes: [],
      languages: [],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    } as any,
    targeting_override: null as any,
    draft_id: D,
    revision: 1,
    bc_id: BC,
    status: options.waitingAccounts ? "PREPARING" : "READY",
    preparation_phase: options.waitingAccounts ? "accounts" : "done",
    strategy_version_id: V,
    provider_connection_id: C,
    execution_connection_id: options.executionConnectionId ?? null,
    application_id: "app-external-001",
    link_config: {},
    delivery_config: {
      schedule_mode: "IMMEDIATE",
      schedule_start_time: null,
      schedule_end_time: null,
    } as any,
    input_counts: {
      drama: { ready: originals.drama.length },
      account: { ready: 3 },
    },
    drama_count: originals.drama.length,
    account_count: 3,
    task_id: null,
    provider_task_id: null,
    error_code: null,
    created_at: "2026-09-09T00:00:00Z",
    updated_at: "2026-09-09T00:00:00Z",
  }
  const preview = {
    skipped_material_count: options.skippedMaterials ? 1 : 0,
    submission_id: null as string | null,
    execution_route: options.missingRoute
      ? null
      : {
          connection_id: C,
          connection_name: "原搭建连接",
          channel: options.channel || "OFFICIAL_API",
          bc_id: BC,
        },
    preview_id: P,
    draft_id: D,
    draft_revision: 1,
    bc_id: BC,
    status: options.previewStatus || "FROZEN",
    currency: "USD",
    campaign_count: options.blocked ? 4 : 6,
    adgroup_count: options.blocked ? 12 : 18,
    ad_count: options.blocked ? 24 : 36,
    blocked_count: options.blocked ? 2 : 0,
    preparing_count: 0,
    input_issue_count: options.blocked ? 1 : 0,
    total_unit_count: 6,
    generation_progress: {
      phase:
        options.previewStatus && options.previewStatus !== "FROZEN"
          ? "inputs"
          : "complete",
      completed_units: options.previewStatus === "BUILDING" ? 0 : 6,
      total_units: 6 as number | null,
      updated_at: new Date().toISOString(),
    },
    daily_budget_sum: options.blocked ? "400.00" : "600.00",
    daily_budget_label: "系列预算合计",
    budget_strategy: "SERIES",
    bid_strategy: "TARGET_ROAS",
    group_generation_mode: "BY_MATERIAL",
    ad_generation_mode: "FIXED",
    creative_count: 2,
    unique_material_count: 23,
    material_allocation_count: 23,
    structure_summary:
      "6 个系列、18 个广告组、每组 2 个广告、创意数量 2、最终 36 个广告、每个广告 1 个素材；广告组按素材数量，广告固定数量；去重素材 23 个，分配引用 23 次",
    content_digest: "frozen-digest",
    error_code: options.previewError || null,
    created_at: "2026-09-09T00:00:00Z",
  }
  let selectedMini: { minis_id: string; name: string } | null =
    options.miniSelected
      ? { minis_id: "mini-real-001", name: "LemonShow" }
      : null
  let candidateSelected = false
  let materials = Array.from({ length: 23 }, (_, i) => ({
    material_id: `99999999-9999-4999-8999-${String(i + 1).padStart(12, "0")}`,
    file_name: `完整剧名1-${String(i + 1).padStart(2, "0")}.mp4`,
    source_bc_id: BC,
    content_key: `material:99999999-9999-4999-8999-${String(i + 1).padStart(12, "0")}`,
    group_no: Math.floor(i / 10) + 1,
    position: (i % 10) + 1,
    shared_with_other_drama: i === 0,
  }))
  let releaseInput: () => void = () => {}
  const waitInput = new Promise<void>((resolve) => (releaseInput = resolve))
  const units = Array.from({ length: options.unitCount || 6 }, (_, i) => ({
    skipped_material_count: options.skippedMaterials ? 1 : 0,
    budget: "100.00",
    currency: "USD",
    unit_id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(i + 1).padStart(12, "0")}`,
    drama_id: i < 3 ? DR : S,
    title: i < 3 ? "完整剧名1" : "完整剧名2",
    advertiser_id: `9007199254740993${6666 + i}`,
    campaign_name: `完整剧名${i < 3 ? 1 : 2}-20260909-batch`,
    readiness: options.blocked && i % 3 === 2 ? "BLOCKED" : "READY",
    reason_codes: options.blocked && i % 3 === 2 ? ["currency_mismatch"] : [],
    group_count: 3,
    ad_count: 6,
    material_count: 23,
    unique_material_count: 23,
    material_allocation_count: 6,
    group_summary: "3 个广告组（广告组按素材数量）",
    ad_summary: "6 个广告（广告固定数量，创意数量 2）",
    material_summary: "去重素材 23 个，广告素材分配 6 次",
    structure_summary:
      "1 个系列、3 个广告组、每组 2 个广告、创意数量 2、最终 6 个广告、每个广告 1 个素材；广告组按素材数量，广告固定数量；去重素材 23 个，分配引用 6 次；系列日预算；竞价策略 目标 ROAS",
  }))
  await page.addInitScript((loggedOut) => {
    if (!sessionStorage.getItem("build-fixture")) {
      if (loggedOut) localStorage.removeItem("access_token")
      else localStorage.setItem("access_token", "build-test-token")
      sessionStorage.setItem("build-fixture", "true")
    }
  }, options.loggedOut)
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname,
      method = req.method(),
      query = url.searchParams,
      headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "*",
      }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    if (path === "/api/login/access-token")
      return route.fulfill({
        headers,
        json: { access_token: "build-test-token", token_type: "bearer" },
      })
    expect(req.headers().authorization).toBe("Bearer build-test-token")
    const body = req.postData() ? req.postDataJSON() : undefined
    requests.push({ path, method, body, query })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    const paged = (items: unknown[]) => {
      const start = Number(query.get("cursor") || 0),
        limit = Number(query.get("limit") || 50)
      return {
        items: items.slice(start, start + limit),
        next_cursor:
          start + limit < items.length ? String(start + limit) : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: T,
        username: "operator",
        is_active: true,
        is_superuser: false,
        full_name: "搭建测试用户",
      })
    if (path === "/api/me/tenants")
      return reply({
        items: [
          {
            id: T,
            name: "搭建租户甲",
            active: true,
            role: options.viewer
              ? "viewer"
              : options.admin
                ? "tenant_admin"
                : "operator",
          },
          { id: T2, name: "搭建租户乙", active: true, role: "operator" },
        ].filter((t) => !query.get("search") || t.id === query.get("search")),
        next_cursor: null,
      })
    if (path.endsWith("/bcs"))
      return reply({
        items: [
          {
            bc_id: BC,
            name: "真实 BC",
            default_connection_id: options.defaultConnectionId || C,
          },
          { bc_id: BC2, name: "备用 BC" },
        ],
        next_cursor: null,
      })
    if (options.deny && path.includes("/build-"))
      return reply({ code: "action_forbidden" }, 403)
    if (path.endsWith("/strategies"))
      return reply(
        paged([
          {
            id: S,
            name: "实际策略",
            active: true,
            latest_version: 1,
            version_id: V,
            config,
            created_by: T,
            created_at: summary.created_at,
          },
        ]),
      )
    if (path.includes("/strategy-versions/"))
      return reply({
        id: V,
        strategy_id: S,
        number: 1,
        config,
        created_by: T,
        created_at: summary.created_at,
        request_id: D,
      })
    if (path.endsWith("/connections") && !path.includes("/providers/")) {
      expect([BC, BC2]).toContain(query.get("bc_id"))
      if (query.get("bc_id") === BC2) return reply(paged([]))
      return reply(
        paged([
          {
            id: C,
            kind: "OFFICIAL_API",
            display_name: "API 搭建连接",
            status: "ACTIVE",
            is_default:
              !options.defaultConnectionId || options.defaultConnectionId === C,
          },
          {
            id: S,
            kind: "OFFICIAL_MCP",
            display_name: "MCP 搭建连接",
            status: "ACTIVE",
            is_default: options.defaultConnectionId === S,
          },
        ]),
      )
    }
    if (path.endsWith("/providers/connections"))
      return reply(
        paged([
          {
            id: C,
            kind: "wangyan",
            display_name: "已验证连接",
            status: "active",
            verified_at: summary.created_at,
          },
        ]),
      )
    // 应用能力接口返回字段契约，不能沿用列表接口的空分页响应。
    if (path.includes("/applications/") && path.endsWith("/capabilities"))
      return reply(linkSchemas[options.linkKind ?? "wangyan"])
    if (path.endsWith("/applications"))
      return reply(
        paged([
          {
            external_id: "unavailable-app",
            name: "禁用应用",
            available: false,
          },
          {
            external_id: "app-external-001",
            name: "实际推广应用",
            available: true,
            tiktok_minis_id: "different-minis-id",
          },
        ]),
      )
    if (path.endsWith("/build-drafts") && method === "GET") {
      const items =
        options.draftList && query.get("bc_id") === BC && path.includes(T)
          ? [
              {
                draft_id: D,
                bc_id: BC,
                revision: summary.revision,
                status: summary.status,
                drama_titles: originals.drama.slice(0, 3),
                drama_input_count: originals.drama.length,
                account_input_count: originals.account.length,
                resolved_account_count: summary.status === "DRAFT" ? 0 : 3,
                strategy_label: "默认策略 v1",
                preview_id: null,
                preview_status: null,
                updated_at: summary.updated_at,
              },
            ]
          : []
      return reply(paged(items))
    }
    if (path.endsWith("/build-drafts") && method === "POST") {
      summary.execution_connection_id = body.execution_connection_id ?? null
      Object.assign(originals, {
        drama: body.drama_lines,
        account: body.account_lines,
      })
      if (options.createUnknown) return route.abort("failed")
      return reply({ draft_id: D, revision: 1 }, 201)
    }
    if (path.includes("/build-draft-requests/"))
      return options.lookup404
        ? reply({ code: "request_not_found" }, 404)
        : reply({ draft_id: D, revision: 1 })
    if (path.endsWith(`/build-drafts/${D}`)) {
      if (method === "PATCH") {
        if (options.conflict)
          return reply({ code: "draft_revision_conflict" }, 409)
        originals.drama = body.drama_lines
        originals.account = body.account_lines
        summary.execution_connection_id = body.execution_connection_id ?? null
        summary.revision++
        mutations.set(body.request_id, {
          draft_id: D,
          revision: summary.revision,
        })
        if (options.updateUnknown) return route.abort("failed")
        preview.status = "OBSOLETE"
        return reply({ draft_id: D, revision: summary.revision })
      }
      return reply(summary)
    }
    if (path.endsWith("/candidate")) {
      candidateSelected = true
      return reply({ task_id: P }, 202)
    }
    if (path.endsWith(`/build-drafts/${D}/delivery`) && method === "PATCH") {
      summary.delivery_config = body.delivery_config
      summary.revision++
      preview.status = "OBSOLETE"
      const result = { draft_id: D, revision: summary.revision }
      mutations.set(body.request_id, result)
      return reply(result)
    }
    if (path.endsWith(`/build-drafts/${D}/targeting`)) {
      if (method === "PATCH") {
        summary.targeting_override = body.targeting_override
        summary.targeting = body.targeting_override || {
          region_mode: "ALL_AVAILABLE",
          region_codes: [],
          languages: [],
          age_groups: [],
          gender: "GENDER_UNLIMITED",
        }
        summary.revision++
        preview.status = "OBSOLETE"
        const result = { draft_id: D, revision: summary.revision }
        mutations.set(body.request_id, result)
        return reply(result)
      }
      return reply({
        region_codes: ["US", "CA"],
        state: "READY",
        account_count: 3,
        verified_account_count: 3,
        unavailable_region_codes: [],
        revision: summary.revision,
      })
    }
    if (path.endsWith(`/build-drafts/${D}/targeting/accounts`))
      return reply({
        items: [
          {
            advertiser_id: "90071992547409936666",
            region_codes: ["US", "CA"],
            reason_codes: [],
          },
        ],
        next_cursor: null,
        total: 1,
      })
    if (path.endsWith(`/build-drafts/${D}/minis`)) {
      if (method === "POST") {
        summary.revision++
        summary.status = "PREPARING"
        selectedMini = { minis_id: body.minis_id, name: "LemonShow" }
        const result = { draft_id: D, revision: summary.revision }
        mutations.set(body.request_id, result)
        if (options.miniUnknown) return route.abort("failed")
        return reply(result)
      }
      return reply({
        state: selectedMini ? "selected" : "choose",
        catalog_job_id: P,
        advertiser_id: "90071992547409936666",
        selected: selectedMini,
        items: [{ minis_id: "mini-real-001", name: "LemonShow" }],
        next_page: null,
        total: 1,
      })
    }
    if (path.endsWith(`/build-drafts/${D}/identities`))
      return reply({
        state: "selected",
        catalog_job_id: P,
        advertiser_id: "90071992547409936666",
        selected: {
          identity_id: "identity-ready",
          identity_type: "TT_USER",
          identity_authorized_bc_id: null,
          display_name: "默认投放身份",
          username: "ready_owner",
          profile_image: null,
        },
        items: [
          {
            identity_id: "identity-ready",
            identity_type: "TT_USER",
            identity_authorized_bc_id: null,
            display_name: "默认投放身份",
            username: "ready_owner",
            profile_image: null,
          },
        ],
        total: 1,
      })
    if (path.endsWith("/prepare")) {
      if (options.prepareUnknown) return route.abort("failed")
      return reply({ task_id: P, revision: summary.revision }, 202)
    }
    if (path.includes("/build-mutation-requests/")) {
      const saved = mutations.get(path.split("/").slice(-1)[0]!)
      return options.lookup404 || !saved
        ? reply({ code: "request_not_found" }, 404)
        : reply(saved)
    }
    if (path.includes("/build-preparation-requests/"))
      return options.lookup404
        ? reply({ code: "request_not_found" }, 404)
        : reply({ task_id: P, revision: summary.revision })
    if (path.includes("/build-drafts/") && path.endsWith("/inputs")) {
      if (options.delayedInput && query.get("cursor")) await waitInput
      const kind = query.get("kind") === "account" ? "account" : "drama"
      return reply(
        paged(
          originals[kind].map((raw_text, i) => ({
            id: `input-${kind}-${i}`,
            kind,
            line_no: i + 1,
            raw_text,
            status: "ready",
            preparation:
              kind === "drama"
                ? {
                    link_status:
                      options.candidate === "drama" &&
                      i === 0 &&
                      !candidateSelected
                        ? "needs_resolution"
                        : "ready",
                    external_drama_id:
                      i === 0
                        ? "6a98f85eadb6903f924e6950"
                        : `provider-drama-${i + 1}`,
                    display_drama_id: String(31091 + i),
                    url:
                      options.empty || options.candidate === "drama"
                        ? null
                        : "https://www.tiktok.com/minis/original?channel=a%2Bb&x=1&x=2",
                    protected_base: options.empty
                      ? null
                      : "original-attribution",

                    provider_input_id: P,
                    candidates:
                      options.candidate === "drama" && i === 0
                        ? [
                            {
                              external_drama_id: "external-drama-01",
                              display_drama_id: "31091",
                              title: "候选正式剧名",
                              language: "en",
                            },
                          ]
                        : [],
                    title: raw_text,
                    reason_code: null,
                    drama:
                      options.empty ||
                      options.waitingAccounts ||
                      (options.candidate === "drama" &&
                        i === 0 &&
                        !candidateSelected)
                        ? null
                        : {
                            drama_id: i === 0 ? DR : S,
                            link_id: P,
                            title: raw_text,
                            first_line: i + 1,
                            material_state: "ready",
                            matched_count: i === 0 ? materials.length : 23,
                          },
                  }
                : null,
            reason_code: null,
            duplicate_of: null,
            advertiser_id:
              kind === "account" ? `9007199254740993${6666 + i}` : null,
            drama_id: kind === "drama" ? DR : null,
            provider_input_id:
              options.candidate === "drama" && kind === "drama" && i === 0
                ? P
                : null,
            candidates:
              options.candidate === kind && i === 0
                ? kind === "drama"
                  ? [
                      {
                        external_drama_id: "external-drama-01",
                        title: "候选正式剧名",
                        language: "en",
                      },
                    ]
                  : [{ advertiser_id: "90071992547409937777" }]
                : [],
          })),
        ),
      )
    }
    if (path.includes("/build-drafts/") && path.endsWith("/dramas"))
      return reply(
        paged(
          options.empty
            ? []
            : [
                {
                  drama_id: DR,
                  link_id: P,
                  title: "完整剧名1",
                  first_line: 1,
                  material_state: "ready",
                  matched_count: materials.length,
                },
                {
                  drama_id: S,
                  link_id: P,
                  title: "完整剧名2",
                  first_line: 2,
                  material_state: "ready",
                  matched_count: 23,
                },
              ],
        ),
      )
    if (path.includes("/build-drafts/") && path.endsWith("/materials"))
      return reply(paged(materials))
    if (path.includes("/build-drafts/") && path.endsWith("/groups")) {
      if (options.conflict)
        return reply({ code: "draft_revision_conflict" }, 409)
      materials = body.groups.flatMap((ids: string[], g: number) =>
        ids.map((id, p) => ({
          ...materials.find((m) => m.material_id === id),
          material_id: id,
          group_no: g + 1,
          position: p + 1,
        })),
      )
      summary.revision++
      mutations.set(body.request_id, {
        draft_id: D,
        revision: summary.revision,
      })
      preview.status = "OBSOLETE"
      if (options.groupUnknown) return route.abort("failed")
      return reply({ draft_id: D, revision: summary.revision })
    }
    if (path.includes("/build-drafts/") && path.includes("/previews")) {
      if (method === "POST" && options.previewUnknown)
        return route.abort("failed")
      return reply({ preview_id: P }, method === "POST" ? 202 : 200)
    }
    if (path.endsWith(`/build-previews/${P}`)) return reply(preview)
    if (path.endsWith(`/build-previews/${P}/submit`)) {
      if (options.submitDenied) return reply({ code: "action_forbidden" }, 403)
      if (options.submitUnknown) return route.abort("failed")
      if (options.submitStatus)
        return reply(
          { code: "submit_response_uncertain" },
          options.submitStatus,
        )
      preview.submission_id = submissionId
      return reply({ submission_id: submissionId, status: "QUEUED" }, 202)
    }
    if (path.includes("/submission-requests/")) {
      if (!options.lookup404) preview.submission_id = submissionId
      return options.lookup404
        ? reply({ code: "resource_not_found" }, 404)
        : reply({ submission_id: submissionId, status: "QUEUED" })
    }
    if (
      options.historicalRead &&
      path.endsWith(`/submissions/${submissionId}/steps`)
    )
      return reply(
        paged([
          {
            step_id: historicalStepId,
            unit_id: P,
            kind: "CAMPAIGN",
            group_id: null,
            planned_ad_id: null,
            material_id: null,
            status: "UNKNOWN",
            remote_id: null,
            error_code: "route_authorization_changed",
            operation_status: null,
            review_status: null,
            mismatch: false,
            checked_at: null,
            title: "原任务剧目",
            advertiser_id: "90071992547409936666",
            can_historical_read: !!options.admin && !options.viewer,
          },
        ]),
      )
    if (path.endsWith(`/execution-steps/${historicalStepId}/historical-read`)) {
      historicalRequestId = body.request_id
      historicalReadCount += 1
      historicalReadId = historicalReadCount === 1 ? S : P
      if (options.historicalReadLost) return route.abort("failed")
      return reply(
        {
          read_id: historicalReadId,
          request_id: historicalRequestId,
          source_step_id: historicalStepId,
          state: "PENDING",
          requires_new_preparation: true,
        },
        202,
      )
    }
    if (path.includes("/historical-build-read-requests/"))
      return options.lookup404 || path.split("/").pop() !== historicalRequestId
        ? reply({ code: "resource_not_found" }, 404)
        : reply({
            read_id: historicalReadId,
            request_id: historicalRequestId,
            source_step_id: historicalStepId,
            state: "PENDING",
            requires_new_preparation: true,
          })
    if (path.endsWith(`/historical-build-reads/${historicalReadId}`))
      return reply({
        read_id: historicalReadId,
        request_id: historicalRequestId,
        source_step_id: historicalStepId,
        state:
          historicalReadCount === 1
            ? options.historicalReadState || "CONFIRMED"
            : "CONFIRMED",
        remote_id:
          historicalReadCount === 1 && options.historicalReadState
            ? null
            : "existing-remote-id",
        mismatch: false,
        requires_new_preparation: true,
      })
    if (path.endsWith(`/submissions/${submissionId}/units`))
      return reply({ items: [], next_cursor: null })
    if (path.endsWith(`/submissions/${submissionId}`)) {
      const zero = { campaign_count: 0, adgroup_count: 0, ad_count: 0 }
      return reply({
        submission_id: submissionId,
        execution_route: preview.execution_route,
        recovery_mode:
          options.historicalRead && options.admin
            ? "REAUTHORIZE_READ"
            : "BLOCKED",
        error_code: options.missingRoute ? "legacy_route_unverifiable" : null,
        preview_id: P,
        draft_id: D,
        batch_short_id: "batch-real",
        bc_id: BC,
        status: options.historicalRead ? "NEEDS_REVIEW" : "QUEUED",
        expanded: false,
        currency: "USD",
        daily_budget_sum: "0.00",
        daily_budget_label: "系列预算合计",
        budget_strategy: "SERIES",
        bid_strategy: "TARGET_ROAS",
        planned: { campaign_count: 6, adgroup_count: 18, ad_count: 36 },
        submitted: zero,
        succeeded: zero,
        failed: zero,
        excluded: zero,
        unknown: zero,
        pending: { campaign_count: 6, adgroup_count: 18, ad_count: 36 },
        stage_counts: {},
        excluded_unit_count: 0,
        drama_count: 2,
        account_count: 3,
        created_at: summary.created_at,
        updated_at: summary.updated_at,
        recovery: {
          can_retry: false,
          can_reconcile: false,
          retryable_step_count: 0,
          reconcilable_step_count: 0,
          reasons: [],
        },
      })
    }

    if (path.includes("/build-previews/") && path.endsWith("/dramas"))
      return reply(
        paged(
          [DR, S].map((drama_id, i) => ({
            drama_id,
            title: `完整剧名${i + 1}`,
            account_count: 3,
            ready_count: options.blocked ? 2 : 3,
            preparing_count: 0,
            blocked_count: options.blocked ? 1 : 0,
            material_count: 23,
            skipped_material_count: options.skippedMaterials ? 1 : 0,
            material_group_count: 3,
            eligible_campaign_count: options.blocked ? 2 : 3,
            eligible_adgroup_count: options.blocked ? 6 : 9,
            eligible_ad_count: options.blocked ? 12 : 18,
            daily_budget_sum: options.blocked ? "200.00" : "300.00",
            unique_material_count: 23,
            material_allocation_count: 23,
            ad_material_allocation_count: 18,
            daily_budget_label: "系列预算合计",
            structure_summary:
              "3 个系列、9 个广告组、每组 2 个广告、创意数量 2、最终 18 个广告、每个广告 1 个素材",
          })),
        ),
      )

    if (path.includes("/build-previews/") && path.endsWith("/units"))
      return reply(
        paged(
          units.filter(
            (u) =>
              (!query.get("readiness") ||
                u.readiness === query.get("readiness")) &&
              (!query.get("drama_id") || u.drama_id === query.get("drama_id")),
          ),
        ),
      )
    if (path.includes("/build-previews/") && path.endsWith("/inputs"))
      return reply(
        paged(
          options.blocked
            ? [
                {
                  kind: "drama",
                  line_no: 3,
                  raw_text: "未解析剧名",
                  status: "failed",
                  reason_code: "drama_not_found",
                  duplicate_of: null,
                },
              ]
            : [],
        ),
      )
    if (path.includes("/build-units/") && path.endsWith("/skipped-materials"))
      return reply(
        paged(
          options.skippedMaterials
            ? [
                {
                  material_id: "99999999-9999-4999-8999-000000000001",
                  file_name: "冻结失败视频-CL6-6.mp4",
                  reason_code: "material_remote_source_unavailable",
                },
              ]
            : [],
        ),
      )
    if (path.includes("/build-units/") && path.endsWith("/groups"))
      return reply(
        paged(
          Array.from({ length: 3 }, (_, i) => ({
            group_id: `group-${i}`,
            group_no: i + 1,
            name: `冻结广告组${i + 1}`,
            material_ids: materials
              .filter(
                (m) =>
                  m.group_no === i + 1 &&
                  (!options.skippedMaterials || m.position !== 1 || i !== 0),
              )
              .map((m) => m.material_id),
            ads: [1, 2].map((n) => ({
              ad_id: `ad-${i}-${n}`,
              creative_no: n,
              name: `冻结广告${i + 1}-SP${n}`,
              copy_id: S,
              text: `真实冻结正文 ${n}`,
              cta_option_ids: ["verified-cta"],
            })),
          })),
        ),
      )
    if (path.includes("/build-units/")) {
      const u = units.find((u) => path.endsWith(u.unit_id))!
      return reply({
        ...u,
        preview_id: P,
        tenant_id: T,
        link_id: P,
        strategy_version_id: V,
        bc_id: BC,
        connection_id: C,
        currency: "USD",
        timezone: "Asia/Singapore",
        protected_base: "完整剧名1",
        url: "https://example.com/real-frozen-link",
        budget: "100.00",
        target_roas: "1.08",
        budget_strategy: "SERIES",
        bid_strategy: "TARGET_ROAS",
        scene_snapshot: {
          minis_id: "real-minis",
          identity_id: "real-identity",
        },
      })
    }
    return reply({ items: [], next_cursor: null })
  })
  return { requests, summary, preview, originals, releaseInput }
}
export async function pickInputs(page: Page) {
  await page.getByRole("combobox", { name: "版权方连接", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "已验证连接" }).click()
  await page.getByRole("combobox", { name: "推广应用", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "实际推广应用" }).click()
  await page.getByRole("combobox", { name: "投放策略", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "实际策略" }).click()
  await page
    .getByLabel("剧目名称", { exact: true })
    .fill("完整剧名1\n完整剧名2")
  await page
    .getByLabel("广告账户", { exact: true })
    .fill("90071992547409936666\n广告户乙\n广告户丙")
}
