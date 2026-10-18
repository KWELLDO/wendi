# 问底 WenDi · 端到端回归测试（e2e）

Playwright + 真实后端（`python3 server.py`）。只覆盖浏览器侧的关键主流程；不 mock 后端本身，
只对**外部/付费**的 `/api/explain` 成功路径打桩（形状照 `server.py` 的 SSE 分支逐字对齐）。

## 运行

```bash
cd e2e
npx playwright test                 # 全部（桌面 11 + 移动 2 + 1 个 test.fail 已知缺陷）
npx playwright test --project=desktop-chromium
npx playwright test --project=mobile-chromium
npx playwright show-report
```

前置：`node_modules` 已装、本机有 `python3`、Playwright Chromium 已下载。无需外网、无需 LLM key。

## 隔离纪律

| 项 | 值 |
|---|---|
| 端口 | `8901`（8899 是预览服务；8902 可能被设计车道占用） |
| 测试库 | `e2e/.tmp/wendi-e2e.db`（每次 `globalSetup` 删除重建） |
| 真实库 | 绝不读写 `~/.wendi/wendi.db`；`lib/env.ts` + `scripts/reset-db.py` 双重路径防呆 |
| LLM 上游 | `WENDI_BASE_URL=http://127.0.0.1:9` + 假 key：漏打桩的调用本地立即失败，不出网 |
| 服务 | `webServer` 在仓库根执行 `python3 server.py`，`reuseExistingServer: false` |

测试产物（`.tmp/`、`playwright-report/`、`test-results/`）都在 `.gitignore` 里。

## 用例地图

| 文件 | 覆盖 |
|---|---|
| `tests/landing.spec.ts` | 落地页无 pageerror/console error；Hero、导航、四个区块、法务链接；CTA 直达 `/app` 注册弹窗 |
| `tests/auth.spec.ts` | 真实邀请码注册 → 登录态 → 退出 → 重新登录；无邀请码的真实 403 `need_invite`；错误密码 401；已知缺陷（见下） |
| `tests/workspace.spec.ts` | 登录态加载；输入→发送→划词→解释→进入→追问（SSE 形状对齐）；未登录提问真实 403 闸门；空态与档位持久化 |
| `tests/share.spec.ts` | `[data-share]` → canvas 真实绘制、JSON 切片随勾选变化、下载 PNG、无外部请求 |
| `tests/theme.spec.ts` | 三态主题（跟随系统/亮/暗）：`data-theme` + `localStorage["wendi-theme"]` + 实际配色；刷新持久化；方向键/Home/End |
| `tests/mobile.spec.ts` | 390×844：落地页无横向溢出；工作台主流程；追问行五个控件不被裁切、输入框独占一行 |

所有用例都通过 `fixtures.ts` 自动收集 `pageerror`、console error、请求失败；未显式允许的错误会让用例失败。

## 选择器纪律

只用稳定锚点：`id`、`role`、`data-*`（`data-input-key`/`data-send`/`data-ask`/`data-share`/
`data-style`/`data-quote-scope`/`data-exp`/`data-msg`/`data-child`/`data-theme-val`）。
不依赖样式类名、DOM 顺序或坐标点击；划词通过 DOM `Range` + `mouseup` 触发真实处理器。

## 已知产品缺陷（用 `test.fail` 记录）

`tests/auth.spec.ts` 的「登录响应里的 quota 缺 plan…」：

- `server.py` 的 `_login` / `_register` 返回 `db.quota_state()` → `{used, limit, date}`，没有 `plan`；
- 前端 `renderQuota()` 依赖 `q.plan` 生成 tooltip（pro→Pro / anon→未登录 / 其他→免费）；
- 结果：Pro 用户登录/注册瞬间 tooltip 显示「免费」，刷新后 `/api/me`（`_quota_payload`，带 `plan`）才正确。
- 该用例以 `test.fail()` 标注：当前预期失败（计入 passed，退出码 0）。产品修复后它会转为「意外通过」，提醒移除标注。

## 未覆盖 / 需要真机或真数据

- 真实 LLM 输出质量、长回答截断、真实上游超时/断流（需要真 key 与外部服务）。
- 真实分享海报在 iOS Safari 的 `canvas.toBlob` 下载行为（无 WebKit、无真机）。
- 微信内置浏览器、系统字体差异导致的视觉效果。
- 站长后台 `/admin.html`、计量台 `/meter.html`、`graph2d.html`、反馈页的独立回归（本套件未涉及）。
- 付费兑换码 / 邀请码传播链、云同步冲突合并的完整组合（只覆盖了注册与登录响应形状）。
- 并发多用户配额、服务端进程重启、SQLite 锁竞争等后端压力场景。
