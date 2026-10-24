import { expect, test } from '../fixtures';
import {
  bodyBackground,
  mockExplain,
  registerAccount,
  seedBrowserSession,
  selectWord,
} from '../lib/helpers';

test.describe('/app 工作台主流程', () => {
  test('对话发送 → 划词解释 → 追问：真实交互 + 流式渲染', async ({ page, request }) => {
    const account = await registerAccount(request, { tag: 'workspace' });
    const explain = await mockExplain(page, {
      chat: {
        chunks: ['量子力学是研究', '微观粒子的运动规律的理论。'],
        quota: { plan: 'free', used: 1, limit: 40 },
      },
      explain: {
        chunks: ['微观粒子是构成物质的', '基本单元，例如电子、夸克。'],
        quota: { plan: 'free', used: 2, limit: 40 },
      },
      followup: {
        chunks: ['电子就是一种微观粒子，', '它带负电。'],
        quota: { plan: 'free', used: 3, limit: 40 },
      },
    });

    await seedBrowserSession(page, { token: account.token });
    await page.goto('/app/');

    // 登录态与配额来自真实 /api/me
    await expect(page.locator('#authBtn')).toHaveText(account.email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toHaveText('0/40');

    // —— 主对话：输入 → 发送 ——
    const question = '什么是量子力学？';
    const convInput = page.locator('[data-input-key^="conv-"]');
    await expect(convInput).toBeVisible();
    await convInput.fill(question);
    await page.click('[data-send]');

    const assistant = page.locator('[data-msg][data-quote-scope]');
    await expect(assistant).toContainText('量子力学是研究微观粒子的运动规律的理论。');
    await expect(page.getByText(question, { exact: true })).toBeVisible();
    await expect(page.locator('#quotaBadge')).toHaveText('1/40');

    const chatRequest = explain.requests.find((r) => r.mode === 'chat');
    expect(chatRequest, '应当向 /api/explain 发出 chat 请求').toBeTruthy();
    expect(chatRequest?.stream).toBe(true);
    expect(chatRequest?.messages?.at(-1)).toEqual({ role: 'user', content: question });

    // —— 划词 → 解释 ——
    await expect(page.locator('[data-msg][data-quote-scope]')).toBeVisible();
    await selectWord(page, '微观粒子', '[data-msg][data-quote-scope]');

    const bubble = page.locator('#bubble');
    await expect(bubble).toBeVisible();
    await expect(page.locator('#bubbleTerm')).toHaveText('微观粒子');

    await page.click('#explainBtn');

    // 解释卡出现在右侧列表 + 预览，正文渲染成功
    const listItem = page.locator('[data-child]');
    await expect(listItem).toHaveCount(1);
    await expect(listItem).toContainText('微观粒子');
    await expect(page.locator('[data-exp]')).toContainText(
      '微观粒子是构成物质的基本单元，例如电子、夸克。',
    );
    await expect(page.locator('#quotaBadge')).toHaveText('2/40');

    const explainRequest = explain.requests.find((r) => r.mode === 'explain');
    expect(explainRequest, '应当向 /api/explain 发出 explain 请求').toBeTruthy();
    expect(explainRequest?.term).toBe('微观粒子');
    expect(explainRequest?.context).toContain('微观粒子');
    expect(explainRequest?.style).toBe('standard');
    expect(explainRequest?.stream).toBe(true);

    // —— 进入解释层 → 追问 ——
    await listItem.locator('[data-enter]').click();
    await expect(page.locator('#crumbs')).toContainText('微观粒子');
    await expect(page.locator('#backBtn')).toBeEnabled();
    const expBody = page.locator('[data-exp]').first();
    await expect(expBody).toContainText('微观粒子是构成物质的基本单元');

    const askInput = page.locator('[data-input-key^="ask-"]');
    await expect(askInput).toBeVisible();
    await askInput.fill('举个例子');
    await page.click('[data-ask]');

    const qa = page.locator('[data-fu]');
    await expect(qa).toHaveCount(1);
    await expect(qa).toContainText('追问：举个例子');
    await expect(qa).toContainText('电子就是一种微观粒子，它带负电。');
    await expect(page.locator('#quotaBadge')).toHaveText('3/40');

    const followupRequest = explain.requests.find((r) => r.mode === 'followup');
    expect(followupRequest, '应当向 /api/explain 发出 followup 请求').toBeTruthy();
    expect(followupRequest?.term).toBe('微观粒子');
    expect(followupRequest?.question).toBe('举个例子');
    expect(followupRequest?.previous).toContain('微观粒子是构成物质的基本单元');

    // 返回上一层：状态保留、无异常
    await page.click('#backBtn');
    await expect(page.locator('#crumbs')).toContainText('主对话');
    await expect(page.locator('#backBtn')).toBeDisabled();
    await expect(page.locator('[data-msg][data-quote-scope]')).toContainText(
      '量子力学是研究微观粒子的运动规律的理论。',
    );
  });

  test('未登录（匿名）提问：核心免费可用，配额走 IP 限速', async ({ page }) => {
    // 核心功能不再收费：匿名直接放行到打桩上游，不弹注册窗。
    const explain = await mockExplain(page, {
      chat: {
        chunks: ['量子力学是研究', '微观粒子的运动规律的理论。'],
        quota: { plan: 'anon', used: 1, limit: 20 },
      },
    });

    await seedBrowserSession(page);
    await page.goto('/app/');
    await expect(page.locator('#authBtn')).toHaveText('登录');

    const question = '什么是量子力学？';
    await page.locator('[data-input-key^="conv-"]').fill(question);
    await page.click('[data-send]');

    // 打桩上游正常回答；done 事件带来的匿名配额形状落到徽章
    await expect(page.locator('[data-msg][data-quote-scope]').first()).toContainText(
      '微观粒子的运动规律',
    );
    await expect(page.locator('#quotaBadge')).toHaveText('1/20');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBeNull();
    expect(explain.requests[0].messages?.[0]?.content).toContain(question);
  });

  test('登录后工作台空态可交互且视觉主题生效', async ({ page, request }) => {
    const account = await registerAccount(request, { tag: 'workspace-empty' });
    await seedBrowserSession(page, { token: account.token });
    await page.goto('/app/');

    await expect(page.locator('[data-input-key^="conv-"]')).toBeVisible();
    await expect(page.locator('[data-send]')).toBeVisible();
    await expect(page.locator('[data-style]')).toBeVisible();
    await expect(page.locator('[data-style]')).toHaveValue('standard');
    await expect(page.locator('[data-child]')).toHaveCount(0);

    // 档位切换写回 state（事件委托），刷新后仍保留
    await page.selectOption('[data-style]', 'rigorous');
    await page.reload();
    await expect(page.locator('[data-style]')).toHaveValue('rigorous');
    await expect(page.locator('[data-input-key^="conv-"]')).toBeVisible();

    // 主题默认跟随系统（不写 data-theme），亮色下背景明显偏亮
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBeNull();
    const bg = await bodyBackground(page);
    expect(bg.luminance).toBeGreaterThan(0.5);
  });
});
