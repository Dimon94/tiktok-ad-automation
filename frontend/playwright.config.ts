import { defineConfig, devices } from '@playwright/test';
import 'dotenv/config'

// 本地检查为每批分配独立端口（PLAYWRIGHT_PORT），两批检查不会复用对方的开发服务器。
const port = process.env.PLAYWRIGHT_PORT ?? '5173'
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? `http://localhost:${port}`
// CI 中工作台页面由 vite preview 提供构建产物；少数用例在浏览器内直接加载源码模块或
// tests/harness 工装页，只有开发服务器能提供，单独放在 workspace-source 项目并另起开发服务器。
// 本地调试两者是同一个开发服务器。
const sourcePort = process.env.CI ? (process.env.PLAYWRIGHT_SOURCE_PORT ?? '5174') : port
const sourceURL = process.env.PLAYWRIGHT_BASE_URL ?? `http://localhost:${sourcePort}`
const workspaceSpecs = /(business-dashboard|workspace-shell|tenants-accounts|providers|strategies|materials|r2-ingest|upload-foundation|image-composition|image-delivery|build-preparation|build-preview|build-task-pages|build-channels|build-link-config|ads-workspace|ads-reporting|ad-management|ads-metric-catalog|ads-column-prefs|ads-detail|ads-detail-config|automation-rule-form|automation-rules)\.spec\.ts/
// 按标题路径（含文件名）与标签匹配：upload-foundation 整个文件，加上标记 @dev-server 的单个用例。
const devServerOnly = /upload-foundation\.spec\.ts|@dev-server/

/**
 * Read environment variables from file.
 * https://github.com/motdotla/dotenv
 */

/**
 * See https://playwright.dev/docs/test-configuration.
 */
export default defineConfig({
  testDir: './tests',
  testIgnore: /acceptance-(batch|r2)\.spec\.ts/,
  /* Run tests in files in parallel */
  fullyParallel: true,
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 2 : 0,
  /* Opt out of parallel tests on CI. */
  workers: process.env.CI ? 1 : undefined,
  /* Reporter to use. See https://playwright.dev/docs/test-reporters */
  reporter: process.env.CI ? 'blob' : 'html',
  /* Shared settings for all the projects below. See https://playwright.dev/docs/api/class-testoptions. */
  use: {
    /* Base URL to use in actions like `await page.goto('/')`. */
    baseURL,

    /* Collect trace when retrying the failed test. See https://playwright.dev/docs/trace-viewer */
    trace: 'on-first-retry',
  },

  /* Configure projects for major browsers */
  projects: [
    { name: 'workspace-source', testMatch: workspaceSpecs, grep: devServerOnly, use: { ...devices['Desktop Chrome'], baseURL: sourceURL, storageState: { cookies: [], origins: [] } } },
    { name: 'workspace', testMatch: workspaceSpecs, grepInvert: devServerOnly, use: { ...devices['Desktop Chrome'], storageState: { cookies: [], origins: [] } } },
    { name: 'setup', testMatch: /.*\.setup\.ts/ },

    {
      name: 'chromium',
      testIgnore: workspaceSpecs,
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'playwright/.auth/user.json',
      },
      dependencies: ['setup'],
    },

    // {
    //   name: 'firefox',
    //   use: {
    //     ...devices['Desktop Firefox'],
    //     storageState: 'playwright/.auth/user.json',
    //   },
    //   dependencies: ['setup'],
    // },

    // {
    //   name: 'webkit',
    //   use: {
    //     ...devices['Desktop Safari'],
    //     storageState: 'playwright/.auth/user.json',
    //   },
    //   dependencies: ['setup'],
    // },

    /* Test against mobile viewports. */
    // {
    //   name: 'Mobile Chrome',
    //   use: { ...devices['Pixel 5'] },
    // },
    // {
    //   name: 'Mobile Safari',
    //   use: { ...devices['iPhone 12'] },
    // },

    /* Test against branded browsers. */
    // {
    //   name: 'Microsoft Edge',
    //   use: { ...devices['Desktop Edge'], channel: 'msedge' },
    // },
    // {
    //   name: 'Google Chrome',
    //   use: { ...devices['Desktop Chrome'], channel: 'chrome' },
    // },
  ],

  /* Run your local dev server before starting the tests */
  // CI 已先执行 `bun run build`：用 vite preview 直接提供构建产物，不再按页面请求现场编译模块。
  // 开发服务器在多 worker 下 CPU 争用严重，会把页面操作拖到超时；本地调试仍用开发服务器。
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : process.env.CI
      ? [
          { command: `bun run preview --port ${port} --strictPort`, url: baseURL, reuseExistingServer: false },
          { command: `bun run dev --port ${sourcePort} --strictPort`, url: sourceURL, reuseExistingServer: false },
        ]
      : {
          command: `bun run dev --port ${port} --strictPort`,
          url: baseURL,
          reuseExistingServer: true,
        },
});
