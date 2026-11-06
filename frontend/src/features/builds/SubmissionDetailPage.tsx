import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { BuildsService } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { displayTime, Identifier } from "@/features/accounts/presentation"
import { normalizeDecimal } from "@/features/strategies/validation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { canManage, isForbidden, RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { ExecutionRoute } from "./ExecutionRoute"
import { SubmissionBadge, SubmissionProgress } from "./SubmissionPresentation"
import { SubmissionRecoveryActions } from "./SubmissionRecoveryActions"
import {
  SubmissionEventsTable,
  SubmissionFilterBar,
  SubmissionStepsTable,
  SubmissionUnitsTable,
} from "./SubmissionTables"
import { rememberedList, submissionKey } from "./submission-page"

type Filters = {
  advertiserId?: string
  dramaId?: string
  kind?: string
  result?: string
}
export function SubmissionDetailPage() {
  const { tenantId, tenant, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname }),
    id = /\/build-tasks\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !id)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description="搭建任务属于提交时的租户与 BC。"
      />
    )
  return (
    <Detail
      key={`${tenantId}:${scope.bcId}:${id}`}
      tenantId={tenantId}
      tenantName={tenant?.name || tenantId}
      bcName={bc.name || scope.bcId}
      bcId={scope.bcId}
      submissionId={id}
      write={scope.role !== "viewer"}
    />
  )
}
function Detail({
  tenantId,
  tenantName,
  bcName,
  bcId,
  submissionId,
  write,
}: {
  tenantId: string
  tenantName: string
  bcName: string
  bcId: string
  submissionId: string
  write: boolean
}) {
  const { scope } = useTenantScope()
  const [technicalOpen, setTechnicalOpen] = useState(false)
  const navigate = useNavigate(),
    client = useQueryClient(),
    search = useRouterState({
      select: (s) => s.location.search as Record<string, unknown>,
    }),
    tab = ["issues", "excluded", "events"].includes(String(search.tab))
      ? String(search.tab)
      : "details",
    stateKey = `submission-filters:${tenantId}:${bcId}:${submissionId}`
  const [filters, setFilters] = useState<Record<string, Filters>>(() => {
    try {
      const v = JSON.parse(sessionStorage.getItem(stateKey) || "{}")
      if (typeof search.result === "string")
        v[tab] = { ...v[tab], result: search.result }
      return v
    } catch {
      return {
        [tab]: {
          result: typeof search.result === "string" ? search.result : undefined,
        },
      }
    }
  })
  useEffect(() => {
    try {
      sessionStorage.setItem(stateKey, JSON.stringify(filters))
    } catch {}
  }, [stateKey, filters])
  const current = filters[tab] || {},
    patch = (value: Filters) => {
      setFilters((prev) => ({ ...prev, [tab]: { ...prev[tab], ...value } }))
      if ("result" in value)
        void navigate({
          to: "/tenants/$tenantId/build-tasks/$submissionId",
          params: { tenantId, submissionId },
          search: { bc_id: bcId, tab, result: value.result || undefined },
          replace: true,
        })
    }
  const query = useQuery({
    queryKey: [...submissionKey(tenantId, bcId), submissionId, "summary"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmission({
          path: { tenant_id: tenantId, submission_id: submissionId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      ["QUEUED", "RUNNING"].includes(q.state.data?.status || "") ? 2000 : false,
  })
  const data = query.data
  const previousProgress = useRef<string | undefined>(undefined)
  const progressSignature = data
    ? JSON.stringify([
        data.updated_at,
        data.status,
        data.stage_counts,
        data.succeeded,
        data.failed,
        data.unknown,
        data.pending,
      ])
    : undefined
  useEffect(() => {
    if (
      previousProgress.current &&
      progressSignature &&
      previousProgress.current !== progressSignature
    ) {
      void client.invalidateQueries({
        queryKey: [...submissionKey(tenantId, bcId), submissionId],
        predicate: (q) => q.queryKey[5] !== "summary",
      })
    }
    previousProgress.current = progressSignature
  }, [progressSignature, client, tenantId, bcId, submissionId])
  const refresh = () =>
    void client.invalidateQueries({
      queryKey: [...submissionKey(tenantId, bcId), submissionId],
    })
  const back = (
    <Button variant="outline" asChild>
      <Link
        to="/tenants/$tenantId/build-tasks"
        params={{ tenantId }}
        search={rememberedList(tenantId, bcId)}
      >
        返回任务列表
      </Link>
    </Button>
  )
  if (query.isPending) return <Skeleton className="h-64 w-full" />
  if (query.error && (!data || isForbidden(query.error)))
    return (
      <div className="flex flex-col gap-4">
        <RequestError error={query.error} retry={() => void query.refetch()} />
        {back}
      </div>
    )
  if (!data || data.bc_id !== bcId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        reason="bc-mismatch"
        resourceBcId={data?.bc_id}
        title="当前 BC 与任务不一致"
        description="请切换到提交时的 BC 查看任务。"
      />
    )
  return (
    <div className="flex min-w-0 flex-col gap-6">
      {query.error && (
        <div className="flex flex-col gap-2">
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
          <p className="text-sm text-muted-foreground">
            当前展示上次成功读取的结果，读取于{" "}
            {displayTime(new Date(query.dataUpdatedAt).toISOString())}。
          </p>
        </div>
      )}
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <WorkspacePageTitle>任务 {data.batch_short_id}</WorkspacePageTitle>
            <SubmissionBadge
              status={data.status}
              correctedAdCount={data.corrected_ad_count}
            />
            <span className="text-sm">
              {data.drama_count} 剧 · {data.account_count} 户
              {data.excluded_unit_count > 0 &&
                ` · 排除 ${data.excluded_unit_count} 个组合`}
            </span>
          </div>
          <p className="text-sm text-muted-foreground">
            {data.actor_name || "暂未获取提交人"} 提交于{" "}
            {displayTime(data.created_at)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={refresh}>
            刷新任务结果
          </Button>
          <Button variant="outline" asChild>
            <Link
              to="/tenants/$tenantId/build-previews/$previewId"
              params={{ tenantId, previewId: data.preview_id }}
              search={{ bc_id: bcId }}
            >
              查看提交配置
            </Link>
          </Button>
          {back}
        </div>
      </div>
      {!!data.continued_by_submission_ids?.length && (
        <Card>
          <CardContent className="flex flex-col gap-2 pt-6">
            <p>
              这份任务的部分范围已由新预览接续，已接续范围不会在此重复创建。历史结果保留。
            </p>
            {data.continued_by_submission_ids.map((id, index) => (
              <Link
                key={id}
                className="text-sm underline underline-offset-4"
                to="/tenants/$tenantId/build-tasks/$submissionId"
                params={{ tenantId, submissionId: id }}
                search={{ bc_id: bcId }}
              >
                查看续建任务{index > 0 ? ` ${index + 1}` : ""}
              </Link>
            ))}
          </CardContent>
        </Card>
      )}
      {!!data.continuation_source_ids?.length && (
        <Card>
          <CardContent className="flex flex-col gap-2 pt-6">
            <p>
              已复用 {data.reused?.campaign_count ?? 0} 个系列 ·{" "}
              {data.reused?.adgroup_count ?? 0} 个广告组 ·{" "}
              {data.reused?.ad_count ?? 0} 条广告；成功数量包含这些已有对象。
            </p>
            {data.continuation_source_ids.map((id, index) => (
              <Link
                key={id}
                className="text-sm underline underline-offset-4"
                to="/tenants/$tenantId/build-tasks/$submissionId"
                params={{ tenantId, submissionId: id }}
                search={{ bc_id: bcId }}
              >
                查看原搭建任务{index > 0 ? ` ${index + 1}` : ""}
              </Link>
            ))}
          </CardContent>
        </Card>
      )}
      <SubmissionProgress
        data={data}
        onTechnicalDetails={() => setTechnicalOpen(true)}
        metadata={
          <div className="flex min-w-0 flex-wrap gap-x-6 gap-y-2 text-sm text-muted-foreground [&>span]:break-words">
            <span>版权方：{data.provider_name || "暂无冻结版权方"}</span>
            <span>策略：{data.strategy_label || "暂未获取"}</span>
            <span title="已提交对象配置日预算之和，非预计实际消耗。">
              {data.daily_budget_label || "配置日预算合计"} {data.currency}{" "}
              {normalizeDecimal(data.daily_budget_sum)}（非实际消耗）
            </span>
          </div>
        }
      />
      {data.recovery_mode === "REAUTHORIZE_READ" && (
        <Alert>
          <AlertDescription>
            原连接已重新授权。请在“异常与待核实”中查看具体步骤，按新授权只读核查；结果不会触发原任务续建。
          </AlertDescription>
        </Alert>
      )}
      {data.status !== "COMPLETED" && (
        <Alert>
          <AlertTitle>
            {["QUEUED", "RUNNING"].includes(data.status)
              ? "任务正在处理"
              : "需要处理"}
          </AlertTitle>
          <AlertDescription>
            <div className="flex flex-col gap-2">
              <p>
                {data.status === "NEEDS_REVIEW"
                  ? "部分结果尚待核实，请查看“异常与待核实”，先确认是否已创建，避免重复创建。"
                  : data.status === "QUEUED"
                    ? "任务已受理，等待执行。"
                    : data.status === "RUNNING"
                      ? "正在准备素材、创建或核查结果，其他可执行组合继续处理。"
                      : "当前任务存在确定失败，请查看原因和可处理范围。"}
              </p>
              {!data.expanded && <p>后台正在展开执行范围，统计将继续更新。</p>}
            </div>
          </AlertDescription>
        </Alert>
      )}
      {/* BLOCKED 也包含没有核查候选的正常完成态，不能据此诊断连接失效。
          真实恢复阻断统一由下方 recovery.reasons 展示，避免重复或误报。 */}
      <SubmissionRecoveryActions
        tenantId={tenantId}
        bcId={bcId}
        submissionId={submissionId}
        recovery={data.recovery}
        write={write}
      />
      <Card className="min-w-0">
        <CardHeader className="min-w-0 gap-4">
          <Tabs
            value={tab}
            onValueChange={(value) =>
              void navigate({
                to: "/tenants/$tenantId/build-tasks/$submissionId",
                params: { tenantId, submissionId },
                search: { bc_id: bcId, tab: value },
              })
            }
          >
            <TabsList className="h-auto flex-wrap">
              <TabsTrigger value="details">搭建明细</TabsTrigger>
              <TabsTrigger value="issues">异常与待核实</TabsTrigger>
              {(data.excluded_unit_count > 0 || tab === "excluded") && (
                <TabsTrigger value="excluded">
                  排除项（{data.excluded_unit_count}）
                </TabsTrigger>
              )}
              <TabsTrigger value="events">操作记录</TabsTrigger>
            </TabsList>
          </Tabs>
          {tab !== "events" && (
            <SubmissionFilterBar
              key={tab}
              tenantId={tenantId}
              bcId={bcId}
              previewId={data.preview_id}
              {...current}
              onAccount={(v) => patch({ advertiserId: v })}
              onDrama={(v) => patch({ dramaId: v })}
              {...(tab === "issues"
                ? {
                    onKind: (v: string) => patch({ kind: v }),
                    onResult: (v: string) => patch({ result: v }),
                  }
                : {})}
            />
          )}
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          {tab === "events" ? (
            <SubmissionEventsTable {...{ tenantId, bcId, submissionId }} />
          ) : tab === "issues" ? (
            <SubmissionStepsTable
              {...{ tenantId, bcId, submissionId, ...current }}
            />
          ) : (
            <>
              {tab === "excluded" && (
                <p className="text-xs text-muted-foreground">
                  排除组合未提交；修复后需在新预览明确提交。
                </p>
              )}
              <SubmissionUnitsTable
                {...{ tenantId, bcId, submissionId }}
                advertiserId={current.advertiserId}
                dramaId={current.dramaId}
                excluded={tab === "excluded"}
              />
            </>
          )}
        </CardContent>
      </Card>
      {technicalOpen && (
        <ManagementSheet
          title="技术详情"
          description="用于定位任务与核对执行连接。"
          dirty={false}
          onClose={() => setTechnicalOpen(false)}
        >
          <div className="flex flex-col gap-4 text-sm">
            <p>
              任务所属：{tenantName} · {bcName}
            </p>
            <div>
              任务编号
              <Identifier value={data.submission_id} />
            </div>
            <div>
              配置记录编号
              <Identifier value={data.preview_id} />
            </div>
            <p>最近更新 {displayTime(data.updated_at)}</p>
            <ExecutionRoute route={data.execution_route} />
            <Button variant="outline" asChild>
              <Link
                to="/tenants/$tenantId/accounts"
                params={{ tenantId }}
                search={{ bc_id: bcId, tab: "connections" }}
              >
                查看连接与授权
              </Link>
            </Button>
          </div>
        </ManagementSheet>
      )}
    </div>
  )
}
