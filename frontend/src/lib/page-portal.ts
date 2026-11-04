import { createContext, useContext } from "react"

// 标签页中的浮层跟随页面容器缓存，避免隐藏页残留的遮罩覆盖当前工作区。
// 工作台导航等页外组件仍使用 Radix 默认的 body 容器。
export const PagePortalContext = createContext<HTMLElement | undefined>(
  undefined,
)
export function usePagePortalContainer() {
  return useContext(PagePortalContext)
}
