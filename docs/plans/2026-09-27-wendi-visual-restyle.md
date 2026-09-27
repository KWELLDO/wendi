# 问底 · 视觉重做蓝图:大报编辑体 editorial-broadsheet(wendi 化)

- 日期:2026-09-27
- 决策记录:ADR 0001(本轮 Q1–Q7 已拍)
- 风格来源:design-library `editorial-broadsheet` —— **只取材质与排版契约层;不搬刊物词法**

## 0. 决策摘要

| # | 决策 |
|---|---|
| Q1 | 以 `editorial-broadsheet` 语法重铸:米白纸 / 近黑墨 / 单一编辑红 / 方角 / 发丝线 / 无阴影 |
| Q2 | 亮色默认;夜版跟随系统(`prefers-color-scheme`),无切换按钮 |
| Q3 | 系统字体栈,零外链 |
| Q4 | 范围:`web/index.html` + `web/graph2d.html`;meter / 3D 实验室暂缓 |
| Q5 | 只取材质与排版,不玩刊物词法(无报头、无大写标签腔、无首字下沉) |
| Q6 | 桌面视觉 + 窄屏不塌;触屏划词另立项 |
| Q7 | 验收:四步流程 + 引导 + 登录 + 分享卡截图对照,sign-off |

## 1. Token 契约(整块替换现 `:root`)

```css
:root{
  /* 纸与墨 */
  --paper:#fbf8f1; --paper-deep:#f4f0e4; --surface:#fffdf8; --surface-2:#f7f3e8;
  --ink:#15130f; --ink-2:#4a463e; --ink-3:#8a8478;
  --rule:#15130f; --hairline:#d8d2c4;
  /* 编辑红:入口专用 */
  --accent:#a5281c; --accent-wash:rgba(165,40,28,.10); --accent-line:rgba(165,40,28,.45);
  /* 语义墨(功能态,克制) */
  --ok:#2c6a5f; --warn:#8a6114; --err:#8f2317;
  /* 反白版口(代码块/提示条) */
  --slab-bg:#1b1813; --slab-fg:#f4f0e4; --slab-line:rgba(244,240,228,.18);
  /* 字体 */
  --serif:"Source Han Serif SC","Noto Serif CJK SC","Songti SC","STSong","SimSun",Georgia,serif;
  --sans:-apple-system,"PingFang SC","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,"JetBrains Mono",Consolas,monospace;
}
@media (prefers-color-scheme: dark){ :root{
  --paper:#1a1713; --paper-deep:#14110d; --surface:#211d18; --surface-2:#1b1713;
  --ink:#ece5d6; --ink-2:#beb5a4; --ink-3:#8d8474;
  --rule:#ece5d6; --hairline:#3a342b;
  --accent:#d76a5f; --accent-wash:rgba(215,106,95,.12); --accent-line:rgba(215,106,95,.50);
  --ok:#6bb3a4; --warn:#d9ac47; --err:#d76a5f;
  --slab-bg:#0f0d0a; --slab-fg:#ece5d6; --slab-line:rgba(236,229,214,.16);
}}
```

- 旧 token 名(`--bg/--panel/--panel2/--panel3/--line/--text/--dim/--gold/--gold-dim`)**全文件替换**,含 JS 内联色值(如 drawShare 里的 `#c9a55c`)与散装色(`#17171a`、`#26262b`、`#2a2620` 等)。
- 圆角统一为 0(方角);阴影只留模态 scrim,卡片阴影全删。
- 纸面噪点:`body{background-image:radial-gradient(rgba(21,19,15,.05) 1px,transparent 1px);background-size:4px 4px}`,夜版用 `rgba(236,229,214,.03)`。
- 红色使用律:**只做入口**(划词气泡、当前项、链接、hover 边框、进度);禁止红色大面积填充;主按钮用墨底纸字。

## 2. 字体与层级

- 内容(解释正文、对话、分享卡):`--serif`;界面控件:默认 `--sans`;元信息/编号/数据:`--mono` 10.5–11px,`letter-spacing:.08em`。
- 字号基本不动(控件 12.5–13px、内容 15–15.5px),本次换的是材质、色、线、字体**家族**,不是尺度。

## 3. 组件映射(逐件)

| 组件(现行 selector) | 新做法 |
|---|---|
| `header` | 纸底;`border-bottom:1px solid var(--rule)`;标题衬线 15px/600;`.sub` sans 12 `--ink-3` |
| 控件(`select`/`button`) | 透明底、`1px solid var(--hairline)`、方角、墨字;hover:墨水线+墨字;focus:红边;主按钮(发送/开始/进入/下载):墨底纸字,hover 红底纸字 |
| `#crumbs` | sans 12.5 `--ink-3`;当前项红;hover 红下划线 |
| `.room/.mainpane` | `--surface` 底 + 1px `--hairline`;方角无阴影 |
| `.side`(`.lists`/`.preview`) | `--paper-deep` 底 + hairline;`.lhead` mono 10.5 `--ink-3` + hairline 下线 |
| `.litem` | hover:`--accent-wash`;`.sel`:左侧 2px 红规 + 标题红 |
| `.litem .k`(深度徽标) | 阶梯改"墨的浓度":L1 墨底纸字;L2–L4 描边(墨→墨2→墨3)方角;不再四色 |
| `.preview/.mini` | mini 正文衬线;`.m-enter` 红底纸字方角;`.m-deeper` 虚线改 hairline 实线 |
| `.content .ctext` | 内容衬线 15.3px/1.85;`.msg .who` mono 10.5 `--ink-3`,用户 `--ok` |
| markdown | `h4` 墨色 600 + hairline 下缘;`code` `--surface-2` + hairline;`pre` 反白版口(`--slab-*`);`blockquote` 左 2px `--accent-line` + `--ink-2` 斜体 |
| `.qa`(追问) | 左 2px `--accent-line`;问红、答 `--ink-2` |
| `#bubble button` | 墨底纸字方角;hover 红底;`::selection{background:var(--accent-wash)}` |
| `#intro .icard` | `--surface` + 1px 墨框;h1 衬线;slogan 红斜体;按钮同控件 |
| `.acard` / `#keyModal` / `.sbox` | `--surface` + 1px 墨框方角;scrim 亮 `rgba(21,19,15,.55)` / 夜 `rgba(0,0,0,.62)` |
| `input/textarea` | `--paper` 底、hairline 边、方角;focus 红边 |
| `#toast` / `#coach` / `#closedList` | 纸面卡 + 墨/hairline;toast = 墨底纸字 |
| `#quotaBadge` | mono 10.5 `--ink-3`;降级态用 `--warn` 文字(⚡降级中) |
| `#keyModal .kstatus` 等金色字 | 一律红(入口语义:状态即关注点) |

## 4. 动效

- 保留:`.room.enter/.back` 摄像机切换、SSE 流式渲染、气泡出现、mini hover 反馈。
- 清零:不新增装饰动效;按钮/链接反馈即时(过渡 ≤ .15s)。
- 补 `@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}`。

## 5. 分享卡(canvas `drawShare`)重绘

纸片剪报,恒定亮色(不随夜版):背景 `#fbf8f1`;内框 1px `#d8d2c4`;顶部 64×4 红段;路径行 sans 15 `#8a8478`;词条 serif 600 46 `#15130f`;meta 行 sans 15(档位红 + 日期 `#8a8478`);正文 serif 23 `#15130f`;语境左 3px `#d8d2c4` + serif 19 `#4a463e`;追问标签红 serif 600 20、答 serif 20 `#4a463e`;页脚 hairline + serif 600 "问底 WenDi" + sans 小字 tagline/site。文件名不变。

## 6. `graph2d.html` 对齐

token 整块替换;SVG:节点 = 纸卡片(`--surface` + hairline,方角),选中/悬停 = 红边红题;连线 = hairline;文字墨色;图例 mono;空态纸感;头部同 index。

## 7. 窄屏("不塌"标准)

- ≤820px:`.room` 纵排(side 落到主面板之下);头部控件自动换行;模态宽 `min(100% - 24px, 360px)`。
- 验证:1024 / 768 / 390 三档,`document.documentElement.scrollWidth === clientWidth`(无横向滚动)。

## 8. 实施顺序

1. `index.html` 重写 `:root` + 全局段(纯 CSS)→ 2. 组件逐件过(按 §3 表)→ 3. `drawShare` 重绘 → 4. `graph2d.html` → 5. 截图自检 + 用户 sign-off。
   全程**不改 DOM 结构与 JS 逻辑**;类名保留,仅样式与 canvas 绘制。

## 9. 验证方案

- 本地跑真服务:`WENDI_API_KEY=<~/.secrets 现役 key> python3 server.py`(key 不打印)。
- 流程冒烟:提问 → 划词 → 解释 → 追问 → 进入下层 → Esc → 导出 md → 分享卡(下载 PNG)→ 图谱页。
- 截图矩阵:亮 1440 / 夜版(emulate dark)1440 / 窄 390 / 分享卡 PNG / graph2d;与 `samples/editorial-broadsheet.html` 并排对照。
- 计算校验:ink/paper、accent/paper、ink/slab 对比度 ≥ AA;`prefers-reduced-motion` 下无动画。
- 回归:`python3 -m unittest discover -s tests`(后端未动,跑一遍留底)。

## 10. 范围外(明确不做)

`meter.html`(站长后台,定稿后另对齐)、`graph3d-test.html`、触屏划词(行为缺陷另立项)、字体文件/外链、后端与 API 变更、刊物词法(报头/三栏/大写腔)。

## 11. 实施记录(2026-09-27)

- `web/index.html` / `web/graph2d.html` 按本蓝图完成换皮;**DOM 结构与 JS 逻辑未动**(仅样式与 canvas 绘制)。
- 验证(本地 8899 真服务 + 真实模型):
  - 流程冒烟:提问 → 回答 → 划词 → 解释(L1) → 进入 → 嵌套划词(L2) → 分享卡(下载 PNG ≈437KB) → Esc 返回,全程通过。
  - 截图矩阵:亮色(引导/对话/房间/分享弹层/登录)、夜版(对话/图谱)、窄屏 390(不塌)。
  - 对比度:墨/纸 17.5:1 · 次墨/纸 8.9:1 · 红/纸 6.8:1 · 红/面板 7.1:1 · 版口 15.5:1——全过 AA。
  - 横向溢出:1440/1024/768/390 四档 `scrollWidth === clientWidth`。
  - reduced-motion:index 过渡 0s、graph2d 节点动画 `none`。
- 已知不在本次范围:index.html 预存在 TDZ 缺陷(`(async()=>{ if(TOKEN)…})()` 位于 `let TOKEN` 之前,每次加载产生静默 ReferenceError、登录态恢复被跳过)——旧版同样存在,与本换皮无关,已记入备忘录。

## 12. 第二轮:布局与排版(2026-09-27,回应"只换皮")

应"布局排版也要换"的反馈,在第一轮材质换皮之上再做结构层重排(仍为纯 CSS + 一处渲染小改):

- **去盒**:主窗/侧栏/预览不再用圆角边框卡片;改为发丝线分区(侧栏 border-left、预览 border-top 用结构线);侧栏定宽 330px 作"边注栏"。
- **量度**:正文与输入行统一 40em 居中量度——`padding: 30px max(22px, calc((100% - 40em)/2)) 56px`;1440 宽下实测左右各 225px。
- **字级/节奏**:解释 16.5px / 行高 1.95;对话 16px / 1.95;h4 加下缘线;用户提问加 2px 红引导线;列表条目改发丝线分隔(每行一规)。
- **头部静默**:全部控件改文字型(无边框、mono/12.5px、hover 红下划线),去"工具栏感";配额徽章去框。
- **输入行**:底线式输入(无盒、聚焦转红底线),发送保持墨块。
- **断流卡片**:错误时保留已出内容 + 红色虚线尾注(`.errnote`),不再整卡换成"[失败]"。

同批修了流式的两个问题(见当次 commit):输出上限提升 + 上游无 `finish_reason` 断流的显式检测。
