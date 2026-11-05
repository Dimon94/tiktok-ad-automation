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
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
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
        <div
          ref={grid}
          className="min-h-0 flex-1 overflow-auto border-y [&_[data-slot=table-container]]:overflow-visible"
        >
          <Table className="min-w-[1060px] table-fixed">
            <colgroup>
              <col className="w-12" />
              <col className="w-[20%]" />
              <col className="w-[17%]" />
              <col className="w-[27%]" />
              <col className="w-[16%]" />
              <col className="w-[20%]" />
            </colgroup>
            <TableHeader className="sticky top-0 bg-muted">
              <TableRow>
                <TableHead className="text-center">#</TableHead>
                {dramaColumns.map(({ key, label }, c) => (
                  <TableHead
                    key={key}
                    className="h-18 whitespace-normal border-l px-3"
                  >
                    <div>
                      {label}
                      <span className="ml-2 text-xs font-normal">
                        {c === 0 || (c === 2 && provider === "other")
                          ? "必填"
                          : c === 4
                            ? "按需填写"
                            : "选填"}
                      </span>
                    </div>
                    <p
                      className="mt-1 text-xs font-normal"
                      title={
                        c === 3
                          ? "自动取链识别真实版权方 ID；手动链接未提供 ID 时生成本地编号"
                          : undefined
                      }
                    >
                      {hints[c]}
                    </p>
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row, r) => (
                // 输入行保持位置身份，编辑值不改变 React key，避免焦点丢失。
                <TableRow key={r}>
                  <TableCell className="bg-muted/30 p-0 text-center text-xs text-muted-foreground">
                    {r + 1}
                  </TableCell>
                  {dramaColumns.map(({ key, label }, c) => (
                    <TableCell key={key} className="border-l p-0">
                      <Input
                        data-cell={`${r}:${c}`}
                        aria-label={`第 ${r + 1} 行 ${label}`}
                        aria-invalid={
                          error instanceof DramaGridError &&
                          error.row === r &&
                          error.column === c
                        }
                        aria-describedby={
                          error ? "drama-grid-error" : undefined
                        }
                        className="h-11 rounded-none border-0 px-3 shadow-none focus-visible:ring-inset"
                        value={row[key] || ""}
                        onChange={(e) =>
                          change(
                            rows.map((item, index) =>
                              index === r
                                ? { ...item, [key]: e.target.value }
                                : item,
                            ),
                          )
                        }
                        onPaste={(e) => {
                          e.preventDefault()
                          try {
                            change(
                              applyGridPaste(
                                rows,
                                r,
                                c,
                                e.clipboardData.getData("text/plain"),
                              ),
                            )
                            focus(r, c)
                          } catch (e) {
                            setError(
                              e instanceof Error ? e : new Error("粘贴失败"),
                            )
                          }
                        }}
                        onKeyDown={(e) => {
                          if (
                            (e.ctrlKey || e.metaKey) &&
                            e.key.toLowerCase() === "z"
                          ) {
                            e.preventDefault()
                            undo()
                            return
                          }
                          let next = r * 5 + c
                          if (e.key === "Tab") next += e.shiftKey ? -1 : 1
                          else if (e.key === "Enter" || e.key === "ArrowDown")
                            next += 5
                          else if (e.key === "ArrowUp") next -= 5
                          else return
                          // 边界 Tab 交还 Dialog 焦点管理；末行需要时由“添加行”或 Enter 扩展。
                          if (next < 0 || next >= 5000) return
                          e.preventDefault()
                          if (next >= rows.length * 5)
                            change([...rows, emptyDramaRow()])
                          focus(Math.floor(next / 5), next % 5)
                        }}
                      />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
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
