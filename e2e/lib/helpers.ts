import { expect, type APIRequestContext, type Page } from '@playwright/test';

import { STORAGE_KEYS } from './env';

export type Quota = { plan: 'anon' | 'free' | 'pro'; used: number; limit: number };

export type SseTurn = {
  /** 每个元素是一条 data: {"delta": …} 事件（与 server.py 的 SSE 形状一致）。 */
  chunks: string[];
  /** done 事件里的 quota，形状同 server.py _quota_payload：{plan, used, limit}。 */
  quota: Quota;
  truncated?: boolean;
};

export type ExplainMode = 'explain' | 'followup' | 'chat';

export type ExplainStub = Partial<Record<ExplainMode, SseTurn | ((body: any) => SseTurn)>>;

export type ExplainRequest = {
  mode?: ExplainMode;
  term?: string;
  context?: string;
  style?: string;
  question?: string;
  previous?: string;
  stream?: boolean;
  room_id?: string;
  depth?: number;
  turns?: number;
  messages?: Array<{ role: string; content: string }>;
  [key: string]: unknown;
};

export function uniqueEmail(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `e2e-${tag}-${Date.now()}-${rand}@example.test`;
}

export type Account = { email: string; password: string; token: string; plan: string; quota: Quota };

/** 走真实 /api/auth/register 建号（开放注册，无需码）；返回 token 供 addInitScript 注入登录态。 */
export async function registerAccount(
  request: APIRequestContext,
  opts: { tag?: string } = {},
): Promise<Account> {
  const email = uniqueEmail(opts.tag ?? 'account');
  const password = 'e2e-pass-123';

  const res = await request.post('/api/auth/register', { data: { email, password } });
  expect(res.status(), `注册失败: ${await res.text()}`).toBe(200);
  const body = (await res.json()) as {
    token: string;
    email: string;
    plan: string;
    quota: Quota;
  };
  expect(body.token).toBeTruthy();
  return { email, password, token: body.token, plan: body.plan, quota: body.quota };
}

/** 打开工作台前的浏览器状态：关掉首次引导，可选注入真实登录 token。 */
export async function seedBrowserSession(page: Page, opts: { token?: string } = {}): Promise<void> {
  await page.addInitScript(
    ({ token, keys }) => {
      localStorage.setItem(keys.intro, '1');
      if (token) localStorage.setItem(keys.token, token);
    },
    { token: opts.token ?? '', keys: STORAGE_KEYS },
  );
}

/** 直接种一棵合法的 app 状态树（graph2d/share 等纯前端能力的最小前置数据）。 */
export async function seedAppState(page: Page, state: unknown): Promise<void> {
  await page.addInitScript(
    ({ value, keys }) => {
      localStorage.setItem(keys.state, value);
      localStorage.setItem(keys.intro, '1');
    },
    { value: JSON.stringify(state), keys: STORAGE_KEYS },
  );
}

/**
 * 打桩 /api/explain 的成功路径。
 *
 * 形状严格照 server.py `_explain_stream`：若干 `data: {"delta": …}`，
 * 最后一条 `data: {"done": true, "quota": {plan, used, limit}}`。
 * 失败时也返回一条可诊断的错误，而不是让请求漏到真实上游。
 */
export async function mockExplain(
  page: Page,
  stubs: ExplainStub,
): Promise<{ requests: ExplainRequest[] }> {
  const requests: ExplainRequest[] = [];

  await page.route('**/api/explain', async (route) => {
    const request = route.request();
    let body: ExplainRequest = {};
    try {
      body = request.postDataJSON() as ExplainRequest;
    } catch {
      body = { __raw: request.postData() } as ExplainRequest;
    }
    requests.push(body);

    const stub = body.mode ? stubs[body.mode] : undefined;
    if (!stub) {
      await route.fulfill({
        status: 500,
        contentType: 'application/json; charset=utf-8',
        body: JSON.stringify({ error: `e2e: 未打桩的 explain mode=${String(body.mode)}` }),
      });
      return;
    }

    const turn = typeof stub === 'function' ? stub(body) : stub;
    const frames = [
      ...turn.chunks.map((delta) => `data: ${JSON.stringify({ delta })}\n\n`),
      `data: ${JSON.stringify({
        done: true,
        quota: turn.quota,
        ...(turn.truncated ? { truncated: true } : {}),
      })}\n\n`,
    ].join('');

    await route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
      },
      body: frames,
    });
  });

  return { requests };
}

/**
 * 用真实 DOM Range + mouseup 触发工作台的划词逻辑（不依赖坐标/真实鼠标轨迹）。
 * 返回选中的词，选中失败时抛错。
 */
export async function selectWord(page: Page, word: string, scopeSelector: string): Promise<void> {
  const ok = await page.evaluate(
    ({ word, scopeSelector }) => {
      const scope = document.querySelector(scopeSelector);
      if (!scope) return false;

      const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
      let node: Node | null;
      while ((node = walker.nextNode())) {
        const text = node.nodeValue ?? '';
        const index = text.indexOf(word);
        if (index < 0) continue;

        const range = document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + word.length);

        const selection = window.getSelection();
        if (!selection) return false;
        selection.removeAllRanges();
        selection.addRange(range);

        const rect = range.getBoundingClientRect();
        scope.dispatchEvent(
          new MouseEvent('mouseup', {
            bubbles: true,
            clientX: rect.left + Math.max(1, rect.width / 2),
            clientY: rect.top + Math.max(1, rect.height / 2),
          }),
        );
        return true;
      }
      return false;
    },
    { word, scopeSelector },
  );

  expect(ok, `在 ${scopeSelector} 中找不到可划选的词：${word}`).toBe(true);
}

/** 从 rgb()/rgba() 里算相对亮度，用于断言「暗色确实更暗」而不是硬编码色值。 */
export async function bodyBackground(page: Page): Promise<{ css: string; luminance: number }> {
  return page.evaluate(() => {
    const css = getComputedStyle(document.body).backgroundColor;
    const m = css.match(/rgba?\(([^)]+)\)/);
    if (!m) return { css, luminance: -1 };
    const [r, g, b] = m[1].split(',').map((v) => Number(v.trim()));
    // sRGB 相对亮度（简化版即可区分明暗）
    const lin = (v: number) => {
      const c = v / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return { css, luminance: 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) };
  });
}
