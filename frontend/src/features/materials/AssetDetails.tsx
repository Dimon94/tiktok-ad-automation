import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useRef, useState } from "react"
import {
  type AccountAsset,
  MaterialLinksService,
  MaterialsService,
  type RemoteMaterialPreview,
  type UploadAttemptPublic,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { displayTime } from "@/features/accounts/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { handleApiError } from "@/lib/api-feedback"
import { bytes, CopyValue, issue, materialKey, Stage } from "./presentation"

type Preview =
  | { kind: "original"; url: string }
  | ({ kind: "remote" } & RemoteMaterialPreview)

export function AssetDetails({
  tenantId,
  materialId,
  onClose,
  onForbidden,
}: {
  tenantId: string
  materialId: string
  onClose: () => void
  onForbidden: () => void
}) {
  const queryClient = useQueryClient()
  const [sessionToken] = useState(() => localStorage.getItem("access_token"))
  const [previewAuthorized, setPreviewAuthorized] = useState(true)
  const [preview, setPreview] = useState<Preview | null>(null),
    [previewError, setPreviewError] = useState(false),
    [previewLoading, setPreviewLoading] = useState(false),
    previewController = useRef<AbortController | null>(null)
  useEffect(() => {
    let revoked = false
    const checkSession = () => {
      if (!revoked && sessionToken !== localStorage.getItem("access_token")) {
        revoked = true
        previewController.current?.abort()
        setPreview(null)
        setPreviewError(false)
        setPreviewLoading(false)
        setPreviewAuthorized(false)
      }
    }
    const unsubscribe = queryClient.getQueryCache().subscribe(checkSession)
    window.addEventListener("storage", checkSession)
    checkSession()
    return () => {
      unsubscribe()
      window.removeEventListener("storage", checkSession)
      previewController.current?.abort()
    }
  }, [queryClient, sessionToken])
  const path = { tenant_id: tenantId, material_id: materialId },
    key = [...materialKey(tenantId), "details", materialId]
  const detail = useQuery({
      queryKey: key,
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.getMaterial({
            path,
            signal,
          })
        ).data,
    }),
    assetPage = useCursorPage(),
    attemptPage = useCursorPage()
  const assets = useQuery({
      queryKey: [...key, "assets", assetPage.cursor, assetPage.limit],
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.getAssets({
            path,
            query: {
              cursor: assetPage.cursor,
              limit: assetPage.limit,
            },
            signal,
          })
        ).data,
    }),
    attempts = useQuery({
      queryKey: [...key, "attempts", attemptPage.cursor, attemptPage.limit],
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.getAttempts({
            path,
            query: {
              cursor: attemptPage.cursor,
              limit: attemptPage.limit,
            },
            signal,
          })
        ).data,
    })
  const [linkOffset, setLinkOffset] = useState(0)
  const linkSources = useQuery({
    queryKey: [...key, "link-sources", linkOffset],
    queryFn: async ({ signal }) =>
      (
        await MaterialLinksService.listMaterialLinkSources({
          path,
          query: { offset: linkOffset, limit: 20 },
          signal,
        })
      ).data,
  })
  const a = useRetainedData(assets.data, assets.error),
    h = useRetainedData(attempts.data, attempts.error)
  useEffect(() => {
    if (
      [detail.error, assets.error, attempts.error, linkSources.error].some(
        isForbidden,
      )
    )
      onForbidden()
  }, [
    detail.error,
    assets.error,
    attempts.error,
    linkSources.error,
    onForbidden,
  ])
  async function loadPreview(kind: "original" | "remote") {
    if (
      !previewAuthorized ||
      !sessionToken ||
      sessionToken !== localStorage.getItem("access_token")
    )
      return
    previewController.current?.abort()
    const controller = new AbortController()
    previewController.current = controller
    setPreviewLoading(true)
    setPreviewError(false)
    setPreview(null)
    try {
      const request = {
        path,
        signal: controller.signal,
      }
      const result: Preview =
        kind === "remote"
          ? {
              kind,
              ...(await MaterialsService.readRemotePreview(request)).data,
            }
          : {
              kind,
              url: (await MaterialsService.readOriginalPreview(request)).data
                .url,
            }
      if (
        !controller.signal.aborted &&
        sessionToken === localStorage.getItem("access_token")
      )
        setPreview(result)
    } catch (error) {
      if (!controller.signal.aborted) {
        if (error instanceof Error) handleApiError(error)
        if (isForbidden(error)) onForbidden()
        setPreviewError(true)
      }
    } finally {
      if (!controller.signal.aborted) setPreviewLoading(false)
    }
  }
  const ac: ColumnDef<AccountAsset>[] = [
    {
      header: "实际 BC",
      cell: ({ row }) => <CopyValue value={row.original.bc_id} />,
    },
    {
      header: "实际账户",
      cell: ({ row }) => <CopyValue value={row.original.advertiser_id} />,
    },
    {
      header: "素材id",
      cell: ({ row: { original: r } }) =>
        r.mid ? <CopyValue value={r.mid} /> : "尚无素材id",
    },
    {
      header: "状态与回查",
      cell: ({ row }) => (
        <div>
          <Stage status={row.original.status} />
          <p className="text-xs text-muted-foreground">
            {displayTime(row.original.verified_at)}
          </p>
        </div>
      ),
    },
  ]
  const hc: ColumnDef<UploadAttemptPublic>[] = [
    {
      header: "上传来源 BC",
      cell: ({ row }) => <CopyValue value={row.original.bc_id} />,
    },
    {
      header: "实际上传账户",
      cell: ({ row }) => <CopyValue value={row.original.advertiser_id} />,
    },
    {
      header: "尝试时间",
      cell: ({ row }) => displayTime(row.original.created_at),
    },
    {
      header: "阶段",
      cell: ({ row }) => (
        <div>
          <Stage status={row.original.status} />
          {row.original.error_code && (
            <p className="text-xs">{issue(row.original.error_code)}</p>
          )}
        </div>
      ),
    },
  ]
  return (
    <ManagementSheet
      title="文件与账户记录"
      description="当前租户素材的上传来源与实际账户记录。账户资产不代表全部目标账户可用。"
      dirty={false}
      onClose={onClose}
    >
      <div className="flex min-w-0 flex-col gap-6 py-2">
        {detail.isPending ? (
          <Skeleton className="h-24" />
        ) : detail.error ? (
          <RequestError
            error={detail.error}
            retry={() => void detail.refetch()}
          />
        ) : (
          detail.data && (
            <section
              aria-label="文件信息"
              className="flex min-w-0 flex-col gap-3"
            >
              <h2 className="font-semibold">原始文件</h2>
              <CopyValue label="文件名" value={detail.data.file_name} />
              <p className="text-sm">原始上传 BC：{detail.data.bc_id}</p>
              <p className="text-sm">
                {bytes(detail.data.byte_size)} · {detail.data.mime_type}
                {detail.data.media_type === "IMAGE" &&
                detail.data.width != null &&
                detail.data.height != null
                  ? ` · ${detail.data.width} × ${detail.data.height}`
                  : ""}
              </p>
              <p className="text-sm text-muted-foreground">
                {detail.data.original_available
                  ? "暂存原件当前可读取"
                  : "暂无可读取的暂存原件"}
              </p>
              <Stage status={detail.data.status} />
              <div className="flex flex-wrap gap-2">
                {detail.data.available_account_count > 0 && (
                  <Button
                    variant="outline"
                    disabled={previewLoading || !previewAuthorized}
                    onClick={() => void loadPreview("remote")}
                  >
                    {previewLoading ? "正在读取预览…" : "预览账户素材"}
                  </Button>
                )}
                {detail.data.original_available && (
                  <Button
                    variant="outline"
                    disabled={previewLoading || !previewAuthorized}
                    onClick={() => void loadPreview("original")}
                  >
                    {previewLoading ? "正在读取预览…" : "预览原文件"}
                  </Button>
                )}
              </div>
              {preview?.kind === "remote" && (
                <div className="flex flex-col gap-1 text-sm">
                  <span>实际预览 BC：{preview.bc_id}</span>
                  <span>实际预览账户</span>
                  <CopyValue value={preview.advertiser_id} />
                  <span className="text-muted-foreground">
                    {preview.width} × {preview.height} · {preview.duration} 秒 ·{" "}
                    {preview.format}
                  </span>
                </div>
              )}
              {previewError && (
                <Alert variant="destructive">
                  <AlertDescription>
                    暂无法预览，请稍后重新获取。
                  </AlertDescription>
                </Alert>
              )}
              {preview && detail.data.media_type === "IMAGE" ? (
                <img
                  alt="原文件图片预览"
                  src={preview.url}
                  className="max-h-80 w-full rounded-lg object-contain"
                  onError={() => {
                    setPreviewError(true)
                    setPreview(null)
                  }}
                />
              ) : (
                preview && (
                  <video
                    aria-label={
                      preview.kind === "remote"
                        ? "账户素材视频预览"
                        : "原文件视频预览"
                    }
                    src={preview.url}
                    controls
                    preload="metadata"
                    className="max-h-80 w-full rounded-lg"
                    onError={() => {
                      setPreviewError(true)
                      setPreview(null)
                    }}
                  >
                    <track kind="captions" />
                  </video>
                )
              )}
            </section>
          )
        )}
        {linkSources.isError && (
          <p role="alert" className="text-sm text-destructive">
            链接上传来源暂时无法读取，请重新打开详情。
          </p>
        )}
        {!!linkSources.data?.total && (
          <section aria-label="链接上传来源" className="flex flex-col gap-3">
            <h2 className="font-semibold">链接上传来源</h2>
            {linkSources.data.items.map((source) => (
              <div
                key={source.id}
                className="flex flex-col gap-1 text-sm break-all"
              >
                <span>
                  第 {source.line_no} 行 · 账户 {source.advertiser_id}
                </span>
                <span>{source.source_url}</span>
                <a
                  className="underline"
                  href={`/tenants/${tenantId}/materials?bc_id=${encodeURIComponent(source.bc_id ?? detail.data?.bc_id ?? "")}&tab=links&link_batch_id=${source.batch_id}`}
                >
                  查看链接上传批次
                </a>
              </div>
            ))}
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!linkOffset || linkSources.isFetching}
                onClick={() => setLinkOffset(Math.max(0, linkOffset - 20))}
              >
                上一页来源
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={
                  linkOffset + 20 >= linkSources.data.total ||
                  linkSources.isFetching
                }
                onClick={() => setLinkOffset(linkOffset + 20)}
              >
                下一页来源
              </Button>
            </div>
          </section>
        )}
        <section
          aria-label="来源上传记录"
          className="flex min-w-0 flex-col gap-4"
        >
          <h2 className="font-semibold">来源上传记录</h2>
          <ServerTable
            rows={h?.items || []}
            columns={hc}
            loading={attempts.isPending}
            fetching={attempts.isFetching}
            error={attempts.error}
            retry={() => void attempts.refetch()}
            filtered={false}
            emptyTitle="暂无文件上传记录"
          />
          <Pager
            paging={attemptPage}
            nextCursor={h?.next_cursor}
            total={h?.total}
            busy={attempts.isFetching}
          />
        </section>
        <section aria-label="账户资产" className="flex min-w-0 flex-col gap-4">
          <h2 className="font-semibold">账户资产</h2>
          <ServerTable
            rows={a?.items || []}
            columns={ac}
            loading={assets.isPending}
            fetching={assets.isFetching}
            error={assets.error}
            retry={() => void assets.refetch()}
            filtered={false}
            emptyTitle="尚无账户资产"
          />
          <Pager
            paging={assetPage}
            nextCursor={a?.next_cursor}
            total={a?.total}
            busy={assets.isFetching}
          />
        </section>
      </div>
    </ManagementSheet>
  )
}
