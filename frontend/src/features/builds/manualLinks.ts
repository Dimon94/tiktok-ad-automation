import type { ManualLinkInput } from "@/client"

export type NamedManualLink = Omit<ManualLinkInput, "line_no"> & {
  title: string
  url: string
  external_drama_id: string
  protected_base: string
  material_identifier?: string
}

export function validMinisUrl(value: string) {
  try {
    const url = new URL(value)
    return (
      !/\s/.test(value) &&
      url.protocol === "https:" &&
      ["tiktok.com", "www.tiktok.com"].includes(url.hostname) &&
      !url.username &&
      !url.password &&
      (!url.port || url.port === "443") &&
      /^\/minis\/[^/]+\/?$/.test(url.pathname)
    )
  } catch {
    return false
  }
}

export function linksForLines(
  text: string,
  links: NamedManualLink[],
): ManualLinkInput[] {
  const lines = text.split("\n").map((title) => title.trim())
  return links.map(({ title, ...link }) => {
    const first = lines.indexOf(title)
    if (first < 0 || lines.lastIndexOf(title) !== first)
      throw new Error(
        `「${title}」的剧名已修改或重复，请在“填写更多剧目信息”中核对`,
      )
    const { material_identifier, ...fields } = link
    return {
      ...fields,
      ...(material_identifier ? { material_identifier } : {}),
      line_no: first + 1,
    }
  })
}
