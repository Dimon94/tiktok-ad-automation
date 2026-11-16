import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useState } from "react"
import {
  type AdMaterialPublic,
  AdsReportingService,
  type EntityRef,
  type MaterialUsePublic,
  type ReportRow,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Empty, EmptyDescription } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { type ColumnDef, formatMetric } from "../metricCatalog"
import { SortableHead } from "../SortableHead"
import { SourceEvidence } from "../SourceEvidence"
import { useVideoPreview, VideoPreviewPlayer } from "./MaterialVideoPreview"
import type { LinkedTarget } from "./useLinkedRows"

const STATUS: Record<string, string> = {
  ENABLE: "已开启",
  DISABLE: "已暂停",
  DELETE: "已删除",
}

function statusText(status: string | null | undefined) {
  return status ? (STATUS[status] ?? status) : "—"
}

/** 素材维度详情：只列出该素材的使用位置（数据在素材行本身）。 */
export function MaterialsTab({
  materials,
  row,
  onOpenDirectory,
  onOpenLinked,
}: {
  row: ReportRow
  materials: MaterialUsePublic[]
  onOpenDirectory: (ref: EntityRef) => void
  onOpenLinked: (target: LinkedTarget) => void
}) {
  if (row.material_instances?.length || row.reconciliation_rows?.length)
    return (
      <MaterialInstanceEvidence
        row={row}
        onOpenDirectory={onOpenDirectory}
        onOpenLinked={onOpenLinked}
      />
    )
  if (!materials.length)
    return (
      <p className="text-sm text-muted-foreground">
        暂无素材使用记录；外部广告仍保留。
      </p>
    )
  return (
    <div className="overflow-x-auto rounded-md border">
      <p className="p-3 text-sm text-muted-foreground">
        当前结果未形成可精确展开的单 creative 实例；下方仅为使用位置。
      </p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>账户</TableHead>
            <TableHead>广告</TableHead>
            <TableHead>名称</TableHead>
            <TableHead>状态</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {materials.map((material) => (
            <TableRow
              key={`${material.use_ref.ad_ref.advertiser_id}:${material.use_ref.ad_ref.remote_id}:${material.use_ref.ad_material_id ?? ""}`}
            >
              <TableCell>{material.use_ref.ad_ref.advertiser_id}</TableCell>
              <TableCell>{material.use_ref.ad_ref.remote_id}</TableCell>
              <TableCell className="max-w-64 truncate" title={material.name}>
                {material.name || material.use_ref.platform_material_id}
              </TableCell>
              <TableCell>{statusText(material.operation_status)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

const AD_MATERIAL_COLUMNS = new Set([
  "spend",
  "impressions",
  "clicks",
  "revenue_d0",
  "roas_d0",
  "revenue_total",
  "roas_total",
])

/**
 * 广告详情的素材：每个素材在这条广告里的创意报表数据，
 * 勾选后只启停本广告里的这些素材，不影响同一视频在其他广告的投放。
 */
export function AdMaterialsTab({
  advertiserId,
  adId,
  startDate,
  endDate,
  columns,
  readonly,
  onToggle,
}: {
  advertiserId: string
  adId: string
  startDate: string
  endDate: string
  columns: ColumnDef[]
  readonly: boolean
  onToggle: (adMaterialIds: string[], value: "ENABLE" | "DISABLE") => void
}) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // 正在预览的素材：本广告所在账户就是读取成片的账户，不需要再找候选账户。
  const preview = useVideoPreview()
  const [previewing, setPreviewing] = useState<AdMaterialPublic | null>(null)
  const startPreview = (item: AdMaterialPublic) => {
    setPreviewing(item)
    preview.mutation.mutate({
      videoId: item.use_ref.platform_material_id,
      advertiserIds: [advertiserId],
    })
  }
  const [sort, setSort] = useState<{ by: string; direction: "asc" | "desc" }>({
    by: "spend",
    direction: "desc",
  })
  const onSort = (by: string) =>
    setSort((current) => ({
      by,
      direction:
        current.by === by && current.direction === "desc" ? "asc" : "desc",
    }))
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "ad-materials",
      bcId,
      advertiserId,
      adId,
      startDate,
      endDate,
    ],
    enabled: Boolean(tenantId && bcId && advertiserId && adId),
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adMaterials({
          path: { tenant_id: tenantId!, remote_id: adId },
          query: {
            advertiser_id: advertiserId,
            bc_id: bcId!,
            start_date: startDate,
            end_date: endDate,
          },
          signal,
        })
      ).data,
  })
  if (query.isLoading) return <Skeleton className="h-40 w-full" />
  if (query.isError)
    return (
      <div className="flex items-center gap-3 text-sm">
        <p className="text-destructive">素材数据读取失败</p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void query.refetch()}
        >
          重试
        </Button>
      </div>
    )
  const items = query.data?.items ?? []
  const creativeRows = query.data?.creative_rows ?? []
  const coverage = query.data?.coverage
  // 响应级证据独立于创意事实与当前使用位置；空行不能解释为零金额。
  const coverageWarning =
    coverage?.status === "INCOMPLETE" ||
    typeof coverage?.unallocated_note === "string" ? (
      <Alert role="status" data-testid="ad-material-coverage">
        <AlertDescription>
          <p>素材报表覆盖不完整，当前金额无法完整归属。</p>
          {typeof coverage?.unallocated_note === "string" && (
            <p>{coverage.unallocated_note}</p>
          )}
        </AlertDescription>
      </Alert>
    ) : null
  if ((query.data?.carousel_creatives?.length ?? 0) > 0)
    return (
      <div className="space-y-3">
        {coverageWarning}
        <Badge variant="secondary">图片轮播</Badge>
        <p className="text-sm text-muted-foreground">
          每个轮播创意展示一份消耗与收入，金额不分配到单张图片。
        </p>
        <CreativeFacts rows={creativeRows} />
        {!query.data?.carousel_linkage_complete && (
          <p className="text-sm text-muted-foreground">
            轮播关联不完整：缺少图片顺序或曲目证据，创意报表金额仍可查看。
          </p>
        )}
        {(query.data?.carousel_creatives ?? []).map((creative, index) => (
          <section
            className="space-y-1 rounded-md border p-3 text-sm"
            key={`${index}:${creative.remote_creative_id ?? ""}`}
          >
            <div className="font-medium">
              轮播 {index + 1} · {creative.ordered_image_ids.length} 张图片
            </div>
            <div className="break-all text-muted-foreground">
              图片顺序：{creative.ordered_image_ids.join(" → ")}
            </div>
            <div>曲目：{creative.music_id}</div>
            <div className="text-muted-foreground">
              平台创意 ID：{creative.remote_creative_id ?? "未返回"}
            </div>
          </section>
        ))}
        {(query.data?.frozen_creative_links ?? []).map((link) => (
          <div key={link.planned_ad_id} className="space-y-1">
            <Button variant="outline" size="sm" asChild>
              <Link
                to="/tenants/$tenantId/build-tasks/$submissionId"
                params={{
                  tenantId: tenantId!,
                  submissionId: link.submission_id,
                }}
                search={{ tab: "details" }}
              >
                查看冻结创意与投放任务
              </Link>
            </Button>
            <p className="text-xs text-muted-foreground">
              冻结内容为提交时的原始意图，不代表当前远端曲目已重新验证。
            </p>
          </div>
        ))}
      </div>
    )
  if (!items.length)
    return (
      <div className="flex flex-col gap-3">
        {coverageWarning}
        <CreativeFacts rows={creativeRows} />
        {!creativeRows.length && !coverageWarning && (
          <Empty>
            <EmptyDescription>
              所选日期暂无创意报表记录，也无当前素材使用位置。
            </EmptyDescription>
          </Empty>
        )}
      </div>
    )
  const shown = columns.filter((column) => AD_MATERIAL_COLUMNS.has(column.key))
  // 接口一次返回本广告全部素材；按展示所用的原始数值排序，避免字符串/格式化值误排。
  // 缺失或非有限数值在两个方向都置底；相同值保持接口顺序，且不修改查询缓存。
  const metricValue = (item: AdMaterialPublic) => {
    const raw = item.metric_buckets?.[0]?.values?.[sort.by]
    if (raw == null || raw.trim() === "") return null
    const value = Number(raw)
    return Number.isFinite(value) ? value : null
  }
  const sortedItems = [...items].sort((left, right) => {
    const a = metricValue(left)
    const b = metricValue(right)
    if (a === null) return b === null ? 0 : 1
    if (b === null) return -1
    return sort.direction === "asc" ? a - b : b - a
  })
  const key = (item: AdMaterialPublic) => item.use_ref.ad_material_id ?? ""
  const selectable = items.filter((item) => key(item))
  const allChecked =
    selectable.length > 0 && selectable.every((item) => selected.has(key(item)))
  const toggle = (id: string, checked: boolean) =>
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  return (
    <div className="space-y-3">
      {coverageWarning}
      <h3 className="text-sm font-medium">创意报表金额（本广告）</h3>
      <CreativeFacts rows={creativeRows} />
      <h3 className="text-sm font-medium">当前素材使用位置</h3>
      {previewing && (
        <section className="flex flex-col gap-2 rounded-md border p-3">
          <div className="flex items-center justify-between gap-2">
            <h3
              className="truncate text-sm font-medium"
              title={previewing.name}
            >
              成片：
              {previewing.name || previewing.use_ref.platform_material_id}
            </h3>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => {
                setPreviewing(null)
                preview.mutation.reset()
              }}
            >
              收起
            </Button>
          </div>
          {preview.mutation.isPending && (
            <p className="text-sm text-muted-foreground">正在读取预览…</p>
          )}
          <VideoPreviewPlayer preview={preview} />
        </section>
      )}
      {!readonly && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">
            已选 {selected.size} 个素材（只作用于本广告）
          </span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={selected.size === 0}
            onClick={() => onToggle([...selected], "ENABLE")}
          >
            启用
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={selected.size === 0}
            onClick={() => onToggle([...selected], "DISABLE")}
          >
            暂停
          </Button>
        </div>
      )}
      <div className="overflow-x-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {!readonly && (
                <TableHead className="w-10">
                  <Checkbox
                    aria-label="全选素材"
                    checked={allChecked}
                    onCheckedChange={(checked) =>
                      setSelected(
                        checked ? new Set(selectable.map(key)) : new Set(),
                      )
                    }
                  />
                </TableHead>
              )}
              <TableHead>名称</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>成片</TableHead>
              {shown.map((column) => (
                <TableHead
                  key={column.key}
                  title={column.fullName}
                  aria-sort={
                    sort.by === column.key
                      ? sort.direction === "asc"
                        ? "ascending"
                        : "descending"
                      : "none"
                  }
                >
                  <SortableHead
                    label={column.label}
                    sortBy={column.key}
                    activeSortBy={sort.by}
                    direction={sort.direction}
                    onSort={onSort}
                  />
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {sortedItems.map((item) => {
              const id = key(item)
              const vector = item.metric_buckets?.[0]
              return (
                <TableRow key={`${item.use_ref.platform_material_id}:${id}`}>
                  {!readonly && (
                    <TableCell>
                      <Checkbox
                        aria-label={`选择 ${item.name}`}
                        disabled={!id}
                        checked={selected.has(id)}
                        onCheckedChange={(checked) =>
                          toggle(id, checked === true)
                        }
                      />
                    </TableCell>
                  )}
                  <TableCell className="max-w-64" title={item.name}>
                    <div className="truncate">
                      {item.name || item.use_ref.platform_material_id}
                    </div>
                  </TableCell>
                  <TableCell>{statusText(item.operation_status)}</TableCell>
                  <TableCell>
                    {/* 图片素材没有成片可播放。 */}
                    {isVideo(item) && preview.ready && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        aria-label={`预览 ${item.name || item.use_ref.platform_material_id}`}
                        disabled={preview.mutation.isPending}
                        onClick={() => startPreview(item)}
                      >
                        预览
                      </Button>
                    )}
                  </TableCell>
                  {shown.map((column) => (
                    <TableCell key={column.key} className="tabular-nums">
                      {formatMetric(
                        vector?.values?.[column.key] ?? null,
                        column.unit,
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

function isVideo(item: AdMaterialPublic) {
  return item.use_ref.material_type.toUpperCase().includes("VIDEO")
}

/** 无当前可操作位置、未知身份和轮播仍保留经济事实与完整图片清单。 */
function CreativeFacts({ rows }: { rows: ReportRow[] }) {
  if (!rows.length) return null
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <section className="rounded-md border p-3 text-sm" key={row.row_key}>
          <div className="font-medium">{row.display?.name}</div>
          {row.metric_buckets?.map((bucket) => (
            <div key={bucket.currency}>
              {bucket.currency} · {bucket.timezone} · 消耗{" "}
              {bucket.values?.spend ?? "未知"} · 收入{" "}
              {bucket.values?.revenue_total ?? "未知"}
              {bucket.availability?.spend === "PARTIAL" && (
                <Badge variant="outline" className="ml-2">
                  部分数据
                </Badge>
              )}
            </div>
          ))}
          <CreativeEvidence row={row} />
        </section>
      ))}
    </div>
  )
}

export function CreativeEvidence({ row }: { row: ReportRow }) {
  const reason = row.coverage?.reason
  const labels: Record<string, string> = {
    creative_identity_unmapped: "创意身份尚未关联，金额已保留。",
    group_reconciliation_difference:
      "广告组广告金额与创意金额的有符号差额，未分配给任何素材。",
    composite_creative_not_allocated_to_images:
      "完整轮播创意金额，不拆分到单张图片。",
  }
  const assets = row.coverage?.assets as
    | { creative_id: string; assets: { platform_material_id?: string }[] }[]
    | undefined
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      {typeof reason === "string" && labels[reason] && <p>{labels[reason]}</p>}
      {row.coverage?.status !== "COMPLETE" && (
        <p>数据尚不完整或源报表已过期。</p>
      )}
      {typeof row.coverage?.unallocated_note === "string" && (
        <p>{row.coverage.unallocated_note}</p>
      )}
      {assets?.map((creative) => (
        <p key={creative.creative_id}>
          创意 {creative.creative_id} · 素材顺序：
          {creative.assets
            .map((asset) => asset.platform_material_id ?? "未知")
            .join(" → ")}
        </p>
      ))}
    </div>
  )
}

/** 所有展开内容直接来自当前查询的冻结载荷，不触发实时详情读取。 */
function MaterialInstanceEvidence({
  row,
  onOpenDirectory,
  onOpenLinked,
}: {
  row: ReportRow
  onOpenDirectory: (ref: EntityRef) => void
  onOpenLinked: (target: LinkedTarget) => void
}) {
  const [page, setPage] = useState(0)
  const instances = row.material_instances ?? []
  const shown = instances.slice(page * 10, (page + 1) * 10)
  return (
    <div className="space-y-3" data-testid="material-instance-result">
      <p className="text-sm">
        投放实例 · {instances.filter((i) => i.row_kind === "INSTANCE").length}{" "}
        条；特殊行 · {instances.filter((i) => i.row_kind !== "INSTANCE").length}{" "}
        条
      </p>
      <p className="text-xs text-muted-foreground">
        creative
        收益支持、D0窗口、自然与付费组成及成熟截止尚未确认；缺失值保持未知。
      </p>
      {shown.map((instance) => (
        <section
          key={instance.evidence_digest}
          className="rounded-md border p-3 text-sm space-y-2"
        >
          <h3 className="font-medium">
            {instance.row_kind === "COMPOSITE"
              ? "轮播组合"
              : instance.row_kind === "UNMAPPED_CREATIVE"
                ? "未映射创意"
                : "素材投放实例"}{" "}
            ·{" "}
            {instance.key.platform_material_id ??
              instance.creative_ids.join("、")}
          </h3>
          <p>
            账户 {instance.key.advertiser_id} · 父广告{" "}
            {instance.key.parent_ad_id} · 使用位置{" "}
            {instance.key.ad_material_id ?? "未知"}
          </p>
          <div className="flex flex-wrap gap-2">
            <InstanceNavigation
              identity={instance.key}
              kind="ad"
              remoteId={instance.key.parent_ad_id}
              onOpen={(ref) =>
                onOpenLinked({
                  dimension: "ad",
                  id: ref.remote_id,
                  advertiserId: ref.advertiser_id,
                  name: ref.remote_id,
                })
              }
            />
            {instance.creative_ids.map((id) => (
              <InstanceNavigation
                key={id}
                identity={instance.key}
                kind="creative"
                remoteId={id}
                onOpen={onOpenDirectory}
              />
            ))}
          </div>
          {instance.metric_buckets.map((bucket) => (
            <p key={`${bucket.currency}:${bucket.timezone}`}>
              {bucket.currency} · {bucket.timezone} · 消耗{" "}
              {bucket.values?.spend ?? "未知"} · 收入{" "}
              {bucket.values?.revenue_total ?? "未知"} · ROAS{" "}
              {bucket.values?.roas_total ?? "未知"}（
              {bucket.availability?.roas_total}）
            </p>
          ))}
          <p className="text-muted-foreground">
            覆盖：{String(instance.coverage.status ?? "未知")}
            ；字段证据：{JSON.stringify(instance.field_state_counts ?? {})}
            {instance.identity_reason &&
              `；身份原因：${instance.identity_reason}`}
            {instance.row_kind !== "INSTANCE" && "；特殊行不计入普通实例合计"}
          </p>
          <details>
            <summary className="cursor-pointer">查看来源与冻结身份</summary>
            <pre className="whitespace-pre-wrap break-all text-xs">
              {JSON.stringify(
                {
                  key: instance.key,
                  facts: instance.fact_keys,
                  digest: instance.evidence_digest,
                },
                null,
                2,
              )}
            </pre>
            {instance.metric_buckets.map(
              (bucket) =>
                bucket.evidence_ref && (
                  <SourceEvidence
                    key={JSON.stringify(bucket.evidence_ref.bucket)}
                    reference={bucket.evidence_ref}
                  />
                ),
            )}
          </details>
        </section>
      ))}
      {instances.length > 10 && (
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={page === 0}
            onClick={() => setPage(page - 1)}
          >
            上一页
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={(page + 1) * 10 >= instances.length}
            onClick={() => setPage(page + 1)}
          >
            下一页
          </Button>
        </div>
      )}
      {(row.parent_controls ?? []).map((control) => (
        <section
          key={control.evidence_digest}
          className="rounded-md border p-3 text-sm space-y-1"
        >
          <h3 className="font-medium">
            父广告对照 · {control.key.parent_ad_id}
          </h3>
          <p className="text-muted-foreground">
            FULL_PARENT_AD 完整父广告范围；不计入素材合计。
          </p>
          <InstanceNavigation
            identity={control.key}
            kind="ad"
            remoteId={control.key.parent_ad_id}
            onOpen={(ref) =>
              onOpenLinked({
                dimension: "ad",
                id: ref.remote_id,
                advertiserId: ref.advertiser_id,
                name: ref.remote_id,
              })
            }
          />
          {control.metric_buckets.map((bucket) => (
            <p key={`${bucket.currency}:${bucket.timezone}`}>
              {bucket.currency} · {bucket.timezone} · 消耗{" "}
              {bucket.values?.spend ?? "未知"} · 收入{" "}
              {bucket.values?.revenue_total ?? "未知"} · ROAS{" "}
              {bucket.values?.roas_total ?? "未知"}（
              {bucket.availability?.roas_total}）
            </p>
          ))}
          <details>
            <summary className="cursor-pointer">查看父级来源</summary>
            <pre className="whitespace-pre-wrap break-all text-xs">
              {JSON.stringify(control, null, 2)}
            </pre>
            {control.metric_buckets.map(
              (bucket) =>
                bucket.evidence_ref && (
                  <SourceEvidence
                    key={JSON.stringify(bucket.evidence_ref.bucket)}
                    reference={bucket.evidence_ref}
                  />
                ),
            )}
          </details>
        </section>
      ))}
      {(row.reconciliation_rows ?? []).map((difference, index) => (
        <section
          key={String(difference.evidence_digest ?? index)}
          className="rounded-md border p-3 text-sm"
        >
          <p>
            父级对账：{String(difference.status)} · {String(difference.reason)}
          </p>
          <p className="text-xs text-muted-foreground">
            差额不计入素材合计；Native Growth 口径未确认时不计算收益差额。
          </p>
          {difference.scope === "ADGROUP_RESIDUAL" && (
            <p>广告组有符号残差；不等于父广告对账，不计入素材合计。</p>
          )}
          <p>
            {String(
              difference.currency ??
                (
                  difference.metric_buckets as
                    | { currency?: string }[]
                    | undefined
                )?.[0]?.currency ??
                "",
            )}{" "}
            ·{" "}
            {String(
              difference.timezone ??
                (
                  difference.metric_buckets as
                    | { timezone?: string }[]
                    | undefined
                )?.[0]?.timezone ??
                "",
            )}{" "}
            · 消耗差额{" "}
            {String(
              (
                difference.metric_buckets as
                  | { values?: Record<string, string | null> }[]
                  | undefined
              )?.[0]?.values?.spend ?? "未知",
            )}{" "}
            · 收益差额：口径未确认
          </p>
          <details>
            <summary className="cursor-pointer">
              查看差额、逐字段原因与来源
            </summary>
            <pre className="whitespace-pre-wrap break-all text-xs">
              {JSON.stringify(difference, null, 2)}
            </pre>
          </details>
        </section>
      ))}
      <details>
        <summary className="cursor-pointer text-xs">冻结结果标识</summary>
        <p className="break-all text-xs">{row.evidence_digest}</p>
      </details>
    </div>
  )
}

/** 当前目录仅控制导航可用性；不会重写冻结身份、证据或金额。 */
function InstanceNavigation({
  identity,
  kind,
  remoteId,
  onOpen,
}: {
  identity: Record<string, string | null>
  kind: "ad" | "creative"
  remoteId: string | null | undefined
  onOpen: (ref: EntityRef) => void
}) {
  const { tenantId, bc } = useTenantScope()
  const availableScope = Boolean(
    tenantId &&
      bc?.bc_id &&
      identity.advertiser_id &&
      remoteId &&
      identity.connection_id,
  )
  const ref: EntityRef = {
    tenant_id: tenantId ?? "",
    advertiser_id: identity.advertiser_id ?? "",
    kind,
    remote_id: remoteId ?? "",
  }
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "detail",
      bc?.bc_id,
      "directory",
      ref,
    ],
    enabled: availableScope,
    retry: false,
    staleTime: 15_000,
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adDetail({
          path: { tenant_id: tenantId!, kind, remote_id: remoteId! },
          query: { bc_id: bc!.bc_id, advertiser_id: ref.advertiser_id },
          signal,
        })
      ).data,
  })
  const label = kind === "ad" ? "父广告" : "creative"
  if (!remoteId)
    return (
      <p className="text-xs text-muted-foreground">
        {label}不可导航：缺少 ID。
      </p>
    )
  if (!availableScope)
    return (
      <p className="text-xs text-muted-foreground">
        {label} {remoteId} 不可导航：身份来源或账户范围未确认。
      </p>
    )
  if (query.isPending)
    return (
      <p className="text-xs text-muted-foreground">
        正在核对 {label} {remoteId} 的当前目录…
      </p>
    )
  if (
    query.isError ||
    !query.data ||
    query.data.statuses?.operation === "DELETE"
  )
    return (
      <p className="text-xs text-muted-foreground">
        {label} {remoteId}{" "}
        不可导航：当前目录未同步、已删除或无读取权限；冻结记录保留。
      </p>
    )
  return (
    <Button size="sm" variant="outline" onClick={() => onOpen(ref)}>
      查看{kind === "creative" ? " " : ""}
      {label} · {remoteId}
    </Button>
  )
}
