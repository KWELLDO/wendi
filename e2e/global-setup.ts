import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

import { DB_PATH, REPO_ROOT, TMP_DIR, assertSafeTestDbPath } from './lib/env';

/**
 * 每次 `npx playwright test` 都从空库开始，保证可重复。
 *
 * 执行顺序说明：Playwright 先启动 webServer（server.py 会在启动时建库），
 * 再跑 globalSetup。这里删掉启动时建出的库并重建 schema 是安全的：
 * server.py 每次请求新开 SQLite 连接，没有常驻句柄；重建后第一条 Admin/注册请求
 * 就能看到新表。若顺序变了（先 globalSetup 后 webServer），结果仍然正确。
 */
export default async function globalSetup(): Promise<void> {
  assertSafeTestDbPath(DB_PATH);
  fs.mkdirSync(TMP_DIR, { recursive: true });

  for (const suffix of ['', '-wal', '-shm']) {
    fs.rmSync(`${DB_PATH}${suffix}`, { force: true });
  }

  execFileSync('python3', [path.join(__dirname, 'scripts', 'reset-db.py')], {
    cwd: REPO_ROOT,
    env: { ...process.env, WENDI_DB: DB_PATH },
    stdio: 'inherit',
  });
}
