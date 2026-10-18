import { expect, test } from '../fixtures';

test.describe('落地页 /', () => {
  test('加载无 pageerror / console error，关键内容与导航可见', async ({ page }) => {
    const response = await page.goto('/');
    expect(response?.status()).toBe(200);

    await expect(page).toHaveTitle(/问底 WenDi/);

    // 品牌 + Hero
    await expect(page.locator('header a[href="/"]').first()).toBeVisible();
    await expect(page.locator('#hero-title')).toBeVisible();
    await expect(page.locator('#hero-title')).toHaveText('问底WenDi');
    await expect(page.getByText('Get to the bottom of it.').first()).toBeVisible();

    // 主导航（功能链接，不是样式类选择器）
    const nav = page.getByRole('navigation', { name: '主导航' });
    await expect(nav).toBeVisible();
    for (const [name, href] of [
      ['它怎么用', '#how'],
      ['和词典的区别', '#diff'],
      ['立场', '#stance'],
      ['关于', '/about.html'],
      ['进入工作台', '/app/'],
    ] as const) {
      await expect(nav.getByRole('link', { name })).toHaveAttribute('href', href);
    }

    // 四个主体区块都在（cta-band 用 enter-title 作为锚点）
    for (const id of ['how', 'diff', 'stance', 'enter-title']) {
      await expect(page.locator(`#${id}`)).toBeVisible();
    }
    await expect(page.getByRole('heading', { name: '划词，一层层到底' })).toBeVisible();

    // CTA 入口指向真实注册/登录
    const signup = page.locator('a[href="/app/?signup=1"]');
    const login = page.locator('a[href="/app/?login=1"]');
    await expect(signup.first()).toBeVisible();
    await expect(login.first()).toBeVisible();
    expect(await signup.count()).toBeGreaterThanOrEqual(2);

    // 页脚法务链接
    for (const href of ['/about.html', '/feedback.html', '/privacy.html', '/terms.html']) {
      await expect(page.locator(`footer a[href="${href}"]`).first()).toBeVisible();
    }
  });

  test('「我有邀请码，注册」直达 /app 并打开注册弹窗', async ({ page }) => {
    await page.goto('/');
    await page.locator('a[href="/app/?signup=1"]').first().click();

    // directAuth 会立刻用 replaceState 清掉 ?signup=1，所以只断言落地结果：
    // 已经进入工作台、注册弹窗打开、邀请码输入可见。
    await expect(page).toHaveURL(/\/app\/$/);
    await expect(page.locator('#authModal')).toBeVisible();
    await expect(page.locator('#authSubmit')).toHaveText('注册');
    await expect(page.locator('#authInvite')).toBeVisible();
    await expect(page.locator('#authInviteHint')).toBeVisible();
  });
});
