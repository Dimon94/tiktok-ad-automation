// 体验版假后端：materials 领域

import { toast } from "sonner"
import { parseMaterialLinks } from "@/features/materials/link-input"
import { imageCandidates } from "../fixtures/image-composition-state"
import {
  addIngestFile,
  completeIngestFile,
  createIngestSession,
  ensureMaterialProduction,
  fileStage,
  type IngestFileState,
  type IngestSessionState,
  ingestRequests,
  ingestSessions,
} from "../fixtures/materials-state"
import { DEMO_MATERIALS, MAIN_BC_ID, MAIN_TENANT_ID } from "../fixtures/world"
import { demoUuid, isoAt, nextId, now } from "../runtime/clock"
import { registerDemoFetch } from "../runtime/isolation"
import { registerDemoRedirect } from "../runtime/navigation"
import { matchesSearch, paginate } from "../runtime/paging"
import { demoError, handle } from "../runtime/registry"

// ========== materials 标签 ==========

// 素材库列表
handle("materials-get_materials", (req) => {
  const { tenant_id } = req.path
  const { cursor, limit, query: search } = req.query

  if (tenant_id !== MAIN_TENANT_ID) {
    return { items: [], next_cursor: null, total: 0 }
  }

  if (req.query.media_type === "IMAGE") {
    const rows = imageCandidates.filter(
      (c) => !search || c.file_name_snapshot.includes(search),
    )
    const page = paginate(
      rows,
      { cursor, limit: limit ?? 50 },
      (row) => row.material_id,
    )
    return {
      items: page.items.map((row) => ({
        material_id: row.material_id,
        content_key: `material:${row.material_id}`,
        media_type: "IMAGE" as const,
        bc_id: MAIN_BC_ID,
        file_name: row.file_name_snapshot,
        byte_size: 1024,
        mime_type: "image/jpeg",
        duration: null,
        width: 720,
        height: 1280,
        created_at: row.created_at,
        original_available: true,
        status: "available" as const,
        available_account_count: 1,
        latest_advertiser_id: null,
        latest_bc_id: MAIN_BC_ID,
      })),
      next_cursor: page.next_cursor,
      total: page.total,
    }
  }
  // 过滤：搜索关键词
  let materials = DEMO_MATERIALS
  if (search) {
    materials = materials.filter((m) =>
      matchesSearch(search, m.file_name, m.id, m.video_id),
    )
  }

  const page = paginate(materials, { cursor, limit: limit ?? 50 }, (m) => m.id)

  return {
    items: page.items.map((m) => ({
      material_id: m.id,
      content_key: `${m.drama_id}/${m.file_name}`,
      bc_id: MAIN_BC_ID,
      file_name: m.file_name,
      byte_size: m.size_bytes,
      mime_type: "video/mp4",
      duration: m.duration_seconds,
      width: 1080,
      height: 1920,
      created_at: m.uploaded_at,
      original_available: true,
      status: "available" as const,
      available_account_count: 1,
      latest_advertiser_id: m.advertiser_id,
      latest_bc_id: MAIN_BC_ID,
    })),
    next_cursor: page.next_cursor,
    total: page.total,
  }
})

// 单个素材详情
handle("materials-get_material", (req) => {
  const { material_id } = req.path
  const material = DEMO_MATERIALS.find(
    (m) => m.id === material_id || m.id.startsWith(material_id),
  )

  if (!material) {
    return demoError(404, "MATERIAL_NOT_FOUND", "素材不存在")
  }

  return {
    material_id: material.id,
    content_key: `${material.drama_id}/${material.file_name}`,
    bc_id: MAIN_BC_ID,
    file_name: material.file_name,
    byte_size: material.size_bytes,
    mime_type: "video/mp4",
    duration: material.duration_seconds,
    width: 1080,
    height: 1920,
    created_at: material.uploaded_at,
    original_available: true,
    status: "available" as const,
    available_account_count: 1,
    latest_advertiser_id: material.advertiser_id,
    latest_bc_id: MAIN_BC_ID,
  }
})

// 素材账户资产列表
handle("materials-get_assets", (req) => {
  const { material_id } = req.path
  const material = DEMO_MATERIALS.find(
    (m) => m.id === material_id || m.id.startsWith(material_id),
  )

  if (!material) {
    return demoError(404, "MATERIAL_NOT_FOUND", "素材不存在")
  }

  return {
    items: [
      {
        asset_id: demoUuid(10000 + DEMO_MATERIALS.indexOf(material)),
        material_id: material.id,
        bc_id: MAIN_BC_ID,
        advertiser_id: material.advertiser_id,
        connection_id: demoUuid(9001),
        video_id: material.video_id,
        mid: null,
        image_id: null,
        cover_url: material.cover_url,
        status: "available" as const,
        verified_at: material.uploaded_at,
      },
    ],
    next_cursor: null,
    total: 1,
  }
})

// 素材上传历史（遗留接口，返回空）
handle("materials-get_attempts", () => {
  return { items: [], next_cursor: null, total: 0 }
})

// 素材原始预览（返回签名 URL）
handle("materials-read_original_preview", (req) => {
  const { material_id } = req.path
  const imageNumber = Number.parseInt(material_id.split("-")[4] ?? "", 16)
  if (
    material_id.startsWith("d0d0d0d0-") &&
    imageNumber >= 1100 &&
    imageNumber < 1170
  ) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320"><rect width="240" height="320" fill="#e5e5e5"/><text x="120" y="160" text-anchor="middle" fill="#404040" font-size="24">图片 ${imageNumber - 1099}</text></svg>`
    return {
      url: `data:image/svg+xml,${encodeURIComponent(svg)}`,
      expires_in: 3600,
    }
  }
  const material = DEMO_MATERIALS.find(
    (m) => m.id === material_id || m.id.startsWith(material_id),
  )

  if (!material) {
    return demoError(404, "MATERIAL_NOT_FOUND", "素材不存在")
  }

  // 返回封面的 data URI 作为预览 URL
  return {
    url: material.cover_url,
    expires_in: 3600,
  }
})

// 素材远端预览（返回签名 URL）
handle("materials-read_remote_preview", (req) => {
  const { material_id } = req.path
  const material = DEMO_MATERIALS.find(
    (m) => m.id === material_id || m.id.startsWith(material_id),
  )

  if (!material) {
    return demoError(404, "MATERIAL_NOT_FOUND", "素材不存在")
  }

  // 体验版不提供真实视频地址，预览展示封面图。
  return {
    bc_id: MAIN_BC_ID,
    url: material.cover_url,
    advertiser_id: material.advertiser_id,
    video_id: material.video_id,
    width: 1080,
    height: 1920,
    duration: material.duration_seconds,
    format: "mp4",
  }
})

// 广告报表素材详情的成片预览：按视频 ID 找示例素材，没有就退回第一个示例素材的封面。
handle("materials-read_account_video_preview", (req) => {
  const { video_id, advertiser_ids } = req.query
  const material =
    DEMO_MATERIALS.find((m) => m.video_id === video_id) ?? DEMO_MATERIALS[0]
  if (!material) return demoError(404, "MATERIAL_NOT_FOUND", "素材不存在")
  return {
    bc_id: MAIN_BC_ID,
    url: material.cover_url,
    advertiser_id: String(advertiser_ids ?? "").split(",")[0] ?? "",
    video_id: String(video_id ?? material.video_id),
    width: 1080,
    height: 1920,
    duration: material.duration_seconds,
    format: "mp4",
  }
})

// 上传批次列表（遗留接口）
handle("materials-read_upload_batches", () => {
  return { items: [], next_cursor: null, total: 0 }
})

// 单个上传批次详情（遗留接口）
handle("materials-read_upload_batch", () => {
  return demoError(404, "BATCH_NOT_FOUND", "批次不存在")
})

// 上传请求状态（遗留接口）
handle("materials-read_upload_request", () => {
  return demoError(404, "REQUEST_NOT_FOUND", "请求不存在")
})

// 以下接口用于遗留上传流程，体验版不实现
handle("materials-post_complete", () =>
  demoError(400, "DEPRECATED", "请使用新的 ingest 流程"),
)
handle("materials-post_object_retry", () =>
  demoError(400, "DEPRECATED", "请使用新的 ingest 流程"),
)
handle("materials-post_part_signature", () =>
  demoError(400, "DEPRECATED", "请使用新的 ingest 流程"),
)
handle("materials-post_upload_batch", () =>
  demoError(400, "DEPRECATED", "请使用新的 ingest 流程"),
)
handle("materials-authorize_reissue", () =>
  demoError(400, "NOT_IMPLEMENTED", "体验版不支持素材替换"),
)

// ========== material-ingest 标签 ==========

// 创建 ingest 会话
handle("create_ingest_session", (req) => {
  const { tenant_id } = req.path
  const { bc_id, file_count, total_bytes } = req.body

  if (tenant_id !== MAIN_TENANT_ID) {
    return demoError(403, "FORBIDDEN", "无权限")
  }

  const session = createIngestSession(bc_id, file_count)

  return {
    session_id: session.session_id,
    bc_id: session.bc_id,
    expected_count: session.expected_count,
    total_bytes: total_bytes,
    status: "active",
    registration_cursor: 0,
    accepted_count: 0,
    uploaded_count: 0,
    ready_count: 0,
    failed_count: 0,
    cleaned_count: 0,
    reserved_bytes: 0,
    stored_bytes: 0,
    next_cursor: null,
    created_at: session.created_at,
  }
})

// 列出 ingest 会话
handle("list_ingest_sessions", (req) => {
  const { tenant_id } = req.path
  const { cursor, limit } = req.query

  if (tenant_id !== MAIN_TENANT_ID) {
    return { items: [], next_cursor: null, total: 0 }
  }

  const sessions = Array.from(ingestSessions.values())
  sessions.sort((a, b) => b.created_at.localeCompare(a.created_at))

  const page = paginate(
    sessions,
    { cursor, limit: limit ?? 50 },
    (s) => s.session_id,
  )

  return {
    items: page.items.map(sessionSummary),
    next_cursor: page.next_cursor,
    total: sessions.length,
  }
})

// 读取 ingest 会话摘要
handle("read_ingest_summary", (req) => {
  const { session_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  return sessionSummary(session)
})

function sessionSummary(session: IngestSessionState) {
  const stages = session.files.map((f) => fileStage(f, session.bc_id))
  const totalBytes = session.files.reduce((sum, f) => sum + f.size, 0)
  const storedBytes = session.files
    .filter((f) => f.completed)
    .reduce((sum, f) => sum + f.size, 0)

  return {
    session_id: session.session_id,
    bc_id: session.bc_id,
    expected_count: session.expected_count,
    total_bytes: totalBytes,
    status: session.sealed ? "sealed" : "registering",
    registration_cursor: session.files.length,
    accepted_count: session.files.length,
    uploaded_count: session.files.filter((f) => f.completed).length,
    ready_count: stages.filter((s) => s.available).length,
    failed_count: session.files.filter((f) => f.error_code).length,
    cleaned_count: stages.filter((s) => s.storage === "deleted").length,
    reserved_bytes: totalBytes,
    stored_bytes: storedBytes,
    next_cursor: null,
    created_at: session.created_at,
  }
}

// 创建 chunk（注册一批文件）：前端按返回的 items 拿到每个文件的 material_id，必须同步返回。
// 同一 request_id 重放时返回首次结果（与后端幂等语义一致）。
handle("create_ingest_chunk", (req) => {
  const { session_id } = req.path
  const { request_id, files } = req.body

  const session = ingestSessions.get(session_id)
  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const existing = ingestRequests.get(request_id)
  const registered =
    (existing?.result as IngestFileState[] | undefined) ??
    files.map((file) =>
      addIngestFile(
        session_id,
        file.client_index,
        file.file_name,
        file.size,
        file.mime_type ?? "video/mp4",
        file.last_modified_ms ?? now(),
      ),
    )
  ingestRequests.set(request_id, {
    request_id,
    status: "complete",
    started_at: existing?.started_at ?? now(),
    result: registered,
  })

  return {
    session_id: session.session_id,
    request_id,
    items: registered.map(fileToPublic),
  }
})

// 读取 chunk 结果（断点续传时按 request_id 找回首次注册结果）
handle("read_ingest_chunk", (req) => {
  const { session_id, request_id } = req.path
  const request = ingestRequests.get(request_id)

  if (!request) {
    return demoError(404, "REQUEST_NOT_FOUND", "请求不存在")
  }

  return {
    session_id,
    request_id: request.request_id,
    items: ((request.result as IngestFileState[] | undefined) ?? []).map(
      fileToPublic,
    ),
  }
})

// 列出会话内文件
handle("list_ingest_files", (req) => {
  const { session_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  return {
    items: session.files.map(fileToPublic),
    next_cursor: null,
    total: session.files.length,
  }
})

// 读取单个文件状态
handle("read_ingest_file", (req) => {
  const { session_id, material_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  return fileToPublic(file)
})

// resume 文件上传（获取上传权限）
handle("resume_ingest_file", (req) => {
  const { session_id, material_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  // 返回完整的 IngestFilePublic
  return fileToPublic(file)
})

// 签发分片上传地址
handle("sign_ingest_parts", (req) => {
  const { session_id, material_id } = req.path
  const { part_numbers } = req.body

  const session = ingestSessions.get(session_id)
  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  // 返回模拟的分片上传地址
  return {
    generation: file.generation,
    upload_id: file.upload_id,
    operation_revision: file.operation_revision,
    items: part_numbers.map((pn, index) => ({
      part_number: pn,
      // 最后一片按剩余字节计算，前端会逐字节核对。
      byte_size: Math.min(
        file.part_size,
        file.size - (pn - 1) * file.part_size,
      ),
      url: `https://demo-storage.invalid/uploads/${session_id}/${material_id}/parts/${pn}`,
      expires_in: 3600,
      // 前端要求许可 ID 与随机数都是 UUID，每次签发都不同。
      permission_id: demoUuid(nextId() * 10 + index),
      permission_nonce: crypto.randomUUID(),
      permission_revision: 1,
    })),
  }
})

// 注册模拟分片上传的处理器
registerDemoFetch("https://demo-storage.invalid/", async (_url, init) => {
  // 模拟分片上传：返回成功响应，带 ETag
  if (init?.method === "PUT") {
    const etag = `"${Math.random().toString(36).slice(2, 18)}"`

    // 模拟网络传输延迟
    await new Promise((resolve) =>
      setTimeout(resolve, 100 + Math.random() * 300),
    )

    return new Response(null, {
      status: 200,
      headers: {
        ETag: etag,
        "Access-Control-Expose-Headers": "ETag",
      },
    })
  }

  return new Response("Not Found", { status: 404 })
})

// 确认分片上传
handle("acknowledge_ingest_parts", (req) => {
  const { session_id, material_id } = req.path
  const { receipts } = req.body

  const session = ingestSessions.get(session_id)
  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  // 记录已上传的分片
  for (const receipt of receipts) {
    const existing = file.parts.find(
      (p) => p.part_number === receipt.part_number,
    )
    if (!existing && receipt.etag) {
      file.parts.push({
        part_number: receipt.part_number,
        byte_size: Math.min(
          file.part_size,
          file.size - (receipt.part_number - 1) * file.part_size,
        ),
        etag: receipt.etag,
      })
    }
  }

  // 更新已接收字节数
  file.received_bytes = file.parts.reduce((sum, p) => sum + p.byte_size, 0)

  return {
    generation: file.generation,
    upload_id: file.upload_id ?? "",
    operation_revision: file.operation_revision,
    accepted_permission_ids: receipts.map((r) => r.permission_id),
  }
})

// 列出已上传分片
handle("list_ingest_parts", (req) => {
  const { session_id, material_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  return {
    generation: file.generation,
    upload_id: file.upload_id ?? "",
    operation_revision: file.operation_revision,
    items: file.parts.map((p) => ({
      part_number: p.part_number,
      byte_size: p.byte_size,
      etag: p.etag,
    })),
    next_cursor: null,
  }
})

// 完成文件上传
handle("complete_ingest_file", (req) => {
  const { session_id, material_id } = req.path

  const session = ingestSessions.get(session_id)
  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  // 完成上传
  completeIngestFile(session_id, material_id)

  return fileToPublic(file)
})

// 取消文件上传
handle("cancel_ingest_file", (req) => {
  const { session_id, material_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  const file = session.files.find((f) => f.material_id === material_id)
  if (!file) {
    return demoError(404, "FILE_NOT_FOUND", "文件不存在")
  }

  file.error_code = "USER_CANCELLED"

  return fileToPublic(file)
})

// 封存会话
handle("seal_ingest_session", (req) => {
  const { session_id } = req.path
  const session = ingestSessions.get(session_id)

  if (!session) {
    return demoError(404, "SESSION_NOT_FOUND", "会话不存在")
  }

  session.sealed = true

  return { sealed: true, issues: [], summary: sessionSummary(session) }
})

// 以下 ingest 接口体验版暂不实现（用不到）
handle("read_ingest_request", () => demoError(404, "NOT_FOUND", "请求不存在"))
handle("read_ingest_part_permissions", () =>
  demoError(400, "NOT_IMPLEMENTED", "体验版不实现"),
)
handle("create_ingest_generation", () =>
  demoError(400, "NOT_IMPLEMENTED", "体验版不实现"),
)

// ========== material-push 标签 ==========
// 外部推送接口，前端不调用，建议豁免
handle("material-push-get_material_batch", () =>
  demoError(404, "NOT_FOUND", "批次不存在"),
)
handle("material-push-get_external_source", () =>
  demoError(404, "NOT_FOUND", "来源不存在"),
)
handle("material-push-post_material_batch", () =>
  demoError(400, "EXTERNAL_ONLY", "仅供外部系统调用"),
)

// ========== material-production 标签 ==========

// 获取素材制作状态
handle("material-production-get_material_production", (req) => {
  const { tenant_id } = req.path
  const state = ensureMaterialProduction(tenant_id)

  return state.enabled
    ? { status: "ready" as const, username: "demo", attempts: 1 }
    : { status: "not_configured" as const }
})

// 获取素材工作台登录链接
handle("material-production-post_material_login_link", (req) => {
  const { tenant_id } = req.path
  const state = ensureMaterialProduction(tenant_id)

  if (!state.enabled) {
    return demoError(403, "PRODUCTION_DISABLED", "素材制作未开通")
  }

  // 返回模拟的登录链接，会被 registerDemoRedirect 拦截
  const loginUrl = `${state.workspace_url}/api/login-links/consume?token=demo-${Date.now()}`

  return {
    login_url: loginUrl,
    expires_at: isoAt(now() + 5 * 60 * 1000),
  }
})

// 注册素材工作台跳转拦截
// 素材制作：用户确认由体验版直达君博（junbo）租户的真实素材平台，但只有有权限的人能进。
// 新开生产版的直达页，由生产版用这台浏览器自己的登录状态免密登录；体验版不持有任何凭证，
// 未登录或没有该租户权限的访客会看到生产登录页或无权限提示。
export const MATERIAL_PRODUCTION_TENANT = "junbo"
registerDemoRedirect(
  (url: URL) =>
    url.hostname === "manjumt.gzjunbo.net" &&
    url.pathname === "/api/login-links/consume",
  () => {
    window.open(
      `/material-production?tenant=${encodeURIComponent(MATERIAL_PRODUCTION_TENANT)}`,
      "_blank",
      "noopener",
    )
    toast.info("已在新标签页打开素材制作", {
      description: `素材平台是君博（${MATERIAL_PRODUCTION_TENANT}）租户的真实系统，需要用有权限的生产账号登录。`,
    })
    return null
  },
)

// 重试素材制作（体验版不实现）
handle("material-production-retry_material_production", () =>
  demoError(400, "NOT_IMPLEMENTED", "体验版不支持重试"),
)

// 辅助函数：文件状态转为公开格式（状态值与前端 stages 标签一致）
function fileToPublic(file: IngestFileState) {
  const session = [...ingestSessions.values()].find((item) =>
    item.files.includes(file),
  )
  const stage = fileStage(file, session?.bc_id ?? MAIN_BC_ID)
  const receivedBytes = file.completed
    ? file.size
    : file.parts.reduce((sum, p) => sum + p.byte_size, 0)

  return {
    material_id: file.material_id,
    client_index: file.client_index,
    file_name: file.file_name,
    size: file.size,
    mime_type: file.mime_type,
    media_type: file.mime_type.startsWith("image/")
      ? ("IMAGE" as const)
      : ("VIDEO" as const),
    last_modified_ms: file.last_modified_ms,
    generation: file.generation,
    upload_id: file.upload_id,
    part_size: file.part_size,
    part_count: file.part_count,
    can_retry: !file.completed && !file.error_code,
    operation_revision: file.operation_revision,
    platform_status: file.error_code ? "failed" : stage.platform,
    temporary_storage_status: stage.storage,
    received_bytes: receivedBytes,
    source_advertiser_id: stage.available ? file.source_advertiser_id : null,
    error_code: file.error_code,
    operation_status: "idle",
    task_id: null,
  }
}

// 链接上传体验数据仅存在于浏览器，不调用平台，不代表真实入库。
const linkBatches = new Map<string, import("@/client").LinkBatchPublic>()
const linkItems = new Map<string, import("@/client").LinkItemPublic[]>()
const linkRequests = new Map<string, string>()
handle("create_link_upload", (req) => {
  const { request_id, text, advertiser_ids, bc_id } = req.body
  const existing = linkRequests.get(request_id)
  if (existing) return linkBatches.get(existing)!
  const parsed = parseMaterialLinks(text)
  if (parsed.errors.length || !parsed.unique.length)
    return demoError(422, "link_input_invalid", "请填写有效素材链接")
  if (
    advertiser_ids.length > 20 ||
    parsed.unique.length * advertiser_ids.length > 2000
  )
    return demoError(422, "link_input_invalid", "请分批提交")
  const id = demoUuid(),
    date = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" })
      .format(new Date())
      .replace(/-/g, "")
  const items = parsed.unique.flatMap((line) =>
    advertiser_ids.map((advertiser_id) => ({
      id: demoUuid(),
      batch_id: id,
      bc_id,
      line_no: line.line,
      source_url: `https://ad-creative-studio-platform.tiktok-row.net/preview?vid=${line.vid}`,
      source_video_id: line.vid,
      advertiser_id,
      requested_file_name: `${date}_${line.vid}_${String(line.line).padStart(3, "0")}.mp4`,
      actual_file_name: `${date}_${line.vid}_${String(line.line).padStart(3, "0")}.mp4`,
      status: "success",
      video_id: `demo-${line.vid}`,
      mid: `demo-mid-${demoUuid()}`,
      material_id: null,
      duplicate_of: null,
      duplicate_line_no: null,
      error_code: null,
      error_message: null,
      request_id: "demo-only",
      can_retry: false,
      can_recheck: false,
      updated_at: new Date().toISOString(),
    })),
  )
  const batch = {
    id,
    bc_id,
    naming_date: date,
    input_count: parsed.lines.length,
    unique_count: parsed.unique.length,
    duplicate_count: parsed.lines.length - parsed.unique.length,
    account_count: advertiser_ids.length,
    total: items.length,
    counts: { success: items.length },
    created_at: new Date().toISOString(),
  }
  linkBatches.set(id, batch)
  linkItems.set(id, items)
  linkRequests.set(request_id, id)
  return batch
})
handle("list_link_uploads", (req) =>
  paginate(
    [...linkBatches.values()].filter((row) => row.bc_id === req.query.bc_id),
    req.query,
  ),
)
handle(
  "read_link_upload",
  (req) =>
    linkBatches.get(req.path.batch_id) ??
    demoError(404, "upload_batch_not_found", "批次不存在"),
)
handle("list_link_upload_items", (req) =>
  paginate(linkItems.get(req.path.batch_id) ?? [], req.query),
)
handle("list_material_link_sources", () => ({
  items: [],
  total: 0,
  next_cursor: null,
}))
handle("retry_link_upload_item", () =>
  demoError(409, "link_retry_unavailable", "体验批次已完成，无需重试"),
)
