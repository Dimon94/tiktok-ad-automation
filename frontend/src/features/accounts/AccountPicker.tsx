import { useQuery } from "@tanstack/react-query"
import { useEffect, useId, useRef, useState } from "react"
import {
  type AccountPublic,
  AccountsService,
  type ResolvedLine,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogTrigger } from "@/components/ui/dialog"
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { PickerDialogContent } from "@/features/tenants/PickerDialogContent"
import { Pager, RequestError, useCursorPage } from "@/features/tenants/shared"
import { availabilityLabels, FilterSelect } from "./presentation"

type Props = {
  tenantId: string
  bcId: string
  value: string
  disabled: boolean
  onChange: (value: string) => void
  capability?: "build" | "upload"
  selectionMode?: "append" | "replace"
  triggerLabel?: string
}

export function AccountPicker(props: Props) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open && !props.disabled} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" disabled={props.disabled}>
          {props.triggerLabel ?? "选择账户"}
        </Button>
      </DialogTrigger>
      {open && !props.disabled && (
        <AccountSelection
          key={`${props.tenantId}:${props.bcId}`}
          {...props}
          close={() => setOpen(false)}
        />
      )}
    </Dialog>
  )
}

function AccountSelection({
  tenantId,
  bcId,
  value,
  onChange,
  close,
  selectionMode = "append",
  capability = "build",
}: Props & { close: () => void }) {
  const id = useId()
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [availability, setAvailability] = useState("all")
  // 绑定范围需要编辑已有选择；广告搭建仍保留原来的追加语义。
  const replacing = selectionMode === "replace"
  const [selected, setSelected] = useState(
    () =>
      new Set<string>(
        replacing
          ? value
              .split("\n")
              .map((id) => id.trim())
              .filter(Boolean)
          : [],
      ),
  )
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkError, setBulkError] = useState<unknown>()
  const paging = useCursorPage()
  const controller = useRef(new AbortController())
  useEffect(() => {
    const current = new AbortController()
    controller.current = current
    return () => current.abort()
  }, [])
  // 复用后端名称解析，不凭名称猜 ID；分批遵守解析接口的 500 行上限。
  const existing = useQuery({
    queryKey: ["tenant", tenantId, "account-picker-existing", bcId, value],
    enabled: !replacing,
    queryFn: async ({ signal }) => {
      const lines = value
        .split("\n")
        .map((raw, line_no) => ({ raw, line_no: line_no + 1 }))
        .filter((line) => line.raw.trim())
      const rows: ResolvedLine[] = []
      for (let start = 0; start < lines.length; start += 500) {
        const result = await AccountsService.postResolve({
          path: { tenant_id: tenantId },
          body: { bc_id: bcId, lines: lines.slice(start, start + 500) },
          signal,
        })
        rows.push(...result.data)
      }
      return rows
    },
  })
  const existingIds = new Set(
    existing.data?.flatMap((row) =>
      row.advertiser_id ? [row.advertiser_id] : [],
    ) ?? [],
  )
  for (const raw of value.split("\n")) existingIds.add(raw.trim())
  const load = async (
    cursor: string | null,
    limit: number,
    signal: AbortSignal,
  ) =>
    (
      await AccountsService.getAccounts({
        path: { tenant_id: tenantId },
        query: {
          bc_id: bcId,
          query: search,
          availability:
            availability === "all"
              ? undefined
              : (availability as AccountPublic["availability"]),
          cursor,
          limit,
        },
        signal,
      })
    ).data
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "account-picker",
      bcId,
      search,
      availability,
      paging.cursor,
      paging.limit,
    ],
    queryFn: ({ signal }) => load(paging.cursor, paging.limit, signal),
  })
  const locked =
    bulkBusy ||
    query.isFetching ||
    existing.isFetching ||
    !!query.error ||
    !!existing.error
  const eligible = (item: AccountPublic) =>
    item.bc_id === bcId &&
    (capability === "upload" ? item.can_upload : item.can_build) &&
    !item.ownership_conflict &&
    (replacing || !existingIds.has(item.advertiser_id))
  const available = (query.data?.items ?? []).filter(eligible)
  const pageCount = available.filter((item) =>
    selected.has(item.advertiser_id),
  ).length
  const chosen = [...selected].filter(
    (accountId) => replacing || !existingIds.has(accountId),
  )
  function toggle(items: AccountPublic[], checked: boolean) {
    setSelected((old) => {
      const next = new Set(old)
      for (const item of items.filter(eligible)) {
        if (checked) next.add(item.advertiser_id)
        else next.delete(item.advertiser_id)
      }
      return next
    })
  }
  async function selectAll() {
    setBulkBusy(true)
    setBulkError(undefined)
    const signal = controller.current.signal
    try {
      // 取齐筛选结果后再一次性提交勾选；中途失败或取消不留下半批结果。
      const ids = new Set<string>()
      let cursor: string | null = null
      do {
        const page = await load(cursor, 100, signal)
        for (const item of page.items.filter(eligible))
          ids.add(item.advertiser_id)
        cursor = page.next_cursor ?? null
      } while (cursor && !signal.aborted)
      if (!signal.aborted) setSelected((old) => new Set([...old, ...ids]))
    } catch (error) {
      if (!signal.aborted) setBulkError(error)
    } finally {
      if (!signal.aborted) setBulkBusy(false)
    }
  }
  function confirm() {
    if (replacing) {
      onChange(chosen.join("\n"))
      close()
      return
    }
    // 保留原文及未解析行，仅按已解析的实际账户 ID 去重，追加新选 ID。
    const resolved = new Map(
      existing.data?.map((row) => [row.line_no, row.advertiser_id]),
    )
    const seen = new Set<string>()
    const lines = value.split("\n").filter((raw, index) => {
      if (!raw.trim()) return false
      const key = resolved.get(index + 1) || raw.trim()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    for (const accountId of chosen)
      if (!seen.has(accountId)) {
        lines.push(accountId)
        seen.add(accountId)
      }
    onChange(lines.join("\n"))
    close()
  }
  return (
    <PickerDialogContent
      title={replacing ? "选择管理账户" : "选择账户"}
      description={
        replacing
          ? "搜索当前 BC 的账户，支持跨页勾选和批量选择；确认后更新管理范围。"
          : "搜索当前 BC 的账户，勾选后将账户 ID 添加到输入框。"
      }
      contained
    >
      <form
        className="shrink-0"
        onSubmit={(event) => {
          event.preventDefault()
          event.stopPropagation()
          if (bulkBusy) return
          setSearch(input.trim())
          paging.reset()
          setBulkError(undefined)
        }}
      >
        <FieldGroup className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
          <Field className="min-w-0">
            <FieldLabel htmlFor={`${id}-search`}>搜索账户</FieldLabel>
            <div className="flex gap-2">
              <Input
                id={`${id}-search`}
                placeholder="关键词（如 MAX P1）或完整 ID"
                maxLength={255}
                value={input}
                disabled={bulkBusy}
                onChange={(event) => setInput(event.target.value)}
              />
              <Button type="submit" disabled={bulkBusy}>
                搜索
              </Button>
            </div>
          </Field>
          <Field>
            <FieldLabel>可用性</FieldLabel>
            <fieldset disabled={bulkBusy}>
              <FilterSelect
                label="可用性"
                value={availability}
                choices={availabilityLabels}
                onChange={(next) => {
                  if (!bulkBusy) {
                    setAvailability(next)
                    paging.reset()
                    setBulkError(undefined)
                  }
                }}
              />
            </fieldset>
          </Field>
        </FieldGroup>
        <p className="mt-2 text-xs text-muted-foreground">
          多个关键词用空格分隔，名称须同时包含，顺序和大小写不限。
        </p>
      </form>
      {/* 全选工具栏与列表左边距一致，两个操作共用同一高度和垂直中线。 */}
      <div className="flex shrink-0 items-center justify-between gap-2 px-3">
        <Field orientation="horizontal" className="h-8 w-auto shrink-0 gap-2">
          <Checkbox
            id={`${id}-all`}
            disabled={locked || !available.length}
            checked={
              pageCount > 0 && pageCount === available.length
                ? true
                : pageCount > 0
                  ? "indeterminate"
                  : false
            }
            onCheckedChange={(checked) => toggle(available, checked === true)}
          />
          <FieldLabel htmlFor={`${id}-all`}>全选本页</FieldLabel>
        </Field>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={locked || !query.data?.total}
          onClick={() => void selectAll()}
        >
          {bulkBusy ? "正在选择…" : "全选筛选结果"}
        </Button>
      </div>
      <div className="min-h-0 overflow-y-auto" aria-busy={locked}>
        {(query.isPending || (!replacing && existing.isPending)) && (
          <p role="status" className="text-sm text-muted-foreground">
            正在读取账户…
          </p>
        )}
        {query.error && (
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
        )}
        {existing.error && (
          <RequestError
            error={existing.error}
            retry={() => void existing.refetch()}
          />
        )}
        {!!bulkError && (
          <RequestError error={bulkError} retry={() => void selectAll()} />
        )}
        {!query.error && (
          <FieldSet className="gap-1">
            <FieldLegend className="sr-only">账户搜索结果</FieldLegend>
            {query.data?.items.map((item) => (
              <Field
                key={item.advertiser_id}
                orientation="horizontal"
                className="px-3 py-3"
                data-disabled={!eligible(item)}
              >
                <Checkbox
                  id={`${id}-${item.advertiser_id}`}
                  disabled={locked || !eligible(item)}
                  checked={
                    (!replacing && existingIds.has(item.advertiser_id)) ||
                    selected.has(item.advertiser_id)
                  }
                  onCheckedChange={(checked) =>
                    toggle([item], checked === true)
                  }
                />
                <FieldLabel
                  htmlFor={`${id}-${item.advertiser_id}`}
                  className="min-w-0 flex-col items-start gap-1 break-all"
                >
                  <span>{item.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {item.advertiser_id}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {!replacing && existingIds.has(item.advertiser_id)
                      ? "已添加 · "
                      : ""}
                    {availabilityLabels[item.availability]} ·{" "}
                    {item.remote_status}
                    {!(capability === "upload"
                      ? item.can_upload
                      : item.can_build)
                      ? capability === "upload"
                        ? " · 当前无素材上传权限"
                        : " · 当前不可搭建（请核对账户状态与搭建权限）"
                      : ""}
                  </span>
                </FieldLabel>
              </Field>
            ))}
          </FieldSet>
        )}
        {!query.isPending && !query.error && query.data?.items.length === 0 && (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>没有符合条件的账户</EmptyTitle>
            </EmptyHeader>
          </Empty>
        )}
      </div>
      <div className="shrink-0">
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          total={query.data?.total}
          busy={locked}
        />
      </div>
      <Separator />
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <span role="status" className="text-sm">
          已选 {chosen.length} 个账户
        </span>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="ghost"
            disabled={bulkBusy || !selected.size}
            onClick={() => setSelected(new Set())}
          >
            清空勾选
          </Button>
          <Button type="button" variant="outline" onClick={close}>
            取消
          </Button>
          <Button
            type="button"
            disabled={locked || (!replacing && !chosen.length)}
            onClick={confirm}
          >
            {replacing ? "确定选择" : "确定添加"}
          </Button>
        </div>
      </div>
    </PickerDialogContent>
  )
}
