import { describe, expect, test } from "bun:test"
import {
  applyGridPaste,
  emptyDramaRow,
  validateDramaRows,
} from "../src/features/builds/dramaGrid"
import { linksForLines } from "../src/features/builds/manualLinks"

describe("剧目详细信息表格", () => {
  test("按五列粘贴并保留空列、ID 前导零与含 Tab 的 Excel 引号字段", () => {
    const rows = applyGridPaste(
      [],
      0,
      0,
      '剧 A\tM01\t\t00123\t归因 A\r\n剧 B\t\thttps://www.tiktok.com/minis/b\t\t"含\t制表符"',
    )
    expect(rows[0]).toEqual({
      title: "剧 A",
      material_identifier: "M01",
      url: "",
      external_drama_id: "00123",
      protected_base: "归因 A",
    })
    expect(rows[1].protected_base).toBe("含\t制表符")
  })
  test("单列从选中单元格向下填充，保留其他字段", () => {
    const rows = [
      {
        ...emptyDramaRow(),
        title: "剧 A",
        url: "https://www.tiktok.com/minis/a",
      },
    ]
    const result = applyGridPaste(rows, 0, 1, "M01\nM02\n")
    expect(result[0].url).toBe(rows[0].url)
    expect(result.map((r) => r.material_identifier)).toEqual(["M01", "M02"])
    expect(rows[0].material_identifier).toBe("")
  })
  test("拒绝越界，不静默截断", () => {
    expect(() => applyGridPaste([], 0, 4, "a\tb")).toThrow("超出")
    expect(() => applyGridPaste([], 999, 0, "a\nb")).toThrow("1000")
    expect(() =>
      validateDramaRows(
        Array.from({ length: 1001 }, (_, i) => ({
          ...emptyDramaRow(),
          title: `剧${i}`,
        })),
        "jiashu",
      ),
    ).toThrow("1000")
  })
  test("素材标识独立于 URL 保存，按剧名重排映射行号", () => {
    const rows = validateDramaRows(
      [{ ...emptyDramaRow(), title: "剧 A", material_identifier: "  M01  " }],
      "jiashu",
    )
    expect(linksForLines("剧 B\n剧 A", rows)).toEqual([
      {
        line_no: 2,
        url: "",
        external_drama_id: "",
        protected_base: "",
        material_identifier: "M01",
      },
    ])
  })
  test("仅网眼手动链接要求归因；其他版权方无自动取链时要求链接", () => {
    const row = { ...emptyDramaRow(), title: "剧", material_identifier: "M01" }
    expect(validateDramaRows([row], "wangyan")).toHaveLength(1)
    expect(() =>
      validateDramaRows(
        [{ ...row, url: "https://www.tiktok.com/minis/a" }],
        "wangyan",
      ),
    ).toThrow("归因名称")
    expect(() => validateDramaRows([row], "other")).toThrow("推广链接")
  })
  test("忽略全空行、拒绝无剧名、重复剧名及错误 URL", () => {
    expect(validateDramaRows([emptyDramaRow()], "jiashu")).toEqual([])
    expect(() =>
      validateDramaRows(
        [{ ...emptyDramaRow(), material_identifier: "M" }],
        "jiashu",
      ),
    ).toThrow("剧目名称")
    const row = { ...emptyDramaRow(), title: "剧" }
    expect(() => validateDramaRows([row, row], "jiashu")).toThrow("重复")
    expect(() =>
      validateDramaRows([{ ...row, url: "https://bad.example" }], "jiashu"),
    ).toThrow("推广链接")
  })
})
