import { expect, type Page } from "@playwright/test"

// Shared acceptance of the user's approved page hierarchy, including business
// pages reached with the existing API fixtures instead of a static mockup.
export async function expectWorkspaceLayout(page: Page) {
  const main = page.locator("#workspace-main")
  await expect(main.getByRole("heading", { level: 1 })).toHaveCount(1)
  await expect(main.getByRole("heading", { level: 1 })).toHaveCSS(
    "font-size",
    "22px",
  )
  await expect(
    page.locator("header").getByRole("heading", { level: 1 }),
  ).toHaveCount(0)
  await expect(main).toHaveCSS("background-color", "oklab(0.97 0 0 / 0.4)")
  for (const button of await main.locator(".bg-primary:visible").all()) {
    await expect(button).toHaveCSS("background-color", "oklch(0.205 0 0)")
  }
  const viewport = page.viewportSize()!
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(viewport.width)
  expect(
    await main.locator('[data-slot="card"]:visible').count(),
  ).toBeGreaterThan(0)
  for (const table of await main
    .locator('[data-slot="table-container"]:visible')
    .all()) {
    const geometry = await table.evaluate((el) => {
      const card = el.closest('[data-slot="card"]')
      if (!card) return null
      const outer = card.getBoundingClientRect()
      const inner = el.getBoundingClientRect()
      return {
        left: inner.left - outer.left,
        right: outer.right - inner.right,
        width: el.clientWidth,
        content: el.scrollWidth,
        scroll: getComputedStyle(el).overflowX,
      }
    })
    expect(geometry, "business tables belong to a padded card").not.toBeNull()
    expect(geometry!.left).toBeGreaterThanOrEqual(
      viewport.width < 1024 ? 16 : 24,
    )
    expect(geometry!.right).toBeGreaterThanOrEqual(
      viewport.width < 1024 ? 16 : 24,
    )
    expect(geometry!.scroll).toBe("auto")
  }
}
