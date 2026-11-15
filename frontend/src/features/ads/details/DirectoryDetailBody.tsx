import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type EntityRef } from "@/client"
import { Button } from "@/components/ui/button"
import {
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { DirectoryObjects, directoryChildKind } from "../DirectoryBrowser"
import { FrozenReport } from "./FrozenReport"
import { DirectoryConfiguration } from "./DirectoryConfiguration"

export function DirectoryDetailBody({
  tenantId,
  bcId,
  ref: objectRef,
  onOpen,
}: {
  tenantId: string
  bcId: string
  ref: EntityRef
  onOpen: (ref: EntityRef) => void
}) {
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "detail",
      bcId,
      "directory",
      objectRef,
    ],
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adDetail({
          path: {
            tenant_id: tenantId,
            kind: objectRef.kind,
            remote_id: objectRef.remote_id,
          },
          query: { bc_id: bcId, advertiser_id: objectRef.advertiser_id },
          signal,
        })
      ).data,
  })
  const detail = query.isError ? undefined : query.data
  return (
    <>
      <SheetHeader>
        <SheetTitle>{detail?.name || objectRef.remote_id}</SheetTitle>
        <SheetDescription>
          {objectRef.kind} · {objectRef.remote_id} · 账户{" "}
          {objectRef.advertiser_id}
        </SheetDescription>
      </SheetHeader>
      <div className="px-4 pb-6 space-y-4">
        {query.isLoading && <p>正在读取详情…</p>}
        {query.isError && (
          <div>
            <p role="alert">对象详情读取失败，请确认当前账户授权后重试。</p>
            <Button variant="outline" onClick={() => void query.refetch()}>
              重试
            </Button>
          </div>
        )}
        {detail && (
          <>
            <p className="text-xs text-muted-foreground">
              当前目录属性 · 来源连接：{detail.source_connection_id ?? "未提供"} · 渠道：
              {detail.source_channel ?? "未提供"} · 发布版本：
              {detail.published_version} · 观察时间：{detail.observed_at}
            </p>
            {detail.parent && (
              <Button variant="link" onClick={() => onOpen(detail.parent!.ref)}>
                查看父对象 · {detail.parent.ref.remote_id}
                {detail.parent.reasons?.includes("parent_not_synced")
                  ? "（未同步）"
                  : ""}
              </Button>
            )}
            <Tabs defaultValue="settings">
              <TabsList>
                <TabsTrigger value="settings">设置</TabsTrigger>
                <TabsTrigger value="report">报表</TabsTrigger>
                {objectRef.kind !== "creative" && (
                  <TabsTrigger value="children">下级对象</TabsTrigger>
                )}
              </TabsList>
              <TabsContent value="settings">
                <DirectoryConfiguration detail={detail} />
              </TabsContent>
              <TabsContent value="report">
                {detail.report_result && detail.report_snapshot ? <FrozenReport row={detail.report_result} snapshot={detail.report_snapshot} /> : <p>该对象没有日报指标。</p>}
              </TabsContent>
              {objectRef.kind in directoryChildKind && (
                <TabsContent value="children">
                  <DirectoryObjects
                    tenantId={tenantId}
                    bcId={bcId}
                    advertiserIds={[objectRef.advertiser_id]}
                    parent={objectRef}
                    onOpen={onOpen}
                  />
                </TabsContent>
              )}
            </Tabs>
          </>
        )}
      </div>
    </>
  )
}
