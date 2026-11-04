import { createFileRoute } from "@tanstack/react-router"
import { BusinessDashboard } from "@/features/business/BusinessDashboard"

export const Route = createFileRoute("/_layout/tenants/$tenantId/business")({
  validateSearch: (search: Record<string, unknown>) => ({
    bc_id: typeof search.bc_id === "string" ? search.bc_id : undefined,
  }),
  component: BusinessDashboard,
  head: () => ({ meta: [{ title: "经营看板 · TK-ADA" }] }),
})
