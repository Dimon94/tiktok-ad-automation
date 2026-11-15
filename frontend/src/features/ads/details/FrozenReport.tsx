import type { QuerySnapshotPublic, ReportRow } from "@/client"
import { columnsFor, formatMetric, useMetricCatalog } from "../metricCatalog"
import type { AdsDimension } from "../search"

export function FrozenReport({ row, snapshot }: {
  row: ReportRow
  snapshot: QuerySnapshotPublic
}) {
  const catalog = useMetricCatalog()
  const columns = columnsFor(catalog.data ?? [], snapshot.filters.dimension as AdsDimension)
  return (
    <section className="space-y-3" data-testid="frozen-report">
      <p className="text-xs text-muted-foreground">
        报表查询时间：{snapshot.snapshot_at} · {snapshot.filters.start_date} — {snapshot.filters.end_date} · 目录设置为当前管理属性
      </p>
      {row.metric_buckets?.map((bucket, i) => (
        <div key={i} className="rounded-md border p-3">
          <p>{bucket.currency} · {bucket.level} · {bucket.source_kind} · {bucket.contract_revision}</p>
          <dl className="grid grid-cols-2 gap-2 text-sm">
            {columns.filter(c => c.key in (bucket.values ?? {})).map(c => (
              <div key={c.key}>
                <dt className="text-muted-foreground">{c.label}</dt>
                <dd>
                  {formatMetric(bucket.values?.[c.key], c.unit)} · {bucket.availability?.[c.key]} {bucket.reasons?.[c.key]}
                </dd>
              </div>
            ))}
          </dl>
          <details>
            <summary>来源、合同与逐日水位</summary>
            <pre className="overflow-auto whitespace-pre-wrap text-xs">
              {JSON.stringify(bucket.provenance, null, 2)}
            </pre>
          </details>
        </div>
      ))}
      <p className="text-sm">
        平台返回 ROAS：{row.display?.platform_roas_total ?? "—"} · {row.display?.platform_roas_total_reason}；
        D0：{row.display?.platform_roas_d0 ?? "—"} · {row.display?.platform_roas_d0_reason}
      </p>
      <details>
        <summary>逐日平台观察</summary>
        <pre className="overflow-auto whitespace-pre-wrap text-xs">{row.display?.platform_roas_observations}</pre>
      </details>
      <p className="whitespace-pre-wrap text-xs">{row.display?.report_layer_status}</p>
      <p className="text-xs">{row.display?.report_semantics}</p>
    </section>
  )
}
