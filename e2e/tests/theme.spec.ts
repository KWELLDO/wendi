import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures';
import { bodyBackground, seedBrowserSession } from '../lib/helpers';

const THEME_KEY = 'wendi-theme';
const htmlTheme = (page: Page) =>
  page.evaluate(() => document.documentElement.getAttribute('data-theme'));
const storedTheme = (page: Page) =>
  page.evaluate((key) => localStorage.getItem(key), THEME_KEY);

test.describe('主题：跟随系统 / 亮 / 暗', () => {
  test('设置面板切换主题：属性 + localStorage + 实际配色，刷新后持久化', async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await seedBrowserSession(page);
    await page.goto('/app/');

    // 默认：跟随系统（没有 data-theme，localStorage 无键）
    expect(await htmlTheme(page)).toBeNull();
    expect(await storedTheme(page)).toBeNull();
    const lightBg = await bodyBackground(page);
    expect(lightBg.luminance).toBeGreaterThan(0.5);

    // —— 切到「暗」——
    await page.click('#settingsBtn');
    const segment = page.locator('#themeSeg');
    await expect(segment).toBeVisible();
    await expect(segment.locator('[data-theme-val="system"]')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    await segment.locator('[data-theme-val="dark"]').click();

    expect(await htmlTheme(page)).toBe('dark');
    expect(await storedTheme(page)).toBe('dark');
    await expect(segment.locator('[data-theme-val="dark"]')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    await expect(segment.locator('[data-theme-val="system"]')).toHaveAttribute(
      'aria-checked',
      'false',
    );
    const darkBg = await bodyBackground(page);
    expect(darkBg.luminance).toBeLessThan(0.2);
    expect(darkBg.luminance).toBeLessThan(lightBg.luminance);

    // 刷新后仍为暗色（<head> 启动脚本在任何样式表之前应用）
    await page.reload();
    expect(await htmlTheme(page)).toBe('dark');
    expect(await storedTheme(page)).toBe('dark');
    expect((await bodyBackground(page)).luminance).toBeLessThan(0.2);

    // —— 切到「亮」——
    await page.click('#settingsBtn');
    await page.locator('#themeSeg [data-theme-val="light"]').click();
    expect(await htmlTheme(page)).toBe('light');
    expect(await storedTheme(page)).toBe('light');
    expect((await bodyBackground(page)).luminance).toBeGreaterThan(0.5);

    await page.reload();
    expect(await htmlTheme(page)).toBe('light');
    expect(await storedTheme(page)).toBe('light');
    expect((await bodyBackground(page)).luminance).toBeGreaterThan(0.5);

    // —— 回到「跟随系统」：属性移除（不是 data-theme="system"），键删除 ——
    await page.click('#settingsBtn');
    await page.locator('#themeSeg [data-theme-val="system"]').click();
    expect(await htmlTheme(page)).toBeNull();
    expect(await storedTheme(page)).toBeNull();

    // 系统变暗时页面跟着变暗（媒体查询生效）
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect
      .poll(async () => (await bodyBackground(page)).luminance)
      .toBeLessThan(0.2);

    // 刷新后仍是「跟随系统」，且在系统暗色下渲染为暗
    await page.reload();
    expect(await htmlTheme(page)).toBeNull();
    expect(await storedTheme(page)).toBeNull();
    expect((await bodyBackground(page)).luminance).toBeLessThan(0.2);

    // 系统切亮 → 跟随系统态跟着变亮
    await page.emulateMedia({ colorScheme: 'light' });
    await expect
      .poll(async () => (await bodyBackground(page)).luminance)
      .toBeGreaterThan(0.5);
  });

  test('主题分段控件支持方向键（radiogroup 键盘操作）', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await seedBrowserSession(page);
    await page.goto('/app/');
    await page.click('#settingsBtn');

    const segment = page.locator('#themeSeg');
    const system = segment.locator('[data-theme-val="system"]');

    await system.focus();
    await expect(system).toBeFocused();
    await expect(system).toHaveAttribute('tabindex', '0');

    // system → light → dark → 环绕回 system
    await page.keyboard.press('ArrowRight');
    expect(await storedTheme(page)).toBe('light');
    await expect(segment.locator('[data-theme-val="light"]')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    await expect(segment.locator('[data-theme-val="light"]')).toBeFocused();

    await page.keyboard.press('ArrowRight');
    expect(await storedTheme(page)).toBe('dark');
    expect(await htmlTheme(page)).toBe('dark');

    await page.keyboard.press('ArrowRight');
    expect(await storedTheme(page)).toBeNull();
    expect(await htmlTheme(page)).toBeNull();
    await expect(system).toBeFocused();
    await expect(system).toHaveAttribute('tabindex', '0');

    // End 跳到末项（暗），Home 回到首项（跟随系统）
    await page.keyboard.press('End');
    expect(await storedTheme(page)).toBe('dark');
    await page.keyboard.press('Home');
    expect(await storedTheme(page)).toBeNull();
  });
});
