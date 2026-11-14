// Excel/飞书通过 TSV 传递矩形数据；引号字段中的换行/Tab 不得拆成新行或新列。
export function clipboardRows(text: string): string[][] {
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
