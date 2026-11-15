import type { ManagementItemPublic } from "@/client"
import { KIND_LABEL } from "./presentation"

export function RoasFacts({ item }: { item: ManagementItemPublic }) {
  const facts = item.roas_facts
  if (!facts) return null
  return <div className="space-y-1">
    <p>ROAS 归属：广告组 · {item.ref.remote_id}</p>
    <p>原比例 {item.original_value} → 目标比例 {item.final_value}</p>
    <p>支持模式：{facts.mode} · {facts.budget_strategy}</p>
    <p>影响：{facts.affected_refs.map(ref => ref.remote_id).join("、")}</p>
    <p>来源：{facts.sources.map(ref => `${KIND_LABEL[ref.kind]} · ${ref.remote_id}`).join("、")}</p>
  </div>
}
