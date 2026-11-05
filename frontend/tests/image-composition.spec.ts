import type { CompositionPlanPublic } from "../src/client"
import { expect, test } from "@playwright/test"
import { buildsBoundary, BC, T, D } from "./utils/buildsBoundary"

test("图片整批规则先查看差异，取消不写入组合", async ({ page }) => {
  const { summary, requests } = await buildsBoundary(page)
  await page.route("**/music/batch-retries", (route) =>
    route.fulfill({ json: [] }),
  )
  Object.assign(summary, { media_kind: "CAROUSEL", structure_override: {} })
  await page.route("**/image-composition", (route) =>
    route.fulfill({
      json: {
        revision: 1,
        active_generation: 0,
        applied_rule_revision: null,
        snapshot_id: D,
        rule: {},
        drama_patches: {},
        candidate_build: null,
        run: null,
        music_counts: {},
      },
    }),
  )
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await expect(page.getByText("整批图片规则", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "查看生成结果", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeDisabled()
  expect(requests.some((r) => r.path.endsWith("/apply"))).toBe(false)
})

async function imageBoundary(page: import("@playwright/test").Page) {
  const base = await buildsBoundary(page, { miniSelected: true })
  Object.assign(base.summary, {
    media_kind: "CAROUSEL",
    structure_override: {},
  })
  const rule = {
    mode: "SEQUENTIAL",
    images_per_unit: 5,
    order: "NATURAL_NAME",
    remainder: "KEEP",
    music_mode: "RECOMMEND",
    fixed_music_id: null,
    single_music_id: null,
  }
  const state = {
    revision: 1,
    active_generation: 1,
    preview_ready: false,
    applied_rule_revision: 1,
    snapshot_id: D,
    rule,
    drama_patches: {},
    candidate_build: null as import("../src/client").CandidateProgress | null,
    run: null as Record<string, unknown> | null,
    music_counts: { READY: 12, NEEDS_SELECTION: 1 },
  }
  const unit = {
    id: D,
    drama_id: "88888888-8888-4888-8888-888888888888",
    kind: "CAROUSEL",
    ordered_material_ids: ["image-1", "image-2", "image-3"],
    content_digest: "synthetic",
    position: 1,
    revision: 1,
    music_revision: 1,
    music_request_generation: 1,
    music_status: "READY",
    music_id: "song-original",
    music_snapshot: { display_name: "原曲" },
    composition_protected: true,
    music_protected: true,
  }
  const calls: { suffix: string; body: Record<string, unknown> }[] = []
  let plan: CompositionPlanPublic = {
    plan_id: D,
    base_revision: 2,
    input_digest: "synthetic",
    unit_count: 15,
    added_unit_count: 14,
    removed_unit_count: 0,
    retained_unit_count: 1,
    protected_unit_count: 1,
    music_counts: { PENDING: 14, NEEDS_SELECTION: 1 },
    image_reference_count: 67,
    distinct_image_count: 67,
    reused_image_count: 0,
    conflict_count: 0,
    conflicts: [],
    dramas: [],
    mode: "PRESERVE_MANUAL",
  }
  await page.route("**/image-composition**", async (route) => {
    const url = new URL(route.request().url()),
      suffix = url.pathname.split("image-composition")[1],
      body = route.request().postDataJSON() as Record<string, unknown> | null
    if (body) calls.push({ suffix, body })
    if (suffix === "") return route.fulfill({ json: state })
    if (suffix === "/plans") {
      state.revision++
      Object.assign(state, {
        rule: body!.rule,
        drama_patches: body!.drama_patches,
      })
      plan = {
        ...plan,
        base_revision: state.revision,
        mode: body!.mode as CompositionPlanPublic["mode"],
      }
      return route.fulfill({ json: plan })
    }
    if (suffix.includes("/plans/") && suffix.endsWith("/units"))
      return route.fulfill({
        json: {
          items: [
            {
              ...unit,
              ordered_material_ids: ["image-tail"],
              music_status: "NEEDS_SELECTION",
              music_id: null,
            },
          ],
          total: 15,
          next_cursor: null,
        },
      })
    if (suffix === "/apply") {
      expect(body!.expected_revision).toBe(plan.base_revision)
      state.run = {
        run_id: D,
        plan_id: D,
        generation: 2,
        status: "PENDING",
        processed: 1,
        total: 15,
        cursor: 1,
        error_code: null,
      }
      return route.fulfill({ json: state.run })
    }
    if (suffix.startsWith("/runs/") && suffix.endsWith("/cancel")) {
      state.run = { ...state.run, status: "CANCELLED" }
      return route.fulfill({ json: state.run })
    }
    if (suffix.startsWith("/runs/")) return route.fulfill({ json: state.run })
    if (suffix.endsWith("/differences"))
      return route.fulfill({
        json: {
          items: plan.conflicts,
          total: plan.conflict_count,
          next_cursor: null,
        },
      })
    if (suffix === "/dramas")
      return route.fulfill({
        json: {
          items: [
            {
              drama_id: unit.drama_id,
              link_id: D,
              title: "完整剧名1",
              first_line: 1,
              material_state: "ready",
              matched_count: 61,
              candidate_count: 61,
              excluded_count: 0,
              unused_count: 0,
              unit_count: 13,
              ready_count: 12,
              selection_count: 1,
              pending_count: 0,
              failed_count: 0,
              protected_count: 1,
              reference_count: 61,
              used_image_count: 61,
            },
          ],
          total: 1,
          next_cursor: null,
        },
      })
    if (suffix === "/units")
      return route.fulfill({
        json: { items: [unit], total: 1, next_cursor: null },
      })
    if (suffix === "/edits") {
      if (body!.expected_revision !== state.revision)
        return route.fulfill({
          status: 409,
          json: { code: "draft_revision_conflict", message: "草稿已更新" },
        })
      plan.conflict_count = 0
      plan.conflicts = []
      const command = body!.command as {
        kind: string
        material_ids?: string[]
        composition_protected?: boolean
        music_protected?: boolean
      }
      if (command.kind === "SET_IMAGES" && command.material_ids)
        unit.ordered_material_ids = command.material_ids
      if (command.kind === "SET_IMAGES") unit.revision++
      if (command.composition_protected !== undefined)
        unit.composition_protected = command.composition_protected
      if (command.music_protected !== undefined)
        unit.music_protected = command.music_protected
      state.revision++
      return route.fulfill({
        json: {
          revision: state.revision,
          affected_units: [D],
          pending_music_ids: [],
        },
      })
    }
    return route.fulfill({ json: { items: [], total: 0, next_cursor: null } })
  })
  await page.route("**/music/batch-retries", (route) =>
    route.fulfill({ json: [] }),
  )
  await page.route("**/music/search?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            music_id: "song-new",
            display_name: "新曲",
            author: "演示",
            duration: 30,
            preview_url: null,
          },
        ],
        next_cursor: null,
        truncated: false,
      },
    }),
  )
  await page.route("**/music/units/*", (route) => {
    calls.push({ suffix: "music", body: route.request().postDataJSON() })
    unit.music_revision++
    unit.music_id = "song-new"
    unit.music_snapshot.display_name = "新曲"
    state.revision++
    return route.fulfill({
      json: {
        revision: state.revision,
        music_revision: unit.music_revision,
        request_generation: 2,
        status: "READY",
        job_ids: [],
      },
    })
  })
  await page.route("**/materials/*/original-preview", (route) =>
    route.fulfill({
      json: {
        url: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3Crect width='80' height='80' fill='gray'/%3E%3C/svg%3E",
      },
    }),
  )
  return {
    ...base,
    state,
    unit,
    calls,
    conflict: () => {
      Object.assign(plan, {
        conflict_count: 1,
        conflicts: [
          {
            code: "PROTECTED_MUSIC_COMPOSITION_CHANGED",
            unit_id: D,
            drama_id: unit.drama_id,
            material_ids: [],
          },
        ],
      })
    },
  }
}

test("61图尾部单图待配乐、取消差异不变，明确应用并刷新恢复生成中", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "查看生成结果", exact: true })
  await expect(dialog.getByText("共 15 个创意", { exact: false })).toBeVisible()
  await expect(dialog.getByText(/1 张 · 待选音乐/)).toBeVisible()
  await dialog.getByRole("button", { name: "返回修改" }).click()
  expect(env.calls.filter((c) => c.suffix === "/apply")).toHaveLength(0)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  await dialog.getByRole("button", { name: "应用并生成" }).click()
  await expect(page.getByText("生成中 1/15", { exact: false })).toBeVisible()
  await page.reload()
  await expect(page.getByText("生成中 1/15", { exact: false })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeDisabled()
})

test("图片编辑取消与未保存离开、选曲取消不写入，确认音乐才保存", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await expect(
    page.getByRole("dialog", { name: "有未保存图片调整" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "继续编辑", exact: true }).click()
  await sheet.getByRole("button", { name: "取消图片调整" }).click()
  expect(env.calls.filter((c) => c.suffix === "/edits")).toHaveLength(0)
  await sheet.getByRole("button", { name: "换音乐", exact: true }).click()
  let picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "取消", exact: true }).click()
  expect(env.calls.filter((c) => c.suffix === "music")).toHaveLength(0)
  await sheet.getByRole("button", { name: "换音乐", exact: true }).click()
  picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "使用这首音乐" }).click()
  await expect(sheet.getByText("新曲", { exact: true })).toBeVisible()
  expect(env.calls.filter((c) => c.suffix === "music")).toHaveLength(1)
})

test("逐剧字段覆盖和恢复整批配置需重新试算", async ({ page }) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "本剧规则", exact: true }).click()
  await sheet.getByLabel("每轮播张数", { exact: true }).fill("4")
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const preview = env.calls.find((c) => c.suffix === "/plans")!
  expect(preview.body.drama_patches).toEqual({
    "88888888-8888-4888-8888-888888888888": { images_per_unit: 4 },
  })
  await page.getByRole("button", { name: "返回修改", exact: true }).click()
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  await sheet.getByRole("button", { name: "本剧规则", exact: true }).click()
  await sheet.getByRole("button", { name: "恢复整批配置" }).click()
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  expect(
    env.calls.filter((c) => c.suffix === "/plans").slice(-1)[0]!.body
      .drama_patches,
  ).toEqual({})
})

test("保护冲突须明确解除，默认保留手工修改", async ({ page }) => {
  const env = await imageBoundary(page)
  env.conflict()
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "查看生成结果", exact: true })
  await expect(
    dialog.getByRole("button", { name: "应用并生成" }),
  ).toBeDisabled()
  expect(env.calls.find((c) => c.suffix === "/plans")!.body.mode).toBe(
    "PRESERVE_MANUAL",
  )
  await dialog.getByRole("button", { name: "确认解除音乐保护" }).click()
  expect(env.unit.music_protected).toBe(false)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  await expect(dialog.getByRole("button", { name: "应用并生成" })).toBeEnabled()
})

test("整批改曲通过差异应用，失败配乐可明确重试", async ({ page }) => {
  const env = await imageBoundary(page)
  env.unit.music_status = "FAILED"
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("radio", { name: "整批指定一首" }).click()
  await page.getByRole("button", { name: "选择音乐", exact: true }).click()
  const picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "使用这首音乐" }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  expect(env.calls.find((c) => c.suffix === "/plans")!.body.rule).toMatchObject(
    { music_mode: "FIXED", fixed_music_id: "song-new" },
  )
  await page.getByRole("button", { name: "返回修改", exact: true }).click()
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await expect(
    sheet.getByRole("button", { name: "重新推荐", exact: true }),
  ).toHaveCount(0)
  await sheet.getByRole("button", { name: "重试失败配乐" }).click()
  expect(env.calls.find((c) => c.suffix === "music")!.body.action).toBe("RETRY")
})

test("后台配乐更新保留未保存图片顺序，内容变化明确阻断覆盖", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  env.unit.music_revision++
  await page.waitForResponse((response) =>
    response.url().includes("image-composition/units"),
  )
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeEnabled()
  const savedUnits = page.waitForResponse((response) =>
    response.url().includes("image-composition/units"),
  )
  await sheet.getByRole("button", { name: "保存图片调整", exact: true }).click()
  await savedUnits
  expect(
    env.calls.filter((c) => c.suffix === "/edits").slice(-1)[0]!.body.command,
  ).toMatchObject({ material_ids: ["image-2", "image-1", "image-3"] })
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  env.unit.revision++
  env.state.revision++
  await page.waitForResponse((response) =>
    response.url().includes("image-composition/units"),
  )
  await expect(
    sheet.getByText(
      "图片已被其他操作更新。请取消本地调整并核对最新内容后再编辑。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeDisabled()
  await sheet.getByRole("button", { name: "取消图片调整" }).click()
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeEnabled()
})

test("取消图片匹配后不沿用轮询缓存，当前组合保留", async ({ page }) => {
  const { state } = await imageBoundary(page)
  let polls = 0
  await page.route("**/image-composition/candidates/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith("/refresh")) {
      state.candidate_build = {
        snapshot_id: D,
        revision: 1,
        generation: 2,
        status: "PENDING",
        processed: 10,
        candidate_count: 10,
        completed_drama_count: 0,
        total_drama_count: 2,
        added: null,
        removed: null,
        error_code: null,
        cutoff: "2026-10-29T00:00:00Z",
      }
    } else if (path.endsWith("/cancel")) {
      state.candidate_build = { ...state.candidate_build!, status: "CANCELLED" }
    } else polls++
    return route.fulfill({ json: state.candidate_build })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "刷新匹配图片" }).click()
  await expect.poll(() => polls).toBeGreaterThan(0)
  await page.getByRole("button", { name: "取消匹配" }).click()
  await expect(page.getByText("图片匹配已停止，可重新刷新。")).toBeVisible()
  await expect(page.getByRole("button", { name: "取消匹配" })).toHaveCount(0)
  expect(state.active_generation).toBe(1)
})

for (const action of [
  "music",
  "other-card",
  "overlapping-music",
  "out-of-order",
] as const) {
  test(`草稿版本变化保留未保存图片：${action}`, async ({ page }) => {
    const env = await imageBoundary(page)
    const second = {
      ...env.unit,
      id: "second-unit",
      position: 2,
      ordered_material_ids: ["other-1", "other-2"],
    }
    await page.route("**/image-composition/units?**", async (route) => {
      if (env.state.revision > 1)
        await new Promise((resolve) => setTimeout(resolve, 150))
      await route.fulfill({
        json: { items: [env.unit, second], total: 2, next_cursor: null },
      })
    })
    let releaseEdit: (() => void) | undefined
    let releaseMusic: (() => void) | undefined
    let musicStarted = false
    await page.route("**/image-composition/edits", async (route) => {
      const body = route.request().postDataJSON()
      env.calls.push({ suffix: "/edits", body })
      if (body.expected_revision !== env.state.revision) {
        return route.fulfill({
          status: 409,
          json: { code: "draft_revision_conflict", message: "草稿已更新" },
        })
      }
      const target = body.command.unit_id === second.id ? second : env.unit
      target.ordered_material_ids = body.command.material_ids
      target.revision++
      env.state.revision++
      const savedRevision = env.state.revision
      if (
        action === "out-of-order" &&
        target === second &&
        savedRevision === 2
      ) {
        releaseMusic!()
        await new Promise<void>((resolve) => {
          releaseEdit = resolve
        })
      }
      return route.fulfill({
        json: {
          revision: savedRevision,
          affected_units: [target.id],
          pending_music_ids: [],
        },
      })
    })
    if (action === "out-of-order") {
      // 父级摘要暂未刷新时，晚到回执也不能让新编辑退回旧版本。
      await page.route("**/image-composition", (route) =>
        route.fulfill({ json: { ...env.state, revision: 1 } }),
      )
    }
    if (action === "overlapping-music" || action === "out-of-order") {
      const gate = new Promise<void>((resolve) => {
        releaseMusic = resolve
      })
      await page.route("**/music/units/*", async (route) => {
        musicStarted = true
        await gate
        env.state.revision++
        env.unit.music_revision++
        await route.fulfill({
          json: {
            revision: env.state.revision,
            music_revision: env.unit.music_revision,
            request_generation: 2,
            status: "READY",
            job_ids: [],
          },
        })
      })
    }
    await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
    await page.getByRole("button", { name: "查看与调整轮播" }).first().click()
    const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
    const first = sheet
      .locator('[data-slot="card"]')
      .filter({ hasText: "创意 1 ·" })
    await first.getByRole("button", { name: "图片 2 前移" }).click()
    if (action === "music") {
      await first.getByRole("button", { name: "换音乐", exact: true }).click()
      const picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
      await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
      await picker.getByRole("button", { name: "搜索", exact: true }).click()
      await picker.getByRole("button", { name: "新曲 · 演示" }).click()
      await picker.getByRole("button", { name: "使用这首音乐" }).click()
    } else {
      if (action === "overlapping-music" || action === "out-of-order") {
        await first
          .getByRole("button", { name: "重新推荐", exact: true })
          .click()
        await expect.poll(() => musicStarted).toBe(true)
      }
      const other = sheet
        .locator('[data-slot="card"]')
        .filter({ hasText: "创意 2 ·" })
      await other.getByRole("button", { name: "图片 2 前移" }).click()
      await other
        .getByRole("button", { name: "保存图片调整", exact: true })
        .click()
    }
    if (action === "out-of-order") {
      await expect.poll(() => env.state.revision).toBe(3)
      // 音乐回执先到、图片保存回执后到，仍须保留连续的两步成功事实。
      await expect.poll(() => !!releaseEdit).toBe(true)
      releaseEdit!()
    } else {
      await expect.poll(() => env.state.revision).toBe(2)
    }
    if (action === "overlapping-music") {
      // 第二张卡片的成功回执先到，原先发出的音乐请求随后推进一个版本。
      await page.waitForResponse((response) =>
        response.url().includes("image-composition/units"),
      )
      releaseMusic!()
      await expect.poll(() => env.state.revision).toBe(3)
    }
    await page.waitForResponse((response) =>
      response.url().includes("image-composition/units"),
    )
    if (action === "out-of-order") {
      const other = sheet
        .locator('[data-slot="card"]')
        .filter({ hasText: "创意 2 ·" })
      await other
        .getByRole("button", { name: "图片 2 前移", exact: true })
        .click()
      await other
        .getByRole("button", { name: "保存图片调整", exact: true })
        .click()
      await expect
        .poll(() => second.ordered_material_ids)
        .toEqual(["other-1", "other-2"])
    }
    await expect(
      first.getByRole("button", { name: "保存图片调整", exact: true }),
    ).toBeEnabled()
    await first
      .getByRole("button", { name: "保存图片调整", exact: true })
      .click()
    expect(
      env.calls.filter((c) => c.suffix === "/edits").slice(-1)[0].body.command,
    ).toMatchObject({ material_ids: ["image-2", "image-1", "image-3"] })
    await expect
      .poll(() => env.unit.ordered_material_ids)
      .toEqual(["image-2", "image-1", "image-3"])
  })
}

test("第二页换曲后重置签名游标，往返分页保留图片编辑", async ({ page }) => {
  const env = await imageBoundary(page)
  const first = { ...env.unit, id: "first-page", position: 1 }
  env.unit.position = 51
  const invalid: string[] = []
  await page.route("**/image-composition/units?**", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor")
    if (cursor && cursor !== `revision-${env.state.revision}`) {
      invalid.push(cursor)
      return route.fulfill({
        status: 422,
        json: { code: "invalid_cursor", message: "分页游标无效" },
      })
    }
    return route.fulfill({
      json: {
        items: [cursor ? env.unit : first],
        total: 51,
        next_cursor: cursor ? null : `revision-${env.state.revision}`,
      },
    })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看与调整轮播" }).first().click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(sheet.getByText("创意 51 · 3 张", { exact: true })).toBeVisible()
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  await sheet.getByRole("button", { name: "上一页", exact: true }).click()
  await sheet.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeEnabled()
  await sheet.getByRole("button", { name: "换音乐", exact: true }).click()
  const picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "使用这首音乐" }).click()
  await expect(sheet.getByText("创意 1 · 3 张", { exact: true })).toBeVisible()
  await sheet.getByRole("button", { name: "下一页", exact: true }).click()
  await sheet.getByRole("button", { name: "保存图片调整", exact: true }).click()
  expect(
    env.calls.filter((c) => c.suffix === "/edits").slice(-1)[0].body.command,
  ).toMatchObject({ material_ids: ["image-2", "image-1", "image-3"] })
  expect(invalid).toEqual([])
})

test("勾选剧目批量字段覆盖、指定曲、恢复与失败重试范围明确", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  const other = "99999999-9999-4999-8999-999999999999",
    untouched = "77777777-7777-4777-8777-777777777777"
  env.state.drama_patches = {
    [untouched]: { images_per_unit: 7 },
    [env.unit.drama_id]: { order: "CREATED_AT" },
  }
  await page.route("**/image-composition/dramas?**", (route) =>
    route.fulfill({
      json: {
        items: [env.unit.drama_id, other, untouched].map((id, i) => ({
          drama_id: id,
          title: `剧${i + 1}`,
          link_id: D,
          first_line: i + 1,
          candidate_count: 61,
          unit_count: 13,
          failed_count: 2,
          pending_count: 0,
          selection_count: 1,
          protected_count: 1,
        })),
        total: 3,
        next_cursor: null,
      },
    }),
  )
  let retry: Record<string, unknown> | null = null
  await page.route("**/music/batch-retries", async (route) => {
    if (route.request().method() === "POST") {
      retry = route.request().postDataJSON()
      env.state.revision++
    }
    const result = {
      run_id: D,
      revision: env.state.revision,
      drama_ids: [env.unit.drama_id, other],
      status: "PENDING",
      processed: 20,
      retried: 18,
      skipped_protected: 1,
      skipped_recovered: 1,
      error_code: null,
    }
    return route.fulfill({
      json:
        route.request().method() === "POST" ? result : retry ? [result] : [],
    })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("checkbox", { name: "选择剧目 剧1" }).check()
  await page.getByRole("checkbox", { name: "选择剧目 剧2" }).check()
  await page.getByRole("button", { name: "修改所选规则" }).click()
  const rules = page.getByRole("dialog", { name: "修改所选剧目规则" })
  await rules.getByLabel("每轮播张数", { exact: true }).fill("4")
  await rules.getByRole("button", { name: "保存所选规则" }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  expect(
    env.calls.filter((c) => c.suffix === "/plans").slice(-1)[0].body
      .drama_patches,
  ).toEqual({
    [env.unit.drama_id]: { order: "CREATED_AT", images_per_unit: 4 },
    [other]: { images_per_unit: 4 },
    [untouched]: { images_per_unit: 7 },
  })
  await page.getByRole("button", { name: "返回修改" }).click()
  await page.getByRole("button", { name: "所选剧指定音乐" }).click()
  const music = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await music.getByLabel("搜索音乐", { exact: true }).fill("新")
  await music.getByRole("button", { name: "搜索", exact: true }).click()
  await music.getByRole("button", { name: "新曲 · 演示" }).click()
  await music.getByRole("button", { name: "使用这首音乐" }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const patched = env.calls.filter((c) => c.suffix === "/plans").slice(-1)[0]
    .body.drama_patches as Record<string, unknown>
  expect(patched[other]).toMatchObject({
    images_per_unit: 4,
    music_mode: "FIXED",
    fixed_music_id: "song-new",
  })
  expect(patched[untouched]).toEqual({ images_per_unit: 7 })
  await page.getByRole("button", { name: "返回修改" }).click()
  await page.getByRole("button", { name: "所选剧恢复整批规则" }).click()
  await page.getByRole("button", { name: "确认恢复所选规则" }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  expect(
    env.calls.filter((c) => c.suffix === "/plans").slice(-1)[0].body
      .drama_patches,
  ).toEqual({ [untouched]: { images_per_unit: 7 } })
  await page.getByRole("button", { name: "返回修改" }).click()
  await page.getByRole("button", { name: "重试所选剧失败配乐" }).click()
  await page.getByRole("button", { name: "确认重试所选失败项" }).click()
  expect(retry).toMatchObject({ drama_ids: [env.unit.drama_id, other] })
  await page.reload()
  await expect(
    page.getByText(/批量重试进行中.*已扫描 20.*已重试 18/),
  ).toBeVisible()
})

test("替换差异呈现剧名稳定创意位置和具体保护损失", async ({ page }) => {
  const env = await imageBoundary(page)
  let replace = false
  await page.route("**/image-composition/plans", (route) => {
    const body = route.request().postDataJSON()
    replace = body.mode === "REPLACE_ALL"
    return route.fulfill({
      json: {
        plan_id: replace ? "replace-plan" : D,
        base_revision: 1,
        input_digest: "test",
        unit_count: 2,
        added_unit_count: 2,
        retained_unit_count: 0,
        removed_unit_count: 2,
        protected_unit_count: 0,
        music_counts: {},
        image_reference_count: 6,
        distinct_image_count: 6,
        reused_image_count: 0,
        conflicts: [],
        conflict_count: 0,
        mode: body.mode,
        dramas: [
          {
            drama_id: env.unit.drama_id,
            drama_title: "月光之剧",
            retained_ids_count: 0,
            removed_ids_count: 2,
            excluded_images_count: 0,
            protection_losses_count: replace ? 1 : 0,
          },
        ],
      },
    })
  })
  await page.route("**/plans/*/differences?**", (route) =>
    route.fulfill({
      json: {
        items: replace
          ? [
              {
                drama_id: env.unit.drama_id,
                drama_title: "月光之剧",
                unit_id: D,
                unit_position: 51,
                composition_protected: true,
                music_protected: true,
                materials: [{ material_id: D, file_name: "月光_封面.jpg" }],
              },
            ]
          : [],
        total: replace ? 1 : 0,
        next_cursor: null,
      },
    }),
  )
  await page.route("**/plans/*/units?**", (route) =>
    route.fulfill({
      json: {
        items: [{ ...env.unit, position: 51, drama_title: "月光之剧" }],
        total: 1,
        next_cursor: null,
      },
    }),
  )
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  await page.getByRole("button", { name: "同时替换手工调整" }).click()
  const diff = page.getByRole("dialog", { name: "查看生成结果", exact: true })
  await expect(
    diff.getByText("月光之剧 · 创意 51", { exact: true }),
  ).toBeVisible()
  await expect(diff.getByText("月光_封面.jpg", { exact: true })).toBeVisible()
  await expect(
    diff.getByText("将失去图片保护、音乐保护", { exact: true }),
  ).toBeVisible()
  await expect(diff.getByRole("button", { name: "应用并生成" })).toBeDisabled()
  await diff.getByRole("checkbox", { name: "我已核对所列保护损失" }).check()
  await expect(diff.getByRole("button", { name: "应用并生成" })).toBeEnabled()
})

test("候选第二页恢复排除后游标重置且保留已选图片，摘要换版本归第一页", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  const invalid: string[] = []
  await page.route("**/image-composition/dramas?**", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor")
    if (cursor && cursor !== `dramas-${env.state.revision}`) {
      invalid.push(cursor)
      return route.fulfill({ status: 422, json: { code: "invalid_cursor" } })
    }
    return route.fulfill({
      json: {
        items: [
          {
            drama_id: env.unit.drama_id,
            title: cursor ? "第二页剧" : "第一页剧",
            link_id: D,
            first_line: 1,
            candidate_count: 61,
            unit_count: 13,
            failed_count: 0,
            pending_count: 0,
            selection_count: 1,
            protected_count: 1,
          },
        ],
        total: 51,
        next_cursor: cursor ? null : `dramas-${env.state.revision}`,
      },
    })
  })
  await page.route("**/image-composition/candidates?**", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor")
    if (cursor && cursor !== `candidates-${env.state.revision}`) {
      invalid.push(cursor)
      return route.fulfill({ status: 422, json: { code: "invalid_cursor" } })
    }
    return route.fulfill({
      json: {
        items: [
          {
            material_id: cursor ? "excluded" : "selected",
            drama_id: env.unit.drama_id,
            file_name_snapshot: cursor ? "待恢复图片.jpg" : "已选图片.jpg",
            enabled: !cursor,
          },
        ],
        total: 51,
        next_cursor: cursor ? null : `candidates-${env.state.revision}`,
      },
    })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "下一页", exact: true })
    .first()
    .click()
  await expect(page.getByText("第二页剧", { exact: true })).toBeVisible()
  env.state.revision++
  await expect(page.getByText("第一页剧", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "查看与调整轮播" }).click()
  await page.getByRole("button", { name: "手工新建轮播" }).click()
  const picker = page.getByRole("dialog", {
    name: "添加本剧候选图片",
    exact: true,
  })
  await picker.getByRole("checkbox", { name: "已选图片.jpg" }).check()
  await picker.getByRole("button", { name: "下一页", exact: true }).click()
  await picker.getByRole("button", { name: "恢复本次使用" }).click()
  await expect(
    picker.getByRole("checkbox", { name: "已选图片.jpg" }),
  ).toBeChecked()
  expect(invalid).toEqual([])
  const restoredRevision = env.state.revision
  await picker
    .getByRole("button", { name: "保存图片调整", exact: true })
    .click()
  await expect(picker).not.toBeVisible()
  expect(
    env.calls.filter((call) => call.suffix === "/edits").at(-1)?.body,
  ).toMatchObject({
    expected_revision: restoredRevision,
    command: { kind: "CREATE", material_ids: ["selected"] },
  })
})

// 最终审查回归：清理原件后仍须按当前 BC 辨认图片，读取不得隐式上传。
for (const failure of [
  "cleaned",
  "original-url",
  "revoked",
  "remote-url",
] as const) {
  test(`轮播缩略图有界授权回退 ${failure}`, async ({ page }) => {
    await imageBoundary(page)
    let recovered = false
    const reads: string[] = [],
      writes: string[] = []
    await page.route("**/materials/**", async (route) => {
      const request = route.request(),
        url = new URL(request.url())
      if (request.method() !== "GET") writes.push(url.pathname)
      if (url.pathname.endsWith("/preview")) {
        reads.push("original")
        return failure === "original-url"
          ? route.fulfill({ json: { url: "/missing-original.png" } })
          : route.fulfill({ status: 409, json: { detail: "upload_not_ready" } })
      }
      if (url.pathname.endsWith("/remote-preview")) {
        reads.push("remote")
        expect(url.searchParams.get("bc_id")).toBe(BC)
        return failure === "revoked"
          ? route.fulfill({ status: 403, json: { detail: "source_revoked" } })
          : route.fulfill({
              json: {
                media_type: "IMAGE",
                bc_id: BC,
                image_id: "platform-image",
                url:
                  failure === "remote-url" && !recovered
                    ? "/missing-remote.png"
                    : "/thumbnail.svg",
              },
            })
      }
      return route.fallback()
    })
    await page.route("**/missing-*.png", (route) =>
      route.fulfill({ status: 404 }),
    )
    await page.route("**/thumbnail.svg", (route) =>
      route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="gray"/></svg>',
      }),
    )
    await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
    await page
      .getByRole("button", { name: "查看与调整轮播", exact: true })
      .first()
      .click()
    const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
    if (failure === "revoked" || failure === "remote-url") {
      await expect(
        sheet.getByText("图片 1 · 预览不可用", { exact: true }),
      ).toBeVisible()
    } else {
      const image = sheet.getByRole("img", { name: "图片 1", exact: true })
      await expect(image).toBeVisible()
      await expect
        .poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth))
        .toBe(80)
    }
    expect(reads.filter((v) => v === "original")).toHaveLength(3)
    expect(reads.filter((v) => v === "remote")).toHaveLength(3)
    if (failure === "remote-url") {
      recovered = true
      await sheet
        .getByRole("button", { name: "重试图片 1 预览", exact: true })
        .click()
      const image = sheet.getByRole("img", { name: "图片 1", exact: true })
      await expect
        .poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth))
        .toBe(80)
      expect(reads.filter((v) => v === "original")).toHaveLength(3)
      expect(reads.filter((v) => v === "remote")).toHaveLength(4)
    }
    expect(writes).toEqual([])
  })
}

for (const mode of ["batch", "drama"] as const) {
  test(`就绪草稿本地规则阻止冻结旧配置 ${mode}`, async ({ page }) => {
    const env = await imageBoundary(page)
    env.state.preview_ready = true
    await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
    const preview = page.getByRole("button", {
      name: "生成搭建预览",
      exact: true,
    })
    await expect(preview).toBeEnabled()
    if (mode === "batch")
      await page.getByLabel("每轮播张数", { exact: true }).fill("3")
    else {
      await page
        .getByRole("button", { name: "查看与调整轮播", exact: true })
        .first()
        .click()
      const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
      await sheet.getByRole("button", { name: "本剧规则", exact: true }).click()
      await sheet.getByLabel("每轮播张数", { exact: true }).fill("3")
      await sheet.getByRole("button", { name: "完成", exact: true }).click()
    }
    await expect(preview).toBeDisabled()
    // 绕过按钮 disabled 调用已挂载的 React 回调，单独验证创建处理器的门槛。
    await preview.evaluate((button) => {
      const key = Object.keys(button).find((key) =>
        key.startsWith("__reactProps$"),
      )!
      ;(button as unknown as Record<string, { onClick: () => void }>)[
        key
      ].onClick()
    })
    expect(
      env.requests.filter(
        (r) => r.method === "POST" && r.path.endsWith("/previews"),
      ),
    ).toHaveLength(0)
    env.state.preview_ready = false
    await page
      .getByRole("button", { name: "查看生成结果", exact: true })
      .click()
    const diff = page.getByRole("dialog", { name: "查看生成结果", exact: true })
    await diff.getByRole("button", { name: "返回修改" }).click()
    await expect(preview).toBeDisabled()
    await page
      .getByRole("button", { name: "查看生成结果", exact: true })
      .click()
    await diff.getByRole("button", { name: "应用并生成" }).click()
    await expect(page.getByText("生成中 1/15", { exact: false })).toBeVisible()
    env.state.applied_rule_revision = env.state.revision
    env.summary.revision = env.state.revision
    env.state.preview_ready = true
    env.state.run = { ...env.state.run, status: "APPLIED" }
    await expect(preview).toBeEnabled()
    expect(
      env.requests.filter(
        (r) => r.method === "POST" && r.path.endsWith("/previews"),
      ),
    ).toHaveLength(0)
  })
}

test("本地规则不阻止查询已有预览", async ({ page }) => {
  const env = await imageBoundary(page)
  env.state.preview_ready = true
  await page.addInitScript(
    ({ tenant, draft }) => {
      sessionStorage.setItem(`build-preview:${tenant}:${draft}`, "1")
    },
    { tenant: T, draft: D },
  )
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByLabel("每轮播张数", { exact: true }).fill("3")
  const recovery = page.getByRole("button", {
    name: "查询原预览结果",
    exact: true,
  })
  await expect(recovery).toBeEnabled()
  await recovery.click()
  await expect
    .poll(
      () =>
        env.requests.filter(
          (r) => r.method === "GET" && r.path.includes("/previews/"),
        ).length,
    )
    .toBeGreaterThan(0)
  expect(
    env.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/previews"),
    ),
  ).toHaveLength(0)
})

test("本地批次和逐剧编辑共同保留门槛，重载后只读服务端规则", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  env.state.preview_ready = true
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByLabel("每轮播张数", { exact: true }).fill("3")
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "本剧规则", exact: true }).click()
  await sheet.getByLabel("每轮播张数", { exact: true }).fill("4")
  await sheet.getByRole("button", { name: "恢复整批配置" }).click()
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeDisabled()
  page.on("dialog", (dialog) => dialog.accept())
  await page.reload()
  await expect(page.getByLabel("每轮播张数", { exact: true })).toHaveValue("5")
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeEnabled()
})

test("过期图片编辑不能通过添加弹窗覆盖新顺序", async ({ page }) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "图片 2 前移", exact: true }).click()
  env.unit.ordered_material_ids = ["image-3", "image-2", "image-1"]
  env.unit.revision++
  env.state.revision++
  await expect(sheet.getByText(/图片已被其他操作更新/)).toBeVisible()
  await expect(
    sheet.getByRole("button", { name: "添加图片", exact: true }),
  ).toBeDisabled()
  expect(env.calls.filter((c) => c.suffix === "/edits")).toHaveLength(0)
})

test("添加图片弹窗打开后并发修改不能提交旧内容", async ({ page }) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "添加图片", exact: true }).click()
  const add = page.getByRole("dialog", {
    name: "添加本剧候选图片",
    exact: true,
  })
  await expect(
    add.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeEnabled()
  env.unit.ordered_material_ids = ["image-3", "image-2", "image-1"]
  env.unit.revision++
  env.state.revision++
  await expect(
    add.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeDisabled()
  await expect(add.getByText(/已更新/)).toBeVisible()
  expect(env.calls.filter((c) => c.suffix === "/edits")).toHaveLength(0)
})

test("添加图片保存固定打开时版本以阻止轮询前并发覆盖", async ({ page }) => {
  const env = await imageBoundary(page)
  await page.route("**/image-composition/edits", (route) => {
    const body = route.request().postDataJSON()
    env.calls.push({ suffix: "/edits", body })
    return route.fulfill({
      status: 409,
      json: { code: "draft_revision_conflict", message: "草稿已更新，请刷新" },
    })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "图片 2 前移", exact: true }).click()
  // 单元尚未轮询，但草稿状态已读回另一操作的新revision。
  env.state.revision++
  await page.waitForResponse((r) =>
    new URL(r.url()).pathname.endsWith("/image-composition"),
  )
  await sheet.getByRole("button", { name: "添加图片", exact: true }).click()
  const add = page.getByRole("dialog", {
    name: "添加本剧候选图片",
    exact: true,
  })
  const save = add.getByRole("button", { name: "保存图片调整", exact: true })
  if (await save.isEnabled()) {
    await save.click()
    await expect
      .poll(() => env.calls.filter((c) => c.suffix === "/edits").length)
      .toBe(1)
    expect(
      env.calls.find((c) => c.suffix === "/edits")!.body.expected_revision,
    ).toBe(1)
  } else {
    expect(env.calls.filter((c) => c.suffix === "/edits")).toHaveLength(0)
  }
})

for (const height of [720, 929]) {
  test(`50及100候选图片在${height}px窗口可翻页和保存`, async ({ page }) => {
    const env = await imageBoundary(page)
    await page.setViewportSize({ width: 1280, height })
    const limits: number[] = []
    await page.route("**/image-composition/candidates?**", (route) => {
      const limit = Number(
        new URL(route.request().url()).searchParams.get("limit"),
      )
      limits.push(limit)
      return route.fulfill({
        json: {
          items: Array.from({ length: limit }, (_, i) => ({
            material_id: `candidate-${i}`,
            file_name_snapshot: `candidate-${i}.png`,
            enabled: true,
          })),
          total: 200,
          next_cursor: "next-candidates",
        },
      })
    })
    await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
    await page
      .getByRole("button", { name: "查看与调整轮播", exact: true })
      .first()
      .click()
    await page
      .getByRole("button", { name: "手工新建轮播", exact: true })
      .click()
    const add = page.getByRole("dialog", {
      name: "添加本剧候选图片",
      exact: true,
    })
    await expect(add.getByRole("checkbox")).toHaveCount(51)
    const save = add.getByRole("button", { name: "保存图片调整", exact: true })
    await expect(save).toBeInViewport()
    await add.getByRole("combobox", { name: "每页条数", exact: true }).click()
    await page.getByRole("option", { name: "100 条", exact: true }).click()
    await expect(add.getByRole("checkbox")).toHaveCount(101)
    expect(limits).toEqual([50, 100])
    await add
      .getByRole("checkbox", { name: "candidate-0.png", exact: true })
      .check()
    await expect(save).toBeInViewport()
    await save.click()
    await expect
      .poll(() => env.calls.filter((c) => c.suffix === "/edits").length)
      .toBe(1)
    expect(
      env.calls.find((c) => c.suffix === "/edits")!.body.command,
    ).toMatchObject({ kind: "CREATE", material_ids: ["candidate-0"] })
  })
}

test("自身配乐回执跨过外部版本时不得升级未保存图片权限", async ({ page }) => {
  const env = await imageBoundary(page)
  await page.route("**/music/units/*", (route) => {
    env.state.revision += 2
    env.unit.music_revision++
    return route.fulfill({
      json: {
        revision: env.state.revision,
        music_revision: env.unit.music_revision,
        request_generation: 2,
        status: "READY",
        job_ids: [],
      },
    })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看与调整轮播" }).click()
  await page.getByRole("button", { name: "图片 2 前移", exact: true }).click()
  await page.getByRole("button", { name: "重新推荐", exact: true }).click()
  await expect.poll(() => env.state.revision).toBe(3)
  await page.getByRole("button", { name: "保存图片调整", exact: true }).click()
  await expect(
    page.getByText("草稿已更新", { exact: true }).first(),
  ).toBeVisible()
  expect(
    env.calls.filter((c) => c.suffix === "/edits").at(-1)?.body
      .expected_revision,
  ).toBe(1)
  expect(env.unit.ordered_material_ids).toEqual([
    "image-1",
    "image-2",
    "image-3",
  ])
})

test("添加中恢复候选后取消弹窗仍可保存原卡片调整", async ({ page }) => {
  const env = await imageBoundary(page)
  let restored = false
  await page.route("**/image-composition/candidates?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            material_id: "extra",
            drama_id: env.unit.drama_id,
            file_name_snapshot: "恢复图片.jpg",
            enabled: restored,
          },
        ],
        total: 1,
        next_cursor: null,
      },
    }),
  )
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看与调整轮播" }).click()
  await page.getByRole("button", { name: "图片 2 前移", exact: true }).click()
  await page.getByRole("button", { name: "添加图片", exact: true }).click()
  const picker = page.getByRole("dialog", {
    name: "添加本剧候选图片",
    exact: true,
  })
  await picker
    .getByRole("button", { name: "恢复本次使用", exact: true })
    .click()
  await expect.poll(() => env.state.revision).toBe(2)
  restored = true
  await picker.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "保存图片调整", exact: true }).click()
  await expect
    .poll(() => env.unit.ordered_material_ids)
    .toEqual(["image-2", "image-1", "image-3"])
  await expect(
    page.getByRole("button", { name: "取消图片调整", exact: true }),
  ).toHaveCount(0)
})
