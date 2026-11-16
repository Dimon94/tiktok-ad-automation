// 与服务端相同的输入边界；最终授权及校验仍由服务端执行。
export function parseMaterialLinks(text: string) {
  const seen = new Map<string, number>()
  const lines: { line: number; vid: string; duplicateOf?: number }[] = []
  const errors: string[] = []
  text.split(/\r\n|\n|\r/).forEach((raw, index) => {
    const value = raw.trim()
    if (!value) return
    try {
      const url = new URL(value)
      const params = [...url.searchParams]
      if (
        url.protocol !== "https:" ||
        url.host !== "ad-creative-studio-platform.tiktok-row.net" ||
        url.username ||
        url.password ||
        url.pathname !== "/preview" ||
        url.hash ||
        params.length !== 1 ||
        params[0][0] !== "vid" ||
        !/^v[a-z0-9]{20,80}$/.test(params[0][1]) ||
        /\s/.test(value)
      )
        throw new Error()
      const vid = params[0][1]
      lines.push({ line: index + 1, vid, duplicateOf: seen.get(vid) })
      if (!seen.has(vid)) seen.set(vid, index + 1)
    } catch {
      errors.push(`第 ${index + 1} 行不是支持的 TikTok 素材预览链接`)
    }
  })
  if (
    lines.length > 500 ||
    text.length > 250000 ||
    text.split(/\r\n|\n|\r/).length > 10000
  )
    errors.push("每批最多 500 行素材链接，请分批提交")
  return { lines, unique: lines.filter((line) => !line.duplicateOf), errors }
}

export const linkStatus: Record<string, string> = {
  pending: "排队中",
  sending: "上传中",
  submitted: "已提交，待核实",
  verifying: "核实中",
  unknown: "结果未知，正在核实",
  needs_review: "待人工核实",
  success: "成功",
  failed: "失败",
  blocked: "已暂停，请核对授权或配置",
}
