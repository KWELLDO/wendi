import { expect, test } from '../fixtures';
import { createInviteCode, registerAccount, uniqueEmail } from '../lib/helpers';

test.describe('注册 / 登录（真实前后端）', () => {
  test('邀请码注册 → 登录态进入工作台 → 退出 → 重新登录', async ({ page, request }) => {
    const email = uniqueEmail('ui-register');
    const password = 'e2e-pass-123';
    const invite = await createInviteCode(request, 7);

    await page.goto('/app/?signup=1');
    await expect(page.locator('#authModal')).toBeVisible();
    await expect(page.locator('#authSubmit')).toHaveText('注册');

    await page.fill('#authEmail', email);
    await page.fill('#authPw', password);
    await page.fill('#authInvite', invite);

    const registerResponse = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/register') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const register = await registerResponse;

    // 真实后端响应形状：200 + token + plan + invite_days + quota
    expect(register.status()).toBe(200);
    const registered = await register.json();
    expect(registered).toMatchObject({
      email,
      plan: 'pro', // 邀请码携带 7 天 Pro
      invite_days: 7,
    });
    expect(registered.token).toBeTruthy();
    // 真实 quota 形状来自 db.quota_state：{used, limit, date}（plan 在顶层返回）
    expect(registered.quota).toMatchObject({ used: 0, limit: 300 });

    // UI：注册成功、进入登录态、配额徽章接上真实 data
    await expect(page.locator('#toast')).toContainText('已注册');
    await expect(page.locator('#mineEmail')).toHaveText(email);
    await expect(page.locator('#minePlan')).toHaveText('（Pro）');
    await expect(page.locator('#authBtn')).toHaveText(email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toBeVisible();
    await expect(page.locator('#quotaBadge')).toHaveText('0/300');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBe(registered.token);

    // 刷新后登录态仍在（/api/me 恢复）
    await page.reload();
    await expect(page.locator('#authBtn')).toHaveText(email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toHaveText('0/300');

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
    expect(loggedIn).toMatchObject({ email, plan: 'pro' });
    expect(loggedIn.token).toBeTruthy();
    expect(loggedIn.token).not.toBe(registered.token);

    await expect(page.locator('#toast')).toContainText('已登录');
    await expect(page.locator('#authBtn')).toHaveText(email.split('@')[0]);
    await expect(page.locator('#quotaBadge')).toHaveText('0/300');
    expect(await page.evaluate(() => localStorage.getItem('wendi-token'))).toBe(loggedIn.token);
  });

  test('无邀请码注册被真实 need_invite 闸门拦下', async ({ page, diag }) => {
    // 真实后端对未登录/无码注册返回 403 + {error, need_invite:true}；
    // Chromium 会把这次 403 记成一条 console error，显式允许它。
    diag.allowConsoleError(/Failed to load resource: the server responded with a status of 403/);

    await page.goto('/app/?signup=1');
    await expect(page.locator('#authInvite')).toBeVisible();

    await page.fill('#authEmail', uniqueEmail('no-invite'));
    await page.fill('#authPw', 'e2e-pass-123');

    const responsePromise = page.waitForResponse(
      (res) => res.url().endsWith('/api/auth/register') && res.request().method() === 'POST',
    );
    await page.click('#authSubmit');
    const response = await responsePromise;

    expect(response.status()).toBe(403);
    const body = await response.json();
    expect(body.need_invite).toBe(true);
    expect(body.error).toContain('内测阶段需要邀请码');

    await expect(page.locator('#authMsg')).toContainText('内测阶段需要邀请码');
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

  test('登录响应 quota 带 plan：Pro 账号登录瞬间即显示 Pro', async ({
    page,
    request,
  }) => {
    // 原缺陷曾用 test.fail 留证：登录/注册响应 quota 缺 plan，Pro 被瞬间标成「免费」。
    // product-dev 已修复（db.quota_state 统一形状且与 /api/me 同源；见 db.py / server.py _login）。
    const account = await registerAccount(request, { tag: 'quota-plan' });

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
