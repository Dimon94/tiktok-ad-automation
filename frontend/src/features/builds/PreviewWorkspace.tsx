import { useMutation, useQuery } from "@tanstack/react-query"
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import { useState } from "react"
import {
  BuildsService,
  type FrozenAd,
  type FrozenGroup,
  type PreviewSummary,
  type PreviewUnit,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { CopyField } from "@/features/providers/presentation"
import { normalizeDecimal } from "@/features/strategies/validation"
import { TargetingSummary } from "@/features/targeting/TargetingForm"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  canManage,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { cn } from "@/lib/utils"
import { bidLabel, budgetLabel, budgetUnitLabel, buildKey } from "./api"
import { ExecutionRoute } from "./ExecutionRoute"
import { PreviewGenerationProgress } from "./PreviewGenerationProgress"
import {
  BuildGuard,
  BuildReason,
  BuildStatus,
  BuildSteps,
  reasonLabels,
} from "./presentation"
import { usePreviewSubmission } from "./usePreviewSubmission"
export type PreviewSubmit = (preview: PreviewSummary) => Promise<void>
export function PreviewWorkspace() {
  const { tenantId, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname }),
    previewId = /\/build-previews\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !previewId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description="冻结预览属于创建时的租户与 BC。"
      />
    )
  return (
    <BuildPreviewPanel
      key={`${tenantId}:${scope.bcId}:${previewId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      previewId={previewId}
      write={scope.role !== "viewer"}
    />
  )
}
export function BuildPreviewPanel({
  tenantId,
  bcId,
  previewId,
  write,
  onSubmit,
}: {
  tenantId: string
  bcId: string
  previewId: string
  write: boolean
  onSubmit?: PreviewSubmit
}) {
  const { scope } = useTenantScope()
  const [tab, setTab] = useState("dramas"),
    [dramaId, setDramaId] = useState<string | undefined>(),
    [unit, setUnit] = useState<PreviewUnit | null>(null),
    navigate = useNavigate()
  const summary = useQuery({
    queryKey: [...buildKey(tenantId, bcId), "preview", previewId, "summary"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.previewSummary({
          path: { tenant_id: tenantId, preview_id: previewId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data?.status === "BUILDING" ? 2000 : false,
  })
  const retryPreview = useMutation({
    mutationFn: async () => {
      const failed = summary.data
      if (
        !write ||
        !failed ||
        failed.bc_id !== bcId ||
        (failed.status !== "FAILED" && !failed.can_refresh_continuation) ||
        failed.submission_id
      )
        throw new Error("当前预览不可重试")
      return (
        await BuildsService.generatePreview({
          path: { tenant_id: tenantId, draft_id: failed.draft_id },
          body: {
            expected_revision: failed.draft_revision,
            ...(failed.status !== "FAILED" && failed.can_refresh_continuation
              ? { refresh_continuation: true }
              : {}),
          },
        })
      ).data
    },
    onSuccess: async (data) => {
      if (data.preview_id === previewId) {
        await summary.refetch()
      } else {
        await navigate({
          to: "/tenants/$tenantId/build-previews/$previewId",
          params: { tenantId, previewId: data.preview_id },
          search: { bc_id: bcId },
        })
      }
    },
  })
  const submission = usePreviewSubmission(
    tenantId,
    bcId,
    previewId,
    write && !summary.error && !summary.data?.submission_id,
  )
  const current = summary.data
  // 服务端任务关联负责跨会话识别，刚受理的本页回执可立即进入只读态。
  const submittedId =
    current?.submission_id || submission.record?.receipt?.submission_id
  const readOnly = !!submittedId
  if (summary.isPending) return <Skeleton className="h-64 w-full" />
  if (summary.error && !current)
    return (
      <RequestError
        error={summary.error}
        retry={() => void summary.refetch()}
      />
    )
  if (!current || current.bc_id !== bcId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        reason="bc-mismatch"
        resourceBcId={current?.bc_id}
        title="当前 BC 与预览不一致"
        description="请切换到预览所属 BC 查看冻结范围。"
      />
    )
  const back = () =>
    void navigate({
      to: "/tenants/$tenantId/build-drafts/$draftId",
      params: { tenantId, draftId: current.draft_id },
      search: { bc_id: bcId },
    })
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>
            {readOnly ? "提交配置" : "搭建预览"}
          </WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            {readOnly ? (
              `本次任务提交时的配置 · 只读 · 版本 ${current.draft_revision}`
            ) : (
              <>
                草稿 v{current.draft_revision} ·{" "}
                <BuildStatus value={current.status} />
              </>
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void summary.refetch()}>
            {readOnly ? "刷新配置状态" : "刷新预览状态"}
          </Button>
          {!readOnly &&
            write &&
            current.can_refresh_continuation &&
            current.status !== "BUILDING" && (
              <Button
                variant="outline"
                disabled={
                  retryPreview.isPending ||
                  submission.busy ||
                  submission.unknown
                }
                onClick={() => retryPreview.mutate()}
              >
                {retryPreview.isPending ? "正在核实续建计划…" : "刷新续建预览"}
              </Button>
            )}
          {submittedId ? (
            <Button variant="outline" asChild>
              <Link
                to="/tenants/$tenantId/build-tasks/$submissionId"
                params={{ tenantId, submissionId: submittedId }}
                search={{ bc_id: bcId }}
              >
                返回任务详情
              </Link>
            </Button>
          ) : (
            <Button variant="outline" onClick={back}>
              返回调整
            </Button>
          )}
        </div>
      </div>
      {!readOnly && <BuildSteps step={3} />}
      {readOnly ? (
        <details>
          <summary className="cursor-pointer text-sm text-muted-foreground">
            查看提交时的连接
          </summary>
          <div className="mt-3">
            <ExecutionRoute route={current.execution_route} />
          </div>
        </details>
      ) : (
        <ExecutionRoute route={current.execution_route} />
      )}
      {!readOnly && current.can_refresh_continuation && (
        <Card>
          <CardContent className="flex flex-col gap-2 pt-6">
            <p className="font-medium">
              {current.continuation_unit_count
                ? "本次续建计划"
                : "这份草稿已有搭建任务"}
            </p>
            {current.continuation_unit_count ? (
              <>
                <p>
                  复用 {current.reused_campaign_count} 个系列 ·{" "}
                  {current.reused_adgroup_count} 个广告组 ·{" "}
                  {current.reused_ad_count} 条广告
                </p>
                <p>
                  新建 {current.new_campaign_count} 个系列 ·{" "}
                  {current.new_adgroup_count} 个广告组 · {current.new_ad_count}{" "}
                  条广告
                </p>
                <p className="text-sm text-muted-foreground">
                  以前成功的对象保留，本次只按新预览继续搭建。原任务记录不变。
                </p>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                修改配置后可刷新续建预览，核对本次保留和新增的对象，无需另建草稿。
              </p>
            )}
            {!!current.continuation_blocked_count && (
              <p>
                有 {current.continuation_blocked_count}{" "}
                个组合暂不能续建，请查看排除原因并处理原任务后刷新预览。
              </p>
            )}
            <div className="flex flex-wrap gap-3 text-sm">
              {current.continuation_source_ids?.map((id, index) => (
                <Link
                  key={id}
                  className="underline underline-offset-4"
                  to="/tenants/$tenantId/build-tasks/$submissionId"
                  params={{ tenantId, submissionId: id }}
                  search={{ bc_id: bcId }}
                >
                  查看原搭建任务
                  {(current.continuation_source_ids?.length || 0) > 1
                    ? ` ${index + 1}`
                    : ""}
                </Link>
              ))}
            </div>
            {retryPreview.error && <RequestError error={retryPreview.error} />}
          </CardContent>
        </Card>
      )}
      {summary.error && current.status !== "BUILDING" && (
        <RequestError
          error={summary.error}
          retry={() => void summary.refetch()}
        />
      )}
      {current?.targeting && (
        <Card>
          <CardContent className="flex flex-col gap-2">
            <p className="font-medium">受众定向 · 严格限制</p>
            <TargetingSummary
              value={current.targeting}
              countries={current.targeting_region_codes}
            />
          </CardContent>
        </Card>
      )}
      <BuildGuard
        dirty={!readOnly && (submission.busy || submission.unknown)}
        title="提交结果尚待核实"
        description="离开只停止本页等待；已受理的广告任务仍会继续。返回此预览可按原请求核实。"
        leaveLabel="离开并稍后核实"
      />
      {!readOnly && !!submission.error && (
        <RequestError error={submission.error} />
      )}
      {!readOnly && submission.unknown && (
        <Alert>
          <AlertDescription>
            <p role="status">
              正在确认提交结果。已保留原请求标识，不会再次整批创建。
            </p>
            <Button
              variant="outline"
              disabled={submission.busy}
              onClick={() => void submission.recover()}
            >
              查询原提交结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!readOnly && current.status === "OBSOLETE" && (
        <Alert variant="destructive">
          <AlertDescription>
            草稿已更新，当前预览已过期。返回调整后重新生成预览，原冻结配置不会被修改。
          </AlertDescription>
        </Alert>
      )}
      {current.status === "FAILED" && (
        <Alert variant="destructive">
          <AlertDescription>
            预览未完成：
            {current.error_code
              ? reasonLabels[current.error_code] || current.error_code
              : "请核实当前草稿后重新生成"}
          </AlertDescription>
        </Alert>
      )}
      {current.status === "BUILDING" ? (
        <PreviewGenerationProgress
          preview={current}
          error={summary.error}
          refreshing={summary.isFetching}
          onRefresh={() => void summary.refetch()}
        />
      ) : current.status === "FAILED" ? (
        <Card>
          <CardContent className="flex min-w-0 flex-col gap-2">
            <p role="status">预览生成失败，可复用已准备的内容重新生成。</p>
            {write && !readOnly && (
              <Button
                className="self-start"
                disabled={retryPreview.isPending}
                onClick={() => retryPreview.mutate()}
              >
                {retryPreview.isPending ? "正在重新生成…" : "重试生成预览"}
              </Button>
            )}
            {retryPreview.error && <RequestError error={retryPreview.error} />}
            <p className="text-sm text-muted-foreground">
              统计尚未完成，暂不展示最终提交数量与预算。
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          {(current.excluded_dramas?.length ?? 0) > 0 && (
            <Alert variant="destructive">
              <AlertTitle>
                {current.excluded_dramas?.length} 部剧本次不会搭建
              </AlertTitle>
              <AlertDescription>
                <p>以下剧在所有账户上都被排除，提交后不会创建任何系列：</p>
                <ul className="mt-1 list-disc pl-5">
                  {current.excluded_dramas?.map((drama) => (
                    <li key={drama.drama_id} className="break-all">
                      {drama.title}
                      {(drama.reason_codes?.length ?? 0) > 0 &&
                        `（${drama.reason_codes
                          ?.map((code) => reasonLabels[code] || code)
                          .join("、")}）`}
                    </li>
                  ))}
                </ul>
                {!readOnly && (
                  <p className="mt-1">
                    如需一并搭建，请先补充素材后重新生成预览；直接提交只会搭建其余剧。
                  </p>
                )}
              </AlertDescription>
            </Alert>
          )}
          {(current.blocked_count > 0 || current.input_issue_count > 0) && (
            <Alert>
              <AlertDescription>
                本次排除 {current.blocked_count} 个阻断组合；另有{" "}
                {current.input_issue_count} 条输入问题，未形成组合。
                {readOnly
                  ? "排除项未包含在本次提交中。"
                  : "仅提交可用与提交后可分发范围，排除项不会自动补入。"}
              </AlertDescription>
            </Alert>
          )}
          <Card className="min-w-0">
            <CardHeader className="min-w-0">
              <Tabs
                value={tab}
                onValueChange={(value) => {
                  setTab(value)
                  setDramaId(undefined)
                }}
              >
                <TabsList>
                  <TabsTrigger value="dramas">剧目汇总</TabsTrigger>
                  <TabsTrigger value="units">账户组合</TabsTrigger>
                  <TabsTrigger value="excluded">排除组合</TabsTrigger>
                  <TabsTrigger value="issues">输入问题</TabsTrigger>
                </TabsList>
              </Tabs>
            </CardHeader>
            <CardContent className="min-w-0">
              {tab === "dramas" ? (
                <PreviewDramaTable
                  tenantId={tenantId}
                  bcId={bcId}
                  preview={current}
                  onDrama={(id) => {
                    setDramaId(id)
                    setTab("units")
                  }}
                />
              ) : tab === "issues" ? (
                <PreviewExclusions
                  tenantId={tenantId}
                  bcId={bcId}
                  previewId={previewId}
                  onEdit={readOnly ? undefined : back}
                />
              ) : (
                <PreviewUnitTable
                  key={tab}
                  tenantId={tenantId}
                  bcId={bcId}
                  previewId={previewId}
                  budgetStrategy={current.budget_strategy}
                  dramaId={dramaId}
                  excluded={tab === "excluded"}
                  onUnit={setUnit}
                />
              )}
            </CardContent>
          </Card>
          <PreviewSummaryBar
            preview={current}
            readOnly={readOnly}
            write={
              !readOnly && write && !submission.forbidden && !summary.error
            }
            onSubmit={onSubmit || submission.submit}
            unavailable={!!submission.record || submission.busy}
          />
        </>
      )}
      {unit && (
        <FrozenUnitSheet
          tenantId={tenantId}
          bcId={bcId}
          unit={unit}
          onClose={() => setUnit(null)}
        />
      )}
    </div>
  )
}
export function PreviewSummaryBar({
  preview,
  write,
  onSubmit,
  unavailable = false,
  readOnly = false,
}: {
  preview: PreviewSummary
  write: boolean
  onSubmit?: PreviewSubmit
  unavailable?: boolean
  readOnly?: boolean
}) {
  const [pending, setPending] = useState(false),
    [error, setError] = useState<unknown>()
  const createsDisabled = preview.creation_status === "DISABLE"
  const submitAction = preview.continuation_unit_count
    ? createsDisabled
      ? "继续搭建并停用"
      : "继续搭建并立即启用"
    : createsDisabled
      ? "创建并停用"
      : "创建并立即启用"
  const submitStatus = createsDisabled ? "停用" : "立即启用"
  const scheduleSummary =
    preview.schedule_type === "SCHEDULE_FROM_NOW" &&
    !preview.schedule_start_time
      ? "立即开始"
      : preview.schedule_type === "SCHEDULE_FROM_NOW"
        ? `指定时间开启：${preview.schedule_start_time} UTC`
        : preview.schedule_end_time
          ? `指定时间：${preview.schedule_start_time} 至 ${preview.schedule_end_time} UTC`
          : `指定时间：${preview.schedule_start_time} UTC 起`
  const strategySummary = `预算策略：${preview.budget_strategy === "ADGROUP" ? "组预算" : "系列预算"}；竞价策略：${bidLabel(preview.bid_strategy)}；投放排期：${scheduleSummary}。`
  const structureSummary = preview.structure_summary
    ? /预算策略|竞价策略/.test(preview.structure_summary)
      ? preview.structure_summary
      : `${preview.structure_summary}；${strategySummary}`
    : strategySummary
  return (
    <Card className={cn("min-w-0", !readOnly && "sticky bottom-0")}>
      <CardContent className="flex min-w-0 flex-col gap-3">
        {!!error && <RequestError error={error} />}
        {!!preview.skipped_material_count && (
          <Alert>
            <AlertTitle>部分素材未纳入本次搭建</AlertTitle>
            <AlertDescription>
              {preview.campaign_count > 0
                ? `已跳过 ${preview.skipped_material_count} 条不可用素材，其余素材继续搭建。`
                : `已跳过 ${preview.skipped_material_count} 条不可用素材；当前没有可提交组合，请补充可用素材或处理其他阻断问题。`}
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="font-semibold">
              {preview.campaign_count} Campaign · {preview.adgroup_count} Ad
              Group · {preview.ad_count} Ad
            </p>
            <p className="mt-1 text-sm">
              {budgetLabel(preview)} {preview.currency}{" "}
              {normalizeDecimal(preview.daily_budget_sum)}
            </p>
            <p className="text-xs text-muted-foreground">
              {structureSummary}
              <span className="block">
                以上金额为配置日预算之和，非预计实际消耗。
              </span>
              {!readOnly &&
                preview.blocked_count > 0 &&
                `排除 ${preview.blocked_count} 个阻断组合。`}
              {!readOnly &&
                preview.preparing_count > 0 &&
                `${preview.preparing_count} 个组合可提交，将在提交后准备目标素材${preview.media_kind === "CAROUSEL" ? "并核验所选配乐" : ""}。`}
            </p>
          </div>
          {write && !readOnly && !preview.submission_id && (
            <div className="flex flex-col gap-2">
              <Button
                disabled={
                  !onSubmit ||
                  unavailable ||
                  pending ||
                  !preview.execution_route ||
                  preview.execution_route.bc_id !== preview.bc_id ||
                  preview.status !== "FROZEN" ||
                  preview.campaign_count === 0
                }
                aria-label={`${submitAction} ${preview.campaign_count} 个 Campaign / ${preview.adgroup_count} 个 Ad Group / ${preview.ad_count} 条 Ad`}
                onClick={async () => {
                  if (!onSubmit || pending) return
                  setPending(true)
                  try {
                    await onSubmit(preview)
                  } catch (e) {
                    setError(e)
                  } finally {
                    setPending(false)
                  }
                }}
              >
                {pending ? "正在确认提交结果" : submitAction}
              </Button>
              <p className="text-xs text-muted-foreground">
                {preview.continuation_unit_count
                  ? `仅新增对象将创建并${submitStatus}，复用对象保持原状态。`
                  : `将直接创建并${submitStatus}，审核与实际投放状态由 TikTok 决定。`}
              </p>
              {!onSubmit && (
                <p className="text-xs text-muted-foreground">
                  提交功能尚未开放，当前可查看与调整预览。
                </p>
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
export function PreviewUnitTable({
  tenantId,
  bcId,
  previewId,
  budgetStrategy,
  excluded,
  dramaId,
  onUnit,
}: {
  tenantId: string
  bcId: string
  previewId: string
  budgetStrategy: PreviewSummary["budget_strategy"]
  excluded: boolean
  dramaId?: string
  onUnit: (unit: PreviewUnit) => void
}) {
  const paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "preview",
        previewId,
        "units",
        excluded,
        dramaId,
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.previewUnits({
            path: { tenant_id: tenantId, preview_id: previewId },
            query: {
              cursor: paging.cursor,
              limit: paging.limit,
              readiness: excluded ? "BLOCKED" : undefined,
              drama_id: dramaId,
            },
            signal,
          })
        ).data,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ServerTable
        rows={query.data?.items || []}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={excluded}
        emptyTitle={excluded ? "没有阻断组合" : "尚无账户组合"}
        columns={[
          {
            header: "剧目 / 账户 ID",
            cell: ({ row }) => (
              <div>
                {row.original.title}
                <p className="break-all font-mono text-xs text-muted-foreground">
                  {row.original.advertiser_id}
                </p>
              </div>
            ),
          },
          {
            header: "准备状态",
            cell: ({ row }) => (
              <>
                {row.original.readiness === "PREPARING" ? (
                  <span>可提交 · 提交后准备</span>
                ) : (
                  <BuildStatus value={row.original.readiness} />
                )}
                <p className="break-all text-xs text-muted-foreground">
                  {row.original.reason_codes.map((code) => (
                    <BuildReason key={code} code={code} />
                  ))}
                </p>
                {!!row.original.skipped_material_count && (
                  <p className="text-xs text-muted-foreground">
                    已跳过 {row.original.skipped_material_count}{" "}
                    条素材，详情可查看原因
                  </p>
                )}
              </>
            ),
          },
          {
            header: "广告组 / 广告 / 素材",
            cell: ({ row }) => (
              <div className="flex flex-col gap-1">
                <span>
                  {row.original.group_count} / {row.original.ad_count} /{" "}
                  {row.original.material_count ?? "—"}
                </span>
                <span className="text-xs text-muted-foreground">
                  {row.original.structure_summary ||
                    `${row.original.group_count} 个广告组、${row.original.ad_count} 个广告`}
                </span>
              </div>
            ),
          },
          {
            header: "预算",
            cell: ({ row }) =>
              `${budgetUnitLabel(budgetStrategy)} ${row.original.currency} ${normalizeDecimal(row.original.budget)}`,
          },
          {
            header: "Campaign 名称",
            cell: ({ row }) => (
              <CopyField
                label="Campaign 名称"
                value={row.original.campaign_name}
              />
            ),
          },
          {
            header: "操作",
            cell: ({ row }) => (
              <Button variant="ghost" onClick={() => onUnit(row.original)}>
                查看冻结详情
              </Button>
            ),
          },
        ]}
      />
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        total={query.data?.total}
        busy={query.isFetching}
      />
    </div>
  )
}
function PreviewExclusions({
  tenantId,
  bcId,
  previewId,
  onEdit,
}: {
  tenantId: string
  bcId: string
  previewId: string
  onEdit?: () => void
}) {
  const [kind, setKind] = useState<"drama" | "account">("drama"),
    paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "preview",
        previewId,
        "issues",
        kind,
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.previewInputs({
            path: { tenant_id: tenantId, preview_id: previewId },
            query: {
              kind,
              issues_only: true,
              cursor: paging.cursor,
              limit: paging.limit,
            },
            signal,
          })
        ).data,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Tabs
        value={kind}
        onValueChange={(v) => {
          setKind(v as "drama" | "account")
          paging.reset()
        }}
      >
        <TabsList>
          <TabsTrigger value="drama">剧目输入</TabsTrigger>
          <TabsTrigger value="account">账户输入</TabsTrigger>
        </TabsList>
      </Tabs>
      <div className="flex min-w-0 flex-col gap-4">
        <ServerTable
          rows={query.data?.items || []}
          loading={query.isPending}
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          emptyTitle="没有输入问题"
          columns={[
            { header: "输入行", accessorKey: "line_no" },
            { header: "原始文本", accessorKey: "raw_text" },
            {
              header: "状态 / 原因",
              cell: ({ row }) => (
                <>
                  <BuildStatus value={row.original.status} />
                  <p className="break-all text-xs">
                    <BuildReason code={row.original.reason_code} />
                  </p>
                </>
              ),
            },
            {
              header: "操作",
              cell: () =>
                onEdit ? (
                  <Button variant="ghost" onClick={onEdit}>
                    返回修正
                  </Button>
                ) : (
                  <span className="text-muted-foreground">未提交</span>
                ),
            },
          ]}
        />
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          total={query.data?.total}
          busy={query.isFetching}
        />
      </div>
    </div>
  )
}
function FrozenUnitSheet({
  tenantId,
  bcId,
  unit,
  onClose,
}: {
  tenantId: string
  bcId: string
  unit: PreviewUnit
  onClose: () => void
}) {
  const paging = useCursorPage(),
    detail = useQuery({
      queryKey: [...buildKey(tenantId, bcId), "unit", unit.unit_id],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.frozenUnit({
            path: { tenant_id: tenantId, unit_id: unit.unit_id },
            signal,
          })
        ).data,
    }),
    groups = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "unit",
        unit.unit_id,
        "groups",
        paging.cursor,
        paging.limit,
      ],
      enabled: detail.data?.bc_id === bcId,
      queryFn: async ({ signal }) =>
        (
          await BuildsService.frozenGroups({
            path: { tenant_id: tenantId, unit_id: unit.unit_id },
            query: { cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
    })
  return (
    <ManagementSheet
      title={`冻结详情 · ${unit.title}`}
      description={`账户 ${unit.advertiser_id}。以下为预览冻结时的真实配置。`}
      dirty={false}
      onClose={onClose}
    >
      <div className="space-y-4">
        {detail.isPending && <Skeleton className="h-32" />}
        {detail.error && <RequestError error={detail.error} />}{" "}
        {detail.data && detail.data.bc_id === bcId && (
          <>
            <CopyField
              label="Campaign 名称"
              value={detail.data.campaign_name}
              expanded
            />
            <p className="text-sm">
              {budgetUnitLabel(detail.data.budget_strategy)}{" "}
              {detail.data.currency} {normalizeDecimal(detail.data.budget)} ·{" "}
              {bidLabel(detail.data.bid_strategy)}
              {detail.data.bid_strategy === "TARGET_ROAS" &&
              detail.data.target_roas !== null
                ? ` ${detail.data.target_roas}`
                : ""}
            </p>
            <CopyField label="推广链接" value={detail.data.url} expanded />
            <p className="break-all text-xs text-muted-foreground">
              策略版本 {detail.data.strategy_version_id}
            </p>
            {detail.data.targeting && (
              <div className="rounded-md border p-3">
                <p className="mb-2 text-sm font-medium">
                  本账户冻结定向 · 严格限制
                </p>
                <TargetingSummary
                  value={detail.data.targeting}
                  countries={detail.data.targeting_region_codes}
                />
              </div>
            )}
            <details className="rounded-md border p-3">
              <summary className="cursor-pointer text-sm">
                Minis、Identity 与场景能力快照
              </summary>
              <pre className="mt-2 whitespace-pre-wrap break-all text-xs">
                {JSON.stringify(detail.data.scene_snapshot, null, 2)}
              </pre>
            </details>
            <h3 className="font-semibold">冻结分组与 SP 创意</h3>
            {!!unit.skipped_material_count && (
              <SkippedMaterials
                tenantId={tenantId}
                bcId={bcId}
                unitId={unit.unit_id}
              />
            )}
            {groups.error && <RequestError error={groups.error} />}{" "}
            {groups.isPending && <Skeleton className="h-32" />}
            {groups.data?.items.map((group) => (
              <GroupDetail
                key={group.group_id}
                group={group}
                tenantId={tenantId}
                bcId={bcId}
                unitId={unit.unit_id}
              />
            ))}
            <Pager
              paging={paging}
              nextCursor={groups.data?.next_cursor}
              total={groups.data?.total}
              busy={groups.isFetching}
            />
          </>
        )}
      </div>
    </ManagementSheet>
  )
}
function SkippedMaterials({
  tenantId,
  bcId,
  unitId,
}: {
  tenantId: string
  bcId: string
  unitId: string
}) {
  const paging = useCursorPage()
  const query = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      "unit",
      unitId,
      "skipped-materials",
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.skippedMaterials({
          path: { tenant_id: tenantId, unit_id: unitId },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  return (
    <section className="flex flex-col gap-3" aria-label="已跳过的素材">
      <Alert>
        <AlertTitle>已跳过的素材</AlertTitle>
        <AlertDescription>
          这些素材不会进入本次提交；保留的素材见下方冻结分组。全部素材不可用的账户组合仍不可搭建。
        </AlertDescription>
      </Alert>
      <ServerTable
        rows={query.data?.items || []}
        columns={[
          { header: "文件名", accessorKey: "file_name" },
          {
            header: "跳过原因",
            cell: ({ row }) => <BuildReason code={row.original.reason_code} />,
          },
        ]}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="没有跳过的素材"
      />
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        total={query.data?.total}
        busy={query.isFetching}
      />
    </section>
  )
}

function AdDetail({ ad }: { ad: FrozenAd }) {
  return (
    <div className="space-y-2 rounded-md bg-muted p-3">
      <h4 className="text-sm font-semibold">
        基础广告 {ad.base_ad_no} · 文案版本 {ad.creative_no}
      </h4>
      {ad.included === false && (
        <div className="text-sm text-destructive">
          已排除本条广告
          {ad.reason_codes?.map((code) => (
            <BuildReason key={code} code={code} />
          ))}
        </div>
      )}
      <CopyField label="Ad 名称" value={ad.name} expanded />
      <p className="whitespace-pre-wrap text-sm">{ad.text}</p>
      <p className="break-all text-xs">
        CTA：{ad.cta_option_ids.join("、") || "无 CTA"}
      </p>
      {ad.creative_units?.map((creative, index) => (
        <div key={creative.unit_id} className="space-y-1 text-xs">
          <p>
            创意 {index + 1} ·{" "}
            {creative.kind === "CAROUSEL"
              ? `轮播，${creative.members.length} 张图片，配乐 ${creative.music_id}`
              : "视频"}
          </p>
          <ol className="list-inside list-decimal break-all">
            {creative.members.map((member) => (
              <li key={member.material_id}>{member.file_name}</li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  )
}

function FrozenAds({
  tenantId,
  bcId,
  unitId,
  groupId,
}: {
  tenantId: string
  bcId: string
  unitId: string
  groupId: string
}) {
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const cursor = cursors[cursors.length - 1]
  const query = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      "unit",
      unitId,
      "group",
      groupId,
      "ads",
      cursor,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.frozenAds({
          path: { tenant_id: tenantId, unit_id: unitId, group_id: groupId },
          query: { cursor, limit: 2 },
          signal,
        })
      ).data,
  })
  return (
    <section aria-label="冻结广告详情" className="space-y-3">
      {query.error && (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      )}
      {query.isPending && <Skeleton className="h-24" />}
      {query.data?.items.map((ad) => (
        <AdDetail key={ad.ad_id} ad={ad} />
      ))}
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">
          共 {query.data?.total ?? "—"} 条广告 · 第 {cursors.length} 页 · 每页 2
          条
        </span>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={query.isFetching || cursors.length === 1}
            onClick={() => setCursors((values) => values.slice(0, -1))}
          >
            上一页广告
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={query.isFetching || !query.data?.next_cursor}
            onClick={() => {
              const next = query.data?.next_cursor
              if (next) setCursors((values) => [...values, next])
            }}
          >
            下一页广告
          </Button>
        </div>
      </div>
    </section>
  )
}

function GroupDetail({
  group,
  tenantId,
  bcId,
  unitId,
}: {
  group: FrozenGroup
  tenantId: string
  bcId: string
  unitId: string
}) {
  const [open, setOpen] = useState(false)
  const version2 = group.contract_version === 2
  return (
    <details
      className="rounded-md border p-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm">
        第 {group.group_no} 组 ·{" "}
        {version2 ? group.material_count : group.material_ids?.length} 份素材 ·{" "}
        {version2 && `${group.creative_unit_count} 个完整创意 · `}
        {version2 ? group.ad_count : group.ads?.length} 条 SP 创意
      </summary>
      {open && (
        <div className="mt-3 space-y-3">
          <CopyField label="Ad Group 名称" value={group.name} expanded />
          <p className="break-all font-mono text-xs">
            {version2 ? "素材 ID 样例（最多 6 项）" : "素材 ID"}：
            {(version2 ? group.sample_material_ids : group.material_ids)?.join(
              "、",
            )}
          </p>
          {version2 ? (
            <FrozenAds
              tenantId={tenantId}
              bcId={bcId}
              unitId={unitId}
              groupId={group.group_id}
            />
          ) : (
            group.ads?.map((ad) => <AdDetail key={ad.ad_id} ad={ad} />)
          )}
        </div>
      )}
    </details>
  )
}

function PreviewDramaTable({
  tenantId,
  bcId,
  preview,
  onDrama,
}: {
  tenantId: string
  bcId: string
  preview: PreviewSummary
  onDrama: (id: string) => void
}) {
  const paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "preview",
        preview.preview_id,
        "dramas",
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.previewDramas({
            path: { tenant_id: tenantId, preview_id: preview.preview_id },
            query: { cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ServerTable
        rows={query.data?.items || []}
        columns={[
          { header: "剧目", accessorKey: "title" },
          {
            header: "素材分配 / 去重素材",
            cell: ({ row }) => (
              <div>
                {row.original.material_count} /{" "}
                {row.original.unique_material_count ?? "—"}
                <span className="text-xs text-muted-foreground">
                  （原分组 {row.original.material_group_count}，广告分配{" "}
                  {row.original.ad_material_allocation_count ?? "—"} 次）
                </span>
                {!!row.original.skipped_material_count && (
                  <p className="text-xs text-muted-foreground">
                    {row.original.skipped_material_count}{" "}
                    条素材在部分或全部账户被跳过
                  </p>
                )}
              </div>
            ),
          },
          {
            header: "提交户 / 总户",
            cell: ({ row }) =>
              `${row.original.eligible_campaign_count} / ${row.original.account_count}`,
          },
          {
            header: "Campaign / Ad Group / Ad",
            cell: ({ row }) =>
              `${row.original.eligible_campaign_count} / ${row.original.eligible_adgroup_count} / ${row.original.eligible_ad_count}`,
          },
          {
            header: budgetLabel(preview),
            cell: ({ row }) =>
              `${preview.currency} ${normalizeDecimal(row.original.daily_budget_sum)}`,
          },
          {
            header: "操作",
            cell: ({ row }) => (
              <Button
                variant="ghost"
                onClick={() => onDrama(row.original.drama_id)}
              >
                查看账户组合
              </Button>
            ),
          },
        ]}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="尚无冻结剧目"
      />
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        total={query.data?.total}
        busy={query.isFetching}
      />
    </div>
  )
}
