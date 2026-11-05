import type { QueryClient } from "@tanstack/react-query"
import {
  BookOpen,
  Compass,
  RotateCcw,
  Sparkles,
  WandSparkles,
} from "lucide-react"
import {
  Fragment,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react"
import { createPortal } from "react-dom"
import { toast } from "sonner"
import type { AppRouter } from "@/app"
import { LoginService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { MAIN_BC_ID, MAIN_TENANT_ID } from "../fixtures/world"
import { DEMO_GAP_EVENT } from "../runtime/gaps"
import { TourOverlay } from "../tour/TourOverlay"
import { CHAPTERS, chapterTour, MAIN_TOUR, pageTourFor } from "../tour/tours"
import type { Tour, TourContext } from "../tour/types"
import { currentFiller, fillBuildInput } from "./samples"

// 体验版外壳：常驻“体验版 · 演示数据”标识、一键进入、主线引导、本页讲解、重置，
// 以及未覆盖操作的醒目提示。外壳渲染在应用路由之外，通过 router.history 读写位置。
// 进入体验版先到“账户与授权”（租户首页会直接进入广告搭建，自动填好的示例数据会让
// 第一次离开页面就弹出“有未保存的修改”）。
export const DEMO_HOME = `/tenants/${MAIN_TENANT_ID}/accounts?bc_id=${MAIN_BC_ID}`

const RELOAD_EVENT = "tk-ada-demo:reload"

/** 模拟整页刷新：重新挂载页面，内存里的演示数据保留。 */
export function reloadDemoPage() {
  window.dispatchEvent(new Event(RELOAD_EVENT))
}

export function Remountable({ children }: { children: ReactNode }) {
  const [key, setKey] = useState(0)
  useEffect(() => {
    const onReload = () => setKey((value) => value + 1)
    window.addEventListener(RELOAD_EVENT, onReload)
    return () => window.removeEventListener(RELOAD_EVENT, onReload)
  }, [])
  return <Fragment key={key}>{children}</Fragment>
}

function useLocation(router: AppRouter) {
  const subscribe = useCallback(
    (callback: () => void) => router.history.subscribe(callback),
    [router],
  )
  const pathname = useSyncExternalStore(
    subscribe,
    () => router.history.location.pathname,
  )
  const search = useSyncExternalStore(
    subscribe,
    () => router.history.location.search,
  )
  return { pathname, search }
}

export function DemoShell({
  router,
  queryClient,
}: {
  router: AppRouter
  queryClient: QueryClient
}) {
  const { pathname, search } = useLocation(router)
  const [active, setActive] = useState<{ tour: Tour; index: number } | null>(
    null,
  )
  const [entering, setEntering] = useState(false)
  const navigate = useCallback(
    (path: string) => router.history.push(path),
    [router],
  )

  useEffect(() => {
    const onGap = (event: Event) =>
      toast.error("体验版暂未覆盖这个操作", {
        description: String((event as CustomEvent).detail),
      })
    window.addEventListener(DEMO_GAP_EVENT, onGap)
    return () => window.removeEventListener(DEMO_GAP_EVENT, onGap)
  }, [])

  // 浏览器标签页标题加上体验版标识，避免与生产版混淆。
  useEffect(() => {
    const mark = () => {
      if (!document.title.startsWith("【体验版】"))
        document.title = `【体验版】${document.title}`
    }
    mark()
    const head = document.querySelector("title")
    if (!head) return
    const observer = new MutationObserver(mark)
    observer.observe(head, {
      childList: true,
      characterData: true,
      subtree: true,
    })
    return () => observer.disconnect()
  }, [])

  const tenantId = /^\/tenants\/([^/]+)/.exec(pathname)?.[1] ?? MAIN_TENANT_ID
  const bcId = new URLSearchParams(search).get("bc_id") ?? MAIN_BC_ID
  const context: TourContext = useMemo(
    () => ({ tenantId, bcId, pathname }),
    [tenantId, bcId, pathname],
  )
  const onLogin = pathname === "/login"
  const pageTour = onLogin ? null : pageTourFor(pathname)

  // 自由体验时，进入“广告搭建”页自动填好示例的版权方、应用、策略、剧目和账户，
  // 体验者直接点“下一步：准备链接与素材”即可；跟着引导时由引导一步步带着选，不自动填。
  const touring = useRef(false)
  touring.current = !!active
  useEffect(() => {
    if (!/\/builds\/new$/.test(pathname)) return
    let cancelled = false
    const started = Date.now()
    const timer = window.setInterval(() => {
      const drama = document.getElementById(
        "build-drama",
      ) as HTMLTextAreaElement | null
      const connection = document.querySelector(
        '[role="combobox"][aria-label="版权方连接"]',
      )
      if (Date.now() - started > 8000) window.clearInterval(timer)
      if (!drama || !connection || touring.current) return
      window.clearInterval(timer)
      if (drama.value.trim()) return
      void fillBuildInput({ quiet: true }).then(() => {
        if (!cancelled && !touring.current)
          toast.success("已自动填好示例数据", {
            position: "top-center",
            description:
              "2 部示例剧 × 2 个示例账户，版权方、应用和策略也已选好。直接点“下一步：准备链接与素材”继续。",
          })
      })
    }, 300)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [pathname])

  // 当前页面或弹窗能填什么示例数据，用于外壳上的“填入示例数据”按钮。
  const [filler, setFiller] = useState<ReturnType<typeof currentFiller>>()
  useEffect(() => {
    const timer = window.setInterval(() => setFiller(currentFiller()), 600)
    return () => window.clearInterval(timer)
  }, [])

  const enterDemo = async (startTour: boolean) => {
    setEntering(true)
    try {
      const { data } = await LoginService.loginAccessToken({
        body: { username: "demo", password: "demo" },
      })
      localStorage.setItem("access_token", data.access_token)
      await queryClient.invalidateQueries({ queryKey: ["currentUser"] })
      navigate(DEMO_HOME)
      if (startTour) setActive({ tour: MAIN_TOUR, index: 0 })
    } finally {
      setEntering(false)
    }
  }

  const reset = () => {
    // 所有演示数据只在内存中，回到起点后整页刷新即恢复初始状态。
    window.location.hash = "#/login"
    window.location.reload()
  }

  const startTour = (tour: Tour) => {
    if (!localStorage.getItem("access_token")) {
      void enterDemo(true)
      return
    }
    setActive({ tour, index: 0 })
  }

  return (
    <>
      {onLogin ? <LoginCard entering={entering} onEnter={enterDemo} /> : null}
      <Dock
        onStartTour={startTour}
        pageTour={pageTour}
        filler={filler}
        onReset={reset}
      />
      {active ? (
        <TourOverlay
          key={`${active.tour.id}:${active.index}`}
          tour={active.tour}
          index={active.index}
          context={context}
          navigate={navigate}
          onNext={() =>
            setActive((current) =>
              current && current.index < current.tour.steps.length - 1
                ? { ...current, index: current.index + 1 }
                : null,
            )
          }
          onPrev={() =>
            setActive((current) =>
              current
                ? { ...current, index: Math.max(0, current.index - 1) }
                : null,
            )
          }
          onClose={() => setActive(null)}
        />
      ) : null}
    </>
  )
}

function useStopOutsidePointer() {
  // 页面弹窗打开时，点击外壳不应被视为“点到弹窗外面”而关闭弹窗。
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const stop = (event: Event) => event.stopPropagation()
    el.addEventListener("pointerdown", stop)
    el.addEventListener("mousedown", stop)
    return () => {
      el.removeEventListener("pointerdown", stop)
      el.removeEventListener("mousedown", stop)
    }
  }, [])
  return ref
}

function Dock({
  onStartTour,
  pageTour,
  filler,
  onReset,
}: {
  onStartTour: (tour: Tour) => void
  pageTour: Tour | null
  filler: ReturnType<typeof currentFiller>
  onReset: () => void
}) {
  const ref = useStopOutsidePointer()
  // 外壳拦截了 pointerdown（避免关掉页面弹窗），菜单改为点击打开。
  const [menuOpen, setMenuOpen] = useState(false)
  return createPortal(
    <div
      ref={ref}
      data-demo-shell
      data-demo-dock
      // 覆盖在左下角的账号信息位置：不遮挡侧栏导航、页面内容和弹窗底部的主操作按钮。
      className="pointer-events-auto fixed bottom-2 left-2 w-[256px] rounded-lg border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs shadow-md dark:bg-amber-950"
      style={{ zIndex: 2147482000 }}
    >
      <p className="flex items-center gap-1 px-1 font-semibold text-amber-800 dark:text-amber-200">
        <Sparkles className="size-3.5" aria-hidden />
        TK-ADA 体验版 · 演示数据
      </p>
      <div className="mt-1 flex items-center gap-0.5">
        <DropdownMenu modal={false} open={menuOpen} onOpenChange={setMenuOpen}>
          <DropdownMenuTrigger asChild>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-xs"
              onClick={() => setMenuOpen((open) => !open)}
            >
              <Compass aria-hidden />
              引导
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            data-demo-shell
            align="start"
            side="top"
            className="w-72"
            style={{ zIndex: 2147482500 }}
          >
            <DropdownMenuLabel>选择要演示的内容</DropdownMenuLabel>
            <DropdownMenuItem onSelect={() => onStartTour(MAIN_TOUR)}>
              <div className="flex flex-col">
                <span className="font-medium">完整流程</span>
                <span className="text-xs text-muted-foreground">
                  7 章，共 {MAIN_TOUR.steps.length} 步，从授权一路到自动规则
                </span>
              </div>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {CHAPTERS.map((chapter, index) => (
              <DropdownMenuItem
                key={chapter.id}
                onSelect={() => onStartTour(chapterTour(chapter))}
              >
                <div className="flex flex-col">
                  <span>
                    第 {index + 1} 章 · {chapter.title}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {chapter.summary} · {chapter.steps.length} 步
                  </span>
                </div>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        {pageTour ? (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs"
            onClick={() => onStartTour(pageTour)}
          >
            <BookOpen aria-hidden />
            本页讲解
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          className="h-7 px-2 text-xs"
          onClick={onReset}
        >
          <RotateCcw aria-hidden />
          重置
        </Button>
      </div>
      {filler ? (
        <Button
          size="sm"
          className="mt-1 h-7 w-full bg-amber-500 text-xs text-white hover:bg-amber-600"
          onClick={() => void filler.run()}
          data-demo-fill
        >
          <WandSparkles aria-hidden />
          {filler.label}
        </Button>
      ) : null}
    </div>,
    document.body,
  )
}

function LoginCard({
  entering,
  onEnter,
}: {
  entering: boolean
  onEnter: (startTour: boolean) => void
}) {
  const ref = useStopOutsidePointer()
  return createPortal(
    <div
      ref={ref}
      data-demo-shell
      data-demo-login
      className="pointer-events-auto fixed top-6 left-1/2 w-[min(440px,calc(100vw-24px))] -translate-x-1/2 rounded-xl border border-amber-300 bg-background p-5 shadow-xl"
      style={{ zIndex: 2147482000 }}
    >
      <p className="mb-1 flex items-center gap-1 text-xs font-medium text-amber-700 dark:text-amber-300">
        <Sparkles className="size-3.5" aria-hidden />
        TK-ADA 体验版
      </p>
      <h1 className="mb-1 text-lg font-semibold">欢迎体验广告投放工具</h1>
      <p className="mb-4 text-sm leading-relaxed text-muted-foreground">
        这里的租户、账户、剧和报表都是演示数据，不连接任何真实账户，可以放心点击任何按钮。刷新或点“重置体验”即可回到初始状态。
      </p>
      <div className="flex flex-wrap gap-2">
        <Button disabled={entering} onClick={() => onEnter(true)}>
          一键进入并开始引导
        </Button>
        <Button
          variant="outline"
          disabled={entering}
          onClick={() => onEnter(false)}
        >
          自由体验
        </Button>
      </div>
    </div>,
    document.body,
  )
}
