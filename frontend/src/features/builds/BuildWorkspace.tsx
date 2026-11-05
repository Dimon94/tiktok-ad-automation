import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import { canManage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { BuildInputPage } from "./BuildInputPage"
import { DraftList } from "./DraftList"

export function BuildWorkspace() {
  const { tenantId, scope, bc, bcPending } = useTenantScope()
  if (!tenantId || !scope?.bcId || !bc)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title={scope?.bcId ? "请先选择有效的 BC" : "尚未连接 TikTok BC"}
        description={
          bcPending
            ? "正在读取 BC 上下文。"
            : "请通过顶栏选择本次搭建使用的 BC。"
        }
      />
    )
  return (
    <BuildForm
      key={`${tenantId}:${scope.bcId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      write={scope.role !== "viewer"}
    />
  )
}

function BuildForm({
  tenantId,
  bcId,
  write,
}: {
  tenantId: string
  bcId: string
  write: boolean
}) {
  const [draftBoxOpen, setDraftBoxOpen] = useState(false)
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>广告搭建</WorkspacePageTitle>
          <p className="text-xs text-muted-foreground">
            填写剧目与账户，准备本次投放。
          </p>
        </div>
        <Sheet open={draftBoxOpen} onOpenChange={setDraftBoxOpen}>
          <SheetTrigger asChild>
            <Button variant="outline">草稿箱</Button>
          </SheetTrigger>
          <SheetContent className="w-full sm:max-w-6xl">
            <SheetHeader className="border-b pr-12">
              <SheetTitle>草稿箱</SheetTitle>
              <SheetDescription>
                当前 BC
                已保存、准备中和待提交的批次，按最近修改排序。已提交批次请到“搭建任务”查看。
              </SheetDescription>
            </SheetHeader>
            <div className="min-h-0 flex-1 overflow-y-auto px-4">
              {draftBoxOpen && (
                <DraftList
                  tenantId={tenantId}
                  bcId={bcId}
                  write={write}
                  onResume={() => setDraftBoxOpen(false)}
                />
              )}
            </div>
            <SheetFooter className="border-t sm:flex-row sm:justify-end">
              <SheetClose asChild>
                <Button variant="outline">关闭草稿箱</Button>
              </SheetClose>
            </SheetFooter>
          </SheetContent>
        </Sheet>
      </div>
      {/* 查看草稿不卸载输入表单；选定原草稿后才导航，由原离页守卫检查未保存修改。 */}
      <BuildInputPage tenantId={tenantId} bcId={bcId} write={write} />
    </div>
  )
}
