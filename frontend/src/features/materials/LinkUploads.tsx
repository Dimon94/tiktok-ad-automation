import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useRef, useState } from "react"
import {
  type LinkBatchPublic,
  type LinkItemPublic,
  MaterialLinksService,
  MaterialsService,
  type RemoteMaterialPreview,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { displayTime } from "@/features/accounts/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { RequestError, ServerTable } from "@/features/tenants/shared"
import { linkStatus } from "./link-input"
import { CopyValue } from "./presentation"

export function LinkUploads({
  tenantId,
  bcId,
  batchId,
  allowed,
  onOpen,
  onDetails,
}: {
  tenantId: string
  bcId: string
  batchId?: string
  allowed: boolean
  onOpen: (id?: string) => void
  onDetails: (id: string) => void
}) {
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<LinkItemPublic | null>(null)
  const history = useQuery({
    queryKey: ["tenant", tenantId, "link-uploads", bcId, offset],
    enabled: !batchId,
    queryFn: async ({ signal }) =>
      (
        await MaterialLinksService.listLinkUploads({
          path: { tenant_id: tenantId },
          query: { bc_id: bcId, offset, limit: 20 },
          signal,
        })
      ).data,
    refetchInterval: 5000,
  })
  const batch = useQuery({
    queryKey: ["tenant", tenantId, "link-uploads", bcId, batchId],
    enabled: !!batchId,
    queryFn: async ({ signal }) =>
      (
        await MaterialLinksService.readLinkUpload({
          path: { tenant_id: tenantId, batch_id: batchId! },
          signal,
        })
      ).data,
    refetchInterval: 3000,
  })
  const sameBc = batch.data?.bc_id === bcId
  const items = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "link-uploads",
      bcId,
      batchId,
      "items",
      offset,
    ],
    enabled: !!batchId && sameBc,
    queryFn: async ({ signal }) =>
      (
        await MaterialLinksService.listLinkUploadItems({
          path: { tenant_id: tenantId, batch_id: batchId! },
          query: { offset, limit: 50 },
          signal,
        })
      ).data,
    refetchInterval: 3000,
  })
  const queryClient = useQueryClient()
  const retry = useMutation({
    mutationFn: (item: LinkItemPublic) =>
      MaterialLinksService.retryLinkUploadItem({
        path: { tenant_id: tenantId, batch_id: batchId!, item_id: item.id },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["tenant", tenantId, "link-uploads"],
      })
      setSelected(null)
    },
  })
  const columns: ColumnDef<LinkItemPublic>[] = [
    { header: "原行号", accessorKey: "line_no" },
    {
      header: "素材与命名",
      cell: ({ row: { original: item } }) => (
        <div className="flex max-w-80 flex-col gap-1 break-all">
          <span>{item.requested_file_name}</span>
          <span className="text-xs text-muted-foreground">
            源 VID：{item.source_video_id}
          </span>
        </div>
      ),
    },
    {
      header: "目标账户",
      cell: ({ row }) => <CopyValue value={row.original.advertiser_id} />,
    },
    {
      header: "状态",
      cell: ({ row: { original: item } }) => (
        <div className="flex flex-col gap-1">
          <Badge variant="outline">
            {item.duplicate_of
              ? "成功 · 相同素材"
              : (linkStatus[item.status] ?? item.status)}
          </Badge>
          {item.error_message && (
            <span className="max-w-64 text-xs text-muted-foreground">
              {item.error_message}
            </span>
          )}
        </div>
      ),
    },
    {
      header: "素材 ID",
      cell: ({ row }) =>
        row.original.mid ? <CopyValue value={row.original.mid} /> : "待核实",
    },
    {
      header: "操作",
      cell: ({ row }) => (
        <Button
          variant="outline"
          size="sm"
          onClick={() => setSelected(row.original)}
        >
          查看详情
        </Button>
      ),
    },
  ]
  const historyColumns: ColumnDef<LinkBatchPublic>[] = [
    {
      header: "提交时间",
      cell: ({ row }) => displayTime(row.original.created_at),
    },
    {
      header: "链接 / 账户 / 任务",
      cell: ({ row: { original: b } }) =>
        `${b.unique_count} / ${b.account_count} / ${b.total}`,
    },
    {
      header: "进度",
      cell: ({ row: { original: b } }) =>
        `成功 ${b.counts.success ?? 0} · 失败 ${b.counts.failed ?? 0} · 待核实 ${(b.counts.needs_review ?? 0) + (b.counts.unknown ?? 0)}`,
    },
    {
      header: "操作",
      cell: ({ row }) => (
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setOffset(0)
            onOpen(row.original.id)
          }}
        >
          查看批次
        </Button>
      ),
    },
  ]
  const data = batchId ? items : history
  const total = data.data?.total ?? 0
  const size = batchId ? 50 : 20
  return (
    <section className="flex min-w-0 flex-col gap-4" aria-label="链接上传队列">
      {batchId && (
        <Button
          variant="outline"
          className="self-start"
          onClick={() => {
            setOffset(0)
            onOpen()
          }}
        >
          返回链接批次
        </Button>
      )}
      {batchId && batch.error && (
        <RequestError error={batch.error} retry={() => void batch.refetch()} />
      )}
      {batchId && batch.isPending && <p role="status">正在读取链接批次…</p>}
      {batch.data && !sameBc && (
        <Alert>
          <AlertDescription>
            该批次属于其他 BC，请切换到对应 BC 查看。
          </AlertDescription>
        </Alert>
      )}
      {sameBc && batch.data && (
        <Alert>
          <AlertDescription>
            {batch.data.unique_count} 个链接 · {batch.data.duplicate_count}{" "}
            个重复输入已忽略 · {batch.data.account_count} 个账户 · 成功{" "}
            {batch.data.counts.success ?? 0}/{batch.data.total}
          </AlertDescription>
        </Alert>
      )}
      {data.error && (
        <RequestError error={data.error} retry={() => void data.refetch()} />
      )}
      {retry.error && (
        <RequestError error={retry.error} retry={() => retry.reset()} />
      )}
      {!batchId && (
        <ServerTable
          columns={historyColumns}
          rows={history.data?.items ?? []}
          fetching={history.isFetching}
          error={history.error}
          retry={() => void history.refetch()}
          filtered={false}
          loading={history.isPending}
          emptyTitle="还没有链接上传批次"
        />
      )}
      {batchId && sameBc && (
        <ServerTable
          columns={columns}
          rows={items.data?.items ?? []}
          fetching={items.isFetching}
          error={items.error}
          retry={() => void items.refetch()}
          filtered={false}
          loading={items.isPending}
          emptyTitle="暂无链接任务"
        />
      )}
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">共 {total} 条</p>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!offset || data.isFetching}
            onClick={() => setOffset(Math.max(0, offset - size))}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={offset + size >= total || data.isFetching}
            onClick={() => setOffset(offset + size)}
          >
            下一页
          </Button>
        </div>
      </div>
      {selected && (
        <LinkItemDetails
          key={`${selected.id}:${bcId}`}
          tenantId={tenantId}
          bcId={bcId}
          item={
            items.data?.items.find((item) => item.id === selected.id) ??
            selected
          }
          onClose={() => setSelected(null)}
          onMaterial={onDetails}
          allowed={allowed}
          retryBusy={retry.isPending}
          onRetry={(item) => retry.mutate(item)}
        />
      )}
    </section>
  )
}

function LinkItemDetails({
  tenantId,
  bcId,
  item,
  allowed,
  retryBusy,
  onRetry,
  onClose,
  onMaterial,
}: {
  tenantId: string
  bcId: string
  item: LinkItemPublic
  allowed: boolean
  retryBusy: boolean
  onRetry: (item: LinkItemPublic) => void
  onClose: () => void
  onMaterial: (id: string) => void
}) {
  const [preview, setPreview] = useState<RemoteMaterialPreview | null>(null)
  const [failed, setFailed] = useState(false)
  const controller = useRef(new AbortController())
  const [sessionToken] = useState(() => localStorage.getItem("access_token"))
  const queryClient = useQueryClient()
  useEffect(() => {
    const current = new AbortController()
    controller.current = current
    const check = () => {
      if (localStorage.getItem("access_token") !== sessionToken) {
        current.abort()
        setPreview(null)
      }
    }
    const unsubscribe = queryClient.getQueryCache().subscribe(check)
    window.addEventListener("storage", check)
    return () => {
      current.abort()
      unsubscribe()
      window.removeEventListener("storage", check)
    }
  }, [sessionToken, queryClient])
  const read = useMutation({
    mutationFn: async () =>
      (
        await MaterialsService.readAccountVideoPreview({
          path: { tenant_id: tenantId },
          query: {
            bc_id: bcId,
            advertiser_ids: item.advertiser_id,
            video_id: item.video_id!,
          },
          signal: controller.current.signal,
        })
      ).data,
    onSuccess: (result) => {
      if (
        !controller.current.signal.aborted &&
        localStorage.getItem("access_token") === sessionToken
      ) {
        setPreview(result)
        setFailed(false)
      }
    },
    onMutate: () => {
      setPreview(null)
      setFailed(false)
    },
  })
  return (
    <ManagementSheet
      title="链接上传详情"
      description="源链接与实际账户素材对应关系。预览从当前广告账户实时读取。"
      dirty={false}
      onClose={onClose}
    >
      <div className="flex flex-col gap-4 break-all">
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-3 text-sm">
          <dt>原行号</dt>
          <dd>{item.line_no}</dd>
          <dt>源链接</dt>
          <dd>{item.source_url}</dd>
          <dt>广告账户</dt>
          <dd>
            <CopyValue value={item.advertiser_id} />
          </dd>
          <dt>请求命名</dt>
          <dd>{item.requested_file_name}</dd>
          <dt>实际命名</dt>
          <dd>{item.actual_file_name ?? "待核实"}</dd>
          <dt>状态</dt>
          <dd>{linkStatus[item.status] ?? item.status}</dd>
          <dt>账户内 VID</dt>
          <dd>{item.video_id ?? "待核实"}</dd>
          <dt>素材 ID</dt>
          <dd>{item.mid ?? "待核实"}</dd>
          <dt>平台请求 ID</dt>
          <dd>{item.request_id ?? "暂无"}</dd>
        </dl>
        {item.duplicate_of && (
          <Alert>
            <AlertDescription>
              平台将此链接识别为已入库的相同素材，保留同一实际素材记录。对应第{" "}
              {item.duplicate_line_no ?? "待核实"} 行。
            </AlertDescription>
          </Alert>
        )}
        {item.error_message && (
          <Alert>
            <AlertDescription>
              {item.error_message}
              {item.error_code ? `（${item.error_code}）` : ""}
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap gap-2">
          {item.status === "success" && item.video_id && (
            <Button
              variant="outline"
              disabled={read.isPending || controller.current.signal.aborted}
              onClick={() => read.mutate()}
            >
              {read.isPending
                ? "正在读取预览…"
                : preview
                  ? "重新读取预览"
                  : "预览账户素材"}
            </Button>
          )}
          {item.material_id && (
            <Button
              variant="outline"
              onClick={() => onMaterial(item.material_id!)}
            >
              查看素材库记录
            </Button>
          )}
          {allowed && (item.can_retry || item.can_recheck) && (
            <Button disabled={retryBusy} onClick={() => onRetry(item)}>
              {item.can_retry ? "重试上传" : "继续核实"}
            </Button>
          )}
        </div>
        {read.error && (
          <RequestError error={read.error} retry={() => read.mutate()} />
        )}
        {failed && (
          <Alert variant="destructive">
            <AlertDescription>
              视频加载失败，预览地址可能已过期，请重新读取。
            </AlertDescription>
          </Alert>
        )}
        {preview && !failed && (
          <>
            <p className="text-xs text-muted-foreground">
              账户 {preview.advertiser_id} · {preview.width} × {preview.height}{" "}
              · {preview.duration} 秒
            </p>
            <video
              aria-label="账户素材预览"
              controls
              preload="metadata"
              src={preview.url}
              className="max-h-96 w-full rounded-lg"
              onError={() => setFailed(true)}
            >
              <track kind="captions" />
            </video>
          </>
        )}
      </div>
    </ManagementSheet>
  )
}
