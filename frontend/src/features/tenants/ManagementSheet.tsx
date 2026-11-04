import { useBlocker } from "@tanstack/react-router"
import { type ReactNode, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { useWorkspacePageGuard } from "@/features/workspace/WorkspaceTabs"
import { cn } from "@/lib/utils"

// Guards both router transitions (including browser Back) and every Sheet close path.
export function ManagementSheet({
  title,
  description,
  dirty,
  pending = false,
  wide = false,
  className,
  onClose,
  children,
  actions,
}: {
  title: string
  description: string
  dirty: boolean
  pending?: boolean
  wide?: boolean
  className?: string
  onClose: () => void
  children: ReactNode
  actions?: ReactNode
}) {
  const [closing, setClosing] = useState(false)
  const opener = useRef<HTMLElement | null>(null)
  const retainsPage = useWorkspacePageGuard(dirty || pending, !pending)
  const blocker = useBlocker({
    shouldBlockFn: ({ next }) => (dirty || pending) && !retainsPage(next),
    enableBeforeUnload: dirty || pending,
    withResolver: true,
  })
  const blocked = closing || blocker.status === "blocked"
  const stay = () => {
    setClosing(false)
    blocker.reset?.()
  }
  const requestClose = () => {
    if (pending) return
    if (dirty) setClosing(true)
    else onClose()
  }
  return (
    <>
      <Sheet
        open
        onOpenChange={(open) => {
          if (!open) requestClose()
        }}
      >
        <SheetContent
          className={cn(
            "w-full",
            wide ? "sm:max-w-3xl" : "sm:max-w-xl",
            className,
          )}
          onOpenAutoFocus={() => {
            opener.current =
              document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            opener.current?.focus()
          }}
        >
          <SheetHeader className="border-b pr-12">
            <SheetTitle>{title}</SheetTitle>
            <SheetDescription>{description}</SheetDescription>
          </SheetHeader>
          <div className="flex-1 overflow-y-auto px-4 py-2">{children}</div>
          <SheetFooter className="border-t sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={requestClose} disabled={pending}>
              {actions ? "取消" : "关闭"}
            </Button>
            {actions}
          </SheetFooter>
        </SheetContent>
      </Sheet>
      <Dialog
        open={blocked}
        onOpenChange={(open) => {
          if (!open) stay()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>有未保存的修改</DialogTitle>
            <DialogDescription>
              离开将丢弃当前表单中的未保存修改，已保存的记录会保留。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={stay}>
              留在当前页
            </Button>
            <Button
              disabled={pending}
              onClick={() => {
                setClosing(false)
                if (blocker.status === "blocked") blocker.proceed()
                else onClose()
              }}
            >
              丢弃未保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
