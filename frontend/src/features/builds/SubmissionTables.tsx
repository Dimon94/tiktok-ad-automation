import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import {
  BuildsService,
  MaterialsService,
  type StepPublic,
  type SubmissionAdPublic,
  type SubmissionEventPublic,
  type SubmissionGroupPublic,
  type SubmissionMaterialPublic,
  type SubmissionUnitPublic,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  displayTime,
  FilterSelect,
  Identifier,
} from "@/features/accounts/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
} from "@/features/tenants/shared"
import { HistoricalReadAction } from "./HistoricalReadAction"
import { BuildReason } from "./presentation"
import {
  PlatformState,
  SubmissionBadge,
  SubmissionTable,
  stepKinds,
  stepStates,
} from "./SubmissionPresentation"
import { eventConclusion, eventKind } from "./submission-events"
import { submissionKey, useSubmissionPaging } from "./submission-page"

type Scope = { tenantId: string; bcId: string; submissionId: string }
export function SubmissionUnitsTable({
  tenantId,
  bcId,
  submissionId,
  advertiserId,
  dramaId,
  excluded = false,
}: {
  tenantId: string
  bcId: string
  submissionId: string
  advertiserId?: string
  dramaId?: string
  excluded?: boolean
}) {
  const [selected, setSelected] = useState<SubmissionUnitPublic | null>(null),
    paging = useSubmissionPaging(
      `submission-units:${tenantId}:${bcId}:${submissionId}:${excluded}:${advertiserId || ""}:${dramaId || ""}`,
    )
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "units",
      excluded,
      advertiserId,
      dramaId,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await (excluded
          ? BuildsService.getSubmissionExcluded
          : BuildsService.getSubmissionUnits)({
          path: { tenant_id: tenantId, submission_id: submissionId },
          query: {
            advertiser_id: advertiserId,
            drama_id: dramaId,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  return (
    <>
      {query.error && (!query.data || isForbidden(query.error)) ? (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <SubmissionTable
            rows={query.data?.items || []}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={false}
            emptyTitle={excluded ? "没有排除组合" : "没有符合筛选的组合"}
            columns={[
              {
                header: "剧目 / 账户",
                cell: ({ row: { original: r } }) => (
                  <div className="flex flex-col gap-1">
                    <span className="font-semibold">{r.title}</span>
                    <span
                      className="max-w-60 truncate text-sm text-muted-foreground"
                      title={r.account_name || r.advertiser_id}
                    >
                      {r.account_name || r.advertiser_id}
                    </span>
                  </div>
                ),
              },
              {
                header: excluded ? "排除原因" : "素材准备",
                cell: ({ row: { original: r } }) =>
                  excluded ? (
                    <Reasons codes={r.reason_codes} />
                  ) : (
                    <div className="flex flex-col gap-1">
                      <span>
                        已准备 {r.ready_material_count ?? "—"} /{" "}
                        {r.material_count ?? "—"} 次素材分配
                      </span>
                      {!!r.unique_material_count && (
                        <span className="text-xs text-muted-foreground">
                          去重素材 {r.unique_material_count} 个
                        </span>
                      )}
                    </div>
                  ),
              },
              {
                header: "已创建",
                cell: ({ row: { original: r } }) =>
                  r.disposition === "EXCLUDED" ? (
                    <span>此组合未提交</span>
                  ) : (
                    <div className="flex flex-col gap-1 text-sm tabular-nums">
                      <span>
                        广告系列 {r.campaign_step?.remote_id ? 1 : 0} / 1
                      </span>
                      <span>
                        广告组 {r.succeeded_group_count ?? "—"} /{" "}
                        {r.group_count ?? "—"}
                      </span>
                      <span>
                        广告 {r.succeeded_ad_count ?? "—"} / {r.ad_count ?? "—"}
                      </span>
                      {r.structure_summary && (
                        <span className="text-xs text-muted-foreground">
                          {r.structure_summary}
                        </span>
                      )}
                    </div>
                  ),
              },
              {
                header: "结果",
                cell: ({ row: { original: r } }) => (
                  <span>
                    {r.disposition === "EXCLUDED" ? (
                      "已排除"
                    ) : r.result_status ? (
                      <SubmissionBadge status={r.result_status} />
                    ) : r.expanded ? (
                      "等待创建"
                    ) : (
                      "正在展开范围"
                    )}
                  </span>
                ),
              },
              {
                header: "操作",
                cell: ({ row: { original: r } }) => (
                  <Button
                    variant="ghost"
                    data-submission-unit={r.unit_id}
                    onClick={() => setSelected(r)}
                  >
                    查看明细
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
        </>
      )}
      {selected && (
        <SubmissionGroupsSheet
          {...{ tenantId, bcId, submissionId }}
          unit={selected}
          onClose={() => {
            const id = selected.unit_id
            setSelected(null)
            requestAnimationFrame(() =>
              document
                .querySelector<HTMLButtonElement>(
                  `[data-submission-unit="${id}"]`,
                )
                ?.focus(),
            )
          }}
        />
      )}
    </>
  )
}
function SubmissionGroupsSheet({
  tenantId,
  bcId,
  submissionId,
  unit,
  onClose,
}: Scope & { unit: SubmissionUnitPublic; onClose: () => void }) {
  const paging = useSubmissionPaging(
      `submission-groups:${tenantId}:${bcId}:${submissionId}:${unit.unit_id}`,
    ),
    [group, setGroup] = useState<SubmissionGroupPublic | null>(null),
    [mode, setMode] = useState<"ads" | "materials">("ads")
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "groups",
      unit.unit_id,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmissionGroups({
          path: {
            tenant_id: tenantId,
            submission_id: submissionId,
            unit_id: unit.unit_id,
          },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  return (
    <ManagementSheet
      title={`${unit.title} · 搭建明细`}
      description={`${unit.account_name || unit.advertiser_id} · 提交配置与当前创建结果`}
      dirty={false}
      className="sm:max-w-5xl"
      onClose={onClose}
    >
      <div className="flex flex-col gap-4">
        <section
          className="flex flex-col gap-2 text-sm"
          aria-label="广告系列详情"
        >
          <h3 className="font-semibold">广告系列</h3>
          <p className="break-all">{unit.campaign_name}</p>
          <div>
            账户 ID
            <Identifier value={unit.advertiser_id} />
          </div>
          {unit.campaign_step?.remote_id && (
            <div>
              广告系列 ID
              <Identifier value={unit.campaign_step.remote_id} />
            </div>
          )}
          <PlatformState step={unit.campaign_step} />
        </section>
        {query.error && (!query.data || isForbidden(query.error)) ? (
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
        ) : (
          <>
            <ServerTable
              rows={query.data?.items || []}
              loading={query.isPending}
              fetching={query.isFetching}
              error={query.error}
              retry={() => void query.refetch()}
              filtered={false}
              emptyTitle="没有冻结素材组"
              columns={[
                {
                  header: "素材组",
                  cell: ({ row: { original: r } }) => (
                    <div className="flex flex-col gap-1">
                      <span>{r.name}</span>
                      <span className="text-xs text-muted-foreground">
                        第 {r.group_no} 组 · {r.material_count} 份素材 ·{" "}
                        {r.ad_count} 条创意
                      </span>
                      {(r.step?.correction?.remote_id || r.step?.remote_id) && (
                        <Identifier
                          value={
                            r.step?.correction?.remote_id ||
                            r.step?.remote_id ||
                            ""
                          }
                        />
                      )}
                      <PlatformState step={r.step} />
                    </div>
                  ),
                },
                {
                  header: "展开",
                  cell: ({ row: { original: r } }) => (
                    <div className="flex flex-col gap-2">
                      <Button
                        variant="outline"
                        onClick={() => {
                          setGroup(r)
                          setMode("ads")
                        }}
                      >
                        查看 SP 创意
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => {
                          setGroup(r)
                          setMode("materials")
                        }}
                      >
                        查看素材
                      </Button>
                    </div>
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
          </>
        )}
        {group && (
          <section className="flex flex-col gap-3 border-t pt-4">
            <h3 className="font-semibold">
              第 {group.group_no} 组 · {mode === "ads" ? "SP 创意" : "素材准备"}
            </h3>
            {mode === "ads" ? (
              <SubmissionAds
                key={group.group_id}
                {...{ tenantId, bcId, submissionId }}
                unitId={unit.unit_id}
                groupId={group.group_id}
              />
            ) : (
              <SubmissionMaterials
                key={group.group_id}
                {...{ tenantId, bcId, submissionId }}
                unitId={unit.unit_id}
                groupId={group.group_id}
              />
            )}
          </section>
        )}
      </div>
    </ManagementSheet>
  )
}
function SubmissionAds({
  tenantId,
  bcId,
  submissionId,
  unitId,
  groupId,
}: Scope & { unitId: string; groupId: string }) {
  const paging = useSubmissionPaging(
    `submission-ads:${tenantId}:${bcId}:${submissionId}:${unitId}:${groupId}`,
  )
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "ads",
      unitId,
      groupId,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmissionAds({
          path: {
            tenant_id: tenantId,
            submission_id: submissionId,
            unit_id: unitId,
            group_id: groupId,
          },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  return query.error && (!query.data || isForbidden(query.error)) ? (
    <RequestError error={query.error} retry={() => void query.refetch()} />
  ) : (
    <>
      <ServerTable<SubmissionAdPublic>
        rows={query.data?.items || []}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="没有冻结广告"
        columns={[
          {
            header: "SP 创意 / 结果",
            cell: ({ row: { original: r } }) => (
              <div className="flex flex-col gap-3">
                <p className="font-medium">
                  SP{r.creative_no} · {r.name}
                </p>
                {r.included === false && (
                  <p className="text-sm text-destructive">已排除本条广告</p>
                )}
                <Reasons codes={r.reason_codes || []} />
                <p className="whitespace-pre-wrap break-words">{r.text}</p>
                <p className="break-all text-xs">
                  CTA ID：{r.cta_option_ids.join("、") || "未记录"}
                </p>
                {r.step && (
                  <SubmissionBadge
                    status={r.step.correction?.status || r.step.status}
                  />
                )}
                <Reasons
                  codes={
                    !r.step?.correction && r.step?.error_code
                      ? [r.step.error_code]
                      : []
                  }
                />
                {(r.step?.correction?.remote_id || r.step?.remote_id) && (
                  <Identifier
                    value={
                      r.step?.correction?.remote_id || r.step?.remote_id || ""
                    }
                  />
                )}
                <PlatformState step={r.step} />
              </div>
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
    </>
  )
}
function SubmissionMaterials({
  tenantId,
  bcId,
  submissionId,
  unitId,
  groupId,
}: Scope & { unitId: string; groupId: string }) {
  const [preview, setPreview] = useState<SubmissionMaterialPublic | null>(null),
    paging = useSubmissionPaging(
      `submission-materials:${tenantId}:${bcId}:${submissionId}:${unitId}:${groupId}`,
    )
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "materials",
      unitId,
      groupId,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmissionMaterials({
          path: {
            tenant_id: tenantId,
            submission_id: submissionId,
            unit_id: unitId,
            group_id: groupId,
          },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  return (
    <>
      {query.error && (!query.data || isForbidden(query.error)) ? (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <ServerTable
            rows={query.data?.items || []}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={false}
            emptyTitle="此组没有素材"
            columns={[
              {
                header: "素材 / 目标账户记录",
                cell: ({ row: { original: r } }) => (
                  <div className="flex flex-col gap-2">
                    <span>
                      {r.position}. {r.file_name}
                    </span>
                    <Identifier value={r.material_id} />
                    {r.step && <SubmissionBadge status={r.step.status} />}
                    <p className="break-all text-xs">
                      {r.media_type === "IMAGE" ? (
                        <>目标图片 ID：{r.image_id || "暂未确认"}</>
                      ) : (
                        <>
                          目标视频 ID：{r.video_id || "暂未确认"}
                          <br />
                          封面 ID：{r.image_id || "暂未确认"}
                        </>
                      )}
                    </p>
                    <Button
                      variant="outline"
                      disabled={!r.preview_available}
                      onClick={() => setPreview(r)}
                    >
                      预览原件
                    </Button>
                  </div>
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
        </>
      )}
      {preview && (
        <MaterialPreview
          tenantId={tenantId}
          bcId={bcId}
          material={preview}
          onClose={() => setPreview(null)}
        />
      )}
    </>
  )
}
function MaterialPreview({
  tenantId,
  bcId,
  material,
  onClose,
}: {
  tenantId: string
  bcId: string
  material: SubmissionMaterialPublic
  onClose: () => void
}) {
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      "original-preview",
      material.material_id,
    ],
    queryFn: async ({ signal }) =>
      (
        await MaterialsService.readOriginalPreview({
          path: { tenant_id: tenantId, material_id: material.material_id },
          signal,
        })
      ).data,
    gcTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>素材原件预览</DialogTitle>
          <DialogDescription>
            {material.file_name} · 与目标账户素材映射分开核实
          </DialogDescription>
        </DialogHeader>
        {query.error ? (
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
        ) : query.data ? (
          <video
            controls
            preload="metadata"
            className="max-h-96 w-full"
            src={query.data.url}
          >
            <track kind="captions" />
          </video>
        ) : (
          <p role="status">正在读取原件预览…</p>
        )}
      </DialogContent>
    </Dialog>
  )
}
export function SubmissionStepsTable({
  tenantId,
  bcId,
  submissionId,
  advertiserId,
  dramaId,
  result,
  kind,
}: {
  tenantId: string
  bcId: string
  submissionId: string
  advertiserId?: string
  dramaId?: string
  result?: string
  kind?: string
}) {
  const [selected, setSelected] = useState<StepPublic | null>(null),
    paging = useSubmissionPaging(
      `submission-steps:${tenantId}:${bcId}:${submissionId}:${advertiserId || ""}:${dramaId || ""}:${result || ""}:${kind || ""}`,
    )
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "steps",
      advertiserId,
      dramaId,
      result,
      kind,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmissionSteps({
          path: { tenant_id: tenantId, submission_id: submissionId },
          query: {
            advertiser_id: advertiserId,
            drama_id: dramaId,
            result,
            kind,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  return (
    <>
      {query.error && (!query.data || isForbidden(query.error)) ? (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <SubmissionTable
            rows={query.data?.items || []}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={false}
            emptyTitle="没有符合筛选的步骤"
            columns={[
              {
                header: "剧目 / 账户",
                cell: ({ row: { original: r } }) => (
                  <div className="flex flex-col gap-2">
                    <span>{r.title || "剧目名称待读取"}</span>
                    {r.advertiser_id && <Identifier value={r.advertiser_id} />}
                  </div>
                ),
              },
              {
                header: "对象定位",
                cell: ({ row: { original: r } }) => (
                  <span>
                    {stepKinds[r.kind] || r.kind}
                    {r.group_no && ` · 第 ${r.group_no} 组`}
                    {r.creative_no && ` · SP${r.creative_no}`}
                  </span>
                ),
              },
              {
                header: "结果 / 原因",
                cell: ({ row: { original: r } }) => (
                  <div className="flex flex-col gap-2">
                    <SubmissionBadge
                      status={r.correction?.status || r.status}
                    />
                    {r.mismatch && !r.correction && <span>结果差异</span>}
                    <Reasons
                      codes={
                        !r.correction && r.error_code ? [r.error_code] : []
                      }
                    />
                  </div>
                ),
              },
              {
                header: "远端对象 / 状态",
                cell: ({ row: { original: r } }) => (
                  <div className="flex flex-col gap-2">
                    {r.correction?.remote_id || r.remote_id ? (
                      <Identifier
                        value={r.correction?.remote_id || r.remote_id || ""}
                      />
                    ) : (
                      <span>尚无已知远端 ID</span>
                    )}
                    <PlatformState step={r} />
                  </div>
                ),
              },
              {
                header: "操作",
                cell: ({ row: { original: r } }) => (
                  <Button
                    variant="ghost"
                    data-submission-step={r.step_id}
                    onClick={() => setSelected(r)}
                  >
                    查看原因
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
        </>
      )}
      {selected && (
        <ManagementSheet
          title="结果与处理建议"
          description={`${selected.title || selected.unit_id} · ${selected.advertiser_id || ""}`}
          dirty={false}
          className="sm:max-w-5xl"
          onClose={() => {
            const id = selected.step_id
            setSelected(null)
            requestAnimationFrame(() =>
              document
                .querySelector<HTMLButtonElement>(
                  `[data-submission-step="${id}"]`,
                )
                ?.focus(),
            )
          }}
        >
          <div className="flex flex-col gap-4">
            <h3 className="font-semibold">
              {stepKinds[selected.kind] || selected.kind} ·{" "}
              {stepStates[selected.correction?.status || selected.status] ||
                selected.status}
            </h3>
            {selected.correction && (
              <p className="text-sm">
                原尝试结果：{stepStates[selected.status] || selected.status}
              </p>
            )}
            <Reasons codes={selected.error_code ? [selected.error_code] : []} />
            <p className="text-sm">
              {selected.correction
                ? "补建已核实，原尝试保留用于审计。"
                : selected.status === "UNKNOWN" || selected.mismatch
                  ? "先确认是否已创建，避免重复创建。请使用当前任务的核查入口。"
                  : selected.status === "FAILED" ||
                      selected.status === "RETRYABLE"
                    ? "已创建部分保持现状。仅在服务端确认可恢复时，使用任务的重试入口；需要修改素材或配置时请生成新预览。"
                    : "此处展示已记录的创建和核查事实。"}
            </p>
            {(selected.correction?.remote_id || selected.remote_id) && (
              <Identifier
                value={
                  selected.correction?.remote_id || selected.remote_id || ""
                }
              />
            )}
            <PlatformState step={selected} />
            <HistoricalReadAction
              key={selected.step_id}
              tenantId={tenantId}
              bcId={bcId}
              submissionId={submissionId}
              stepId={selected.step_id}
              eligible={selected.can_historical_read === true}
            />
            <SubmissionEventsTable
              {...{ tenantId, bcId, submissionId }}
              stepId={selected.step_id}
            />
          </div>
        </ManagementSheet>
      )}
    </>
  )
}
export function SubmissionEventsTable({
  tenantId,
  bcId,
  submissionId,
  stepId,
}: Scope & { stepId?: string }) {
  const paging = useSubmissionPaging(
    `submission-events:${tenantId}:${bcId}:${submissionId}:${stepId || ""}`,
  )
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      submissionId,
      "events",
      stepId,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmissionEvents({
          path: { tenant_id: tenantId, submission_id: submissionId },
          query: {
            step_id: stepId,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  return query.error && (!query.data || isForbidden(query.error)) ? (
    <RequestError error={query.error} retry={() => void query.refetch()} />
  ) : (
    <>
      <ServerTable<SubmissionEventPublic>
        rows={query.data?.items || []}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="尚无已记录的操作证据"
        columns={[
          {
            header: "记录时间",
            cell: ({ row: { original: r } }) => displayTime(r.observed_at),
          },
          {
            header: "剧目 / 账户",
            cell: ({ row: { original: r } }) => (
              <div className="flex max-w-72 flex-col gap-1 whitespace-normal">
                <span className="font-medium">
                  {r.title || "剧目名称待读取"}
                </span>
                <span
                  className="truncate text-sm text-muted-foreground"
                  title={r.account_name || r.advertiser_id}
                >
                  {r.account_name || r.advertiser_id}
                </span>
              </div>
            ),
          },
          {
            header: "操作记录",
            cell: ({ row: { original: r } }) => (
              <div className="flex flex-col gap-1">
                <span>
                  {eventKind(r.kind)} · {eventConclusion(r.conclusion)}
                </span>
                {r.remote_message && (
                  <p className="max-w-xl whitespace-pre-wrap break-words text-sm">
                    {r.remote_message}
                  </p>
                )}
                {r.remote_code != null && (
                  <span className="text-xs text-muted-foreground">
                    TikTok 错误码：{r.remote_code}
                  </span>
                )}
                {r.request_id && (
                  <div className="text-xs">
                    <span>TikTok 请求编号</span>
                    <Identifier value={r.request_id} />
                  </div>
                )}
                <details>
                  <summary className="cursor-pointer text-xs text-muted-foreground">
                    技术记录
                  </summary>
                  <div className="mt-2 flex flex-col gap-1 text-xs">
                    <span>
                      {r.conclusion} · 尝试序号 {r.attempt}
                    </span>
                    <span>步骤编号</span>
                    <Identifier value={r.step_id} />
                    <span>记录编号</span>
                    <Identifier value={r.evidence_id} />
                    <span>账户编号</span>
                    <Identifier value={r.advertiser_id} />
                  </div>
                </details>
              </div>
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
    </>
  )
}
export function SubmissionDramaPicker({
  tenantId,
  bcId,
  previewId,
  value,
  onChange,
}: {
  tenantId: string
  bcId: string
  previewId: string
  value?: string
  onChange: (id?: string) => void
}) {
  const [open, setOpen] = useState(false),
    [label, setLabel] = useState(""),
    paging = useSubmissionPaging(
      `submission-drama-picker:${tenantId}:${bcId}:${previewId}`,
    )
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      "drama-picker",
      previewId,
      paging.cursor,
      paging.limit,
    ],
    enabled: open,
    queryFn: async ({ signal }) =>
      (
        await BuildsService.previewDramas({
          path: { tenant_id: tenantId, preview_id: previewId },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  return (
    <>
      <Button type="button" variant="outline" onClick={() => setOpen(true)}>
        {value ? label || "已筛选剧目" : "筛选剧目"}
      </Button>
      {value && (
        <Button
          type="button"
          variant="ghost"
          onClick={() => onChange(undefined)}
        >
          清除剧目
        </Button>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>选择冻结剧目</DialogTitle>
            <DialogDescription>
              仅查看当前任务原冻结范围中的剧目。
            </DialogDescription>
          </DialogHeader>
          {query.error && (!query.data || isForbidden(query.error)) ? (
            <RequestError
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : (
            <>
              <ServerTable
                rows={query.data?.items || []}
                loading={query.isPending}
                fetching={query.isFetching}
                error={query.error}
                retry={() => void query.refetch()}
                filtered={false}
                emptyTitle="没有冻结剧目"
                columns={[
                  { header: "剧目", accessorKey: "title" },
                  {
                    header: "筛选",
                    cell: ({ row: { original: r } }) => (
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => {
                          setLabel(r.title)
                          onChange(r.drama_id)
                          setOpen(false)
                        }}
                      >
                        查看该剧
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
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
export function SubmissionFilterBar({
  tenantId,
  bcId,
  previewId,
  advertiserId,
  dramaId,
  onAccount,
  onDrama,
  kind,
  result,
  onKind,
  onResult,
}: {
  tenantId: string
  bcId: string
  previewId: string
  advertiserId?: string
  dramaId?: string
  onAccount: (v: string) => void
  onDrama: (v?: string) => void
  kind?: string
  result?: string
  onKind?: (v: string) => void
  onResult?: (v: string) => void
}) {
  const [input, setInput] = useState(advertiserId || "")
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(e) => {
        e.preventDefault()
        e.stopPropagation()
        onAccount(input)
      }}
    >
      <Field className="w-full sm:w-72">
        <FieldLabel htmlFor="submission-account-filter">账户筛选</FieldLabel>
        <Input
          id="submission-account-filter"
          placeholder="完整账户 ID"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </Field>
      <Button type="submit" variant="outline">
        筛选账户
      </Button>
      <SubmissionDramaPicker
        {...{ tenantId, bcId, previewId }}
        value={dramaId}
        onChange={onDrama}
      />
      {onKind && (
        <FilterSelect
          label="步骤阶段"
          value={kind || "all"}
          onChange={(v) => onKind(v === "all" ? "" : v)}
          choices={stepKinds}
        />
      )}
      {onResult && (
        <FilterSelect
          label="创建结果筛选"
          value={result || "all"}
          onChange={(v) => onResult(v === "all" ? "" : v)}
          choices={{
            FAILED: "确定失败",
            UNKNOWN: "结果待核实",
            MISMATCH: "结果差异",
            RETRYABLE: "可重试",
            PENDING: "待处理",
            RUNNING: "处理中",
            SUCCEEDED: "已成功",
          }}
        />
      )}
      <Button
        type="button"
        variant="ghost"
        onClick={() => {
          setInput("")
          onAccount("")
          onDrama(undefined)
          onKind?.("")
          onResult?.("")
        }}
      >
        清除明细筛选
      </Button>
    </form>
  )
}

function Reasons({ codes }: { codes: string[] }) {
  return (
    <>
      {codes.map((code) => (
        <BuildReason key={code} code={code} />
      ))}
    </>
  )
}
