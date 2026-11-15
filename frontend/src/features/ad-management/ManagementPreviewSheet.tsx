import { useMutation } from "@tanstack/react-query"
import { useEffect, useMemo, useRef, useState } from "react"
import {
  AdManagementService,
  type FrozenSelection,
  type ManagementTaskPublic,
  type MutationSpec,
  type RenameRowProblem,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import type { AdsDimension } from "@/features/ads/search"
import { RenameFields } from "./RenameFields"
import {
  emptyRenameDraft,
  parsePastedRows,
  renameRuleFrom,
} from "./renameRules"

export type ManagementAction =
  | { field: "status"; value: "ENABLE" | "DISABLE" }
  | { field: "roas" | "budget" }
  | { field: "name" }

type Props = {
  tenantId: string
  bcId: string
  selection: FrozenSelection | null
  action: ManagementAction | null
  // 发起动作时所在的报表维度，用来说明“选中的是什么、实际改的是哪一层”。
  dimension: AdsDimension
  open: boolean
  onOpenChange: (open: boolean) => void
  onSubmittingChange?: (pending: boolean) => void
  onSubmitted: (task: ManagementTaskPublic) => void
}

type MutationMode = MutationSpec["mode"]

const MODE_OPTIONS: Array<{ value: MutationMode; label: string }> = [
  { value: "set", label: "设为" },
  { value: "add", label: "增加" },
  { value: "subtract", label: "减少" },
  { value: "increase_percent", label: "增加百分比" },
  { value: "decrease_percent", label: "减少百分比" },
]

export function managementActionKey(action: ManagementAction | null) {
  if (!action) return ""
  if (action.field === "status") return `status:${action.value}`
  return action.field
}

export function actionTitle(action: ManagementAction | null) {
  if (!action) return "批量管理"
  if (action.field === "status")
    return action.value === "ENABLE" ? "批量启用" : "批量暂停"
  if (action.field === "name") return "批量改名"
  return action.field === "roas" ? "修改目标 ROAS" : "修改预算"
}

export function actionScope(
  action: ManagementAction | null,
  dimension: AdsDimension,
): string[] {
  // 改名的说明放在各规则页签里，这里不再重复。
  if (!action || action.field === "name") return []
  if (action.field === "status") {
    const verb = action.value === "ENABLE" ? "启用" : "暂停"
    if (dimension === "account") return [`将${verb}所选账户下的全部广告系列。`]
    if (dimension === "drama") return [`将${verb}所选剧对应的全部广告系列。`]
    if (dimension === "material")
      return [
        `只${verb}该素材在对应广告里的使用，广告本身和其它素材不受影响。`,
        "仅 Smart+ 广告支持单独开关素材。",
      ]
    const lines = [
      `只${verb}所选的${DIMENSION_LABELS[dimension]}本身，不会改动它的上级或下级。`,
    ]
    lines.push(
      action.value === "ENABLE"
        ? "如果上级处于暂停状态，下级启用后仍不会投放，需要单独启用上级。"
        : "暂停系列或广告组后，其下的全部内容都会停止投放。",
    )
    return lines
  }
  if (action.field === "roas") {
    const target: Partial<Record<AdsDimension, string>> = {
      campaign: "将修改所选系列下的全部广告组。",
      adgroup:
        "将修改所选广告组；CBO 系列会连同同系列的其它广告组一起修改。",
      ad: "将修改广告所属的广告组；CBO 系列会连同同系列的其它广告组一起修改。",
    }
    return [
      "目标 ROAS 只设置在广告组上，系列和广告没有单独的目标 ROAS。",
      target[dimension] ?? "",
      "CBO 系列的目标 ROAS 必须一致；ABO 广告组独立修改。",
    ].filter(Boolean)
  }
  const target: Partial<Record<AdsDimension, string>> = {
    account: "将修改所选账户下全部系列的预算。",
    drama: "将修改所选剧对应的全部系列的预算。",
    campaign: "所选系列如果使用组预算，会改为修改它下面的每个广告组。",
    adgroup:
      "所选广告组如果所属系列使用系列预算，会改为修改该系列的预算（同系列只改一次）。",
  }
  return [
    "预算改在实际持有预算的那一层：系列开启系列预算（CBO）时改系列日预算，使用组预算时改各广告组日预算。",
    target[dimension] ?? "",
    "按加减或百分比调整时，每个对象按自己的原值分别计算。",
  ].filter(Boolean)
}

// 只接受大于 0 的普通十进制数；"0x10"、"1e3" 之类后端 Decimal 不认的写法一律拒绝。
const DIMENSION_LABELS: Record<AdsDimension, string> = {
  account: "账户",
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  material: "素材",
  drama: "剧",
}
const PLAIN_DECIMAL = /^\d+(\.\d+)?$/

export function ManagementPreviewSheet({
  tenantId,
  bcId,
  selection,
  action,
  dimension,
  open,
  onOpenChange,
  onSubmittingChange,
  onSubmitted,
}: Props) {
  const [submissionError, setSubmissionError] = useState<Error | null>(null)
  const [mode, setMode] = useState<MutationMode>("set")
  const [value, setValue] = useState("")
  const [draft, setDraft] = useState(() =>
    emptyRenameDraft(selection ? "replace" : "explicit"),
  )
  // 规则改名仅操作冻结的勾选；表格粘贴始终按粘贴的 ID 重新解析目标。
  const needsSelection =
    action?.field === "name" && draft.kind !== "explicit" && !selection
  const rule = useMemo(() => renameRuleFrom(draft), [draft])
  // 同一提交遇到网络超时复用幂等键；参数改变才分配新键。
  const attempt = useRef<{
    signature: string
    key: string
    selectionId?: string
    problems?: RenameRowProblem[]
  } | null>(null)
  const autoSubmitted = useRef(false)
  const inFlight = useRef(false)
  const mutation: MutationSpec | null =
    action?.field === "status"
      ? { field: "status", mode: "set", value: action.value }
      : action?.field === "name"
        ? rule
          ? { field: "name", mode: "set", rename: rule }
          : null
        : action &&
            PLAIN_DECIMAL.test(value.trim()) &&
            Number.isFinite(Number(value)) &&
            Number(value) > 0
          ? { field: action.field, mode, value: value.trim() }
          : null
  const submit = useMutation({
    mutationFn: async () => {
      if (!mutation) throw new Error("请填写完整操作参数")
      if (needsSelection) throw new Error("请先在列表勾选要改名的推广系列")
      const signature = JSON.stringify({
        bcId,
        selection: selection?.selection_id,
        mutation,
        text: draft.text,
      })
      if (attempt.current?.signature !== signature)
        attempt.current = { signature, key: crypto.randomUUID() }
      const current = attempt.current
      if (!current.selectionId) {
        if (mutation.field === "name" && mutation.rename?.kind === "explicit") {
          const parsed = parsePastedRows(draft.text)
          if (parsed.errors.length) throw new Error(parsed.errors.join("；"))
          const resolved = (
            await AdManagementService.resolveRenameSelection({
              path: { tenant_id: tenantId },
              body: {
                bc_id: bcId,
                rows: parsed.rows.map((row) => ({
                  campaign_id: row.campaignId,
                  new_name: row.newName,
                })),
              },
            })
          ).data
          // 失败行与操作意图一起保存，在任务详情计入跳过并逐行显示。
          current.problems = resolved.problems
          if (!resolved.selection)
            throw new Error(
              resolved.problems
                .map((row) => `${row.campaign_id}: ${row.reason}`)
                .join("；"),
            )
          current.selectionId = resolved.selection.selection_id
        } else current.selectionId = selection?.selection_id
      }
      if (!current.selectionId) throw new Error("请先选择要管理的对象")
      return (
        await AdManagementService.submitDirectTask({
          path: { tenant_id: tenantId },
          body: {
            bc_id: bcId,
            selection_id: current.selectionId,
            mutation,
            idempotency_key: current.key,
            row_problems: current.problems ?? [],
          },
        })
      ).data
    },
    onError: (error) => setSubmissionError(error),
    onSuccess: (task) => {
      onOpenChange(false)
      onSubmitted(task)
    },
    onSettled: () => {
      inFlight.current = false
      onSubmittingChange?.(false)
    },
  })
  const startSubmit = () => {
    if (inFlight.current) return
    inFlight.current = true
    onSubmittingChange?.(true)
    setSubmissionError(null)
    submit.mutate()
  }
  useEffect(() => {
    if (
      open &&
      selection &&
      action?.field === "status" &&
      !autoSubmitted.current
    ) {
      autoSubmitted.current = true
      startSubmit()
    }
  }, [open, selection, action])
  // 启停点击直接提交；只有失败时才显示重试入口，不增加确认步骤。
  if (action?.field === "status")
    return submissionError ? (
      <Alert variant="destructive">
        <AlertDescription>
          {submissionError.message}
          <Button variant="outline" onClick={startSubmit}>
            重试提交
          </Button>
        </AlertDescription>
      </Alert>
    ) : null
  const numeric = action?.field === "budget" || action?.field === "roas"
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{actionTitle(action)}</SheetTitle>
          <p className="text-sm text-muted-foreground">
            BC：{bcId} · 提交后由后台校验并执行，无法修改的对象会记录跳过原因。
          </p>
        </SheetHeader>
        <div className="space-y-4 px-4">
          {actionScope(action, dimension).map((line) => (
            <p key={line} className="text-sm text-muted-foreground">
              {line}
            </p>
          ))}
          {numeric && (
            <div className="space-y-3">
              <Select
                value={mode}
                onValueChange={(next) => setMode(next as MutationMode)}
              >
                <SelectTrigger aria-label="调整方式">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MODE_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Input
                aria-label="管理新值"
                inputMode="decimal"
                placeholder="请输入大于 0 的数值"
                value={value}
                onChange={(event) => setValue(event.target.value)}
              />
            </div>
          )}
          {action?.field === "name" && (
            <>
              <p className="text-sm text-muted-foreground">
                {draft.kind === "explicit"
                  ? "按表格中的推广系列 ID 改名，不受列表勾选范围限制。"
                  : selection
                    ? "仅修改本次勾选的推广系列。"
                    : "请先在列表勾选要改名的推广系列，或切换到“表格粘贴”按 ID 改名。"}
              </p>
              <RenameFields value={draft} onChange={setDraft} />
            </>
          )}
          {submissionError && (
            <Alert variant="destructive">
              <AlertDescription>
                {submissionError instanceof Error
                  ? submissionError.message
                  : "提交失败，请重试"}
              </AlertDescription>
            </Alert>
          )}
        </div>
        <SheetFooter>
          <Button
            disabled={!mutation || needsSelection || submit.isPending}
            onClick={startSubmit}
          >
            {submit.isPending
              ? "提交中…"
              : submissionError
                ? "重试提交"
                : "提交管理任务"}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
