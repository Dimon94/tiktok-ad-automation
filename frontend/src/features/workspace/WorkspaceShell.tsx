import { Link } from "@tanstack/react-router"
import { Building2, ShieldCheck } from "lucide-react"
import type { CSSProperties, ReactNode } from "react"
import type { UserPublic } from "@/client"
import AppSidebar from "@/components/Sidebar/AppSidebar"
import type { NavGroup } from "@/components/Sidebar/Main"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"

import { WorkspaceTabBar } from "./WorkspaceTabs"

// P02 supplies the resolved tenant/BC and scoped routes. No example context is used.
export interface WorkspaceContext {
  tenant: { id: string; name: string }
  bc: { id: string; name: string } | null
  roleLabel: string
  managedByPlatform?: boolean
}

export interface WorkspaceShellProps {
  children: ReactNode
  user?: UserPublic | null
  context?: WorkspaceContext | null
  groups?: NavGroup[]
  contextSelector?: ReactNode
  bcSelector?: ReactNode
  platform?: boolean
}

export function WorkspaceShell({
  children,
  user,
  context,
  groups,
  contextSelector,
  bcSelector,
  platform = false,
}: WorkspaceShellProps) {
  // 按确认原型保留紧凑的 194px 桌面导航；移动抽屉和图标折叠沿用原交互。
  return (
    <SidebarProvider style={{ "--sidebar-width": "194px" } as CSSProperties}>
      <a href="#workspace-main" className="skip-link">
        跳转到主要内容
      </a>
      <AppSidebar user={user} groups={groups} />
      <SidebarInset>
        <header className="sticky top-0 z-20 grid shrink-0 grid-cols-1 items-center gap-x-4 border-b bg-background px-4 text-sm lg:px-5 xl:grid-cols-[minmax(10rem,1fr)_auto]">
          <div className="flex h-11 min-w-0 items-center gap-2">
            <SidebarTrigger aria-label="切换导航" className="-ml-1" />
            <Separator
              orientation="vertical"
              className="mx-2 data-[orientation=vertical]:h-4"
            />
            <div
              data-slot="workspace-page-title"
              className="min-w-0 flex-1 text-muted-foreground"
            >
              {platform ? "平台管理" : "投放工作"}
            </div>
          </div>
          <div className="flex min-w-0 max-w-full flex-wrap items-center gap-x-3 gap-y-2 pb-3 xl:py-1.5">
            {contextSelector ?? (
              <span className="flex min-w-0 items-center gap-2 text-muted-foreground">
                <Building2 className="size-4 shrink-0" />
                <span className="truncate">
                  {platform
                    ? "平台管理"
                    : (context?.tenant.name ?? "未接入租户")}
                </span>
              </span>
            )}
            {!platform && (
              <>
                <Separator
                  orientation="vertical"
                  className="data-[orientation=vertical]:h-4"
                />
                {bcSelector ?? (
                  <span className="min-w-0 truncate text-muted-foreground">
                    {context?.bc?.name ?? "BC 未连接"}
                  </span>
                )}
              </>
            )}
            <Badge variant="secondary">
              <ShieldCheck />
              {context?.roleLabel ??
                (user?.is_superuser ? "平台管理员" : "已登录")}
            </Badge>
            {context?.managedByPlatform && (
              <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                <span
                  className="max-w-40 truncate"
                  title={`当前代管：${context.tenant.name}`}
                >
                  当前代管：{context.tenant.name}
                </span>
                <Button asChild size="sm" variant="ghost">
                  <Link to="/platform/tenants">返回平台</Link>
                </Button>
              </span>
            )}
          </div>
        </header>
        <WorkspaceTabBar />
        <div
          id="workspace-main"
          tabIndex={-1}
          className="flex min-w-0 flex-1 flex-col gap-4 bg-muted/40 p-4 outline-none lg:p-5"
        >
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
