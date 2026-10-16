# 0001 · 视觉方向:大报编辑体(wendi 化)

- 状态:accepted
- 日期:2026-09-27
- 修订:2026-10-16(夜版改三态主题,见文末 Revision)
- 修订:2026-10-16(`meter.html` 换皮到纸墨编辑体、3D 实验室下线,见文末 Revision)

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
