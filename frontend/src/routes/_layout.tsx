import { createFileRoute, redirect } from "@tanstack/react-router"
import { workspaceNavigation } from "@/components/Sidebar/navigation"
import { BCSelector } from "@/features/accounts/BCSelector"
import { useMaterialProduction } from "@/features/material-production/useMaterialProduction"
import { canManage, roleLabels } from "@/features/tenants/shared"
import {
  TenantAccessGate,
  TenantScopeProvider,
  TenantSelector,
  useTenantScope,
} from "@/features/tenants/TenantScope"
import { ApiFeedback } from "@/features/workspace/ApiFeedback"
import { WorkspaceShell } from "@/features/workspace/WorkspaceShell"
import {
  WorkspaceTabPages,
  WorkspaceTabsProvider,
} from "@/features/workspace/WorkspaceTabs"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { rememberLoginReturn } from "@/lib/login-return"

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: ({ location }) => {
    if (!isLoggedIn()) {
      rememberLoginReturn(location.pathname + location.searchStr)
      throw redirect({ to: "/login" })
    }
  },
})
function Layout() {
  const { user, error, isPending } = useAuth()
  if (isPending || error || !user)
    return (
      <WorkspaceShell user={user}>
        <ApiFeedback />
        <p role="status">
          {error
            ? "暂时无法读取用户信息，请刷新页面重试。"
            : "正在载入 TK-ADA…"}
        </p>
      </WorkspaceShell>
    )
  return (
    <TenantScopeProvider user={user}>
      <ScopedLayout />
    </TenantScopeProvider>
  )
}
function ScopedLayout() {
  const { scope, tenant, tenantId, user, platform, bc } = useTenantScope()
  const materialProduction = useMaterialProduction(tenantId ?? undefined)
  const prefix = `/tenants/${tenantId}`
  // 平台视图不展示租户菜单；未接入租户时交给侧栏展示全局入口。
  const groups = tenantId
    ? workspaceNavigation({
        prefix,
        manage: canManage(scope?.role),
        platformAdmin: !!user.is_superuser,
        materialProduction:
          scope && scope.role !== "viewer" ? materialProduction : undefined,
      })
    : platform
      ? []
      : undefined
  return (
    <WorkspaceTabsProvider
      key={`${user.id}:${tenantId ?? (platform ? "platform" : "global")}:${scope?.role ?? ""}`}
      groups={groups ?? workspaceNavigation({ manage: !!user.is_superuser })}
    >
      <WorkspaceShell
        user={user}
        platform={platform}
        context={
          tenant && scope
            ? {
                tenant: { id: tenant.id, name: tenant.name },
                bc: bc ? { id: bc.bc_id, name: bc.name } : null,
                roleLabel: roleLabels[scope.role],
                managedByPlatform: scope.role === "platform_admin",
              }
            : null
        }
        contextSelector={tenantId ? <TenantSelector /> : undefined}
        bcSelector={tenant ? <BCSelector key={tenantId} /> : undefined}
        groups={groups}
      >
        <ApiFeedback />
        <TenantAccessGate>
          <WorkspaceTabPages />
        </TenantAccessGate>
      </WorkspaceShell>
    </WorkspaceTabsProvider>
  )
}
