import { Worksheet } from "@/components/worksheet/Worksheet"
import { clipboardRows } from "@/components/worksheet/clipboard"
import { useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { errorMessage } from "./api"

export type DramaPlanRow = {
  id?: string
  read_only?: boolean
  business_date: string
  scheduled_time: string
  title: string
  provider_connection_id: string
  application_id: string
  promotion_url: string
  external_drama_id: string
  order_materials: boolean
  optimizer_id: string
}

export const planColumns: Array<{
  key:
    | "scheduled_time"
    | "title"
    | "provider_connection_id"
    | "application_id"
    | "promotion_url"
    | "external_drama_id"
    | "order_materials"
  label: string
}> = [
  { key: "scheduled_time", label: "时间" },
  { key: "title", label: "剧目名称" },
  { key: "provider_connection_id", label: "版权方连接" },
  { key: "application_id", label: "应用 ID" },
  { key: "promotion_url", label: "推广链接（可选）" },
  { key: "external_drama_id", label: "剧目 ID（文本）" },
  { key: "order_materials", label: "下单素材" },
]

export function emptyPlanRow(date: string, optimizerId: string): DramaPlanRow {
  return {
    business_date: date,
    scheduled_time: "",
    title: "",
    provider_connection_id: "",
    application_id: "",
    promotion_url: "",
    external_drama_id: "",
    order_materials: false,
    optimizer_id: optimizerId,
  }
}

export function applyPlanPaste(
  rows: DramaPlanRow[],
  startRow: number,
  startColumn: number,
  value: string,
  date: string,
  optimizerId: string,
): DramaPlanRow[] {
  const incoming = clipboardRows(value)
  if (incoming.length + startRow > 1000)
    throw new Error("一次最多填写 1000 部剧")
  if (incoming.some((line) => line.length + startColumn > planColumns.length))
    throw new Error("粘贴区域超出计划列")
  if (
    rows
      .slice(startRow, startRow + incoming.length)
      .some((row) => row.read_only)
  )
    throw new Error("粘贴区域包含已执行的只读行")
  const result = rows.map((row) => ({ ...row }))
  while (result.length < startRow + incoming.length)
    result.push(emptyPlanRow(date, optimizerId))
  incoming.forEach((line, rowIndex) => {
    line.forEach((part, columnIndex) => {
      const key = planColumns[startColumn + columnIndex].key
      if (key === "order_materials")
        result[startRow + rowIndex][key] = /^(1|true|yes|是)$/i.test(
          part.trim(),
        )
      else result[startRow + rowIndex][key] = part
    })
  })
  return result
}

type Props = {
  businessDate: string
  disabled?: boolean
  optimizerId: string
  rows?: DramaPlanRow[]
  onChange?: (rows: DramaPlanRow[]) => void
  onDateChange?: (date: string) => void
  onSave?: (rows: DramaPlanRow[]) => Promise<void> | void
}

export function DramaPlanGrid({
  businessDate,
  disabled = false,
  optimizerId,
  rows: initialRows,
  onChange,
  onDateChange,
  onSave,
}: Props) {
  const initial = useMemo(
    () =>
      Array.from(
        { length: Math.max(10, initialRows?.length ?? 0) },
        (_, index) =>
          initialRows?.[index] ?? emptyPlanRow(businessDate, optimizerId),
      ),
    [businessDate, initialRows, optimizerId],
  )
  const [historyState, setHistoryState] = useState<{
    key: string
    items: DramaPlanRow[][]
  }>({ key: "", items: [] })
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const resetKey = `${businessDate}:${optimizerId}`
  const history = historyState.key === resetKey ? historyState.items : []
  const rows = initial
  const update = (next: DramaPlanRow[]) => {
    setHistoryState({ key: resetKey, items: [...history.slice(-19), rows] })
    onChange?.(next)
  }
  const undo = () => {
    const previous = history[history.length - 1]
    if (!previous) return
    setHistoryState({ key: resetKey, items: history.slice(0, -1) })
    onChange?.(previous)
  }
  return (
    <Card data-testid="drama-plan-grid">
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <CardTitle>剧单排期</CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm text-muted-foreground" htmlFor="plan-date">
            日期
          </label>
          <Input
            id="plan-date"
            data-testid="plan-date"
            type="date"
            disabled={disabled || saving}
            className="w-auto"
            value={businessDate}
            onChange={(event) => onDateChange?.(event.target.value)}
          />
          <Button
            type="button"
            variant="outline"
            onClick={undo}
            disabled={disabled || saving || !history.length}
          >
            撤销
          </Button>
          <Button
            variant="outline"
            disabled={disabled || saving || rows.length >= 1000}
            onClick={() =>
              update([...rows, emptyPlanRow(businessDate, optimizerId)])
            }
          >
            添加行
          </Button>
          {onSave && (
            <Button
              type="button"
              onClick={async () => {
                setSaving(true)
                setError(null)
                try {
                  await onSave(rows)
                  setHistoryState({ key: resetKey, items: [] })
                } catch (reason) {
                  setError(errorMessage(reason))
                } finally {
                  setSaving(false)
                }
              }}
              disabled={disabled || saving}
            >
              {saving ? "保存中…" : "保存剧单"}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="min-w-0 overflow-x-auto">
        {error && (
          <p role="alert" className="mb-3 text-sm text-destructive">
            {error}
          </p>
        )}
        <Worksheet
          testId="drama-plan-table"
          className="max-h-[60vh]"
          columns={planColumns.map((column, index) => ({
            ...column,
            width: [120, 220, 200, 180, 300, 180, 100][index] + "px",
          }))}
          rowCount={rows.length}
          disabled={disabled || saving}
          readOnly={(row) => rows[row].read_only === true}
          cellTestId={(row, column) => `plan-cell-${row}-${column}`}
          value={(row, column) => {
            const key = planColumns[column].key
            return key === "order_materials"
              ? rows[row][key]
                ? "是"
                : "否"
              : rows[row][key]
          }}
          onChange={(row, column, value) => {
            const key = planColumns[column].key
            update(
              rows.map((item, index) =>
                index === row
                  ? {
                      ...item,
                      [key]:
                        key === "order_materials"
                          ? /^(是|1|true)$/i.test(value)
                          : value,
                    }
                  : item,
              ),
            )
          }}
          onPaste={(row, column, value) => {
            try {
              update(
                applyPlanPaste(
                  rows,
                  row,
                  column,
                  value,
                  businessDate,
                  optimizerId,
                ),
              )
              setError(null)
            } catch (reason) {
              setError(errorMessage(reason))
            }
          }}
          onUndo={undo}
          onAddRow={() =>
            update([...rows, emptyPlanRow(businessDate, optimizerId)])
          }
        />
        <p className="mt-3 text-xs text-muted-foreground">
          Tab 切换 · Enter 向下 · Ctrl / ⌘ Z 撤销 · 剧目 ID 保留前导零
        </p>
        <p className="mt-3 text-xs text-muted-foreground">
          素材只在执行时按剧目和版权方连接匹配；此处不选择素材。
        </p>
      </CardContent>
    </Card>
  )
}
