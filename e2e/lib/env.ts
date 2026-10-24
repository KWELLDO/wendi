import os from 'node:os';
import path from 'node:path';

/** 被测服务固定端口：8899 是预览服务、8902 可能被设计车道占用，e2e 独占 8901。 */
export const PORT = 8901;
export const BASE_URL = `http://127.0.0.1:${PORT}`;

export const E2E_DIR = path.resolve(__dirname, '..');
export const REPO_ROOT = path.resolve(E2E_DIR, '..');
export const TMP_DIR = path.join(E2E_DIR, '.tmp');
export const DB_PATH = path.join(TMP_DIR, 'wendi-e2e.db');

/** 仅测试用管理令牌：走 /api/admin/*（发增值兑换码等）。 */
export const ADMIN_TOKEN = 'e2e-admin-token';

/** 前端 localStorage 键（见 web/app/index.html）。 */
export const STORAGE_KEYS = {
  state: 'explainer-proto-v2',
  theme: 'wendi-theme',
  token: 'wendi-token',
  intro: 'wendi-intro',
} as const;

/**
 * 防呆：测试只允许写 e2e/.tmp/ 下的库，绝不允许碰用户真实库 ~/.wendi/wendi.db。
 * 任何一处配置漂移都在启动时直接报错，而不是悄悄污染真数据。
 */
export function assertSafeTestDbPath(dbPath: string): void {
  const resolved = path.resolve(dbPath);
  const realDb = path.resolve(os.homedir(), '.wendi', 'wendi.db');
  const allowedDir = path.join(TMP_DIR, '');

  if (resolved === realDb) {
    throw new Error(`[e2e] 拒绝使用真实数据库: ${resolved}`);
  }
  if (!resolved.startsWith(allowedDir)) {
    throw new Error(`[e2e] 测试数据库必须位于 ${allowedDir} 下，实际: ${resolved}`);
  }
}

/** 供 playwright.config 的 webServer.env 使用（过滤 undefined，保证类型干净）。 */
export function baseEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined) env[key] = value;
  }
  return env;
}
