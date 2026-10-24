import { defineConfig } from '@playwright/test';

import { BASE_URL, DB_PATH, PORT, REPO_ROOT, assertSafeTestDbPath, baseEnv } from './lib/env';

assertSafeTestDbPath(DB_PATH);

export default defineConfig({
  testDir: './tests',
  // 同一个测试库 + 同一台本地服务：串行跑，避免注册/配额互相踩。
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  globalSetup: './global-setup.ts',

  use: {
    baseURL: BASE_URL,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    actionTimeout: 8_000,
    navigationTimeout: 20_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    {
      name: 'desktop-chromium',
      testIgnore: /mobile\.spec\.ts/,
      use: { viewport: { width: 1280, height: 900 } },
    },
    {
      name: 'mobile-chromium',
      testMatch: /mobile\.spec\.ts/,
      use: {
        // 没有 WebKit / 真机：用 Chromium + 移动 viewport + touch 模拟。
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
        deviceScaleFactor: 3,
      },
    },
  ],

  webServer: {
    // 从仓库根启动产品服务本体（不是 mock server）。
    command: 'python3 server.py',
    cwd: REPO_ROOT,
    url: `${BASE_URL}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: 'ignore',
    stderr: 'ignore',
    env: {
      ...baseEnv(),
      WENDI_PORT: String(PORT),
      WENDI_DB: DB_PATH,
      WENDI_MODEL: 'deepseek-flash',
      // 测试环境显式给一个假 key，并把上游指到黑洞地址：
      // 任何漏打桩的 explain 调用会本地立即失败（测试红），绝不出网/不花钱。
      WENDI_API_KEY: 'e2e-no-external-calls',
      WENDI_BASE_URL: 'http://127.0.0.1:9',
      WENDI_ADMIN_TOKEN: 'e2e-admin-token',
      PYTHONUNBUFFERED: '1',
    },
  },
});
