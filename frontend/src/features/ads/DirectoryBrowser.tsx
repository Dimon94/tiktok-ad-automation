import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { AccountsService, AdsReportingService, type EntityRef } from "@/client"
import { Button } from "@/components/ui/button"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"

export type DirectoryDetailEntry = { source: "directory"; ref: EntityRef }
export const directoryChildKind = {
  campaign: "adgroup",
  adgroup: "ad",
  ad: "creative",
} as const

export function DirectoryObjects({
  tenantId,
  bcId,
  advertiserIds,
  parent,
  onOpen,
}: {
  tenantId: string
  bcId: string
  advertiserIds: string[]
  parent?: EntityRef
  onOpen: (ref: EntityRef) => void
}) {
  const [cursors, setCursors] = useState<Array<string | undefined>>([undefined])
  const kind = parent
    ? directoryChildKind[parent.kind as keyof typeof directoryChildKind]
    : "campaign"
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "directory",
      bcId,
      advertiserIds,
      parent,
      cursors[cursors.length - 1],
    ],
    enabled: advertiserIds.length > 0 && Boolean(kind),
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.readDirectoryObjects({
          path: { tenant_id: tenantId },
          query: {
            bc_id: bcId,
            advertiser_ids: advertiserIds,
            kind,
            parent_kind: parent?.kind as
              | "campaign"
              | "adgroup"
              | "ad"
              | undefined,
            parent_remote_id: parent?.remote_id,
            parent_advertiser_id: parent?.advertiser_id,
            cursor: cursors[cursors.length - 1],
            limit: 20,
          },
          signal,
        })
      ).data,
  })
  const page = query.isError ? undefined : query.data
  if (!kind) return null
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        已观察的目录对象；当前页不代表平台完整清单。
      </p>
      {query.isLoading && <p>正在读取目录…</p>}
      {query.isError && (
        <div>
          <p role="alert">目录读取失败，请确认当前账户授权后重试。</p>
          <Button variant="outline" onClick={() => void query.refetch()}>
            重试
          </Button>
        </div>
      )}
      {page?.items.map((item) => (
        <div
          key={`${item.ref.advertiser_id}:${item.ref.remote_id}`}
          className="flex items-center justify-between border-b py-2 gap-3"
        >
          <Button variant="link" onClick={() => onOpen(item.ref)}>
            {item.name || "未提供名称"} · {item.ref.remote_id}
          </Button>
          <span className="text-xs text-muted-foreground">
            {item.observed_at} · 版本 {item.published_version}
          </span>
        </div>
      ))}
      {page?.items.length === 0 && <p>当前范围暂无已同步目录对象。</p>}
      <div className="flex gap-2">
        <Button
          variant="outline"
          disabled={cursors.length === 1}
          onClick={() => setCursors((c) => c.slice(0, -1))}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          disabled={!page?.next_cursor}
          onClick={() => setCursors((c) => [...c, page!.next_cursor!])}
        >
          下一页
        </Button>
      </div>
    </div>
  )
}

export function DirectoryBrowser({
  tenantId,
  bcId,
  onOpen,
}: {
  tenantId: string
  bcId: string
  onOpen: (ref: EntityRef) => void
}) {
  const [account, setAccount] = useState<{ id: string; name: string }>()
  return (
    <section className="rounded-lg border p-4 space-y-3" aria-label="广告目录">
      <h2 className="font-medium">广告目录</h2>
      <DirectoryPicker<{ id: string; name: string }>
        label="目录账户"
        valueLabel={account?.name}
        selectedId={account?.id}
        queryKey={["tenant", tenantId, "directory-account", bcId]}
        load={async (query, cursor, limit, signal) => {
          const { data } = await AccountsService.getAccounts({
            path: { tenant_id: tenantId },
            query: { bc_id: bcId, query, cursor, limit },
            signal,
          })
          return {
            ...data,
            items: data.items.map((a) => ({
              id: a.advertiser_id,
              name: a.name || a.advertiser_id,
            })),
          }
        }}
        renderItem={(a) => (
          <span>
            {a.name} · {a.id}
          </span>
        )}
        onSelect={setAccount}
      />
      {account ? (
        <DirectoryObjects
          key={account.id}
          tenantId={tenantId}
          bcId={bcId}
          advertiserIds={[account.id]}
          onOpen={onOpen}
        />
      ) : (
        <p className="text-sm text-muted-foreground">
          选择授权账户，查看尚无报表的系列、广告组、广告和创意。
        </p>
      )}
    </section>
  )
}
