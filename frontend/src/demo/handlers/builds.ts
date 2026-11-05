import { imageState } from "../fixtures/image-composition-state"
import {
  findImageUnit,
  freezeImageDemo,
  imageDeliveries,
} from "../fixtures/image-delivery"
import { IMAGE_DEMO_STRUCTURE } from "../fixtures/image-structure"
// 体验版假后端：批量搭建领域（builds）

import {
  type DemoDraft,
  type DemoPreview,
  demoDrafts,
  demoMutationRequests,
  demoPreviews,
} from "../fixtures/builds-data"
import {
  DEMO_ADVERTISERS,
  DEMO_DRAMAS,
  DEMO_IDENTITIES,
  DEMO_STRATEGIES,
  deliveryAdvertisers,
  MAIN_BC_ID,
  MAIN_TENANT_ID,
  materialsOfDrama,
} from "../fixtures/world"
import { demoUuid, isoNow, now, stageAt } from "../runtime/clock"
import { paginate } from "../runtime/paging"
import { DemoReply, demoError, handle } from "../runtime/registry"

// ==================== 草稿管理 ====================

// 创建草稿
handle("builds-create", (req) => {
  const { tenant_id } = req.path
  const body = req.body!

  const draft: DemoDraft = {
    media_kind: body.media_kind ?? "VIDEO",
    draft_id: demoUuid(),
    tenant_id,
    bc_id: body.bc_id,
    revision: 1,
    status: "DRAFT",
    preparation_phase: "not_started",
    strategy_version_id: body.strategy_version_id,
    provider_connection_id: body.provider_connection_id ?? "",
    execution_connection_id: body.execution_connection_id ?? null,
    application_id: body.application_id ?? "",
    link_config: body.link_config ?? {},
    delivery_config: {
      schedule_mode: "IMMEDIATE",
      schedule_start_time: null,
      schedule_end_time: null,
    },
    targeting: {
      region_mode: "ALL_AVAILABLE",
      region_codes: [],
      languages: [],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    },
    targeting_override: null,
    drama_lines: body.drama_lines ?? [],
    account_lines: body.account_lines ?? [],
    drama_count: 0,
    account_count: 0,
    input_counts: { drama: { ready: 0 }, account: { ready: 0 } },
    task_id: null,
    provider_task_id: null,
    error_code: null,
    created_at: isoNow(),
    updated_at: isoNow(),
    selected_mini: null,
    selected_identity: null,
    prepare_started_at: null,
  }

  demoDrafts.push(draft)

  return new DemoReply(201, {
    draft_id: draft.draft_id,
    media_kind: draft.media_kind ?? "VIDEO",
    structure_override: draft.structure_override ?? {},
    revision: draft.revision,
  })
})

// 列出草稿
handle("builds-list_drafts", (req) => {
  const { tenant_id } = req.path
  const { bc_id } = req.query

  const filtered = demoDrafts.filter(
    (d) => d.tenant_id === tenant_id && (!bc_id || d.bc_id === bc_id),
  )

  const items = filtered.map((d) => ({
    draft_id: d.draft_id,
    bc_id: d.bc_id,
    revision: d.revision,
    status: d.status,
    drama_titles: d.drama_lines.slice(0, 3),
    drama_input_count: d.drama_lines.length,
    account_input_count: d.account_lines.length,
    resolved_account_count: d.input_counts.account.ready,
    strategy_label: `${DEMO_STRATEGIES.find((s) => s.version_id === d.strategy_version_id)?.name || "默认策略"} v1`,
    preview_id:
      demoPreviews.find(
        (p) => p.draft_id === d.draft_id && p.status !== "OBSOLETE",
      )?.preview_id ?? null,
    preview_status:
      demoPreviews.find(
        (p) => p.draft_id === d.draft_id && p.status !== "OBSOLETE",
      )?.status ?? null,
    updated_at: d.updated_at,
  }))

  return paginate(items, req.query, (item) => item.draft_id)
})

// 获取草稿详情
handle("builds-summary", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  // 准备状态按时间推进
  if (draft.status === "PREPARING" && draft.prepare_started_at) {
    const progress = stageAt(draft.prepare_started_at, [
      ["PREPARING", 2000],
      ["READY", 0],
    ])
    if (progress.done) {
      draft.status = "READY"
      draft.preparation_phase = "done"
      draft.drama_count = draft.drama_lines.length
      draft.account_count = draft.account_lines.length
      draft.input_counts = {
        drama: { ready: draft.drama_lines.length },
        account: { ready: draft.account_lines.length },
      }
    }
  }

  return {
    draft_id: draft.draft_id,
    media_kind: draft.media_kind ?? "VIDEO",
    structure_override: draft.structure_override ?? {},
    revision: draft.revision,
    bc_id: draft.bc_id,
    status: draft.status,
    strategy_version_id: draft.strategy_version_id,
    provider_connection_id: draft.provider_connection_id,
    execution_connection_id: draft.execution_connection_id,
    application_id: draft.application_id,
    link_config: draft.link_config as {
      [key: string]: string | number | boolean | null
    },
    delivery_config: draft.delivery_config,
    targeting: draft.targeting,
    targeting_override: draft.targeting_override,
    input_counts: draft.input_counts,
    drama_count: draft.drama_count,
    account_count: draft.account_count,
    task_id: draft.task_id,
    provider_task_id: draft.provider_task_id,
    error_code: draft.error_code,
    preparation_phase: draft.preparation_phase as
      | "accounts"
      | "links"
      | "materials"
      | "scenes"
      | "done"
      | null,
    created_at: draft.created_at,
    updated_at: draft.updated_at,
  }
})

// 更新草稿
handle("builds-update", (req) => {
  const { draft_id } = req.path
  const body = req.body!
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  if (body.expected_revision && body.expected_revision !== draft.revision) {
    return demoError(409, "draft_revision_conflict", "草稿版本冲突")
  }

  if (body.drama_lines) draft.drama_lines = body.drama_lines
  if (body.account_lines) draft.account_lines = body.account_lines
  if (body.execution_connection_id !== undefined) {
    draft.execution_connection_id = body.execution_connection_id
  }
  if (body.strategy_version_id)
    draft.strategy_version_id = body.strategy_version_id

  draft.revision++
  draft.updated_at = isoNow()
  draft.status = "DRAFT"

  // 废弃旧预览
  demoPreviews
    .filter((p) => p.draft_id === draft_id)
    .forEach((p) => {
      p.status = "OBSOLETE"
    })

  const result = { draft_id, revision: draft.revision }
  if (body.request_id) demoMutationRequests.set(body.request_id, result)

  return result
})

// 更新投放配置
handle("builds-update_delivery", (req) => {
  const { draft_id } = req.path
  const body = req.body!
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  if (body.expected_revision && body.expected_revision !== draft.revision) {
    return demoError(409, "draft_revision_conflict", "草稿版本冲突")
  }

  draft.delivery_config = body.delivery_config
  draft.revision++
  draft.updated_at = isoNow()

  // 废弃旧预览
  demoPreviews
    .filter((p) => p.draft_id === draft_id)
    .forEach((p) => {
      p.status = "OBSOLETE"
    })

  const result = { draft_id, revision: draft.revision }
  if (body.request_id) demoMutationRequests.set(body.request_id, result)

  return result
})

// 更新定向
handle("builds-update_targeting", (req) => {
  const { draft_id } = req.path
  const body = req.body!
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  if (body.expected_revision && body.expected_revision !== draft.revision) {
    return demoError(409, "draft_revision_conflict", "草稿版本冲突")
  }

  draft.targeting_override = body.targeting_override
  draft.targeting = body.targeting_override || draft.targeting
  draft.revision++
  draft.updated_at = isoNow()

  // 废弃旧预览
  demoPreviews
    .filter((p) => p.draft_id === draft_id)
    .forEach((p) => {
      p.status = "OBSOLETE"
    })

  const result = { draft_id, revision: draft.revision }
  if (body.request_id) demoMutationRequests.set(body.request_id, result)

  return result
})

// 准备草稿
handle("builds-prepare", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  draft.status = "PREPARING"
  draft.preparation_phase = "inputs"
  draft.prepare_started_at = now()
  draft.task_id = demoUuid()

  return new DemoReply(202, {
    task_id: draft.task_id,
    revision: draft.revision,
  })
})

// 获取准备请求状态
handle("builds-saved_prepare_request", (_req) => {
  // 简单返回任务 ID
  return {
    task_id: demoUuid(),
    revision: 1,
  }
})

// ==================== 输入解析 ====================

// 获取输入列表
handle("builds-inputs", (req) => {
  const { draft_id } = req.path
  const { kind } = req.query
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  const isAccount = kind === "account"
  const lines = isAccount ? draft.account_lines : draft.drama_lines

  const items = lines.map((line, index) => {
    const base = {
      id: `input-${kind}-${index}`,
      kind: kind as "drama" | "account",
      line_no: index + 1,
      raw_text: line,
      status: "ready" as const,
      reason_code: null,
      duplicate_of: null,
      provider_input_id: null,
      candidates: [],
    }

    if (isAccount) {
      // 账户输入：按 ID 或名称匹配
      const advertiser = DEMO_ADVERTISERS.find(
        (a) => a.advertiser_id === line || a.name === line,
      )
      return {
        ...base,
        advertiser_id: advertiser?.advertiser_id ?? null,
        drama_id: null,
        preparation: null,
      }
    }
    // 剧目输入：按标题匹配
    const drama = DEMO_DRAMAS.find(
      (d) => d.title === line || d.title_zh === line || d.external_id === line,
    )
    const materials = drama ? materialsOfDrama(drama.id) : []

    return {
      ...base,
      advertiser_id: null,
      drama_id: drama?.id ?? null,
      preparation: drama
        ? {
            link_status: "ready" as const,
            external_drama_id: drama.external_id,
            display_drama_id: drama.external_id,
            url: `https://minis.example.invalid/${drama.external_id}`,
            protected_base: drama.title,
            provider_input_id: demoUuid(),
            candidates: [],
            title: drama.title,
            reason_code: null,
            drama: {
              drama_id: drama.id,
              link_id: demoUuid(),
              title: drama.title,
              first_line: index + 1,
              material_state: "ready" as const,
              matched_count: materials.length,
            },
          }
        : null,
    }
  })

  return paginate(items, req.query, (item) => item.id)
})

// 获取剧目列表
handle("builds-dramas", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  const items = draft.drama_lines
    .map((line, index) => {
      const drama = DEMO_DRAMAS.find(
        (d) =>
          d.title === line || d.title_zh === line || d.external_id === line,
      )
      if (!drama) return null
      const materials = materialsOfDrama(drama.id)

      return {
        drama_id: drama.id,
        link_id: demoUuid(),
        title: drama.title,
        first_line: index + 1,
        material_state: "ready" as const,
        matched_count: materials.length,
      }
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)

  return paginate(items, req.query, (item) => item.drama_id)
})

// 获取素材列表
handle("builds-materials", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  // 获取所有匹配的剧目的素材
  const dramas = draft.drama_lines
    .map((line) =>
      DEMO_DRAMAS.find(
        (d) =>
          d.title === line || d.title_zh === line || d.external_id === line,
      ),
    )
    .filter((d): d is NonNullable<typeof d> => d !== null)

  const allMaterials = dramas.flatMap((drama) => materialsOfDrama(drama.id))

  const items = allMaterials.map((m, index) => ({
    material_id: m.id,
    file_name: m.file_name,
    source_bc_id: MAIN_BC_ID,
    content_key: `material:${m.id}`,
    group_no: Math.floor(index / 10) + 1,
    position: (index % 10) + 1,
    shared_with_other_drama: false,
  }))

  return paginate(items, req.query, (item) => item.material_id)
})

// 编辑分组
handle("builds-edit_groups", (req) => {
  const { draft_id } = req.path
  const body = req.body!
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  if (body.expected_revision && body.expected_revision !== draft.revision) {
    return demoError(409, "draft_revision_conflict", "草稿版本冲突")
  }

  // 分组调整（这里不实际存储，只更新版本）
  draft.revision++
  draft.updated_at = isoNow()

  // 废弃旧预览
  demoPreviews
    .filter((p) => p.draft_id === draft_id)
    .forEach((p) => {
      p.status = "OBSOLETE"
    })

  const result = { draft_id, revision: draft.revision }
  if (body.request_id) demoMutationRequests.set(body.request_id, result)

  return result
})

// ==================== 身份和 Minis ====================

// 获取身份选项
handle("builds-identity_options", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  return {
    state: draft.selected_identity
      ? ("selected" as const)
      : ("choose" as const),
    catalog_job_id: demoUuid(),
    advertiser_id: deliveryAdvertisers(draft.bc_id)[0].advertiser_id,
    selected: draft.selected_identity
      ? {
          identity_id: draft.selected_identity.identity_id,
          identity_type: draft.selected_identity.identity_type as
            | "TT_USER"
            | "BC_AUTH_TT",
          display_name: draft.selected_identity.display_name,
        }
      : null,
    items: DEMO_IDENTITIES.map((identity) => ({
      identity_id: identity.identity_id,
      identity_type: identity.identity_type as "TT_USER" | "BC_AUTH_TT",
      identity_authorized_bc_id: null,
      display_name: identity.display_name,
      username: identity.display_name.toLowerCase().replace(/\s+/g, "_"),
      profile_image: null,
    })),
    total: DEMO_IDENTITIES.length,
  }
})

// 选择身份
handle("builds-select_identity", (req) => {
  const { draft_id } = req.path
  const body = req.body!
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  const identity = DEMO_IDENTITIES.find(
    (i) => i.identity_id === body.identity_id,
  )
  if (identity) {
    draft.selected_identity = {
      identity_id: identity.identity_id,
      identity_type: identity.identity_type,
      display_name: identity.display_name,
    }
  }

  draft.revision++
  draft.updated_at = isoNow()

  const result = { draft_id, revision: draft.revision }
  if (body.request_id) demoMutationRequests.set(body.request_id, result)

  return result
})

// 获取 Minis 选项
handle("builds-minis_options", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  const miniOptions = [
    { minis_id: "mini-demo-001", name: "DramaHub" },
    { minis_id: "mini-demo-002", name: "ReelBox" },
    { minis_id: "mini-demo-003", name: "SeriesPlay" },
  ]

  return {
    state: draft.selected_mini ? ("selected" as const) : ("choose" as const),
    catalog_job_id: demoUuid(),
    advertiser_id: deliveryAdvertisers(draft.bc_id)[0].advertiser_id,
    selected: draft.selected_mini,
    items: miniOptions,
    next_page: null,
    total: miniOptions.length,
  }
})

// 演示目录即时返回；手动刷新保留已选目标，不伪造远端同步任务。
function refreshDraftCatalog(tenantId: string, draftId: string) {
  const draft = demoDrafts.find(
    (d) => d.draft_id === draftId && d.tenant_id === tenantId,
  )
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")
  return { expired: 0 }
}
handle("builds-refresh_minis_catalog", (req) =>
  refreshDraftCatalog(req.path.tenant_id, req.path.draft_id),
)
handle("builds-refresh_identities_catalog", (req) =>
  refreshDraftCatalog(req.path.tenant_id, req.path.draft_id),
)

// 选择 Mini
handle("builds-select_mini", (req) => {
  const { draft_id } = req.path
  const body = req.body!
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  draft.selected_mini = {
    minis_id: body.minis_id,
    name: "Selected Mini",
  }

  draft.revision++
  draft.status = "PREPARING"
  draft.prepare_started_at = now()
  draft.updated_at = isoNow()

  const result = { draft_id, revision: draft.revision }
  if (body.request_id) demoMutationRequests.set(body.request_id, result)

  return result
})

// ==================== 定向 ====================

// 获取定向选项
handle("builds-targeting_options", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  return {
    region_codes: ["US", "CA", "GB", "AU"],
    state: "READY" as const,
    account_count: draft.account_count,
    verified_account_count: draft.account_count,
    unavailable_region_codes: [],
    revision: draft.revision,
  }
})

// 获取定向账户
handle("builds-targeting_accounts", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  const accounts = draft.account_lines
    .map((line) =>
      DEMO_ADVERTISERS.find((a) => a.advertiser_id === line || a.name === line),
    )
    .filter((a): a is NonNullable<typeof a> => a !== null)

  const items = accounts.map((a) => ({
    advertiser_id: a.advertiser_id,
    region_codes: ["US", "CA"],
    reason_codes: [],
  }))

  return {
    items,
    next_cursor: null,
    total: items.length,
  }
})

// 获取定向地区
handle("builds-targeting_regions", (_req) => {
  return {
    region_codes: ["US", "CA", "GB", "AU"],
    state: "READY" as const,
  }
})

// ==================== 预览生成 ====================

// 生成预览
handle("builds-generate_preview", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  if (draft.media_kind === "CAROUSEL") {
    const state = imageState(draft_id)
    if (!state.preview_ready)
      return demoError(
        409,
        state.preview_reason_codes?.[0] ?? "image_rules_not_applied",
        "请完成图片规则与配乐",
      )
    if (
      Object.entries(IMAGE_DEMO_STRUCTURE).some(
        ([key, value]) =>
          draft.structure_override?.[
            key as keyof typeof IMAGE_DEMO_STRUCTURE
          ] !== value,
      )
    )
      return demoError(
        409,
        "demo_structure_scope",
        "本合成验收使用固定1共享组、每广告6单元、1文案的代表性结构",
      )
  }
  const previous = demoPreviews.find(
    (p) => p.draft_id === draft_id && p.draft_revision === draft.revision,
  )
  if (previous) return new DemoReply(202, { preview_id: previous.preview_id })
  const preview_id = demoUuid()

  // 计算剧目和账户数量
  const dramas = draft.drama_lines
    .map((line) =>
      DEMO_DRAMAS.find(
        (d) =>
          d.title === line || d.title_zh === line || d.external_id === line,
      ),
    )
    .filter((d): d is NonNullable<typeof d> => d !== null)

  const accounts = draft.account_lines
    .map((line) =>
      DEMO_ADVERTISERS.find((a) => a.advertiser_id === line || a.name === line),
    )
    .filter((a): a is NonNullable<typeof a> => a !== null)

  const unitCount = dramas.length * accounts.length
  const groupsPerUnit = 3
  const adsPerGroup = 2

  const preview: DemoPreview = {
    preview_id,
    draft_id,
    draft_revision: draft.revision,
    bc_id: draft.bc_id,
    status: "BUILDING",
    currency: "USD",
    campaign_count: unitCount,
    adgroup_count: unitCount * groupsPerUnit,
    ad_count: unitCount * groupsPerUnit * adsPerGroup,
    blocked_count: 0,
    preparing_count: 0,
    input_issue_count: 0,
    total_unit_count: unitCount,
    generation_progress: {
      phase: "inputs",
      completed_units: 0,
      total_units: unitCount,
      updated_at: isoNow(),
    },
    daily_budget_sum: `${unitCount * 100}.00`,
    daily_budget_label: "系列预算合计",
    budget_strategy: "SERIES",
    bid_strategy: "TARGET_ROAS",
    group_generation_mode: "BY_MATERIAL",
    ad_generation_mode: "FIXED",
    creative_count: 2,
    unique_material_count: dramas.reduce(
      (sum, d) => sum + materialsOfDrama(d.id).length,
      0,
    ),
    material_allocation_count: dramas.reduce(
      (sum, d) => sum + materialsOfDrama(d.id).length,
      0,
    ),
    structure_summary: `${unitCount} 个系列、${unitCount * groupsPerUnit} 个广告组、每组 ${adsPerGroup} 个广告、创意数量 2、最终 ${unitCount * groupsPerUnit * adsPerGroup} 个广告、每个广告 1 个素材；广告组按素材数量，广告固定数量`,
    content_digest: `digest-${preview_id}`,
    error_code: null,
    created_at: isoNow(),
    submission_id: null,
    execution_route: {
      connection_id: draft.execution_connection_id || demoUuid(301),
      connection_name: "API 搭建连接",
      channel: "OFFICIAL_API",
      bc_id: draft.bc_id,
    },
    skipped_material_count: 0,
    build_started_at: now(),
  }

  if (draft.media_kind === "CAROUSEL") {
    const units = freezeImageDemo(draft, preview_id)
    const ads = units.reduce((sum, u) => sum + u.ads.length, 0)
    Object.assign(preview, {
      preparing_count: units.length,
      adgroup_count: units.length,
      ad_count: ads,
      group_generation_mode: "FIXED",
      ad_generation_mode: "BY_MATERIAL",
      creative_count: 1,
      unique_material_count: new Set(
        units.flatMap((u) => u.ads.flatMap((a) => a.material_ids)),
      ).size,
      material_allocation_count: units.reduce(
        (n, u) => n + u.ads.reduce((m, a) => m + a.material_ids.length, 0),
        0,
      ),
      structure_summary: `合成演示 · ${units.length} 个系列 / 共享广告组 · ${ads} 条广告 · M6 · K1`,
    })
  }
  demoPreviews.push(preview)

  return new DemoReply(202, { preview_id })
})

// 获取预览请求状态
handle("builds-preview_request", (_req) => {
  // 简单返回预览 ID
  return { preview_id: demoUuid() }
})

// 获取预览详情
handle("builds-preview_summary", (req) => {
  const { preview_id } = req.path
  const preview = demoPreviews.find((p) => p.preview_id === preview_id)
  if (!preview) return demoError(404, "preview_not_found", "预览不存在")

  // 预览生成按时间推进
  if (preview.status === "BUILDING" && preview.build_started_at) {
    const progress = stageAt(preview.build_started_at, [
      ["BUILDING", 3000],
      ["FROZEN", 0],
    ])
    if (progress.done) {
      preview.status = "FROZEN"
      preview.generation_progress.phase = "complete"
      preview.generation_progress.completed_units = preview.total_unit_count
    } else {
      preview.generation_progress.completed_units = Math.floor(
        preview.total_unit_count * progress.progress,
      )
    }
    preview.generation_progress.updated_at = isoNow()
  }

  // 获取草稿以继承 delivery 配置
  const draft = demoDrafts.find((d) => d.draft_id === preview.draft_id)

  return {
    contract_version: (draft?.media_kind === "CAROUSEL" ? 2 : 1) as 1 | 2,
    media_kind: draft?.media_kind ?? "VIDEO",
    preview_id: preview.preview_id,
    draft_id: preview.draft_id,
    draft_revision: preview.draft_revision,
    bc_id: preview.bc_id,
    status: preview.status,
    currency: preview.currency,
    campaign_count: preview.campaign_count,
    adgroup_count: preview.adgroup_count,
    ad_count: preview.ad_count,
    blocked_count: preview.blocked_count,
    preparing_count: preview.preparing_count,
    input_issue_count: preview.input_issue_count,
    total_unit_count: preview.total_unit_count,
    generation_progress: {
      phase: preview.generation_progress.phase as
        | "inputs"
        | "dramas"
        | "units"
        | "digest"
        | "complete",
      completed_units: preview.generation_progress.completed_units,
      total_units: preview.generation_progress.total_units,
      updated_at: preview.generation_progress.updated_at,
    },
    daily_budget_sum: preview.daily_budget_sum,
    daily_budget_label: preview.daily_budget_label,
    budget_strategy: preview.budget_strategy as "SERIES" | "ADGROUP",
    bid_strategy: preview.bid_strategy as "HIGHEST_VALUE" | "TARGET_ROAS",
    creation_status: "ENABLE" as const,
    schedule_type:
      draft?.delivery_config.schedule_mode === "IMMEDIATE"
        ? ("SCHEDULE_FROM_NOW" as const)
        : ("SCHEDULE_START_END" as const),
    schedule_start_time: draft?.delivery_config.schedule_start_time ?? null,
    schedule_end_time: draft?.delivery_config.schedule_end_time ?? null,
    group_generation_mode: preview.group_generation_mode as
      | "FIXED"
      | "BY_MATERIAL",
    ad_generation_mode: preview.ad_generation_mode as "FIXED" | "BY_MATERIAL",
    creative_count: preview.creative_count,
    unique_material_count: preview.unique_material_count,
    material_allocation_count: preview.material_allocation_count,
    structure_summary: preview.structure_summary,
    content_digest: preview.content_digest,
    error_code: preview.error_code,
    created_at: preview.created_at,
    submission_id: preview.submission_id,
    // 提交按钮要求预览带有冻结的执行连接且属于当前 BC。
    execution_route: preview.execution_route && {
      ...preview.execution_route,
      channel: "OFFICIAL_API" as const,
    },
  }
})

// 获取预览剧目列表
handle("builds-preview_dramas", (req) => {
  const { preview_id } = req.path
  const preview = demoPreviews.find((p) => p.preview_id === preview_id)
  if (!preview) return demoError(404, "preview_not_found", "预览不存在")

  const draft = demoDrafts.find((d) => d.draft_id === preview.draft_id)
  if (!draft) return { items: [], next_cursor: null, total: 0 }

  const dramas = draft.drama_lines
    .map((line) =>
      DEMO_DRAMAS.find(
        (d) =>
          d.title === line || d.title_zh === line || d.external_id === line,
      ),
    )
    .filter((d): d is NonNullable<typeof d> => d !== null)

  if (imageDeliveries.has(preview_id)) {
    const units = imageDeliveries.get(preview_id)!
    return paginate(
      dramas.map((drama) => {
        const owned = units.filter((u) => u.preview.drama_id === drama.id)
        const sample = owned[0]?.preview
        return {
          drama_id: drama.id,
          title: drama.title,
          account_count: owned.length,
          ready_count: 0,
          preparing_count: owned.length,
          blocked_count: 0,
          material_count: sample?.material_count ?? 0,
          skipped_material_count: 0,
          material_group_count: 1,
          eligible_campaign_count: owned.length,
          eligible_adgroup_count: owned.length,
          eligible_ad_count: owned.reduce((n, u) => n + u.ads.length, 0),
          daily_budget_sum: `${owned.length * 100}.00`,
          daily_budget_label: "系列预算合计",
          structure_summary: sample?.structure_summary ?? "合成演示",
        }
      }),
      req.query,
      (row) => row.drama_id,
    )
  }
  const accountCount = draft.account_lines.length

  const items = dramas.map((drama) => {
    const materials = materialsOfDrama(drama.id)
    return {
      drama_id: drama.id,
      title: drama.title,
      account_count: accountCount,
      ready_count: accountCount,
      preparing_count: 0,
      blocked_count: 0,
      material_count: materials.length,
      skipped_material_count: 0,
      material_group_count: 3,
      eligible_campaign_count: accountCount,
      eligible_adgroup_count: accountCount * 3,
      eligible_ad_count: accountCount * 3 * 2,
      daily_budget_sum: `${accountCount * 100}.00`,
      unique_material_count: materials.length,
      material_allocation_count: materials.length,
      ad_material_allocation_count: accountCount * 3 * 2,
      daily_budget_label: "系列预算合计",
      structure_summary: `${accountCount} 个系列、${accountCount * 3} 个广告组、每组 2 个广告、创意数量 2、最终 ${accountCount * 3 * 2} 个广告、每个广告 1 个素材`,
    }
  })

  return paginate(items, req.query, (item) => item.drama_id)
})

// 获取预览输入列表
handle("builds-preview_inputs", (_req) => {
  // 预览输入问题（空列表表示没有问题）
  return { items: [], next_cursor: null, total: 0 }
})

// 获取预览单元列表
handle("builds-preview_units", (req) => {
  const { preview_id } = req.path
  const { readiness, drama_id } = req.query
  const preview = demoPreviews.find((p) => p.preview_id === preview_id)
  if (!preview) return demoError(404, "preview_not_found", "预览不存在")

  const draft = demoDrafts.find((d) => d.draft_id === preview.draft_id)
  if (!draft) return { items: [], next_cursor: null, total: 0 }

  const dramas = draft.drama_lines
    .map((line) =>
      DEMO_DRAMAS.find(
        (d) =>
          d.title === line || d.title_zh === line || d.external_id === line,
      ),
    )
    .filter((d): d is NonNullable<typeof d> => d !== null)

  const accounts = draft.account_lines
    .map((line) =>
      DEMO_ADVERTISERS.find((a) => a.advertiser_id === line || a.name === line),
    )
    .filter((a): a is NonNullable<typeof a> => a !== null)

  if (imageDeliveries.has(preview_id))
    return paginate(
      imageDeliveries
        .get(preview_id)!
        .map((u) => u.preview)
        .filter(
          (u) =>
            (!drama_id || u.drama_id === drama_id) &&
            (!readiness || u.readiness === readiness),
        ),
      req.query,
      (u) => u.unit_id,
    )

  const allUnits = dramas.flatMap((drama) =>
    accounts.map((account, _accountIndex) => {
      const materials = materialsOfDrama(drama.id)
      return {
        unit_id: demoUuid(),
        drama_id: drama.id,
        title: drama.title,
        advertiser_id: account.advertiser_id,
        campaign_name: `${drama.provider_kind}-${drama.title}-${drama.external_id}`,
        readiness: "READY" as const,
        reason_codes: [],
        group_count: 3,
        ad_count: 6,
        material_count: materials.length,
        unique_material_count: materials.length,
        material_allocation_count: 6,
        group_summary: "3 个广告组（广告组按素材数量）",
        ad_summary: "6 个广告（广告固定数量，创意数量 2）",
        material_summary: `去重素材 ${materials.length} 个，广告素材分配 6 次`,
        structure_summary: `1 个系列、3 个广告组、每组 2 个广告、创意数量 2、最终 6 个广告、每个广告 1 个素材；广告组按素材数量，广告固定数量；去重素材 ${materials.length} 个，分配引用 6 次；系列日预算；竞价策略 目标 ROAS`,
        budget: "100.00",
        currency: "USD",
        skipped_material_count: 0,
      }
    }),
  )

  // 过滤
  const filtered = allUnits.filter((u) => {
    if (readiness && u.readiness !== readiness) return false
    if (drama_id && u.drama_id !== drama_id) return false
    return true
  })

  return paginate(filtered, req.query, (item) => item.unit_id)
})

// 获取冻结单元详情
handle("builds-frozen_unit", (req) => {
  const { unit_id } = req.path

  const image = findImageUnit(unit_id)
  if (image) {
    const p = demoPreviews.find((p) => p.preview_id === image.previewId)!
    const u = image.unit.preview
    return {
      ...u,
      contract_version: 2 as const,
      media_kind: "CAROUSEL" as const,
      preview_id: p.preview_id,
      tenant_id: MAIN_TENANT_ID,
      link_id: demoUuid(1200),
      strategy_version_id: DEMO_STRATEGIES[0].version_id,
      bc_id: p.bc_id,
      connection_id: demoUuid(301),
      currency: "USD",
      timezone: "UTC",
      protected_base: u.title,
      url: "https://minis.example.invalid/synthetic",
      budget: "100.00",
      target_roas: "1.08",
      budget_strategy: "SERIES" as const,
      bid_strategy: "TARGET_ROAS" as const,
      scene_snapshot: { synthetic_demo: true },
    }
  }
  // 返回单元详细信息
  return {
    contract_version: 1 as const,
    media_kind: "VIDEO" as const,
    unit_id,
    preview_id: demoUuid(),
    tenant_id: MAIN_TENANT_ID,
    link_id: demoUuid(),
    strategy_version_id: DEMO_STRATEGIES[0].version_id,
    bc_id: MAIN_BC_ID,
    connection_id: demoUuid(301),
    drama_id: DEMO_DRAMAS[0].id,
    title: DEMO_DRAMAS[0].title,
    advertiser_id: deliveryAdvertisers()[0].advertiser_id,
    campaign_name: `${DEMO_DRAMAS[0].provider_kind}-${DEMO_DRAMAS[0].title}-${DEMO_DRAMAS[0].external_id}`,
    currency: "USD",
    timezone: "Asia/Shanghai",
    protected_base: DEMO_DRAMAS[0].title,
    url: `https://minis.example.invalid/${DEMO_DRAMAS[0].external_id}`,
    budget: "100.00",
    target_roas: "1.08",
    budget_strategy: "SERIES" as const,
    bid_strategy: "TARGET_ROAS" as const,
    readiness: "READY" as const,
    reason_codes: [],
    group_count: 3,
    ad_count: 6,
    material_count: materialsOfDrama(DEMO_DRAMAS[0].id).length,
    unique_material_count: materialsOfDrama(DEMO_DRAMAS[0].id).length,
    material_allocation_count: 6,
    scene_snapshot: {
      minis_id: "mini-demo-001",
      identity_id: DEMO_IDENTITIES[0].identity_id,
    },
    skipped_material_count: 0,
  }
})

// 获取冻结单元分组
handle("builds-frozen_groups", (req) => {
  const image = findImageUnit(req.path.unit_id)
  if (image) return paginate([image.unit.group], req.query, (g) => g.group_id)
  const materials = materialsOfDrama(DEMO_DRAMAS[0].id).slice(0, 12)
  const groups = [0, 1, 2].map((groupIndex) => {
    const groupMaterials = materials.filter((_, i) => i % 3 === groupIndex)
    return {
      group_id: `group-${groupIndex}`,
      group_no: groupIndex + 1,
      name: `冻结广告组${groupIndex + 1}`,
      material_ids: groupMaterials.map((m) => m.id),
      ads: [1, 2].map((adNo) => ({
        ad_id: `ad-${groupIndex}-${adNo}`,
        base_ad_no: groupIndex * 2 + adNo,
        creative_no: adNo,
        name: `冻结广告${groupIndex + 1}-SP${adNo}`,
        copy_id: demoUuid(),
        text: `精彩短剧等你来看！SP${adNo}`,
        cta_option_ids: ["cta-download"],
        material_ids: groupMaterials.map((m) => m.id),
      })),
    }
  })

  return paginate(groups, {}, (item) => item.group_id)
})

// 获取跳过素材列表
handle("builds-skipped_materials", (_req) => {
  // 通常为空
  return { items: [], next_cursor: null, total: 0 }
})

// 手动链接
handle("builds-put_manual_link", (req) => {
  const { draft_id } = req.path
  const draft = demoDrafts.find((d) => d.draft_id === draft_id)
  if (!draft) return demoError(404, "draft_not_found", "草稿不存在")

  draft.revision++
  draft.updated_at = isoNow()

  return { draft_id: draft.draft_id, revision: draft.revision }
})

// ==================== 提交 ====================

// 授权历史只读
handle("builds-authorize_historical_read", (req) => {
  const { step_id } = req.path
  const body = req.body
  return new DemoReply(202, {
    read_id: demoUuid(),
    request_id: body?.request_id || demoUuid(),
    source_step_id: step_id,
    state: "PENDING" as const,
    requires_new_preparation: true as const,
  })
})

// 获取历史只读请求
handle("builds-get_historical_read_request", (req) => {
  return {
    read_id: demoUuid(),
    request_id: req.path.request_id,
    source_step_id: demoUuid(),
    state: "PENDING" as const,
    requires_new_preparation: true as const,
  }
})

// 获取历史只读
handle("builds-get_historical_read", (req) => {
  return {
    read_id: req.path.read_id,
    request_id: demoUuid(),
    source_step_id: demoUuid(),
    state: "CONFIRMED" as const,
    remote_id: "existing-remote-id",
    mismatch: false,
    requires_new_preparation: true as const,
  }
})

// 保存的请求
handle("builds-saved_request", (req) => {
  const { request_id } = req.path
  const saved = demoMutationRequests.get(request_id)
  if (!saved) return demoError(404, "request_not_found", "请求不存在")
  return saved
})

// 保存的变更
handle("builds-saved_mutation", (req) => {
  const { request_id } = req.path
  const saved = demoMutationRequests.get(request_id)
  if (!saved) return demoError(404, "request_not_found", "请求不存在")
  return saved
})

// 此演示数据是历史视频预览；V2 广告分页只读取实际 V2 冻结记录。
handle("builds-frozen_ads", (req) => {
  const image = findImageUnit(req.path.unit_id)
  if (!image || image.unit.group.group_id !== req.path.group_id)
    return demoError(
      409,
      "preview_contract_expired",
      "此演示预览使用历史分组详情",
    )
  return paginate(image.unit.ads, req.query, (ad) => ad.ad_id)
})
