import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { renderNameExample } from "./validation"
export const EXAMPLE_BASE = "{b30008/s328302/c3}-The Bond"
export function StrategyNamingExample({
  nameTemplate,
}: {
  nameTemplate: string
}) {
  const examples = [
    { label: "嘉书", campaign: renderNameExample(nameTemplate) },
    {
      label: "网眼",
      campaign: renderNameExample(
        nameTemplate,
        EXAMPLE_BASE,
        "328302",
        "wangyan",
      ),
    },
  ]
  return (
    <Card role="region" aria-label="广告命名示例">
      <CardHeader>
        <CardTitle>广告命名示例</CardTitle>
        <CardDescription>
          标签按填写顺序生成；网眼的版权方标识使用完整归因标识，版权方拼音使用
          wangyan。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5 text-sm">
        {examples.map(({ label, campaign }) => (
          <section
            key={label}
            aria-label={label}
            className="flex flex-col gap-2"
          >
            <p className="font-medium">{label}</p>
            {campaign ? (
              <dl className="flex flex-col gap-2">
                {[
                  ["Campaign", campaign],
                  ["Ad Group", `${campaign}-g01`],
                  ["Ad", `${campaign}-g01-sp1`],
                ].map(([level, value]) => (
                  <div key={level}>
                    <dt className="text-muted-foreground">{level}</dt>
                    <dd className="break-all font-mono">{value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p>修正名称格式后显示示例。</p>
            )}
          </section>
        ))}
        <p className="text-muted-foreground">
          示例日期与随机号仅用于说明。同一搭建批次复用随机号；实际名称和长度在搭建预览核验。
        </p>
      </CardContent>
    </Card>
  )
}
