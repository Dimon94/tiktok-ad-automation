import { type NamedManualLink, validMinisUrl } from "./manualLinks"

export const dramaColumns = [
  { key: "title", label: "剧目名称", limit: 1000 },
  { key: "material_identifier", label: "素材标识", limit: 1000 },
  { key: "url", label: "推广链接", limit: 8192 },
  { key: "external_drama_id", label: "剧目 ID", limit: 255 },
  { key: "protected_base", label: "归因名称", limit: 1000 },
] as const

export const emptyDramaRow = (): NamedManualLink => ({
  title: "",
  material_identifier: "",
  url: "",
  external_drama_id: "",
  protected_base: "",
})

export class DramaGridError extends Error {
  constructor(
    message: string,
    public row: number,
    public column: number,
  ) {
    super(message)
  }
}

// Excel/飞书通过 TSV 传递矩形数据；引号字段中的换行/Tab 不得拆成新行或新列。
function clipboardRows(text: string): string[][] {
  const rows: string[][] = []
  let row: string[] = [],
    value = "",
    quoted = false
  const source = text.replace(/\r\n?/g, "\n")
  for (let i = 0; i < source.length; i++) {
    const char = source[i]
    if (char === '"' && (quoted || !value)) {
      if (quoted && source[i + 1] === '"') {
        value += '"'
        i++
      } else quoted = !quoted
    } else if (!quoted && (char === "\t" || char === "\n")) {
      row.push(value)
      value = ""
      if (char === "\n") {
        rows.push(row)
        row = []
      }
    } else value += char
  }
  if (quoted) throw new Error("粘贴内容的引号未闭合，请检查源表格")
  if (value || row.length || !source.endsWith("\n")) {
    row.push(value)
    rows.push(row)
  }
  return rows
}

export function applyGridPaste(
  rows: NamedManualLink[],
  startRow: number,
  startColumn: number,
  text: string,
) {
  const incoming = clipboardRows(text)
  if (incoming.some((row) => row.length + startColumn > 5))
    throw new Error("粘贴区域超出五列，请调整起始单元格")
  if (incoming.length + startRow > 1000)
    throw new Error("一次最多填写 1000 部剧")
  const result = rows.map((row) => ({ ...row }))
  while (result.length < incoming.length + startRow)
    result.push(emptyDramaRow())
  incoming.forEach((row, i) => {
    row.forEach((value, j) => {
      result[startRow + i][dramaColumns[startColumn + j].key] = value
    })
  })
  return result
}

export function validateDramaRows(
  rows: NamedManualLink[],
  provider: string,
): NamedManualLink[] {
  const seen = new Set<string>()
  return rows.flatMap((raw, index) => {
    if (!dramaColumns.some(({ key }) => raw[key]?.trim())) return []
    if (index >= 1000)
      throw new DramaGridError("一次最多填写 1000 部剧", index, 0)
    const row = emptyDramaRow()
    const fail = (column: number, message: string): never => {
      throw new DramaGridError(`第 ${index + 1} 行${message}`, index, column)
    }
    dramaColumns.forEach(({ key, label, limit }, col) => {
      const value = raw[key] || ""
      if ([...value].some((char) => char.charCodeAt(0) < 32))
        fail(col, `${label}不能包含换行或制表符`)
      row[key] = value.trim()
      if (row[key]!.length > limit) fail(col, `${label}最多 ${limit} 字符`)
    })
    if (!row.title) fail(0, "请填写剧目名称")
    if (seen.has(row.title)) fail(0, "剧目名称重复，请每部剧保留一行")
    seen.add(row.title)
    if (
      (row.url && !validMinisUrl(row.url)) ||
      (!row.url && provider === "other")
    )
      fail(2, "请填写完整的 HTTPS TikTok Minis 推广链接")
    if (row.url && provider === "wangyan" && !row.protected_base)
      fail(4, "网眼手动链接需填写归因名称")
    return [row]
  })
}

export function rowsForDramaText(
  text: string,
  details: NamedManualLink[],
): NamedManualLink[] {
  const rows = text
    .split("\n")
    .map((title) => title.trim())
    .filter(Boolean)
    .map((title) => ({
      ...emptyDramaRow(),
      ...details.find((row) => row.title === title),
      title,
    }))
  // 文本里删除/改名后，保留待核对的补充信息，避免静默丢失链接或标识。
  for (const detail of details)
    if (!rows.some((row) => row.title === detail.title))
      rows.push({ ...emptyDramaRow(), ...detail })
  return rows
}
