import { statSync } from 'node:fs';

import { expect, test } from '../fixtures';
import { seedAppState } from '../lib/helpers';

type SeedCard = {
  id: string;
  kind: 'exp';
  children: never[];
  term: string;
  context: string;
  parentTerm: null;
  style: 'standard';
  text: string;
  loading: boolean;
  error: null;
  followups: Array<{ q: string; a: string }>;
  createdAt: number;
};

function seedState(): unknown {
  const root = {
    id: 'e2e-root',
    kind: 'conv',
    children: [] as SeedCard[],
    messages: [
      { role: 'user', text: '什么是量子力学？', at: Date.now() - 90_000 },
      { role: 'assistant', text: '量子力学研究微观粒子的运动规律。', at: Date.now() - 80_000 },
    ],
  };
  const card: SeedCard = {
    id: 'e2e-card',
    kind: 'exp',
    children: [],
    term: '微观粒子',
    context: '量子力学研究微观粒子的运动规律。',
    parentTerm: null,
    style: 'standard',
    text: '微观粒子是构成物质的基本单元，例如电子、夸克。',
    loading: false,
    error: null,
    followups: [{ q: '举个例子', a: '比如电子。' }],
    createdAt: Date.now() - 60_000,
  };
  root.children.push(card);

  return {
    root,
    focusPath: [root.id, card.id], // 直接停在这张解释卡上，主区就是可分享的卡片
    selected: {},
    removed: [],
    defaultStyle: 'standard',
  };
}

test.describe('分享卡片', () => {
  test('[data-share] 打开分享模态，canvas 真实绘制、JSON 切片随选项变化、可下载 PNG', async ({
    page,
  }) => {
    const externalRequests: string[] = [];
    page.on('request', (req) => {
      const url = new URL(req.url());
      if (!/^https?:$/.test(url.protocol)) return; // blob:/data: 是本地下载，不算外网
      if (url.hostname !== '127.0.0.1') externalRequests.push(req.url());
    });

    await seedAppState(page, seedState());
    await page.goto('/app/');

    const shareButton = page.locator('[data-share]');
    await expect(shareButton).toBeVisible();
    await shareButton.click();

    const modal = page.locator('#shareModal');
    await expect(modal).toBeVisible();

    // canvas 是按 2x 渲染的真实位图（W=750 → width=1500），且确实画了东西
    const canvasInfo = await page.evaluate(() => {
      const canvas = document.querySelector('#shareCanvas') as HTMLCanvasElement | null;
      if (!canvas) return null;
      const ctx = canvas.getContext('2d');
      if (!ctx) return null;
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      let darkPixels = 0;
      // 每 8 个像素采样一次即可区分「空白画布」和「有文字/边框」
      for (let i = 0; i < data.length; i += 32) {
        const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
        if (r < 140 && g < 140 && b < 140) darkPixels += 1;
      }
      return { width: canvas.width, height: canvas.height, darkPixels };
    });

    expect(canvasInfo).not.toBeNull();
    expect(canvasInfo?.width).toBe(1500);
    expect(canvasInfo?.height ?? 0).toBeGreaterThan(300);
    expect(canvasInfo?.darkPixels ?? 0).toBeGreaterThan(300);

    // JSON 切片 = 未来 permalink 的请求体，形状必须正确
    await page.click('#shareJsonBtn');
    const jsonPanel = page.locator('#shareJson');
    await expect(jsonPanel).toBeVisible();

    const payload = JSON.parse(await jsonPanel.innerText());
    expect(payload).toMatchObject({
      v: 1,
      app: '问底 WenDi',
      term: '微观粒子',
      style: '标准',
    });
    expect(payload.text).toContain('微观粒子是构成物质的基本单元');
    expect(payload.chain).toEqual(['微观粒子']);
    expect(payload.context).toContain('量子力学研究微观粒子');
    expect(payload.followups).toEqual([{ q: '举个例子', a: '比如电子。' }]);

    // 勾选项即时影响 JSON 与画布（onchange → drawShare + refreshShareJson）
    await page.uncheck('#optCtx');
    const withoutContext = JSON.parse(await jsonPanel.innerText());
    expect(withoutContext.context).toBeUndefined();
    expect(withoutContext.followups).toEqual([{ q: '举个例子', a: '比如电子。' }]);

    await page.uncheck('#optFu');
    const withoutFollowups = JSON.parse(await jsonPanel.innerText());
    expect(withoutFollowups.followups).toBeUndefined();

    await page.uncheck('#optPath');
    const withoutPath = JSON.parse(await jsonPanel.innerText());
    expect(withoutPath.chain).toBeUndefined();

    // 勾回来：选项可逆，不残留错误状态
    await page.check('#optPath');
    await page.check('#optCtx');
    await page.check('#optFu');
    const restored = JSON.parse(await jsonPanel.innerText());
    expect(restored.chain).toEqual(['微观粒子']);
    expect(restored.context).toContain('量子力学研究微观粒子');
    expect(restored.followups).toHaveLength(1);

    // 下载 PNG：真实 Blob → <a download>，文件名按卡片标题生成
    const downloadPromise = page.waitForEvent('download');
    await page.click('#shareDl');
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe('问底-微观粒子.png');
    const downloadPath = await download.path();
    expect(downloadPath).toBeTruthy();
    expect(statSync(downloadPath as string).size).toBeGreaterThan(1_000);

    // 关闭后模态消失
    await page.click('#shareClose');
    await expect(modal).toBeHidden();

    // 分享完全本地完成，不请求任何外部服务
    expect(externalRequests).toEqual([]);
  });
});
