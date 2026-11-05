import { LayoutGrid } from "lucide-react"
import type { UserPublic } from "@/client"
import { WorkspaceBrand } from "@/components/Common/WorkspaceBrand"
import { Separator } from "@/components/ui/separator"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
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
        {user?.is_superuser && (
          <Main
            items={[
              {
                icon: LayoutGrid,
                title: "平台管理",
                path: "/platform/tenants",
              },
            ]}
          />
        )}
        <Separator />
        <User user={user} />
      </SidebarFooter>
    </Sidebar>
  )
}
export default AppSidebar
