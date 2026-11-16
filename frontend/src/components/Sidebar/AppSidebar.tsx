import { useRouterState } from "@tanstack/react-router"
import { BrainCircuit, FlaskConical } from "lucide-react"
import type { UserPublic } from "@/client"
import { WorkspaceBrand } from "@/components/Common/WorkspaceBrand"
import { Separator } from "@/components/ui/separator"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { Main, type NavGroup } from "./Main"
import { workspaceNavigation } from "./navigation"
import { User } from "./User"

export function AppSidebar({
  user,
  groups,
}: {
  user?: UserPublic | null
  groups?: NavGroup[]
}) {
  const isTenantManagement = useRouterState({
    select: (state) => state.location.pathname === "/platform/tenants",
  })
  // 未传入分组时（未接入租户或用户信息加载中）展示全局入口。
  const navigation =
    groups ?? workspaceNavigation({ manage: !!user?.is_superuser })
  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader>
        <WorkspaceBrand />
      </SidebarHeader>
      <SidebarContent>
        {navigation
          .filter((group) => group.items.length > 0)
          .map((group) => (
            <Main key={group.label} label={group.label} items={group.items} />
          ))}
      </SidebarContent>
      <SidebarFooter>
        {/* workspaceNavigation 将 AI 独立分组放到这里之前，保证它紧邻体验版入口。 */}
        <SidebarGroup className="py-0" data-testid="experience-entry-group">
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  tooltip="打开体验版"
                  data-testid="experience-entry"
                  asChild
                >
                  <a href="/demo/" target="_blank" rel="noopener noreferrer">
                    <FlaskConical />
                    <span>打开体验版</span>
                  </a>
                </SidebarMenuButton>
              </SidebarMenuItem>
              {/* DSH 仅供平台租户管理使用；与体验版共用菜单，避免分组留白叠加。 */}
              {user?.is_superuser && isTenantManagement && (
                <SidebarMenuItem>
                  <SidebarMenuButton tooltip="DSH 管理台" asChild>
                    <a href="/api/admin/dsh/entry">
                      <BrainCircuit />
                      <span>DSH 管理台</span>
                    </a>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <Separator />
        <User user={user} />
      </SidebarFooter>
    </Sidebar>
  )
}
export default AppSidebar
