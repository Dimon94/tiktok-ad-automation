import { useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Worksheet } from "@/components/worksheet/Worksheet"
import {
  applyGridPaste,
  DramaGridError,
  dramaColumns,
  emptyDramaRow,
  rowsForDramaText,
  validateDramaRows,
} from "./dramaGrid"
import type { NamedManualLink } from "./manualLinks"

export function ManualLinksDialog({
  text,
  links,
  provider,
  onSave,
  onClose,
}: {
  text: string
  links: NamedManualLink[]
  provider: string
  onSave: (rows: NamedManualLink[]) => void
  onClose: () => void
}) {
  const [rows, setRows] = useState(() => {
    const initial = rowsForDramaText(text, links)
    while (initial.length < 10) initial.push(emptyDramaRow())
    return initial
  })
  const history = useRef<NamedManualLink[][]>([])
  const [error, setError] = useState<Error | null>(null)
  const grid = useRef<HTMLDivElement>(null)
  const change = (next: NamedManualLink[], remember = true) => {
    if (remember) {
      history.current.push(rows)
      if (history.current.length > 40) history.current.shift()
    }
    setRows(next)
    setError(null)
  }
  const undo = () => {
    const previous = history.current.pop()
    if (previous) change(previous, false)
  }
  const focus = (r: number, c: number) =>
    requestAnimationFrame(() => {
      grid.current
        ?.querySelector<HTMLInputElement>(`[data-cell="${r}:${c}"]`)
        ?.focus()
    })
  const save = () => {
    try {
      onSave(validateDramaRows(rows, provider))
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e : new Error("请检查剧目信息"))
      if (e instanceof DramaGridError) focus(e.row, e.column)
    }
  }
  const hints = [
    "填写完整剧名",
    "留空按剧名匹配素材",
    provider === "other" ? "请提供已有推广链接" : "留空自动取链",
    "留空自动识别 / 生成编号",
    provider === "wangyan" ? "手动填链接时必填" : "自动取链时由系统获取",
  ]
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="flex h-[88svh] w-[calc(100%-2rem)] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-[1440px]">
        <DialogHeader className="shrink-0 px-6 pt-6 pb-4 pr-12">
          <DialogTitle>填写更多剧目信息</DialogTitle>
          <DialogDescription>
            支持逐格编辑、单列粘贴和多行多列粘贴；请按下方列顺序粘贴，不含表头。
          </DialogDescription>
        </DialogHeader>
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 px-6 pb-4">
          <p className="text-sm text-muted-foreground">
            已填写 {rows.filter((row) => row.title.trim()).length} 部剧 · 最多
            1000 部
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!history.current.length}
              onClick={undo}
            >
              撤销
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => change(Array.from({ length: 10 }, emptyDramaRow))}
            >
              清空
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={rows.length >= 1000}
              onClick={() => {
                change([...rows, emptyDramaRow()])
                focus(rows.length, 0)
              }}
            >
              添加行
            </Button>
          </div>
        </div>
        <div ref={grid} className="flex min-h-0 flex-1 flex-col">
          <Worksheet
            className="flex-1 border-x-0"
            columns={dramaColumns.map((column, index) => ({
              ...column,
              width: [220, 180, 300, 170, 220][index] + "px",
              heading: (
                <>
                  <div>{column.label}</div>
                  <p className="mt-1 text-xs font-normal">{hints[index]}</p>
                </>
              ),
            }))}
            rowCount={rows.length}
            value={(row, column) => rows[row][dramaColumns[column].key] || ""}
            onChange={(row, column, value) =>
              change(
                rows.map((item, index) =>
                  index === row
                    ? { ...item, [dramaColumns[column].key]: value }
                    : item,
                ),
              )
            }
            onPaste={(row, column, value) => {
              try {
                change(applyGridPaste(rows, row, column, value))
                focus(row, column)
              } catch (error) {
                setError(error instanceof Error ? error : new Error("粘贴失败"))
              }
            }}
            onUndo={undo}
            onAddRow={() => change([...rows, emptyDramaRow()])}
            invalid={(row, column) =>
              error instanceof DramaGridError &&
              error.row === row &&
              error.column === column
            }
            errorId={error ? "drama-grid-error" : undefined}
          />
        </div>
        <div className="flex shrink-0 flex-wrap justify-between gap-2 px-6 py-3 text-xs text-muted-foreground">
          <span>空白行不计入 · 剧目 ID 保留前导零</span>
          <span>Tab 切换 · Enter 向下 · Ctrl / ⌘ Z 撤销</span>
        </div>
        {error && (
          <p
            id="drama-grid-error"
            role="alert"
            className="shrink-0 px-6 pb-3 text-sm text-destructive"
          >
            {error.message}
          </p>
        )}
        <DialogFooter className="shrink-0 border-t bg-muted/20 px-6 py-4">
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button onClick={save}>确认并更新剧目</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
