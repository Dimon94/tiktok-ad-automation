import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useBlocker, useNavigate, useRouterState } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { useCallback, useEffect, useRef, useState } from "react"
import { MaterialIngestService, MaterialsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { useWorkspacePageGuard } from "@/features/workspace/WorkspaceTabs"
import { AssetDetails } from "./AssetDetails"
import { BatchHistory } from "./BatchHistory"
import { BatchUploadSheet } from "./BatchUploadSheet"
import { CarouselPresetList } from "./CarouselPresetList"
import { MaterialTable } from "./MaterialTable"
import { materialKey, namingHint, uploadKey } from "./presentation"
import { UploadQueue } from "./UploadQueue"
import { useUploadManager } from "./useUploadManager"

export function MaterialsPage() {
  const { tenantId, scope, bc } = useTenantScope()
  if (!tenantId || !scope) return null
  return (
    <MaterialWorkspace
      key={tenantId}
      tenantId={tenantId}
      targetBcId={bc?.bc_id ?? null}
      allowed={scope.role !== "viewer"}
    />
  )
}

function MaterialWorkspace({
  tenantId,
  targetBcId,
  allowed,
}: {
  tenantId: string
  targetBcId: string | null
  allowed: boolean
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const search = useRouterState({
    select: (state) =>
      state.location.search as {
        bc_id?: string
        tab?: string
        batch_id?: string
      },
  })
  const tab =
    search.tab === "uploads"
      ? "uploads"
      : search.tab === "carousels"
        ? "carousels"
        : "library"
  const [details, setDetails] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [sheet, setSheet] = useState(false)
  const [pinnedBc, setPinnedBc] = useState<string | null>(null)
  const [uploadBusy, setUploadBusy] = useState(false)
  const [started, setStarted] = useState<string | null>(null)
  const batchId = search.batch_id
  // 队列使用服务端保存的 BC，与顶栏为下一次上传选定的目标分开。
  const batch = useQuery({
    queryKey: [...materialKey(tenantId), "viewed-session", batchId],
    enabled: !!batchId,
    queryFn: async ({ signal }) => {
      try {
        return (
          await MaterialIngestService.readIngestSummary({
            path: { tenant_id: tenantId, session_id: batchId! },
            signal,
          })
        ).data
      } catch (error) {
        // 旧批次只在明确 404 后读取历史归属，不能用顶栏 BC 猜测。
        if (!(error instanceof AxiosError) || error.response?.status !== 404)
          throw error
        const history = (
          await MaterialsService.readUploadBatch({
            path: { tenant_id: tenantId, batch_id: batchId! },
            signal,
          })
        ).data
        queryClient.setQueryData(
          [...uploadKey(tenantId, history.bc_id), "legacy-batch", batchId],
          history,
        )
        return history
      }
    },
  })
  const managerBc =
    (sheet || started ? pinnedBc : null) ??
    (batchId ? batch.data?.bc_id : null) ??
    pinnedBc ??
    targetBcId
  const liveManagerBc = useRef(managerBc)
  liveManagerBc.current = managerBc
  const reportUploadBusy = useCallback(
    (busy: boolean) => {
      // 恢复中的原请求同样固定 BC；旧管理器卸载后的迟到通知不能覆盖新管理器。
      if (liveManagerBc.current !== managerBc) return
      setUploadBusy(busy)
      if (busy && managerBc) setPinnedBc(managerBc)
    },
    [managerBc],
  )
  const write = allowed && !forbidden
  const revoke = () => setForbidden(true)
  const go = (
    next: "library" | "uploads" | "carousels",
    id: string | null | undefined = batchId,
  ) =>
    void navigate({
      to: "/tenants/$tenantId/materials",
      params: { tenantId },
      search: { bc_id: search.bc_id, tab: next, batch_id: id ?? undefined },
    })
  useEffect(() => {
    // 先卸载文件选择侧栏，避免它的未保存保护拦截已成功批次的导航。
    if (started && !sheet) {
      void navigate({
        to: "/tenants/$tenantId/materials",
        params: { tenantId },
        search: { bc_id: search.bc_id, tab: "uploads", batch_id: started },
      }).then(() =>
        setStarted((current) => (current === started ? null : current)),
      )
    }
  }, [started, sheet, navigate, tenantId, search.bc_id])
  return (
    <>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>素材库</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            当前租户全部素材。视频临时中转，保留实际上传来源与账户素材记录。
          </p>
        </div>
        {write && (
          <Button
            disabled={!targetBcId || uploadBusy}
            onClick={() => {
              setPinnedBc(targetBcId)
              setSheet(true)
            }}
          >
            批量上传
          </Button>
        )}
      </div>
      {write && !targetBcId && (
        <p className="text-sm text-muted-foreground">
          请选择有效的目标 BC 后上传素材。
        </p>
      )}
      <Alert>
        <AlertDescription>{namingHint}</AlertDescription>
      </Alert>
      <Tabs
        value={tab}
        onValueChange={(value) =>
          go(value as "library" | "uploads" | "carousels")
        }
      >
        <TabsList>
          <TabsTrigger value="carousels">轮播库</TabsTrigger>
          <TabsTrigger value="library">素材库</TabsTrigger>
          <TabsTrigger value="uploads">上传队列</TabsTrigger>
        </TabsList>
      </Tabs>
      {tab === "carousels" && (
        <CarouselPresetList tenantId={tenantId} write={write} />
      )}
      <section
        aria-label="素材目录"
        className="min-w-0"
        hidden={tab !== "library"}
      >
        <MaterialTable
          tenantId={tenantId}
          onDetails={setDetails}
          onForbidden={revoke}
          enabled={tab === "library"}
        />
      </section>
      <section
        aria-label="上传批次与队列"
        className="min-w-0"
        hidden={tab !== "uploads"}
      >
        {!batchId && (
          <BatchHistory
            tenantId={tenantId}
            enabled={tab === "uploads"}
            onForbidden={revoke}
            onOpen={(id) => go("uploads", id)}
          />
        )}
        {batchId && batch.error && (
          <RequestError
            error={batch.error}
            retry={() => void batch.refetch()}
          />
        )}
        {batchId && batch.isPending && <p role="status">正在读取上传批次…</p>}
      </section>
      {managerBc && (
        <UploadWorkspace
          key={`${tenantId}:${managerBc}`}
          tenantId={tenantId}
          bcId={managerBc}
          batchId={
            batchId && batch.data?.bc_id === managerBc ? batchId : undefined
          }
          visible={tab === "uploads"}
          allowed={write}
          sheet={sheet}
          onSheet={setSheet}
          onBusy={reportUploadBusy}
          onForbidden={revoke}
          onDetails={setDetails}
          onStarted={(id) => {
            setPinnedBc(managerBc)
            setStarted(id)
          }}
          onHistory={() => go("uploads", null)}
        />
      )}
      {details && (
        <AssetDetails
          key={details}
          tenantId={tenantId}
          materialId={details}
          onClose={() => setDetails(null)}
          onForbidden={revoke}
        />
      )}
    </>
  )
}

function UploadWorkspace({
  tenantId,
  bcId,
  batchId,
  visible,
  allowed,
  sheet,
  onSheet,
  onBusy,
  onForbidden,
  onDetails,
  onStarted,
  onHistory,
}: {
  tenantId: string
  bcId: string
  batchId?: string
  visible: boolean
  allowed: boolean
  sheet: boolean
  onSheet: (value: boolean) => void
  onBusy: (value: boolean) => void
  onForbidden: () => void
  onDetails: (id: string) => void
  onStarted: (id: string) => void
  onHistory: () => void
}) {
  // 管理器仅在冻结 BC 改变时重新创建，顶栏切换不重定向已提交批次。
  const manager = useUploadManager(tenantId, bcId)
  const pendingFiles = useRef<HTMLInputElement>(null)
  const recoveryAttempted = useRef<string | null>(null)
  const active = manager.transferring || manager.unfinished || !!manager.pending
  useEffect(() => {
    onBusy(active || manager.creating)
    return () => onBusy(false)
  }, [active, manager.creating, onBusy])
  useEffect(() => {
    if (!allowed) manager.revokePermission()
  }, [allowed, manager.revokePermission])
  useEffect(() => {
    if (
      manager.pending &&
      !manager.creating &&
      recoveryAttempted.current !== manager.pending.requestId
    ) {
      recoveryAttempted.current = manager.pending.requestId
      void manager.recover().then((session) => {
        if (session) {
          onSheet(false)
          onStarted(session.session_id)
        }
      })
    }
  }, [manager, onSheet, onStarted])
  // 本地文件传输需保持上传控制器挂载；离开前继续使用原有传输守卫。
  const retainsPage = useWorkspacePageGuard(active, false)
  const blocker = useBlocker({
    shouldBlockFn: ({ next }) =>
      active &&
      !retainsPage(next) &&
      (next.pathname !== `/tenants/${tenantId}/materials` ||
        (!!(next.search as { batch_id?: string }).batch_id &&
          (next.search as { batch_id?: string }).batch_id !== batchId &&
          (next.search as { batch_id?: string }).batch_id !==
            manager.sessionId)),
    enableBeforeUnload: active,
    withResolver: true,
  })
  const write = allowed && !manager.forbidden
  return (
    <>
      {manager.pending && !manager.creating && (
        <Alert>
          <AlertDescription>
            <p>导入创建结果尚未确认，按原请求核实。未查到结果不代表未创建。</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                void manager.recover().then((batch) => {
                  if (batch) {
                    onSheet(false)
                    onStarted(batch.session_id)
                  }
                })
              }
            >
              核实导入创建结果
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={manager.creating || !manager.pending.metadataReady}
              onClick={() =>
                void manager.retryCreation().then((session) => {
                  if (session) onStarted(session.session_id)
                })
              }
            >
              按原请求继续创建
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {manager.pending && !manager.pending.metadataReady && (
        <Alert>
          <AlertDescription>
            <p>
              本地文件登记曾中断。请重新选择此次导入的全部文件，按原请求继续。
            </p>
            <Button
              variant="outline"
              disabled={manager.creating}
              onClick={() => pendingFiles.current?.click()}
            >
              重选此导入全部文件
            </Button>
            <Input
              ref={pendingFiles}
              aria-label="重选此导入全部文件"
              className="hidden"
              type="file"
              multiple
              accept="video/*"
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                event.target.value = ""
                if (files.length)
                  void manager.restorePendingFiles(files).then((session) => {
                    if (session) onStarted(session.session_id)
                  })
              }}
            />
          </AlertDescription>
        </Alert>
      )}

      {batchId && (
        <div hidden={!visible}>
          <UploadQueue
            key={batchId}
            tenantId={tenantId}
            bcId={bcId}
            batchId={batchId}
            manager={manager}
            write={write}
            onDetails={onDetails}
            onForbidden={onForbidden}
            onHistory={onHistory}
          />
        </div>
      )}
      {sheet && (
        <BatchUploadSheet
          manager={manager}
          onClose={() => onSheet(false)}
          onStarted={(batch) => {
            onSheet(false)
            onStarted(batch.session_id)
          }}
        />
      )}
      <Dialog
        open={blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>暂停本地传输并离开？</DialogTitle>
            <DialogDescription>
              未完整接收的文件将暂停，需要返回原租户与 BC
              继续传输。已接收文件的后台处理继续，已创建批次不会删除或改绑。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => blocker.reset?.()}>
              留在当前页
            </Button>
            <Button
              disabled={manager.creating}
              onClick={() => blocker.proceed?.()}
            >
              暂停并离开
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
