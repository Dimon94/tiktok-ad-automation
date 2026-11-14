import { useRef, type ReactNode } from "react"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

export type WorksheetColumn = {
  key: string
  label: string
  width: string
  heading?: ReactNode
}

/** 排期和搭建共用连续单元格及焦点移动；数据校验仍由各业务入口负责。 */
export function Worksheet({
  columns,
  rowCount,
  value,
  onChange,
  onPaste,
  onUndo,
  onAddRow,
  readOnly,
  disabled,
  invalid,
  errorId,
  testId,
  cellTestId,
  className,
}: {
  columns: readonly WorksheetColumn[]
  rowCount: number
  value: (row: number, column: number) => string
  onChange: (row: number, column: number, value: string) => void
  onPaste: (row: number, column: number, value: string) => void
  onUndo: () => void
  onAddRow: () => void
  readOnly?: (row: number) => boolean
  disabled?: boolean
  invalid?: (row: number, column: number) => boolean
  errorId?: string
  testId?: string
  cellTestId?: (row: number, column: number) => string
  className?: string
}) {
  const container = useRef<HTMLDivElement>(null)
  const focus = (index: number) =>
    requestAnimationFrame(() => {
      container.current
        ?.querySelector<HTMLInputElement>(
          `[data-cell="${Math.floor(index / columns.length)}:${index % columns.length}"]`,
        )
        ?.focus()
    })
  return (
    <div
      ref={container}
      className={cn(
        "min-h-0 overflow-auto border [&_[data-slot=table-container]]:overflow-visible",
        className,
      )}
      data-testid={testId}
    >
      <Table
        className="table-fixed"
        style={{
          minWidth: columns.reduce(
            (sum, column) => sum + Number.parseInt(column.width, 10),
            64,
          ),
        }}
      >
        <colgroup>
          <col style={{ width: 64 }} />
          {columns.map((column) => (
            <col key={column.key} style={{ width: column.width }} />
          ))}
        </colgroup>
        <TableHeader className="sticky top-0 z-10 bg-muted">
          <TableRow>
            <TableHead className="text-center">#</TableHead>
            {columns.map((column) => (
              <TableHead
                key={column.key}
                className="h-16 whitespace-normal border-l px-3"
              >
                {column.heading ?? column.label}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: rowCount }, (_, row) => (
            <TableRow key={row}>
              <TableCell className="bg-muted/30 p-0 text-center text-xs text-muted-foreground">
                {row + 1}
                {readOnly?.(row) && <span className="block">只读</span>}
              </TableCell>
              {columns.map((column, col) => (
                <TableCell key={column.key} className="border-l p-0">
                  <Input
                    data-cell={`${row}:${col}`}
                    data-testid={cellTestId?.(row, col)}
                    aria-label={`第 ${row + 1} 行 ${column.label}`}
                    aria-invalid={invalid?.(row, col)}
                    aria-describedby={errorId}
                    disabled={disabled || readOnly?.(row)}
                    className="h-11 rounded-none border-0 px-3 shadow-none focus-visible:ring-inset"
                    value={value(row, col)}
                    onChange={(event) => onChange(row, col, event.target.value)}
                    onPaste={(event) => {
                      event.preventDefault()
                      onPaste(
                        row,
                        col,
                        event.clipboardData.getData("text/plain"),
                      )
                    }}
                    onKeyDown={(event) => {
                      if (
                        (event.ctrlKey || event.metaKey) &&
                        event.key.toLowerCase() === "z"
                      ) {
                        event.preventDefault()
                        onUndo()
                        return
                      }
                      let next = row * columns.length + col
                      let step = 0
                      if (event.key === "Tab") step = event.shiftKey ? -1 : 1
                      else if (event.key === "Enter")
                        step = event.shiftKey ? -columns.length : columns.length
                      else if (event.key === "ArrowDown") step = columns.length
                      else if (event.key === "ArrowUp") step = -columns.length
                      else return
                      next += step
                      // 跳过冻结行，不把键盘焦点送进禁用输入。
                      while (
                        next >= 0 &&
                        next < rowCount * columns.length &&
                        readOnly?.(Math.floor(next / columns.length))
                      )
                        next += step
                      if (next < 0 || next >= 1000 * columns.length) return
                      event.preventDefault()
                      if (next >= rowCount * columns.length) onAddRow()
                      focus(next)
                    }}
                  />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
