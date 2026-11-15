import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useRouterState } from "@tanstack/react-router"
import { useCallback, useEffect, useRef, useState } from "react"
import {
  BuildsService,
  type DraftDramaPublic,
  type DraftInputPublic,
  type DraftSummary,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { FilterSelect } from "@/features/accounts/presentation"
import { useTargetingDirectory } from "@/features/targeting/useTargetingDirectory"
import {
  canManage,
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import {
  buildKey,
  loadDraftInputs,
  mutationKey,
  readPendingMutation,
} from "./api"
import { BuildDramaTable } from "./BuildDramaTable"
import { BuildInputPage } from "./BuildInputPage"
import { BuildLinkSheet } from "./BuildLinkSheet"
import {
  DraftSchedule,
  deliveryConfig,
  type ScheduleValue,
  scheduleError,
  scheduleFromSummary,
} from "./DraftSchedule"
import { DraftTargeting } from "./DraftTargeting"
import { DramaMaterialSheet } from "./DramaMaterialSheet"
import { IdentityTargetPicker } from "./IdentityTargetPicker"
import { ImageBatchConfig } from "./ImageBatchConfig"
import { imageApi, imageKey, imagePath } from "./imageCompositionApi"
import { MiniTargetPicker } from "./MiniTargetPicker"
import { NativeCheckpointCard } from "./NativeCheckpointCard"
import { preparationLabel } from "./preparationProgress"
import {
  BuildError,
  BuildReason,
  BuildStatus,
  BuildSteps,
  reportError,
  unknownOutcome,
} from "./presentation"
import { useDraftIdentities } from "./useDraftIdentities"
import { useDraftMinis } from "./useDraftMinis"
export function BuildPreparationPage() {
  const { tenantId, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname })
  const draftId = /\/build-drafts\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !draftId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description="草稿属于创建时的租户与 BC。请通过顶栏选择。"
      />
    )
  return (
    <Preparation
      key={`${tenantId}:${scope.bcId}:${draftId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      draftId={draftId}
      write={scope.role !== "viewer"}
    />
  )
}
function Preparation({
  tenantId,
  bcId,
  draftId,
  write,
}: {
  tenantId: string
  bcId: string
  draftId: string
  write: boolean
}) {
  const { scope } = useTenantScope()
  const queryClient = useQueryClient(),
    navigate = useNavigate(),
    search = useRouterState({
      select: (s) => s.location.search as { edit?: boolean; prepare?: boolean },
    }),
    [tab, setTab] = useState("dramas"),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [imageRulesPending, setImageRulesPending] = useState(false),
    [forbidden, setForbidden] = useState(false),
    [material, setMaterial] = useState<DraftDramaPublic | null>(null),
    [selectedImageDramas, setSelectedImageDramas] = useState<
      Record<string, string>
    >({}),
    [linkId, setLinkId] = useState<string | null>(null),
    [restored, setRestored] = useState<{
      drama: string[]
      account: string[]
      manualLinks?: import("./manualLinks").NamedManualLink[]
    } | null>(null),
    [restoring, setRestoring] = useState(false),
    [progress, setProgress] = useState(0),
    [restoreError, setRestoreError] = useState<unknown>()
  const controller = useRef(new AbortController()),
    restoreController = useRef<AbortController | null>(null),
    restoreStarted = useRef(false)
  const operationKey = `build-prepare:${tenantId}:${bcId}:${draftId}`
  const [pending, setPending] = useState<string | null>(() =>
      sessionStorage.getItem(operationKey),
    ),
    [previewRevision, setPreviewRevision] = useState<number | null>(() => {
      const v = sessionStorage.getItem(`build-preview:${tenantId}:${draftId}`)
      return v ? Number(v) : null
    })
  const summary = useQuery({
    queryKey: [...buildKey(tenantId, bcId), draftId, "summary"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.summary({
          path: { tenant_id: tenantId, draft_id: draftId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data?.status === "PREPARING" ? 2000 : false,
  })
  const [, refreshMutationState] = useState(0)
  // null 表示沿用草稿已保存的排期；用户修改后暂存本地，生成预览时一并保存。
  const [scheduleEdit, setScheduleEdit] = useState<ScheduleValue | null>(null),
    [scheduleProblem, setScheduleProblem] = useState("")
  const pendingMutation = readPendingMutation(
    mutationKey(tenantId, bcId, draftId),
  )
  const current = summary.data,
    scoped = current?.bc_id === bcId,
    allowed = write && !forbidden && !isForbidden(summary.error)
  const minis = useDraftMinis(tenantId, bcId, current)
  const identities = useDraftIdentities(tenantId, bcId, current)
  const targeting = useTargetingDirectory(tenantId, bcId, current)
  const imageState = useQuery({
    queryKey: imageKey(tenantId, draftId),
    queryFn: async ({ signal }) =>
      (
        await imageApi.compositionState({
          path: imagePath(tenantId, draftId),
          signal,
        })
      ).data,
    enabled: current?.media_kind === "CAROUSEL",
    refetchInterval: 2000,
  })
  const imageReady =
    current?.media_kind !== "CAROUSEL" ||
    (!imageRulesPending &&
      imageState.data?.preview_ready === true &&
      !imageState.isError &&
      imageState.data.revision === current.revision)
  const isNative =
    current?.scene_selection?.kind === "NATIVE_SERIES_DIRECT_VIDEO"
  const previewReady =
    imageReady &&
    current?.status === "READY" &&
    !summary.isFetching &&
    !summary.isError &&
    (isNative ||
      (minis.isSuccess &&
        !minis.isFetching &&
        minis.data.state === "selected" &&
        identities.isSuccess &&
        !identities.isFetching &&
        identities.data.state === "selected" &&
        targeting.data?.state === "READY" &&
        !targeting.isFetching &&
        !targeting.isError &&
        !targeting.data.unavailable_region_codes?.length))
  const previewHint = !imageReady
    ? "请完成图片组合、配乐及当前规则应用。预览会逐账户核验投放资格并列出排除广告。"
    : summary.isFetching
      ? "正在核对准备状态…"
      : current?.status !== "READY"
        ? "请先完成剧目和账户准备，再生成预览。"
        : isNative
          ? "按当前版本核验目的地、素材、CTA及占用后冻结完整投放计划。"
          : !current.drama_count
            ? "推广链接尚未就绪，请点击“解析并准备”重新获取，或补充已有链接；完成后会自动匹配推广小程序。"
            : !current.account_count
              ? "请先修正广告账户输入并重新准备。"
              : minis.isError
                ? "小程序信息读取失败，请刷新重试。"
                : minis.isPending || minis.isFetching
                  ? "正在核对推广小程序…"
                  : minis.data?.state === "pending"
                    ? "请先更新可用小程序并完成选择。"
                    : minis.data?.state === "unavailable"
                      ? "当前没有可用小程序，请检查账户授权。"
                      : minis.data?.state === "conflict"
                        ? "剧目指向不同小程序，请先核对推广目标。"
                        : minis.data?.state !== "selected"
                          ? "请先选择本批次推广小程序，再生成预览。"
                          : identities.isError
                            ? "投放身份读取失败，请刷新重试。"
                            : identities.isPending || identities.isFetching
                              ? "正在核对投放身份…"
                              : identities.data?.state === "pending"
                                ? "请先更新可用投放身份。"
                                : identities.data?.state === "unavailable"
                                  ? "当前没有可用投放身份，请检查账户授权。"
                                  : identities.data?.state === "stale"
                                    ? "原投放身份已不可用，请重新选择。"
                                    : identities.data?.state !== "selected"
                                      ? "请先选择本批次投放身份，再生成预览。"
                                      : !previewReady
                                        ? "请核实受众定向与共同可投国家，再生成预览。"
                                        : "预览将明确列出可搭建范围与排除原因。"
  useEffect(() => {
    const ctrl = new AbortController()
    controller.current = ctrl
    return () => {
      ctrl.abort()
      restoreController.current?.abort()
    }
  }, [])
  const restore = useCallback(async () => {
    if (!current || !scoped || restoring) return
    restoreController.current?.abort()
    const ctrl = new AbortController()
    restoreController.current = ctrl
    setRestoring(true)
    setRestoreError(undefined)
    setProgress(0)
    try {
      const data = await loadDraftInputs(
        tenantId,
        draftId,
        ctrl.signal,
        setProgress,
      )
      const { data: latest } = await BuildsService.summary({
        path: { tenant_id: tenantId, draft_id: draftId },
        signal: ctrl.signal,
      })
      if (latest.revision !== current.revision)
        throw new Error("草稿恢复期间已改变")
      if (!ctrl.signal.aborted) setRestored(data)
    } catch (e) {
      if (!ctrl.signal.aborted) setRestoreError(e)
    } finally {
      if (restoreController.current === ctrl) setRestoring(false)
    }
  }, [current, scoped, restoring, tenantId, draftId])
  useEffect(() => {
    if (search.edit && scoped && !restoreStarted.current) {
      const timer = setTimeout(() => {
        restoreStarted.current = true
        void restore()
      }, 0)
      return () => clearTimeout(timer)
    }
  }, [search.edit, scoped, restore])
  const resumePreparation = useRef(false)
  const refetchSummary = summary.refetch
  const prepare = useCallback(
    async (recover = false) => {
      if (
        busy ||
        !allowed ||
        !scoped ||
        readPendingMutation(mutationKey(tenantId, bcId, draftId))
      )
        return
      setBusy(true)
      setError(undefined)
      const requestId = pending || crypto.randomUUID()
      try {
        if (recover || pending) {
          await BuildsService.savedPrepareRequest({
            path: { tenant_id: tenantId, request_id: requestId },
            signal: controller.current.signal,
          })
        } else {
          sessionStorage.setItem(operationKey, requestId)
          setPending(requestId)
          await BuildsService.prepare({
            path: { tenant_id: tenantId, draft_id: draftId },
            body: { request_id: requestId },
            signal: controller.current.signal,
          })
        }
        sessionStorage.removeItem(operationKey)
        setPending(null)
        await refetchSummary()
        await navigate({
          to: "/tenants/$tenantId/build-drafts/$draftId",
          params: { tenantId, draftId },
          search: { bc_id: bcId },
        })
      } catch (e) {
        if (!controller.current.signal.aborted) {
          reportError(e)
          setError(e)
          if (!unknownOutcome(e) && !recover && !pending) {
            sessionStorage.removeItem(operationKey)
            setPending(null)
          }
          if ((e as { response?: { status: number } }).response?.status === 403)
            setForbidden(true)
        }
      } finally {
        setBusy(false)
      }
    },
    [
      busy,
      allowed,
      scoped,
      pending,
      tenantId,
      operationKey,
      draftId,
      bcId,
      navigate,
      refetchSummary,
    ],
  )
  const automatic = useRef(false)
  useEffect(() => {
    if (
      search.prepare &&
      scoped &&
      allowed &&
      !automatic.current &&
      (pending ||
        sessionStorage.getItem(`build-start:${tenantId}:${bcId}:${draftId}`))
    ) {
      const timer = setTimeout(() => {
        automatic.current = true
        sessionStorage.removeItem(`build-start:${tenantId}:${bcId}:${draftId}`)
        void prepare(!!pending)
      }, 0)
      return () => clearTimeout(timer)
    }
  }, [
    search.prepare,
    scoped,
    allowed,
    prepare,
    pending,
    tenantId,
    bcId,
    draftId,
  ])
  async function preview(recover = false) {
    if (!current || busy || !allowed || !scoped || pendingMutation) return
    // 原请求结果查询仍可恢复；只有新建预览必须满足当前必填条件。
    if (!recover && previewRevision === null && !previewReady) return
    const creating = !recover && previewRevision === null
    const savedSchedule = scheduleFromSummary(current)
    const schedule = scheduleEdit ?? savedSchedule
    const problem = creating ? scheduleError(schedule) : ""
    setScheduleProblem(problem)
    if (problem) return
    setBusy(true)
    setError(undefined)
    let revision = previewRevision ?? current.revision
    try {
      let previewId: string
      if (
        creating &&
        JSON.stringify(schedule) !== JSON.stringify(savedSchedule)
      ) {
        // 排期变化先保存（沿用已准备结果、换新 revision），再按新 revision 生成预览；
        // 保存结果未知时沿用通用修改回查，避免重复提交。
        const key = mutationKey(tenantId, bcId, draftId)
        const requestId = crypto.randomUUID()
        sessionStorage.setItem(
          key,
          JSON.stringify({ requestId, kind: "delivery", prepare: false }),
        )
        try {
          const { data } = await BuildsService.updateDelivery({
            path: { tenant_id: tenantId, draft_id: draftId },
            body: {
              request_id: requestId,
              expected_revision: revision,
              delivery_config: deliveryConfig(schedule),
            },
            signal: controller.current.signal,
          })
          revision = data.revision
        } catch (e) {
          if (!unknownOutcome(e)) sessionStorage.removeItem(key)
          refreshMutationState((value) => value + 1)
          void summary.refetch()
          throw e
        }
        sessionStorage.removeItem(key)
        setScheduleEdit(null)
      }
      if (recover || previewRevision !== null) {
        const { data } = await BuildsService.previewRequest({
          path: { tenant_id: tenantId, draft_id: draftId, revision },
          signal: controller.current.signal,
        })
        previewId = data.preview_id
      } else {
        sessionStorage.setItem(
          `build-preview:${tenantId}:${draftId}`,
          String(revision),
        )
        setPreviewRevision(revision)
        const { data } = await BuildsService.generatePreview({
          path: { tenant_id: tenantId, draft_id: draftId },
          body: { expected_revision: revision },
          signal: controller.current.signal,
        })
        previewId = data.preview_id
      }
      sessionStorage.removeItem(`build-preview:${tenantId}:${draftId}`)
      setPreviewRevision(null)
      await navigate({
        to: "/tenants/$tenantId/build-previews/$previewId",
        params: { tenantId, previewId },
        search: { bc_id: bcId },
      })
    } catch (e) {
      if (!controller.current.signal.aborted) {
        reportError(e)
        setError(e)
        if (!unknownOutcome(e) && !recover && previewRevision === null) {
          sessionStorage.removeItem(`build-preview:${tenantId}:${draftId}`)
          setPreviewRevision(null)
        }
      }
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    if (!busy && resumePreparation.current) {
      resumePreparation.current = false
      void prepare()
    }
  }, [busy, prepare])
  async function recoverMutation() {
    if (!pendingMutation || busy) return
    setBusy(true)
    setError(undefined)
    try {
      const { data } = await BuildsService.savedMutation({
        path: { tenant_id: tenantId, request_id: pendingMutation.requestId },
        signal: controller.current.signal,
      })
      if (data.draft_id !== draftId) throw new Error("修改结果不属于当前草稿")
      if (!controller.current.signal.aborted) {
        sessionStorage.removeItem(mutationKey(tenantId, bcId, draftId))
        resumePreparation.current =
          ["minis", "manual_link"].includes(pendingMutation.kind) &&
          pendingMutation.prepare
        await summary.refetch()
      }
    } catch (e) {
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const edit = () => {
    restoreStarted.current = false
    setRestored(null)
    void navigate({
      to: "/tenants/$tenantId/build-drafts/$draftId",
      params: { tenantId, draftId },
      search: { bc_id: bcId, edit: true },
    })
  }
  if (summary.isPending) return <Skeleton className="h-64 w-full" />
  if (summary.error && !summary.data)
    return (
      <RequestError
        error={summary.error}
        retry={() => void summary.refetch()}
      />
    )
  if (!current || !scoped)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        reason="bc-mismatch"
        resourceBcId={current?.bc_id}
        title="当前 BC 与草稿不一致"
        description="请返回草稿所属 BC 查看，或在当前 BC 新建搭建。"
      />
    )
  if (search.edit) {
    return (
      <div className="flex min-w-0 flex-col gap-6">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <WorkspacePageTitle>编辑搭建输入</WorkspacePageTitle>
            <p className="text-sm text-muted-foreground">
              服务器草稿 v{current.revision} · 本地输入会保留，保存时检查版本。
            </p>
          </div>
          <Button variant="outline" onClick={() => void summary.refetch()}>
            检查最新草稿状态
          </Button>
        </div>
        {summary.error && (
          <RequestError
            error={summary.error}
            retry={() => void summary.refetch()}
          />
        )}
        {!restored ? (
          <Card>
            <CardContent className="flex min-w-0 flex-col gap-3">
              <p role="status">
                {restoring
                  ? `正在恢复完整原输入，已加载 ${progress} 行…`
                  : "原输入尚未完整恢复，编辑与保存尚未开放。"}
              </p>
              {!!restoreError && <RequestError error={restoreError} />}
              <Button
                variant="outline"
                onClick={() => {
                  if (restoring) {
                    restoreController.current?.abort()
                    setRestoring(false)
                  } else void restore()
                }}
              >
                {restoring ? "取消加载" : "重新加载完整输入"}
              </Button>
            </CardContent>
          </Card>
        ) : (
          <BuildInputPage
            tenantId={tenantId}
            bcId={bcId}
            write={allowed}
            summary={current}
            original={restored}
            onCancel={() =>
              void navigate({
                to: "/tenants/$tenantId/build-drafts/$draftId",
                params: { tenantId, draftId },
                search: { bc_id: bcId },
              })
            }
            onSaved={async (start) => {
              setRestored(null)
              await summary.refetch()
              await navigate({
                to: "/tenants/$tenantId/build-drafts/$draftId",
                params: { tenantId, draftId },
                search: { bc_id: bcId },
              })
              if (start) void prepare()
            }}
          />
        )}
      </div>
    )
  }
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>准备与调整</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            草稿 v{current.revision} · <BuildStatus value={current.status} />
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() =>
            void queryClient.invalidateQueries({
              queryKey: buildKey(tenantId, bcId),
            })
          }
        >
          刷新准备结果
        </Button>
      </div>
      <BuildSteps step={2} />
      {summary.error && (
        <RequestError
          error={summary.error}
          retry={() => void summary.refetch()}
        />
      )}
      {pendingMutation && (
        <Alert>
          <AlertDescription>
            <p>
              此草稿有尚待确认的修改。请先查询原修改结果，再准备或生成预览。
            </p>
            <Button disabled={busy} onClick={() => void recoverMutation()}>
              查询原修改结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!!error && <BuildError error={error} />}
      {pending && !busy && (
        <Alert>
          <AlertDescription>
            <p>准备请求结果待核实，请查询原请求；不会重复发起取链。</p>
            <Button disabled={busy} onClick={() => void prepare(true)}>
              查询原准备结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {previewRevision !== null && (
        <Alert>
          <AlertDescription>
            <p>正在确认 v{previewRevision} 的原预览结果。</p>
            <Button disabled={busy} onClick={() => void preview(true)}>
              查询原预览结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          ["已确认剧目", current.drama_count],
          ["有效账户", current.account_count],
          [
            "剧目输入",
            Object.values(current.input_counts.drama || {}).reduce(
              (a, b) => a + b,
              0,
            ),
          ],
          [
            "账户输入",
            Object.values(current.input_counts.account || {}).reduce(
              (a, b) => a + b,
              0,
            ),
          ],
        ].map(([label, value]) => (
          <Card key={label}>
            <CardContent>
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="mt-1 text-xl font-semibold">{value}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <p role="status" className="text-sm text-muted-foreground">
        已输入{" "}
        {Object.values(current.input_counts.drama || {}).reduce(
          (a, b) => a + b,
          0,
        )}{" "}
        行 · 链接已确认 {current.drama_count} 部 ·{" "}
        {current.status === "PREPARING"
          ? preparationLabel(current)
          : current.status === "DRAFT"
            ? "等待准备"
            : current.status === "BLOCKED"
              ? "准备需处理，请查看具体原因"
              : "准备完成，请核对各项结果"}
      </p>
      {current.error_code && (
        <p role="alert" className="break-all text-sm text-destructive">
          准备异常：{current.error_code}
        </p>
      )}
      {current.media_kind === "CAROUSEL" && (
        <ImageBatchConfig
          tenantId={tenantId}
          summary={current}
          write={allowed && !busy && current.status !== "PREPARING"}
          drama={material}
          selectedDramas={selectedImageDramas}
          onRulesPendingChange={setImageRulesPending}
          onCloseDrama={() => setMaterial(null)}
          onChanged={() => void summary.refetch()}
        />
      )}
      <Card className="min-w-0">
        <CardHeader className="min-w-0">
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList>
              <TabsTrigger value="dramas">剧目与素材</TabsTrigger>
              <TabsTrigger value="accounts">账户解析</TabsTrigger>
            </TabsList>
          </Tabs>
        </CardHeader>
        <CardContent className="min-w-0">
          {tab === "dramas" ? (
            <BuildDramaTable
              key={current.revision}
              tenantId={tenantId}
              bcId={bcId}
              summary={current}
              onMaterial={setMaterial}
              selectedDramas={selectedImageDramas}
              onSelectDramas={setSelectedImageDramas}
              onLink={setLinkId}
              write={allowed && !busy && !pending && !pendingMutation}
              onChanged={() => void prepare()}
              onEdit={edit}
            />
          ) : (
            <AccountInputTable
              key={`${tab}:${current.revision}`}
              tenantId={tenantId}
              bcId={bcId}
              summary={current}
              write={allowed}
              onEdit={edit}
            />
          )}
        </CardContent>
      </Card>
      {current.scene_selection?.kind === "NATIVE_SERIES_DIRECT_VIDEO" ? (
        <NativeCheckpointCard tenantId={tenantId} summary={current} />
      ) : (
        <>
          <MiniTargetPicker
            tenantId={tenantId}
            bcId={bcId}
            summary={current}
            write={allowed && !busy && !pending && !pendingMutation}
            onPrepare={() => prepare()}
            onRefresh={() => {
              refreshMutationState((value) => value + 1)
              void summary.refetch()
            }}
          />
          <DraftTargeting
            tenantId={tenantId}
            bcId={bcId}
            summary={current}
            write={allowed && !busy && !pending && !pendingMutation}
            onPrepare={() => prepare()}
            onRefresh={() => {
              refreshMutationState((value) => value + 1)
              void summary.refetch()
            }}
          />
          <IdentityTargetPicker
            tenantId={tenantId}
            bcId={bcId}
            summary={current}
            write={allowed && !busy && !pending && !pendingMutation}
            onPrepare={() => prepare()}
            onRefresh={() => {
              refreshMutationState((value) => value + 1)
              void summary.refetch()
            }}
          />
        </>
      )}
      <DraftSchedule
        value={scheduleEdit ?? scheduleFromSummary(current)}
        onChange={(value) => {
          setScheduleProblem("")
          setScheduleEdit(value)
        }}
        disabled={
          !allowed ||
          busy ||
          !!pending ||
          !!pendingMutation ||
          previewRevision !== null
        }
        error={scheduleProblem}
      />
      <Card className="sticky bottom-0 min-w-0">
        <CardContent className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <span className="text-sm" role="status">
            {previewHint}
          </span>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={busy} onClick={edit}>
              {allowed ? "返回输入" : "查看原输入"}
            </Button>
            {allowed && (
              <>
                <Button
                  variant="outline"
                  disabled={
                    busy ||
                    !!pending ||
                    !!pendingMutation ||
                    current.status === "PREPARING"
                  }
                  onClick={() => void prepare()}
                >
                  {current.status === "PREPARING" ? "准备中…" : "解析并准备"}
                </Button>
                <Button
                  disabled={
                    busy ||
                    !!pending ||
                    previewRevision !== null ||
                    !!pendingMutation ||
                    !previewReady
                  }
                  onClick={() => void preview()}
                >
                  生成搭建预览
                </Button>
              </>
            )}
          </div>
        </CardContent>
      </Card>
      {linkId && (
        <BuildLinkSheet
          tenantId={tenantId}
          linkId={linkId}
          onClose={() => setLinkId(null)}
        />
      )}
      {material && current.media_kind !== "CAROUSEL" && (
        <DramaMaterialSheet
          tenantId={tenantId}
          bcId={bcId}
          summary={current}
          drama={material}
          write={allowed && current.status === "READY"}
          onClose={() => setMaterial(null)}
          onSaved={() => {
            setMaterial(null)
            void summary.refetch()
          }}
        />
      )}
    </div>
  )
}
function AccountInputTable({
  tenantId,
  bcId,
  summary,
  write,
  onEdit,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  write: boolean
  onEdit: () => void
}) {
  const paging = useCursorPage()
  const [status, setStatus] = useState("all")
  const [candidate, setCandidate] = useState<DraftInputPublic | null>(null)
  const query = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      summary.revision,
      "inputs",
      "account",
      status,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.inputs({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: {
            kind: "account",
            status: status === "all" ? undefined : status,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    refetchInterval: summary.status === "PREPARING" ? 2000 : false,
  })
  const previousStatus = useRef(summary.status)
  const refetchInputs = query.refetch
  useEffect(() => {
    if (previousStatus.current !== summary.status) {
      previousStatus.current = summary.status
      void refetchInputs()
    }
  }, [summary.status, refetchInputs])
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <FilterSelect
        label="输入状态"
        value={status}
        onChange={(value) => {
          setStatus(value)
          paging.reset()
        }}
        choices={Object.fromEntries(
          Object.keys(summary.input_counts.account || {}).map((k) => [k, k]),
        )}
      />
      <ServerTable
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        rows={query.data?.items || []}
        loading={query.isPending}
        columns={[
          { header: "输入行", accessorKey: "line_no" },
          { header: "原始文本", accessorKey: "raw_text" },
          {
            header: "结果",
            cell: ({ row }) => (
              <>
                <BuildStatus value={row.original.status} />
                <p className="break-all text-xs text-muted-foreground">
                  <BuildReason code={row.original.reason_code} />
                  {row.original.duplicate_of != null &&
                    ` · 合并到第 ${row.original.duplicate_of} 行`}
                </p>
              </>
            ),
          },
          {
            header: "账户 ID",
            cell: ({ row }) => (
              <span className="break-all font-mono text-xs">
                {row.original.advertiser_id || "尚未解析"}
              </span>
            ),
          },
          {
            header: "操作",
            cell: ({ row }) =>
              write && (
                <Button
                  variant="ghost"
                  onClick={() =>
                    row.original.candidates.length
                      ? setCandidate(row.original)
                      : onEdit()
                  }
                >
                  {row.original.candidates.length ? "查看账户候选" : "返回修正"}
                </Button>
              ),
          },
        ]}
        emptyTitle="没有符合条件的输入"
      />
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        total={query.data?.total}
        busy={query.isFetching}
      />
      <Dialog
        open={!!candidate}
        onOpenChange={(open) => {
          if (!open) setCandidate(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>账户名称存在歧义</DialogTitle>
            <DialogDescription>
              仅继续处理第 {candidate?.line_no} 行：{candidate?.raw_text}
            </DialogDescription>
          </DialogHeader>
          <div className="flex max-h-80 flex-col gap-2 overflow-y-auto">
            {candidate?.candidates.map((item, index) => (
              <Button
                key={String(item.advertiser_id || index)}
                className="h-auto w-full justify-start whitespace-normal text-left"
                variant="outline"
                disabled={!write || typeof item.advertiser_id !== "string"}
                onClick={() => {
                  void navigator.clipboard.writeText(String(item.advertiser_id))
                  setCandidate(null)
                  onEdit()
                }}
              >
                复制账户 ID 并返回修正 ·{" "}
                {String(item.advertiser_id || "ID 待核实")}
              </Button>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
