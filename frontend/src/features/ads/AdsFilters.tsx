import { LoaderCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { AdsSearch } from "./search"
export function AdsFilters({
  search,
  onChange,
  onApply,
  onReset,
  onSaveView,
  loading,
  savingView,
  disableSaveView,
}: {
  search: AdsSearch
  onChange: (next: Partial<AdsSearch>) => void
  onApply: () => void
  onReset: () => void
  onSaveView: () => void
  loading?: boolean
  savingView?: boolean
  disableSaveView?: boolean
}) {
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        onApply()
      }}
    >
      <div className="space-y-1.5">
        <Label htmlFor="ads-start">开始日期</Label>
        <Input
          id="ads-start"
          type="date"
          value={search.start_date}
          onChange={(event) => onChange({ start_date: event.target.value })}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="ads-end">结束日期</Label>
        <Input
          id="ads-end"
          type="date"
          value={search.end_date}
          onChange={(event) => onChange({ end_date: event.target.value })}
        />
      </div>
      <div className="min-w-56 flex-1 space-y-1.5">
        <Label htmlFor="ads-search">搜索广告</Label>
        <Input
          id="ads-search"
          aria-label="搜索广告"
          value={search.query ?? ""}
          onChange={(event) => onChange({ query: event.target.value })}
          placeholder="名称、剧名、版权方或 ID（多关键词 AND）"
        />
      </div>
      {["campaign", "adgroup", "account", "drama"].includes(search.dimension) && (
        <div className="space-y-1.5">
          <Label htmlFor="ads-contribution">广告贡献子集 ID</Label>
          <Input id="ads-contribution" placeholder="留空查看整对象"
            value={search.contribution?.ids.join(",") ?? ""}
            onChange={(event) => {
              const ids = event.target.value.split(",").map((id) => id.trim()).filter(Boolean)
              onChange({ contribution: ids.length ? { kind: "ad", ids } : null })
            }} />
        </div>
      )}
      {/* 默认只列所选日期有数据的对象；大租户目录有数万个无数据对象，全部列出会明显变慢。
          账户维度始终列出全部账户。 */}
      <div className="flex h-9 items-center gap-2">
        <Checkbox
          id="ads-include-empty"
          checked={search.include_empty ?? false}
          onCheckedChange={(checked) =>
            onChange({ include_empty: checked === true })
          }
        />
        <Label htmlFor="ads-include-empty" className="font-normal">
          显示无数据对象
        </Label>
      </div>
      <Button type="submit" aria-label="应用筛选" disabled={loading}>
        {loading && (
          <LoaderCircle className="animate-spin" aria-hidden="true" />
        )}
        {loading ? "查询中…" : "应用筛选"}
      </Button>
      <Button type="button" variant="outline" onClick={onReset}>
        重置
      </Button>
      <Button
        type="button"
        variant="outline"
        disabled={savingView || disableSaveView}
        onClick={onSaveView}
      >
        {savingView ? "保存中…" : "保存筛选"}
      </Button>
    </form>
  )
}
