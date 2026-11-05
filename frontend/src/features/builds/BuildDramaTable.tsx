import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useRef, useState } from "react"
import {
  BuildsService,
  type DraftDramaPublic,
  type DraftInputPublic,
  type DraftSummary,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { FilterSelect } from "@/features/accounts/presentation"
import { Pager, ServerTable, useCursorPage } from "@/features/tenants/shared"
import { buildKey } from "./api"
import { canChooseDrama, DramaInputSheet } from "./DramaInputSheet"
import { imageApi, imageKey, imagePath } from "./imageCompositionApi"
import { useImageCursorPage } from "./imageWorkspaceState"
import { ManualLinkSheet } from "./ManualLinkSheet"
import { materialWaitingLabel } from "./preparationProgress"
import { BuildReason, BuildStatus } from "./presentation"

function linkLabel(input: DraftInputPublic, status: DraftSummary["status"]) {
  const progress = input.preparation
  const state = progress?.link_status || input.status
  const labels: Record<string, string> = {
    ready: "已获取",
    duplicate: "重复输入",
    empty: "空输入",
    invalid: "输入无效",
    needs_resolution: "待选择剧目",
    not_found: "未找到剧目",
    failed: "获取失败",
    result_unknown: "结果待核实",
    retryable_error: "暂未获取，等待重试",
    blocked_auth: "连接需要重新认证",
    config_conflict: "链接配置需处理",
    checking: "正在核对链接…",
    creating: "正在获取链接…",
    verifying: "正在核实链接…",
  }
  if (
    status === "BLOCKED" &&
    ["pending", "resolving", "checking", "creating", "verifying"].includes(
      state,
    )
  )
    return "准备已暂停"
  if (labels[state]) return labels[state]
  // 只有确认正式剧名后才称为取链，排队及解析期间不提前宣称解析成功。
  if (["pending", "unresolved", "resolved", "resolving"].includes(state)) {
    if (status === "DRAFT") return "等待解析"
    if (status !== "PREPARING") return "准备已暂停"
    return progress?.title ? "正在获取链接…" : "正在解析剧目…"
  }
  return "待处理"
}

export function BuildDramaTable({
  tenantId,
  bcId,
  summary,
  onMaterial,
  selectedDramas,
  onSelectDramas,
  onLink,
  write,
  onChanged,
  onEdit,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  selectedDramas: Record<string, string>
  onSelectDramas: (selected: Record<string, string>) => void
  onMaterial: (drama: DraftDramaPublic) => void
  onLink: (id: string) => void
  write: boolean
  onChanged: () => void
  onEdit: () => void
}) {
  const [manualInput, setManualInput] = useState<DraftInputPublic | null>(null)
  const paging = useCursorPage()
  const [detailId, setDetailId] = useState<string | null>(null)
  const query = useQuery({
    // 状态变化仍沿用同一版本的数据，避免 PREPARING → READY 清空表格。
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      summary.revision,
      "drama-progress",
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.inputs({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: { kind: "drama", cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
    refetchInterval: summary.status === "PREPARING" || detailId ? 2000 : false,
  })
  const previousStatus = useRef(summary.status)
  const refetch = query.refetch
  useEffect(() => {
    if (previousStatus.current !== summary.status) {
      previousStatus.current = summary.status
      void refetch()
    }
  }, [summary.status, refetch])
  const detail = query.data?.items.find((input) => input.id === detailId)
  const [showInputs, setShowInputs] = useState(
    summary.media_kind !== "CAROUSEL" || summary.status !== "READY",
  )
  const [issue, setIssue] = useState<"all" | "music" | "unused" | "protected">(
    "all",
  )
  const imageState = useQuery({
    queryKey: imageKey(tenantId, summary.draft_id),
    queryFn: async ({ signal }) =>
      (
        await imageApi.compositionState({
          path: imagePath(tenantId, summary.draft_id),
          signal,
        })
      ).data,
    enabled: summary.media_kind === "CAROUSEL",
    refetchInterval: 2000,
  })
  const imageRevision = Math.max(
    summary.revision,
    imageState.data?.revision ?? 0,
  )
  const imageGeneration = imageState.data?.active_generation ?? 0
  const imagePaging = useImageCursorPage(
    `${summary.draft_id}:${imageRevision}:${imageGeneration}:${issue}`,
  )
  const images = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      "image-dramas",
      imageRevision,
      imageGeneration,
      issue,
      imagePaging.cursor,
    ],
    enabled: summary.media_kind === "CAROUSEL",
    queryFn: async ({ signal }) =>
      (
        await BuildsService.imageDramas({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: {
            issue,
            cursor: imagePaging.cursor,
            limit: imagePaging.limit,
          },
          signal,
        })
      ).data,
    refetchInterval: summary.media_kind === "CAROUSEL" ? 2000 : false,
  })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {summary.media_kind === "CAROUSEL" && (
        <>
          <FilterSelect
            allLabel="全部剧目"
            label="只处理问题"
            value={issue}
            onChange={(v) => {
              setIssue(v as typeof issue)
              imagePaging.reset()
            }}
            choices={{
              music: "待配乐",
              unused: "有未使用图片",
              protected: "有手动调整",
            }}
          />
          <ServerTable
            rows={images.data?.items ?? []}
            loading={images.isPending}
            fetching={images.isFetching}
            error={images.error}
            retry={() => void images.refetch()}
            filtered={issue !== "all"}
            emptyTitle="没有符合条件的图片剧目"
            columns={[
              {
                header: "选择",
                cell: ({ row }) => (
                  <Checkbox
                    aria-label={`选择剧目 ${row.original.title}`}
                    checked={!!selectedDramas[row.original.drama_id]}
                    disabled={
                      !write ||
                      (!selectedDramas[row.original.drama_id] &&
                        Object.keys(selectedDramas).length >= 100)
                    }
                    onCheckedChange={(checked) => {
                      const next = { ...selectedDramas }
                      if (checked)
                        next[row.original.drama_id] = row.original.title
                      else delete next[row.original.drama_id]
                      onSelectDramas(next)
                    }}
                  />
                ),
              },
              {
                header: "剧目",
                cell: ({ row }) => (
                  <div className="flex flex-col gap-1">
                    <span>{row.original.title}</span>
                    <span className="text-xs text-muted-foreground">
                      {row.original.material_identifier
                        ? `按素材标识：${row.original.material_identifier}`
                        : "按剧名匹配"}
                    </span>
                  </div>
                ),
              },
              {
                header: "规则来源",
                cell: ({ row }) =>
                  Object.keys(
                    imageState.data?.drama_patches[row.original.drama_id] ?? {},
                  ).length
                    ? "有覆盖"
                    : "整批",
              },
              { header: "候选图片", accessorKey: "candidate_count" },
              { header: "轮播 / 单图", accessorKey: "unit_count" },
              {
                header: "待处理",
                cell: ({ row }) => (
                  <span>
                    待选 {row.original.selection_count} · 配乐中{" "}
                    {row.original.pending_count} · 失败{" "}
                    {row.original.failed_count}
                  </span>
                ),
              },
              { header: "未使用图片", accessorKey: "unused_count" },
              { header: "手动保护", accessorKey: "protected_count" },
              {
                header: "推广链接",
                cell: ({ row }) => (
                  <Button
                    variant="link"
                    onClick={() => onLink(row.original.link_id)}
                  >
                    查看链接
                  </Button>
                ),
              },
              {
                header: "操作",
                cell: ({ row }) => (
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      onClick={() => onMaterial(row.original)}
                    >
                      查看与调整轮播
                    </Button>
                    <Button variant="ghost" onClick={onEdit}>
                      修改输入与链接
                    </Button>
                  </div>
                ),
              },
            ]}
          />
          <Pager
            paging={imagePaging}
            nextCursor={images.data?.next_cursor}
            total={images.data?.total}
            busy={images.isFetching}
          />
        </>
      )}
      {summary.media_kind === "CAROUSEL" && (
        <Button variant="outline" onClick={() => setShowInputs(!showInputs)}>
          {showInputs ? "收起" : "查看"}输入解析与推广链接
        </Button>
      )}
      {showInputs && (
        <ServerTable
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          rows={query.data?.items || []}
          loading={query.isPending}
          columns={(
            [
              {
                header: "剧目 / 输入行",
                cell: ({ row }) => (
                  <div>
                    <span>
                      {row.original.preparation?.title ||
                        row.original.raw_text ||
                        "（空行）"}
                    </span>
                    <p className="text-xs text-muted-foreground">
                      第 {row.original.line_no} 行
                    </p>
                  </div>
                ),
              },
              {
                header: "版权方剧目 ID",
                cell: ({ row }) => (
                  <span className="break-all">
                    {row.original.preparation?.display_drama_id || "未提供"}
                  </span>
                ),
              },
              {
                header: "推广链接",
                cell: ({ row }) => {
                  const input = row.original
                  const progress = input.preparation
                  return (
                    <div>
                      {progress?.link_status === "ready" && progress.drama ? (
                        <Button
                          variant="ghost"
                          onClick={() => onLink(progress.drama!.link_id)}
                        >
                          {input.manual_link?.url ? "已填写" : "已获取"}
                        </Button>
                      ) : (
                        <span>{linkLabel(input, summary.status)}</span>
                      )}
                      {(progress?.reason_code ||
                        input.reason_code ||
                        input.duplicate_of != null) && (
                        <p className="text-xs text-muted-foreground">
                          <BuildReason
                            code={progress?.reason_code || input.reason_code}
                          />
                          {input.duplicate_of != null &&
                            `合并到第 ${input.duplicate_of} 行`}
                        </p>
                      )}
                    </div>
                  )
                },
              },
              {
                header: "匹配素材",
                cell: ({ row }) => {
                  const drama = row.original.preparation?.drama
                  // 未开始匹配与匹配后零素材是两种结果。
                  const identifier =
                    row.original.manual_link?.material_identifier
                  const count =
                    drama && drama.material_state !== "pending"
                      ? drama.matched_count
                      : "—"
                  return (
                    <div className="flex flex-col gap-1">
                      <span>{count}</span>
                      <span className="text-xs text-muted-foreground">
                        {identifier
                          ? `按素材标识：${identifier}`
                          : "按剧名匹配"}
                      </span>
                    </div>
                  )
                },
              },
              {
                header: "素材准备",
                cell: ({ row }) => {
                  const drama = row.original.preparation?.drama
                  if (!drama || drama.material_state === "pending")
                    return materialWaitingLabel(row.original, summary)
                  if (drama.material_state === "matching") return "正在匹配…"
                  return <BuildStatus value={drama.material_state} />
                },
              },
              {
                header: "操作",
                cell: ({ row }) => {
                  const input = row.original
                  const drama = input.preparation?.drama
                  return (
                    <div className="flex flex-wrap gap-1">
                      {write &&
                        input.raw_text.trim() &&
                        !["empty", "duplicate", "invalid"].includes(
                          input.status,
                        ) && (
                          <Button
                            variant="ghost"
                            onClick={() => setManualInput(input)}
                          >
                            {input.manual_link?.url || input.preparation?.url
                              ? "修改推广链接"
                              : "补充推广链接"}
                          </Button>
                        )}
                      {summary.media_kind !== "CAROUSEL" &&
                        (drama ||
                          (input.raw_text.trim() &&
                            !["empty", "duplicate", "invalid"].includes(
                              input.status,
                            ))) && (
                          <Button
                            variant="ghost"
                            disabled={!drama}
                            onClick={() => drama && onMaterial(drama)}
                          >
                            查看与调整素材
                          </Button>
                        )}
                      <Button
                        variant="ghost"
                        onClick={() => setDetailId(input.id)}
                      >
                        {write && canChooseDrama(input)
                          ? "选择剧目"
                          : "输入详情"}
                      </Button>
                      {write &&
                        ["failed", "invalid", "not_found"].includes(
                          input.preparation?.link_status || input.status,
                        ) && (
                          <Button variant="ghost" onClick={onEdit}>
                            返回修正
                          </Button>
                        )}
                    </div>
                  )
                },
              },
            ] satisfies ColumnDef<DraftInputPublic>[]
          ).filter(
            (column) =>
              summary.media_kind !== "CAROUSEL" ||
              !["匹配素材", "素材准备"].includes(String(column.header)),
          )}
          emptyTitle="尚未输入剧目"
        />
      )}
      {manualInput && (
        <ManualLinkSheet
          tenantId={tenantId}
          summary={summary}
          input={manualInput}
          onClose={() => setManualInput(null)}
          onSaved={onChanged}
        />
      )}
      {detail && (
        <DramaInputSheet
          key={detail.id}
          tenantId={tenantId}
          input={detail}
          write={write}
          onClose={() => setDetailId(null)}
          onChanged={() => {
            void query.refetch()
            onChanged()
          }}
          onRefresh={query.refetch}
        />
      )}
      {showInputs && (
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          total={query.data?.total}
          busy={query.isFetching}
        />
      )}
    </div>
  )
}
