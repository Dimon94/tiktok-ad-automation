import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { type CarouselPresetPublic, MaterialsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { change } from "@/features/builds/imageCompositionApi"
import { RequestError } from "@/features/tenants/shared"
export function CarouselPresetList({
  tenantId,
  write,
  destination,
  onLoaded,
}: {
  tenantId: string
  write: boolean
  destination?: { draftId: string; dramaId: string; revision: number }
  onLoaded?: (revision: number) => void
}) {
  const [name, setName] = useState(""),
    [offset, setOffset] = useState(0),
    [selected, setSelected] = useState<CarouselPresetPublic | null>(null),
    [copyName, setCopyName] = useState(""),
    [reuse, setReuse] = useState(false),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false)
  const query = useQuery({
    queryKey: ["carousel-presets", tenantId, name, offset],
    queryFn: async ({ signal }) =>
      (
        await MaterialsService.listVersions({
          path: { tenant_id: tenantId },
          query: { name, offset, limit: 20 },
          signal,
        })
      ).data,
  })
  async function confirm() {
    if (!selected) return
    setBusy(true)
    try {
      if (destination) {
        const result = await MaterialsService.loadVersion({
          path: { tenant_id: tenantId, version_id: selected.id },
          body: {
            ...change(destination.revision),
            draft_id: destination.draftId,
            drama_id: destination.dramaId,
            allow_reuse: reuse,
          },
        })
        onLoaded?.(result.data.revision)
      } else
        await MaterialsService.copyVersion({
          path: { tenant_id: tenantId, version_id: selected.id },
          body: { request_id: crypto.randomUUID(), name: copyName.trim() },
        })
      setSelected(null)
      await query.refetch()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>轮播库</CardTitle>
        <CardDescription>
          保存可复用的图片组合、顺序与已选音乐。上传图片后，在广告搭建的「准备与调整
          → 查看与调整轮播」中组合并选择「存入轮播库」。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">
          再次使用时，在对应剧目的「查看与调整轮播 →
          从轮播库添加」中加载；加载后的修改独立保存。首次搭建可直接使用图片，无需先存入轮播库。
        </p>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="carousel-search">搜索轮播</FieldLabel>
            <Input
              id="carousel-search"
              value={name}
              onChange={(e) => {
                setName(e.target.value)
                setOffset(0)
              }}
            />
          </Field>
        </FieldGroup>
        {!!(error || query.error) && (
          <RequestError error={error || query.error} />
        )}{" "}
        {query.data?.items.map((item) => (
          <div
            key={item.id}
            className="flex items-center justify-between gap-3"
          >
            <span>
              {item.name} · v{item.version} · {item.ordered_material_ids.length}{" "}
              张
            </span>
            <Button
              variant="outline"
              disabled={!write}
              onClick={() => {
                setSelected(item)
                setCopyName(`${item.name} 副本`)
                setReuse(false)
              }}
            >
              {destination ? "加载到本剧" : "复制为新版本"}
            </Button>
          </div>
        ))}
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - 20))}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            disabled={offset + 20 >= (query.data?.total ?? 0)}
            onClick={() => setOffset(offset + 20)}
          >
            下一页
          </Button>
        </div>
      </CardContent>
      {selected && (
        <Dialog
          open
          onOpenChange={(v) => {
            if (!v) setSelected(null)
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {destination ? "加载轮播" : "复制轮播版本"}
              </DialogTitle>
              <DialogDescription>
                {destination
                  ? "创建独立轮播并保护图片，不覆盖现有轮播。已排除的图片须另行恢复。"
                  : "复制版本独立保存，不更改原版本。"}
              </DialogDescription>
            </DialogHeader>
            {destination ? (
              <Button
                variant={reuse ? "default" : "outline"}
                onClick={() => setReuse(!reuse)}
              >
                {reuse ? "已确认允许图片复用" : "明确允许与现有轮播复用图片"}
              </Button>
            ) : (
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="copy-name">新版本名称</FieldLabel>
                  <Input
                    id="copy-name"
                    value={copyName}
                    onChange={(e) => setCopyName(e.target.value)}
                  />
                </Field>
              </FieldGroup>
            )}
            {!!error && <RequestError error={error} />}
            <DialogFooter>
              <Button variant="outline" onClick={() => setSelected(null)}>
                取消
              </Button>
              <Button
                disabled={busy || (!destination && !copyName.trim())}
                onClick={() => void confirm()}
              >
                确认{destination ? "加载" : "复制"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </Card>
  )
}
