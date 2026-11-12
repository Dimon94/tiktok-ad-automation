import { useState } from "react"
import type { NativeSeriesPlanSet } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Checkbox } from "@/components/ui/checkbox"
import { NativeSelectionFields } from "./NativeSelectionFields"

export const emptyNativePlanSet = (): NativeSeriesPlanSet => ({
  kind: "NATIVE_SERIES_DIRECT_VIDEO",
  plans: [
    {
      plan_key: crypto.randomUUID(),
      selection: {
        kind: "NATIVE_SERIES_DIRECT_VIDEO",
        advertiser_id: "",
        regions: [],
        video_sources: [],
      },
    },
  ],
})

export function NativePlanSetFields({
  value,
  onChange,
  disabled,
  tenantId,
  draftId,
}: {
  value: NativeSeriesPlanSet
  onChange: (value: NativeSeriesPlanSet) => void
  disabled: boolean
  tenantId: string
  draftId?: string
}) {
  const [selected, setSelected] = useState<string[]>([])
  const [regions, setRegions] = useState("")
  return (
    <section className="space-y-4" aria-label="原生计划集">
      <p>逐计划明确账户与资产；最多 20 份计划（当前技术资源边界）。</p>
      {value.plans.map((plan, index) => (
        <section
          key={plan.plan_key}
          className="space-y-3 rounded-lg border p-4"
          aria-label={`计划 ${index + 1}`}
        >
          <div className="flex items-center gap-3">
            <Checkbox
              aria-label={`选中计划 ${index + 1}`}
              disabled={disabled}
              checked={selected.includes(plan.plan_key)}
              onCheckedChange={(checked) =>
                setSelected(
                  checked
                    ? [...selected, plan.plan_key]
                    : selected.filter((key) => key !== plan.plan_key),
                )
              }
            />
            <span>
              计划 {index + 1} · {plan.plan_key}
            </span>
            <Button
              variant="outline"
              disabled={disabled || value.plans.length >= 20}
              onClick={() =>
                onChange({
                  ...value,
                  plans: [
                    ...value.plans,
                    { ...structuredClone(plan), plan_key: crypto.randomUUID() },
                  ],
                })
              }
            >
              复制计划 {index + 1}
            </Button>
            <Button
              variant="ghost"
              disabled={disabled || value.plans.length === 1}
              onClick={() =>
                onChange({
                  ...value,
                  plans: value.plans.filter(
                    (p) => p.plan_key !== plan.plan_key,
                  ),
                })
              }
            >
              删除
            </Button>
          </div>
          <label className="block">
            精确目标账户
            <Input
              aria-label="精确目标账户"
              disabled={disabled}
              value={plan.selection.advertiser_id}
              onChange={(e) =>
                onChange({
                  ...value,
                  plans: value.plans.map((p) =>
                    p.plan_key === plan.plan_key
                      ? {
                          ...p,
                          selection: {
                            ...p.selection,
                            advertiser_id: e.target.value,
                          },
                        }
                      : p,
                  ),
                })
              }
            />
          </label>
          <NativeSelectionFields
            tenantId={tenantId}
            draftId={draftId}
            planKey={plan.plan_key}
            value={plan.selection}
            disabled={disabled}
            onChange={(selection) =>
              onChange({
                ...value,
                plans: value.plans.map((p) =>
                  p.plan_key === plan.plan_key ? { ...p, selection } : p,
                ),
              })
            }
          />
        </section>
      ))}
      <Button
        variant="outline"
        disabled={disabled || value.plans.length >= 20}
        onClick={() =>
          onChange({
            ...value,
            plans: [...value.plans, ...emptyNativePlanSet().plans],
          })
        }
      >
        添加计划
      </Button>
      <div className="space-y-2 rounded-lg border p-4">
        <p>
          批量编辑影响：
          {value.plans
            .filter((p) => selected.includes(p.plan_key))
            .map(
              (p) =>
                `${p.plan_key} / ${p.selection.advertiser_id} / ${p.selection.native_series_id ?? "待选择"}`,
            )
            .join("；") || "未选择计划"}
        </p>
        <Input
          aria-label="批量投放地区"
          value={regions}
          disabled={disabled}
          onChange={(e) => setRegions(e.target.value.toUpperCase())}
          placeholder="US,CA"
        />
        <Button
          disabled={disabled || !selected.length || !regions}
          onClick={() =>
            onChange({
              ...value,
              plans: value.plans.map((p) =>
                selected.includes(p.plan_key)
                  ? {
                      ...p,
                      selection: {
                        ...p.selection,
                        regions: regions.split(","),
                      },
                    }
                  : p,
              ),
            })
          }
        >
          应用到选中计划
        </Button>
      </div>
    </section>
  )
}
