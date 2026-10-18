# 0001 · 视觉方向:大报编辑体(wendi 化)

- 状态:accepted
- 日期:2026-09-27
- 修订:2026-10-16(夜版改三态主题,见文末 Revision)
- 修订:2026-10-16(`meter.html` 换皮到纸墨编辑体、3D 实验室下线,见文末 Revision)
- 修订:2026-10-18(原生弹窗退场 + 全站共享 `wdiag`;工作台首屏卡瘦身;z-index 语义化,见文末 Revision)

主界面/图谱/计量台此前是三套互不相关的暗色观感(暗灰 + 金为主),无排版层级、无主张;实测线上四屏后,决定采用设计库 `editorial-broadsheet`(大报编辑体)的**材质与排版契约**重做用户可见面:新闻纸底 + 近黑墨 + 单一编辑红 + 发丝线 + 方角,衬线/无衬线/等宽三层字体;亮色默认、夜版跟随系统(`prefers-color-scheme`,无切换按钮——**此句已修订,见文末 Revision**);**只取契约层,不搬刊物词法**(无报头/三栏/大写标签等报纸道具);交互性动效保留(房间进出/流式/气泡),装饰动效清零。本次范围 `web/index.html` + `web/graph2d.html`。

## Considered Options

- **家族「档案纸 · 编辑体」**(象牙 + 藏青 + 金):与 kwelldo 站群同源,但气质更"档案室";且"红 = 入口"的语义更贴"划词"这个核心交互。
- **保留暗色并精修**:方向不变,失败模式不变(无主张)。
- **`dark-academia`(暗色书斋)**:戏剧化、长期易腻,且与"不评判"的产品立场相冲。

## Consequences

- 分享卡片(canvas)按同套 token 重绘为"纸片剪报";分享卡不随夜版反色——纸制品是印刷物。
- 夜版为新增设计物(此前不存在);设计库 `editorial-broadsheet` spec 的暗色块由本项目首定义。
- `meter.html`(站长后台)与 3D 实验室暂不换皮;窄屏只保"不塌";触屏划词是行为缺陷,另行立项。
- 实现蓝图:`docs/plans/2026-09-27-wendi-visual-restyle.md`;风格登记:`design-library/styles/editorial-broadsheet.md`。

## Revision:夜版由「跟随系统」改为三态(跟随系统 / 亮 / 暗)(2026-10-16)

**推翻**:上文「夜版跟随系统(`prefers-color-scheme`,无切换按钮)」中被推翻的是**「无切换按钮」**这一条——产品负责人当日要求给用户手动选择的入口。上文其余契约(亮色默认、单一编辑红、方角、发丝线、三层字体、动效边界、「只取契约层不搬刊物词法」)全部继续有效。

**新决定**:

- 主题三态:**跟随系统 / 亮 / 暗**,**默认跟随系统**(与旧行为等价,不选过的用户观感不变)。
- 控件位置:**「设置 → 外观」**里的三态分段控件(`role="radiogroup"` + `role="radio"` + `aria-checked`,方向键可切换),**不进报头**——报头刚从 10 个信息位收到 4 个,不再加东西。落地页不放控件。
- 跨页契约(冻结,各页一致):
  - 存储:`localStorage["wendi-theme"] = "light" | "dark"`;**键缺失或值为非法 = 跟随系统**。
  - 生效:手动选定时 `<html data-theme="light|dark">`;**跟随系统时该属性被移除**(不是设成 `"system"`),夜版仍由 `@media (prefers-color-scheme: dark)` 命中。
  - `<head>` 内、**任何 `<link rel="stylesheet">` 之前**执行同步启动脚本,消首帧闪色(FOUC)。
  - 切换后派发 `window` 事件 `wendi:theme`(`detail.theme ∈ {"system","light","dark"}`),供其它页面/组件响应。
  - 跟随系统时监听 `matchMedia("(prefers-color-scheme: dark)")` 的 `change`,运行期系统明暗变化即时跟随;手动选亮/暗时**不受**系统变化影响。
- 暗色 token 单一真源:具体色值只在 `:root` 的 `--dk-*` 里写一份;「媒体查询」与「属性选择器」两个入口只做「token 名 → `--dk-*`」的改名映射(两块必须同步,已在注释中标注,且紧挨着放)。
- `<html>` 设 `color-scheme`(`light dark` / `light` / `dark`),滚动条与原生表单控件配色跟着主题走。

**已知未覆盖**(诚实记录,非本次范围):

- `<meta name="theme-color">`(移动端浏览器 chrome 配色)按 HTML 规范只能跟媒体查询,无法随手动选择变:手动选**暗**而系统为**亮**时,移动端地址栏仍是亮色。要修需用 JS 改写 meta,未纳入本次契约。
- 分享卡片(canvas)继续用固定的亮色纸面绘制,不随任何主题反色——与上条 Consequences 一致,三态下不变。

**本次实现范围**:`web/site.css`(token 结构 + 暗色映射 + `color-scheme`)、`web/app/index.html`(启动脚本 + 内联 token 三态覆盖 + 设置面板「外观」分组)、`web/index.html`(启动脚本 + 版口奶白 token 的三态映射)。其余文档页(关于 / 隐私 / 条款 / 反馈 / 站长后台)按同一契约补启动脚本即可生效;`meter.html` 属另一套 terracotta 系统,本次不纳入。

## Revision:`meter.html` 换皮到纸墨编辑体、3D 实验室下线(2026-10-16)

**推翻**:上文 Consequences「`meter.html`(站长后台)与 3D 实验室暂不换皮」——两条均被推翻:

- 产品负责人当日要求:`meter.html` 从自带的一整套 terracotta 体系换皮到纸墨编辑体。原状与本 ADR 全面冲突——独立色板(`--terra:#c2410c` 等)、圆角 8–12px 与 `border-radius:999px` 胶囊、`.22em` 字距的大写 kicker、衬线正文、`sage / gold / blood` 三语义色。后果:从设置面板与站长后台都能进本页,用户看到的是「两个长得不像同一个产品的后台」。
- `graph3d-test.html`(被本 ADR 否决的旧暗金体系:`#141416` + `#c9a55c` + 圆角)删除。

**新决定**:

- `meter.html` 对齐 `admin.html`(站长后台)的模式:`<link rel="stylesheet" href="/site.css">` + 页内 `<style>` 只放**控制台私有**的、密度更高的表格 / 状态块 / KPI / 趋势图样式。删掉 meter 自带的整个 token 定义块(色值唯一真源回到 `site.css`,后续抽到 `tokens.css` 时本页无需再动)。
- 逐项换法:`sage / gold / blood` → `--ok / --warn / --err`;圆角全部归 0(方角),`border-radius:999px` 胶囊 → `.state` 式描边方块;kicker(大写 + `.22em`)→ 元信息风格(mono / 10.5px / 字距 `.08em`,对齐 `.lhead` / `.ptitle`);正文 serif → sans(控制台不是阅读物),数字保持 mono;terracotta 强调色 → 单一编辑红 `var(--accent)`(遵守既有纪律:红色只做入口,不做大面积填充)。功能与 JS 不动。
- `<head>` 内、任何 `<link rel="stylesheet">` **之前**插入三态启动脚本(与全站逐字一致)。
- **关键约束(坑,务必记)**:`meter.html` 有**第二个暗色出口**——`body` 的点阵背景(`@media (prefers-color-scheme: dark){ body{ background-image:radial-gradient(...) } }`)。换肤时若只处理 token 块,「系统亮 + 用户强选暗」时点阵背景会**漏色**。本次做法:**删掉 meter 自带的 body 背景覆盖**,改为直接吃 `site.css` 的 `var(--noise)`(`body{background:var(--paper);background-image:radial-gradient(var(--noise) 1px,transparent 1px)}`)——该 token 在「系统暗」与「手动暗」两个入口均已映射,故三态自动跟随,不再有独立出口。
- `graph3d-test.html`:无任何入口链接,但 URL 可达——等于既定决策留了一扇没关的门,故 `git rm` 闭合。`deploy/deploy.sh` 保留 `--exclude 'graph3d-test.html'` 一行作为历史记录(删文件后无害)。

**本次实现范围**:`web/meter.html`、删除 `web/graph3d-test.html`、本 ADR、`docs/project-and-business.md` 的引用清理。`web/site.css`、`web/app/index.html`、`web/graph2d.html`、`web/index.html` 未动。

## Revision:原生弹窗退场、全站共享 `wdiag`;工作台首屏卡瘦身;z-index 语义化(2026-10-18)

**背景**:前两轮把「材质与排版契约」铺到了用户可见面,但**交互层原封未动**——全站还剩 19 处浏览器原生 `confirm / prompt / alert`(工作台 3、图谱 `graph2d` 3、站长后台 `admin` 13)。这些框由操作系统绘制:系统字体、系统圆角、跟随 OS 而非站点主题,出现在纸墨编辑体里如同贴了一张某系统截图。也就是说,**「只取契约层,不搬刊物词法」在材质上做到了,在交互上没做**——原生对话框是这套契约上最大的一处漏,且漏在最需要「这个产品被人设计过」的破坏性动作上(清空、删云端副本)。

**新决定**:

- 新增全站共享的对话框 / 轻提示组件:`web/ui.js` + `web/ui.css`,暴露 `window.wdiag = { confirm, prompt, toast }`。各页在 `tokens.css` **之后**引 `ui.css` 与 `ui.js`(`defer`),页面**不预置任何标记**——DOM 由 JS 按需创建。(`ui.js` 顶部注释即契约正文;`ui.css` 顶部注释即 DOM 结构说明。)
- 实现方式:**原生 `<dialog>` + `showModal()`**,吃浏览器 top layer 与 `inert`,而不是自绘遮罩 + 手写焦点陷阱;遮罩走 `::backdrop` 的 `var(--scrim)`。轻提示同理走 **popover**(`showPopover()` 进 top layer)——设计意图是**提示能盖在模态之上**:确认框出现时再来一条状态回执,提示不该被吃掉。
- 视觉延续同一套契约:方角、发丝线、纸面墨字;红只做两处小面积语义色(危险按钮底、提示左边条),不做大面积填充。`ui.css` **不写**任何主题分支,只吃 `tokens.css` 的 token——三态主题因此自动成立。这与 `meter.html` 那次的「第二个暗色出口」是同一类坑,本次从结构上避免。
- **`alert()` 一律换 `wdiag.toast`(不阻塞),不做成模态框**。理由:这 19 处里的 `alert` 全是**状态回执**(「已复制 N 个」「校验未通过」),不是需要用户做决定的动作;做成模态等于让用户为一条通知多付一次点击。
- 行为契约(与视觉同等重要,后续页面照此写):
  - 焦点:打开前记 `document.activeElement`,关闭后**归还**;`danger: true` 时初始焦点落**「取消」**而非确定(防误按 Enter 触发破坏性动作)。
  - 收口:**Promise 单次 resolve 闸门**——确定 / 取消 / Esc / 点遮罩四条路径谁先到谁算,后来的全部丢弃。
  - `prompt` 取消返回 **`null`**(与原生语义一致;`admin.html` 原有的 `=== null` 守卫靠这条)。
  - `showModal()` 不可用时**退化到原生 `confirm / prompt`**——宁可暂时难看,不在无 dialog 支持的环境里静默失能。
- **z-index 语义化**:`tokens.css` 新增八档命名层级 `--z-sticky / --z-pill / --z-coach / --z-toast / --z-modal / --z-modal-hi / --z-modal-top / --z-max`,替代裸数字。本次**只整理 `web/app/index.html`**(全站唯一有八档无序裸值的文件);`admin.html` / `graph2d.html` 的 0–10 整数各自处在**独立的局部层叠上下文**内,跨页比较无意义,**不动**——避免为统一而制造真实的层级错乱。
- **工作台首屏卡(intro)瘦身**:信息按「现在要做什么 / 想了解更多」分层——两行操作说明压成一句(保留「划它」「继续划」「一层层到底」与 `<b>` 强调);快捷键与数据/额度说明折进**一个** `<details>`(summary:「快捷键 · 数据与额度」);4 个站点链接降到页脚一行小字(它们是**导航**,不是「想了解更多」,折起来只会更难发现);`#anonNote` 只留**可操作**的那句(去反馈页留邮箱),删掉与按钮文案重复的「内测阶段只对持邀请码的人开放」。窄屏展开 `<details>` 时卡片**自内滚**(`max-height` + `overflow-y:auto`),而不是撑破不滚动的 `fixed` 遮罩导致按钮点不到。

**已知未覆盖**(诚实记录,非本次范围):

- `<meta name="theme-color">` 仍不随主题、分享卡片(canvas)继续固定亮色纸面——两项同上一次 Revision,本次未动。
- `::backdrop` 的**实际渲染色没有实测值**(伪元素 `getComputedStyle` 取不到),只有 CSS 声明值。
- 本地匿名态无邀请码,`/api/explain` 真实 LLM 链路未跑;工作台侧回归走**打桩网络层**(只替换 `apiStream`,DOM / 事件 / 状态机均为真实代码路径)。

**本次实现范围**:新增 `web/ui.js`、`web/ui.css`;改 `web/admin.html`(13 处)、`web/graph2d.html`(3 处)、`web/app/index.html`(3 处 + intro 卡 + z-index)、`web/tokens.css`(八档层级变量)、本 ADR。`web/site.css`、`web/index.html`、`web/meter.html`、`server.py`、`deploy/` 未动。
