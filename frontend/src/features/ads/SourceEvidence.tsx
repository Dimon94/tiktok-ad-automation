import { useInfiniteQuery } from "@tanstack/react-query"
import { useState } from "react"
import { AdsReportingService, type EvidenceRef } from "@/client"
import { Button } from "@/components/ui/button"
import { useTenantScope } from "@/features/tenants/TenantScope"
import useAuth from "@/hooks/useAuth"

/** 沿用原来源展开：逐页读取同一冻结结果，不以当前目录或新查询替代。 */
export function SourceEvidence({ reference }: { reference: EvidenceRef }) {
  const { tenantId, bc } = useTenantScope()
  const { user } = useAuth()
  const [open, setOpen] = useState(false)
  const evidence = useInfiniteQuery({
    queryKey: ["report-evidence", tenantId, bc?.bc_id, user?.id, reference],
    enabled:
      open && !!tenantId && !!bc && reference.owner === "QUERY_SNAPSHOT_ROWS",
    initialPageParam: undefined as string | undefined,
    retry: false,
    queryFn: async ({ pageParam, signal }) => {
      const result = await AdsReportingService.queryAds({
        path: { tenant_id: tenantId! },
        query: {
          bc_id: bc!.bc_id,
          view: "evidence",
          snapshot_id: reference.owner_id,
          evidence_row_key: reference.row_key ?? undefined,
          evidence_bucket: JSON.stringify(reference.bucket),
          evidence_component: reference.component ?? undefined,
          evidence_cursor: pageParam,
          limit: 50,
        },
        signal,
      })
      return result.data.evidence_page!
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>逐日平台观察 · {reference.record_count} 条</summary>
      {evidence.isPending && open && <p>正在读取冻结证据…</p>}
      {evidence.isError && (
        <p role="alert">冻结证据暂不可读取，请重新查询报表。</p>
      )}
      {evidence.data?.pages.map((page, index) => (
        <pre
          key={index}
          className="max-h-80 max-w-xl overflow-auto whitespace-pre-wrap text-xs"
        >
          {JSON.stringify(page.records, null, 2)}
        </pre>
      ))}
      {evidence.hasNextPage && (
        <Button
          size="sm"
          variant="outline"
          disabled={evidence.isFetchingNextPage}
          onClick={() => void evidence.fetchNextPage()}
        >
          继续读取
        </Button>
      )}
    </details>
  )
}
