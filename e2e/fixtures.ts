import { test as base } from '@playwright/test';

export type Diagnostics = {
  /** 所有 console error 原文（按出现顺序） */
  consoleErrors: string[];
  /** 未捕获异常（pageerror） */
  pageErrors: string[];
  /** 非导航取消造成的请求失败 */
  failedRequests: string[];
  /** 声明「本用例预期会出现」的 console error（例：闸门用例真实的 403 资源错误）。 */
  allowConsoleError(pattern: RegExp): void;
};

/**
 * 浏览器诊断夹具：每个用例结束自动断言
 *   - 没有未捕获异常（pageerror）
 *   - 没有未被显式允许的 console error
 *   - 没有意外的请求失败
 * 允许项必须由用例显式声明，因此不会把真实缺陷一起吞掉。
 */
export const test = base.extend<{ diag: Diagnostics }>({
  diag: async ({ page }, use) => {
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    const failedRequests: string[] = [];
    const allowed: RegExp[] = [];

    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => {
      pageErrors.push(err.stack || err.message);
    });
    page.on('requestfailed', (request) => {
      const reason = request.failure()?.errorText ?? 'unknown';
      // 导航/SSE 关闭造成的 ERR_ABORTED 不是应用错误
      if (reason.includes('ERR_ABORTED')) return;
      failedRequests.push(`${request.method()} ${request.url()} -> ${reason}`);
    });

    const diag: Diagnostics = {
      consoleErrors,
      pageErrors,
      failedRequests,
      allowConsoleError(pattern) {
        allowed.push(pattern);
      },
    };

    await use(diag);

    const problems: string[] = [];

    for (const text of consoleErrors) {
      if (allowed.some((pattern) => pattern.test(text))) continue;
      problems.push(`未预期的 console error: ${text}`);
    }
    for (const err of pageErrors) {
      problems.push(`未捕获异常(pageerror): ${err}`);
    }
    for (const failed of failedRequests) {
      problems.push(`请求失败: ${failed}`);
    }

    if (problems.length > 0) {
      throw new Error(`浏览器诊断断言失败：\n- ${problems.join('\n- ')}`);
    }
  },
});

export { expect } from '@playwright/test';
