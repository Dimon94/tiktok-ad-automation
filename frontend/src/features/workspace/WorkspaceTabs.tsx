import {
  createMemoryHistory,
  createRouter,
  Outlet,
  RouterContextProvider,
  useBlocker,
  useRouter,
  useRouterState,
} from "@tanstack/react-router"
import { X } from "lucide-react"
import {
  Activity,
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useId,
  useInsertionEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react"
import { createPortal } from "react-dom"
import type { NavGroup } from "@/components/Sidebar/Main"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { PagePortalContext } from "@/lib/page-portal"

interface Destination {
  pathname: string
  search: Record<string, unknown>
}
interface PageTab {
  scope: string
  path: string
  href: string
  title: string
}
interface TabContext {
  tabs: PageTab[]
  path: string
  sameScope: (next: Destination) => boolean
  markDirty: (path: string, source: string, dirty: boolean) => void
  forgetDirty: (path: string, source: string) => void
  close: (path: string) => void
}
const WorkspaceTabsContext = createContext<TabContext | null>(null)
const PageContext = createContext<string | null>(null)

function scopeOf({ pathname, search }: Destination) {
  const tenant = /^\/tenants\/([^/]+)/.exec(pathname)?.[1]
  return tenant
    ? `tenant:${tenant}:${search.bc_id ?? ""}`
    : pathname.startsWith("/platform/") || pathname === "/admin"
      ? "platform"
      : "global"
}

// 每个标签使用独立的内存路由保存匹配及查询参数；导航与离页守卫仍交给主路由。
// 只隐藏共享 Outlet 会让其订阅的新匹配销毁旧表单，因此不能单靠 Activity 缓存。
function TabPage({ href }: { href: string }) {
  const mainRouter = useRouter()
  const [pageRouter] = useState(() => {
    const history = createMemoryHistory({ initialEntries: [href] })
    const router = createRouter({ ...mainRouter.options, history })
    router.navigate = mainRouter.navigate
    history.block = mainRouter.history.block
    return router
  })
  const [ready, setReady] = useState(false)
  const [container] = useState(() => {
    const element = document.createElement("div")
    element.className = "flex min-w-0 flex-col gap-6"
    return element
  })
  const mount = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    // 隐藏标签脱离当前文档但保留 React 树，避免不同表单的固定 ID 相互干扰。
    mount.current?.append(container)
    return () => {
      container.remove()
    }
  }, [container])
  useLayoutEffect(() => {
    let active = true
    // 主路由已完成鉴权与导航，内存历史仅同步该标签自己的 URL。
    if (pageRouter.history.location.href !== href)
      pageRouter.history.replace(href)
    void pageRouter.load().then(() => {
      if (active) setReady(true)
    })
    return () => {
      active = false
    }
  }, [href, pageRouter])
  return (
    <div ref={mount} className="contents">
      {createPortal(
        ready ? (
          <PagePortalContext.Provider value={container}>
            <RouterContextProvider router={pageRouter}>
              <Outlet />
            </RouterContextProvider>
          </PagePortalContext.Provider>
        ) : null,
        container,
      )}
    </div>
  )
}

function titleFor(path: string, groups: NavGroup[]) {
  const item = groups
    .flatMap((group) => group.items)
    .filter(
      (item) =>
        item.path && (path === item.path || path.startsWith(`${item.path}/`)),
    )
    .sort((a, b) => b.path!.length - a.path!.length)[0]
  if (item)
    return path === item.path
      ? item.title
      : `${item.title} · ${path.endsWith("/new") ? "新建" : "详情"}`
  if (/\/build-drafts\//.test(path)) return "广告搭建 · 草稿"
  if (/\/build-previews\//.test(path)) return "广告搭建 · 预览"
  if (/\/campaign-copies\//.test(path)) return "批量复制"
  return (
    (
      {
        "/": "广告搭建",
        "/settings": "个人设置",
        "/admin": "用户管理",
        "/platform/tenants": "平台租户管理",
      } as Record<string, string>
    )[path] ?? "页面详情"
  )
}

export function WorkspaceTabsProvider({
  children,
  groups,
}: {
  children: ReactNode
  groups: NavGroup[]
}) {
  const router = useRouter()
  const location = useRouterState({ select: (state) => state.location })
  const current: PageTab = {
    scope: scopeOf(location),
    path: location.pathname,
    href: location.href,
    title: titleFor(location.pathname, groups),
  }
  const [tabs, setTabs] = useState<PageTab[]>([current])
  const [closeRequest, setCloseRequest] = useState<string | null>(null)
  const dirty = useRef(new Map<string, Set<string>>())
  const scope = scopeOf(location)
  const sameScope = (next: Destination) => scopeOf(next) === scope
  const hasDirty = () =>
    !!localStorage.getItem("access_token") &&
    [...dirty.current.values()].some((sources) => sources.size)
  const blocker = useBlocker({
    shouldBlockFn: ({ next }) => !sameScope(next) && hasDirty(),
    enableBeforeUnload: hasDirty,
    withResolver: true,
  })

  // 同步登记已提交的 URL，确保新页面与标签选中态在同一帧切换。
  const existing = tabs.find((tab) => tab.path === current.path)
  // BC 切换清空旧标签；当前租户素材页由下方稳定 key 保留上传控制器。
  if (tabs.some((tab) => tab.scope !== scope)) {
    setTabs([current])
  } else if (
    !existing ||
    existing.href !== current.href ||
    existing.title !== current.title
  ) {
    setTabs(
      existing
        ? tabs.map((tab) => (tab.path === current.path ? current : tab))
        : [...tabs, current],
    )
  }
  const close = async (path: string, confirmed = false) => {
    if (tabs.length < 2) return
    if (!confirmed && dirty.current.get(path)?.size) {
      setCloseRequest(path)
      return
    }
    if (path === location.pathname) {
      const index = tabs.findIndex((tab) => tab.path === path)
      const target = tabs[index - 1] ?? tabs[index + 1]
      await router.navigate({ href: target.href, ignoreBlocker: confirmed })
      // 用户取消已有离页守卫时，不能删除仍在显示的页面。
      if (router.state.location.pathname === path) return
    }
    dirty.current.delete(path)
    setTabs((items) => items.filter((tab) => tab.path !== path))
    setCloseRequest(null)
  }
  return (
    <WorkspaceTabsContext.Provider
      value={{
        tabs,
        path: current.path,
        sameScope,
        markDirty: (path, source, value) => {
          const sources = dirty.current.get(path) ?? new Set<string>()
          if (value) sources.add(source)
          else sources.delete(source)
          dirty.current.set(path, sources)
        },
        forgetDirty: (path, source) => {
          dirty.current.get(path)?.delete(source)
        },
        close: (path) => {
          void close(path)
        },
      }}
    >
      {children}
      <Dialog
        open={!!closeRequest || blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) {
            setCloseRequest(null)
            blocker.reset?.()
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>有未保存的内容</DialogTitle>
            <DialogDescription>
              {closeRequest
                ? "关闭标签会丢弃该页面未保存的输入。"
                : "离开当前工作区会关闭所有标签，并丢弃未保存的输入。"}
              服务器中已保存的记录会保留。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setCloseRequest(null)
                blocker.reset?.()
              }}
            >
              留在当前页
            </Button>
            <Button
              onClick={() => {
                if (closeRequest) void close(closeRequest, true)
                else blocker.proceed?.()
              }}
            >
              {closeRequest ? "丢弃并关闭" : "丢弃未保存修改"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </WorkspaceTabsContext.Provider>
  )
}

export function WorkspaceTabBar() {
  const context = useContext(WorkspaceTabsContext)
  const router = useRouter()
  const bar = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!context?.path) return
    bar.current
      ?.querySelector('[data-state="active"]')
      ?.scrollIntoView({ block: "nearest", inline: "nearest" })
  }, [context?.path])
  if (!context) return null
  return (
    <div
      ref={bar}
      className="min-w-0 overflow-x-auto border-b bg-background px-4 py-1 lg:px-5"
      data-slot="workspace-tabs"
    >
      <Tabs
        value={context.path}
        onValueChange={(path) => {
          const tab = context.tabs.find((item) => item.path === path)
          if (tab) void router.navigate({ href: tab.href })
        }}
      >
        <TabsList
          aria-label="已打开的页面"
          className="h-auto justify-start gap-1"
        >
          {context.tabs.map((tab) => (
            <div key={tab.path} className="flex shrink-0 items-center gap-0.5">
              <TabsTrigger
                id={`workspace-tab-${encodeURIComponent(tab.path)}`}
                aria-controls={`workspace-panel-${encodeURIComponent(tab.path)}`}
                value={tab.path}
                className="h-7 max-w-56"
                title={tab.title}
                onKeyDown={(event) => {
                  if (event.key === "Delete") {
                    event.preventDefault()
                    context.close(tab.path)
                  }
                }}
              >
                <span className="truncate">{tab.title}</span>
              </TabsTrigger>
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                aria-label={`关闭标签：${tab.title}`}
                disabled={context.tabs.length < 2}
                onClick={() => context.close(tab.path)}
              >
                <X />
              </Button>
            </div>
          ))}
        </TabsList>
      </Tabs>
    </div>
  )
}

export function WorkspaceTabPages() {
  const context = useContext(WorkspaceTabsContext)
  if (!context) return <Outlet />
  return context.tabs.map((tab) => (
    <Activity
      key={tab.path}
      mode={tab.path === context.path ? "visible" : "hidden"}
    >
      <PageContext.Provider value={tab.path}>
        <div
          role="tabpanel"
          id={`workspace-panel-${encodeURIComponent(tab.path)}`}
          aria-labelledby={`workspace-tab-${encodeURIComponent(tab.path)}`}
          className="flex min-w-0 flex-col gap-6"
        >
          <TabPage
            key={
              /^\/tenants\/[^/]+\/materials$/.test(tab.path)
                ? tab.path
                : tab.scope
            }
            href={tab.href}
          />
        </div>
      </PageContext.Provider>
    </Activity>
  ))
}

export function useWorkspaceTabHref(path?: string) {
  const context = useContext(WorkspaceTabsContext)
  return context?.tabs.find((tab) => tab.path === path)?.href
}

// 所有可编辑页面登记未保存状态；隐藏时仍保存登记，真正卸载才移除。
// Activity 的布局/普通 Effects 在隐藏时也会清理，因此用插入 Effect 区分实际卸载。
export function useWorkspacePageGuard(dirty: boolean, retainOnSwitch = true) {
  const context = useContext(WorkspaceTabsContext)
  const path = useContext(PageContext)
  const id = useId()
  const currentContext = useRef(context)
  currentContext.current = context
  useInsertionEffect(
    () => () => {
      if (path) currentContext.current?.forgetDirty(path, id)
    },
    [path, id],
  )
  useEffect(() => {
    if (context && path) context.markDirty(path, id, dirty && retainOnSwitch)
  }, [context, path, id, dirty, retainOnSwitch])
  // 本地传输不交给通用丢弃提示：原页面负责暂停、取消请求和跨租户离开。
  return (next: Destination) =>
    retainOnSwitch &&
    !!context &&
    !!path &&
    (!context.sameScope(next) || next.pathname !== path)
}
