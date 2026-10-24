import { expect, test } from '../fixtures';
import { ADMIN_TOKEN } from '../lib/env';
import { registerAccount, uniqueEmail } from '../lib/helpers';

test.describe('注册 / 登录（真实前后端）', () => {
  test('开放注册 → 登录态进入工作台 → 退出 → 重新登录', async ({ page, request }) => {
    const email = uniqueEmail('ui-register');
    const password = 'e2e-pass-123';

    await page.goto('/app/?signup=1');
    await expect(page.locator('#authModal')).toBeVisible();
    await expect(page.locator('#authSubmit')).toHaveText('注册');

    await page.fill('#authEmail', email);
    await page.fill('#authPw', password);

    const registerResponse = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/register') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const register = await registerResponse;

    // 真实后端响应形状：200 + token + plan + quota（开放注册 = free，Pro 靠兑换码）
    expect(register.status()).toBe(200);
    const registered = await register.json();
    expect(registered).toMatchObject({ email, plan: 'free' });
    expect(registered.token).toBeTruthy();
    // 真实 quota 形状来自 db.quota_state：{used, limit, date}（plan 在顶层返回）
    expect(registered.quota).toMatchObject({ used: 0, limit: 40 });

    // UI：注册成功、进入登录态、配额徽章接上真实 data
    await expect(page.locator('#toast')).toContainText('已注册');
    await expect(page.locator('#mineEmail')).toHaveText(email);
    await expect(page.locator('#minePlan')).toHaveText('（免费）');
    await expect(page.locator('#authBtn')).toHaveText(email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toBeVisible();
    await expect(page.locator('#quotaBadge')).toHaveText('0/40');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBe(registered.token);

    // 刷新后登录态仍在（/api/me 恢复）
    await page.reload();
    await expect(page.locator('#authBtn')).toHaveText(email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toHaveText('0/40');

    // 首次访问的引导卡在登录态下给「开始」而不是注册入口；点掉它
    const intro = page.locator('#intro');
    await expect(intro).toBeVisible();
    await expect(page.locator('#ibtnsAuthed')).toBeVisible();
    await page.click('#introGo');
    await expect(intro).toBeHidden();

    // 退出：token 清掉、回到未登录 UI
    await page.click('#authBtn');
    await expect(page.locator('#authMine')).toBeVisible();
    await page.click('#logoutBtn');
    await expect(page.locator('#authForm')).toBeVisible();
    await expect(page.locator('#authBtn')).toHaveText('登录');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBeNull();

    // 重新登录（页面刷新后默认就是登录模式；走真实 /api/auth/login）
    await expect(page.locator('#authSubmit')).toHaveText('登录');
    await page.fill('#authEmail', email);
    await page.fill('#authPw', password);

    const loginResponse = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/login') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const login = await loginResponse;

    expect(login.status()).toBe(200);
    const loggedIn = await login.json();
    expect(loggedIn).toMatchObject({ email, plan: 'free' });
    expect(loggedIn.token).toBeTruthy();
    expect(loggedIn.token).not.toBe(registered.token);

    await expect(page.locator('#toast')).toContainText('已登录');
    await expect(page.locator('#authBtn')).toHaveText(email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toHaveText('0/40');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBe(loggedIn.token);
  });

  test('重复邮箱注册被真实 409 拦下', async ({ page, request, diag }) => {
    // 开放注册的守门规则只剩「邮箱唯一」；真实后端返回 409 + {error}。
    // Chromium 会把 4xx 记成一条 console error，显式允许它。
    diag.allowConsoleError(/Failed to load resource: the server responded with a status of 409/);

    const account = await registerAccount(request, { tag: 'dup-email' });

    await page.goto('/app/?signup=1');
    await expect(page.locator('#authModal')).toBeVisible();
    await page.fill('#authEmail', account.email);
    await page.fill('#authPw', 'e2e-pass-123');

    const responsePromise = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/register') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const response = await responsePromise;

    expect(response.status()).toBe(409);
    const body = await response.json();
    expect(body.error).toContain('已经注册过');

    await expect(page.locator('#authMsg')).toContainText('已经注册过');
    // 失败后仍然是未登录，且没有拿到 token
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBeNull();
    await expect(page.locator('#authForm')).toBeVisible();
  });

  test('密码错误登录：真实 401 与错误文案', async ({ page, request, diag }) => {
    diag.allowConsoleError(/Failed to load resource: the server responded with a status of 401/);

    const account = await registerAccount(request, { tag: 'wrong-password' });

    await page.goto('/app/?login=1');
    await expect(page.locator('#authSubmit')).toHaveText('登录');

    await page.fill('#authEmail', account.email);
    await page.fill('#authPw', 'definitely-not-the-password');

    const responsePromise = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/login') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const response = await responsePromise;

    expect(response.status()).toBe(401);
    expect(await response.json()).toMatchObject({ error: '邮箱或密码不正确' });
    await expect(page.locator('#authMsg')).toContainText('邮箱或密码不正确');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBeNull();
  });

  test('兑换码开通 Pro 后，登录响应 quota 带 plan：登录瞬间即显示 Pro', async ({
    page,
    request,
  }) => {
    // 原缺陷曾用 test.fail 留证：登录/注册响应 quota 缺 plan，Pro 被瞬间标成「免费」。
    // product-dev 已修复（db.quota_state 统一形状且与 /api/me 同源；见 db.py / server.py _login）。
    // Pro 现在只来自增值兑换码：admin 发码 → 用户 /api/redeem。
    const account = await registerAccount(request, { tag: 'quota-plan' });
    const gen = await request.post('/api/admin/codes', {
      headers: { Authorization: `Bearer ${ADMIN_TOKEN}` },
      data: { n: 1, days: 30, note: 'e2e-pro' },
    });
    expect(gen.status()).toBe(200);
    const { codes } = (await gen.json()) as { codes: string[] };
    const redeem = await request.post('/api/redeem', {
      headers: { Authorization: `Bearer ${account.token}` },
      data: { code: codes[0] },
    });
    expect(redeem.status()).toBe(200);

    await page.goto('/app/?login=1');
    await page.fill('#authEmail', account.email);
    await page.fill('#authPw', account.password);

    const responsePromise = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/login') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const response = await responsePromise;
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body.plan).toBe('pro');
    expect(body.quota).toHaveProperty('plan', 'pro'); // 修复后：quota 与 /api/me 同形
    await expect(page.locator('#quotaBadge')).toHaveText('0/300');
    await expect(page.locator('#quotaBadge')).toHaveAttribute('title', /^Pro /);
  });
});
