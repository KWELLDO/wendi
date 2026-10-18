import { expect, test } from '../fixtures';
import {
  mockExplain,
  registerAccount,
  seedBrowserSession,
  selectWord,
} from '../lib/helpers';

/**
 * 390×844 移动端 viewport（Chromium 模拟，无 WebKit / 真机）。
 * 重点回归：≤560px 时追问行会换行（输入框独占一行），五个控件不能被 .mainpane 裁掉。
 */
test.describe('移动端 390×844', () => {
  test('落地页无横向溢出，Hero 与 CTA 可见', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#hero-title')).toBeVisible();
    await expect(page.locator('a[href="/app/?signup=1"]').first()).toBeVisible();
    await expect(page.locator('a[href="/app/?login=1"]').first()).toBeVisible();

    const layout = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.innerWidth + 1);
  });

  test('工作台对话、解释、追问在窄屏可用，追问行控件不被裁切', async ({ page, request }) => {
    const account = await registerAccount(request, { tag: 'mobile' });
    const explain = await mockExplain(page, {
      chat: {
        chunks: ['量子力学研究', '微观粒子的运动规律。'],
        quota: { plan: 'pro', used: 1, limit: 300 },
      },
      explain: {
        chunks: ['微观粒子是构成物质的', '基本单元。'],
        quota: { plan: 'pro', used: 2, limit: 300 },
      },
      followup: {
        chunks: ['比如电子。'],
        quota: { plan: 'pro', used: 3, limit: 300 },
      },
    });

    await seedBrowserSession(page, { token: account.token });
    await page.goto('/app/');

    // 顶栏三个动作都在视口内
    for (const id of ['#settingsBtn', '#authBtn']) {
      const box = await page.locator(id).boundingBox();
      expect(box, `${id} 应有布局尺寸`).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(390.5);
    }

    // 主对话：输入 → 发送
    const convInput = page.locator('[data-input-key^="conv-"]');
    await expect(convInput).toBeVisible();
    await convInput.fill('什么是量子力学？');
    await page.click('[data-send]');
    await expect(page.locator('[data-msg][data-quote-scope]')).toContainText(
      '量子力学研究微观粒子的运动规律。',
    );

    // 划词 → 解释 → 进入（同桌面流程）
    await selectWord(page, '微观粒子', '[data-msg][data-quote-scope]');
    await expect(page.locator('#bubble')).toBeVisible();
    await page.click('#explainBtn');
    await expect(page.locator('[data-child]')).toContainText('微观粒子');
    await page.locator('[data-child] [data-enter]').click();
    await expect(page.locator('#crumbs')).toContainText('微观粒子');

    // 追问行：输入框 + 问 + 档位 + 重讲 + 分享，全部完整落在视口宽度内
    const askInput = page.locator('[data-input-key^="ask-"]');
    await askInput.scrollIntoViewIfNeeded();
    await expect(askInput).toBeVisible();
    await expect(page.locator('[data-ask]')).toBeVisible();
    await expect(page.locator('[data-share]')).toBeVisible();
    await expect(page.locator('[data-rexplain]')).toBeVisible();
    await expect(page.locator('[data-style]')).toBeVisible();

    const controls = [
      '[data-input-key^="ask-"]',
      '[data-ask]',
      '[data-style]',
      '[data-rexplain]',
      '[data-share]',
    ];
    for (const selector of controls) {
      const box = await page.locator(selector).boundingBox();
      expect(box, `${selector} 应有布局尺寸`).not.toBeNull();
      expect(box!.x, `${selector} 左侧被裁切`).toBeGreaterThanOrEqual(-0.5);
      expect(
        box!.x + box!.width,
        `${selector} 右侧超出 390px 视口（被裁切）`,
      ).toBeLessThanOrEqual(390.5);
    }

    // 窄屏下输入框独占一行：宽度明显大于按钮行，可正常输入
    const inputBox = await askInput.boundingBox();
    const askBox = await page.locator('[data-ask]').boundingBox();
    expect(inputBox!.width).toBeGreaterThan(askBox!.width * 2);

    // 追问仍然可用
    await askInput.fill('举个例子');
    await page.click('[data-ask]');
    await expect(page.locator('[data-fu]')).toContainText('比如电子。');
    await expect(page.locator('#quotaBadge')).toHaveText('3/300');

    // 没有横向滚动
    const layout = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.innerWidth + 1);
    expect(explain.requests.map((r) => r.mode)).toEqual(['chat', 'explain', 'followup']);
  });
});
