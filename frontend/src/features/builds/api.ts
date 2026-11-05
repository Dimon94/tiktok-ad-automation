import {
  BuildsService,
  type DraftInputPublic,
  type Page_DraftInputPublic_,
  type PreviewSummary,
} from "@/client"
import type { NamedManualLink } from "./manualLinks"
export const buildKey = (tenantId: string, bcId: string) =>
  ["tenant", tenantId, "builds", bcId] as const

export function budgetLabel(
  preview: Pick<PreviewSummary, "budget_strategy" | "daily_budget_label">,
) {
  return (
    preview.daily_budget_label ||
    (preview.budget_strategy === "ADGROUP" ? "组预算合计" : "系列预算合计")
  )
}

export function budgetUnitLabel(strategy: PreviewSummary["budget_strategy"]) {
  return strategy === "ADGROUP" ? "组日预算" : "系列日预算"
}

export function bidLabel(strategy: PreviewSummary["bid_strategy"]) {
  return strategy === "TARGET_ROAS" ? "目标 ROAS" : "最高价值"
}
export async function loadDraftInputs(
  tenantId: string,
  draftId: string,
  signal: AbortSignal,
  progress: (count: number) => void,
) {
  const output: {
    drama: string[]
    account: string[]
    manualLinks: NamedManualLink[]
  } = {
    manualLinks: [],
    drama: [],
    account: [],
  }
  let count = 0
  for (const kind of ["drama", "account"] as const) {
    let cursor: string | null = null
    const seen = new Set<string>()
    const rows: DraftInputPublic[] = []
    do {
      const { data }: { data: Page_DraftInputPublic_ } =
        await BuildsService.inputs({
          path: { tenant_id: tenantId, draft_id: draftId },
          query: { kind, cursor, limit: 100 },
          signal,
        })
      if (signal.aborted) throw new DOMException("Aborted", "AbortError")
      rows.push(...data.items)
      count += data.items.length
      progress(count)
      cursor = data.next_cursor ?? null
      if (cursor && seen.has(cursor)) throw new Error("重复游标，停止恢复")
      if (cursor) seen.add(cursor)
    } while (cursor)
    if (kind === "drama")
      output.manualLinks = rows
        .filter((row) => Object.keys(row.manual_link || {}).length > 0)
        .map((row) => ({
          title: row.raw_text.trim(),
          url: String(row.manual_link?.url || ""),
          material_identifier: String(
            row.manual_link?.material_identifier || "",
          ),
          external_drama_id: String(row.manual_link?.external_drama_id || ""),
          protected_base: String(row.manual_link?.protected_base || ""),
        }))
    output[kind] = rows
      .sort((a, b) => a.line_no - b.line_no)
      .map((row) => row.raw_text)
  }
  return output
}

export const mutationKey = (tenantId: string, bcId: string, draftId: string) =>
  `build-mutation:${tenantId}:${bcId}:${draftId}`
export function readPendingMutation(
  key: string,
): { requestId: string; kind: string; prepare: boolean } | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null")
    return value && typeof value.requestId === "string" ? value : null
  } catch {
    return null
  }
}

export async function loadDraftMaterials(
  tenantId: string,
  draftId: string,
  dramaId: string,
  signal: AbortSignal,
  progress: (count: number) => void,
) {
  const all: import("@/client").DraftMaterialPublic[] = []
  let cursor: string | null = null
  const seen = new Set<string>()
  do {
    const { data }: { data: import("@/client").Page_DraftMaterialPublic_ } =
      await BuildsService.materials({
        path: { tenant_id: tenantId, draft_id: draftId, drama_id: dramaId },
        query: { cursor, limit: 100 },
        signal,
      })
    if (signal.aborted) throw new DOMException("Aborted", "AbortError")
    all.push(...data.items)
    progress(all.length)
    cursor = data.next_cursor ?? null
    if (cursor && seen.has(cursor)) throw new Error("重复游标")
    if (cursor) seen.add(cursor)
  } while (cursor)
  return all.sort((a, b) => a.group_no - b.group_no || a.position - b.position)
}
