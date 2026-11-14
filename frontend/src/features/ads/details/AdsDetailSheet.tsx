import { useQuery } from "@tanstack/react-query"
import { ArrowLeft } from "lucide-react"
import type { ReactNode } from "react"
import type { EntityRef } from "@/client"
import { AdsReportingService, type ReportRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { ManagementAction } from "@/features/ad-management/ManagementPreviewSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { rowDisplayName } from "../AdsNameCell"
import {
  actionsFor,
  toolbarAction,
  toolbarActionLabel,
} from "../AdsTableToolbar"
import type { DirectoryDetailEntry } from "../DirectoryBrowser"
import { columnsFor, useMetricCatalog } from "../metricCatalog"
import { type AdsDimension, rowAdvertiserId, rowObjectId } from "../search"
import { statusLabel } from "../statusLabel"
import { FrozenReport } from "./FrozenReport"
import { DirectoryConfiguration } from "./DirectoryConfiguration"
import { DirectoryDetailBody } from "./DirectoryDetailBody"
import { HistoryTab } from "./HistoryTab"
import { AdMaterialsTab, MaterialsTab } from "./MaterialsTab"
import { OverviewTab } from "./OverviewTab"
import { TrendTab } from "./TrendTab"
import { type LinkedTarget, useLinkedRows } from "./useLinkedRows"

const KIND_LABELS: Record<AdsDimension, string> = {
  account: "账户",
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  material: "素材",
  drama: "剧",
}
// 只有系列、广告组、广告有目录配置与管理任务记录，才请求对象详情接口。
type DetailKind = "campaign" | "adgroup" | "ad"
const DETAIL_KINDS = new Set<AdsDimension>(["campaign", "adgroup", "ad"])

/**
 * 详情抽屉里的一层：表格里点开的行，或从详情里点关联对象跳转过去的目标。
 * 关联目标按 ID 重新查询真实报表行，因此与在该维度报表里点开的详情一致、可直接操作。
 */
export type DetailEntry =
  | DirectoryDetailEntry
  | { source: "table"; row: ReportRow; dimension: AdsDimension }
  | { source: "linked"; target: LinkedTarget }

/** 详情里的单对象操作：带上该行所属的快照与维度，冻结选择时只认这一行。 */
export type DetailActionScope = {
  dimension: AdsDimension
  snapshotId: string | null
}

export type AdsDetailSheetProps = {
  onOpenDirectory: (ref: EntityRef) => void
  entry: DetailEntry | null
  // 表格来源的行在工作台里已按最新表格数据替换；关联来源为 null，由抽屉自行查询。
  tableRow: ReportRow | null
  canGoBack: boolean
  startDate: string
  endDate: string
  readonly: boolean
  snapshotId?: string | null
  onBack: () => void
  onClose: () => void
  onAction: (
    row: ReportRow,
    action: ManagementAction,
    scope: DetailActionScope,
  ) => void
  // 广告详情里只启停本广告的指定素材（按广告内素材 ID）。
  onAdMaterialAction: (
    advertiserId: string,
    adId: string,
    adMaterialIds: string[],
    value: "ENABLE" | "DISABLE",
  ) => void
  onOpenLinked: (target: LinkedTarget) => void
}

export function AdsDetailSheet({
  entry,
  onOpenDirectory,
  tableRow,
  canGoBack,
  startDate,
  endDate,
  readonly,
  snapshotId,
  onBack,
  onClose,
  onAction,
  onAdMaterialAction,
  onOpenLinked,
}: AdsDetailSheetProps) {
  const { tenantId, bc } = useTenantScope()
  const target = entry?.source === "linked" ? entry.target : null
  const linked = useLinkedRows({
    dimension: target?.dimension ?? "campaign",
    ids: target ? [target.id] : [],
    advertiserId: target?.advertiserId,
    startDate,
    endDate,
    enabled: Boolean(target),
  })
  // 同一 ID 理论上只对应一个对象；仍按账户与 ID 双重比对，避免误用别的账户的行。
  const linkedRow = target
    ? (linked.data?.items?.find(
        (row) =>
          rowObjectId(row, target.dimension) === target.id &&
          rowAdvertiserId(row) === target.advertiserId,
      ) ?? null)
    : null
  const dimension =
    entry?.source === "table"
      ? entry.dimension
      : (target?.dimension ?? "campaign")
  const row = entry?.source === "table" ? tableRow : linkedRow
  const rowSnapshotId =
    entry?.source === "table"
      ? (snapshotId ?? null)
      : (linked.data?.snapshot?.snapshot_id ?? null)
  return (
    <Sheet
      open={entry !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-none md:w-[60vw] md:min-w-[720px]"
      >
        {canGoBack && (
          <div className="px-4 pt-4">
            <Button type="button" variant="ghost" size="sm" onClick={onBack}>
              <ArrowLeft className="size-4" aria-hidden="true" />
              返回
            </Button>
          </div>
        )}
        {entry?.source === "directory" && tenantId && bc ? (
          <DirectoryDetailBody
            key={`${bc.bc_id}:${entry.ref.kind}:${entry.ref.advertiser_id}:${entry.ref.remote_id}`}
            tenantId={tenantId}
            bcId={bc.bc_id}
            ref={entry.ref}
            onOpen={onOpenDirectory}
          />
        ) : row ? (
          <DetailBody
            key={`${dimension}:${row.row_key}`}
            row={row}
            dimension={dimension}
            startDate={startDate}
            endDate={endDate}
            readonly={readonly}
            snapshotId={rowSnapshotId}
            onAction={(action) =>
              onAction(row, action, { dimension, snapshotId: rowSnapshotId })
            }
            onAdMaterialAction={onAdMaterialAction}
            onOpenLinked={onOpenLinked}
          />
        ) : (
          target && (
            <LinkedState
              target={target}
              loading={linked.isLoading}
              failed={linked.isError}
              onRetry={() => void linked.refetch()}
            />
          )
        )}
      </SheetContent>
    </Sheet>
  )
}

/** 关联对象尚未读到时的抽屉内容：读取中、读取失败或不在当前报表范围。 */
function LinkedState({
  target,
  loading,
  failed,
  onRetry,
}: {
  target: LinkedTarget
  loading: boolean
  failed: boolean
  onRetry: () => void
}) {
  return (
    <>
      <SheetHeader className="gap-2 pr-10">
        <SheetTitle className="break-words">{target.name}</SheetTitle>
        <SheetDescription>
          {KIND_LABELS[target.dimension]} · {target.id}
        </SheetDescription>
      </SheetHeader>
      <div className="px-4 pb-6">
        {loading ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <div className="flex items-center gap-3 text-sm">
            <p className="text-destructive">
              {failed
                ? "对象数据读取失败"
                : "当前 BC 的报表里没有找到这个对象，可能已删除或尚未同步"}
            </p>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              重试
            </Button>
          </div>
        )}
      </div>
    </>
  )
}

function DetailBody({
  row,
  dimension,
  startDate,
  endDate,
  readonly,
  snapshotId,
  onAction,
  onAdMaterialAction,
  onOpenLinked,
}: {
  row: ReportRow
  dimension: AdsDimension
  startDate: string
  endDate: string
  readonly: boolean
  snapshotId: string | null
  onAction: (action: ManagementAction) => void
  onAdMaterialAction: AdsDetailSheetProps["onAdMaterialAction"]
  onOpenLinked: (target: LinkedTarget) => void
}) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  const catalog = useMetricCatalog()
  const columns = columnsFor(catalog.data ?? [], dimension)
  const kind = DETAIL_KINDS.has(dimension) ? (dimension as DetailKind) : null
  const remoteId = rowObjectId(row, dimension)
  const advertiserId = rowAdvertiserId(row)
  const detailQuery = useQuery({
    // 键包含 BC：切换 BC 后不会显示旧 BC 的对象详情。
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "detail",
      bcId,
      snapshotId,
      kind,
      advertiserId,
      remoteId,
    ],
    enabled: Boolean(tenantId && bcId && kind && remoteId && advertiserId),
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adDetail({
          path: {
            tenant_id: tenantId!,
            kind: kind!,
            remote_id: remoteId!,
          },
          query: {
            advertiser_id: advertiserId,
            bc_id: bcId!,
            snapshot_id: snapshotId ?? undefined,
          },
          signal,
        })
      ).data,
  })
  const detail = detailQuery.data
  const name = detail?.name ?? rowDisplayName(row, dimension)
  const status = statusLabel(row.display ?? {})
  // 没有快照的行无法冻结选择，不显示操作按钮。
  const actions =
    readonly ||
    !snapshotId ||
    (dimension === "material" && !row.material_uses?.length)
      ? []
      : actionsFor(dimension)
  // 素材挂在广告下面：只有广告（及素材自身）显示素材页签，系列、广告组不显示。
  const hasMaterials = dimension === "ad" || dimension === "material"
  return (
    <>
      <SheetHeader className="gap-2 pr-10" data-testid="ads-detail-header">
        <SheetTitle className="break-words">{name}</SheetTitle>
        <SheetDescription className="flex flex-wrap items-center gap-2">
          <span>{KIND_LABELS[dimension]}</span>
          {remoteId && <span>· {remoteId}</span>}
          <Badge variant={status.tone} title={status.raw}>
            {status.label}
          </Badge>
        </SheetDescription>
        {actions.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {actions.map((key) => (
              <Button
                key={key}
                type="button"
                size="sm"
                variant="outline"
                onClick={() => onAction(toolbarAction(key))}
              >
                {toolbarActionLabel(key)}
              </Button>
            ))}
          </div>
        )}
      </SheetHeader>
      {/* 父组件以行键为 key：切换到另一个对象时页签与区间回到默认。 */}
      <Tabs defaultValue="overview" className="px-4 pb-6">
        <TabsList>
          <TabsTrigger value="overview">概览</TabsTrigger>
          <TabsTrigger value="trend">趋势</TabsTrigger>
          {kind && <TabsTrigger value="settings">设置</TabsTrigger>}
          {hasMaterials && <TabsTrigger value="materials">素材</TabsTrigger>}
          {kind && <TabsTrigger value="history">操作记录</TabsTrigger>}
        </TabsList>
        <TabsContent value="overview" className="mt-4">
          {detail?.report_result && detail.report_snapshot && <FrozenReport row={detail.report_result} snapshot={detail.report_snapshot} />}
          <OverviewTab
            row={row}
            dimension={dimension}
            name={name}
            detail={detail}
            loading={Boolean(kind) && detailQuery.isLoading}
            columns={columns}
            startDate={startDate}
            endDate={endDate}
            onOpenLinked={onOpenLinked}
          />
        </TabsContent>
        <TabsContent value="trend" className="mt-4">
          <TrendTab
            snapshotId={snapshotId ?? undefined}
            row={row}
            dimension={dimension}
            startDate={startDate}
            endDate={endDate}
            columns={columns}
          />
        </TabsContent>
        {kind && (
          <TabsContent value="settings" className="mt-4">
            <DetailState query={detailQuery}>
              {detail && <DirectoryConfiguration detail={detail} />}
            </DetailState>
          </TabsContent>
        )}
        {hasMaterials && (
          <TabsContent value="materials" className="mt-4">
            {dimension === "ad" && remoteId ? (
              <AdMaterialsTab
                advertiserId={advertiserId}
                adId={remoteId}
                startDate={startDate}
                endDate={endDate}
                columns={columns}
                readonly={readonly}
                onToggle={(ids, value) =>
                  onAdMaterialAction(advertiserId, remoteId, ids, value)
                }
              />
            ) : (
              <MaterialsTab
                materials={(row.material_uses ?? []).map((use) => ({
                  use_ref: use,
                  name: row.display?.material_name ?? "",
                  external: true,
                }))}
              />
            )}
          </TabsContent>
        )}
        {kind && tenantId && bcId && (
          <TabsContent value="history" className="mt-4">
            <DetailState query={detailQuery}>
              <HistoryTab
                tenantId={tenantId}
                bcId={bcId}
                items={detail?.operation_history ?? []}
              />
            </DetailState>
          </TabsContent>
        )}
      </Tabs>
    </>
  )
}

function DetailState({
  query,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; refetch: () => unknown }
  children: ReactNode
}) {
  if (query.isLoading) return <Skeleton className="h-40 w-full" />
  if (query.isError)
    return (
      <div className="flex items-center gap-3 text-sm">
        <p className="text-destructive">对象详情读取失败</p>
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
  return <>{children}</>
}
