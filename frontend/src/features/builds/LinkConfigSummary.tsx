import type { LinkConfigSchema } from "@/client"

const optionLabel = (schema: LinkConfigSchema, name: string, value: unknown) =>
  schema.fields
    .find((field) => field.name === name)
    ?.options?.find((option) => option.value === String(value))?.label ??
  String(value)

// 按“本次调整 > 按剧自动 > 应用默认 > 系统默认”展示每个参数实际生效的值与来源。
export function LinkConfigSummary({
  schema,
  overrides,
  compact = false,
}: {
  schema: LinkConfigSchema
  overrides: Record<string, number>
  compact?: boolean
}) {
  if (!schema.fields.length)
    return (
      <p className="text-sm text-muted-foreground">
        参数与版权方后台一致，无需设置
      </p>
    )
  return (
    <ul
      className={
        compact
          ? "flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"
          : "flex flex-col gap-1 text-sm"
      }
    >
      {schema.fields.map((field) => {
        const override = overrides[field.name]
        const appDefault = schema.application_defaults?.[field.name]
        let text: string
        if (override !== undefined)
          text = `${field.label} ${optionLabel(schema, field.name, override)}（本次调整）`
        else if (field.source === "per_drama")
          text = `${field.label}：${field.rule}`
        else if (appDefault !== undefined && appDefault !== null)
          text = `${field.label} ${optionLabel(schema, field.name, appDefault)}（应用默认）`
        else if (schema.defaults?.[field.name] !== undefined)
          text = `${field.label} ${String(schema.defaults[field.name])}（系统默认）`
        else text = `${field.label}：未设置`
        return <li key={field.name}>{text}</li>
      })}
    </ul>
  )
}
