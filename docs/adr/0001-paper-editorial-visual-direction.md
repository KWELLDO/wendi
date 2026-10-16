# 0001 · 视觉方向:大报编辑体(wendi 化)

- 状态:accepted
- 日期:2026-09-27
- 修订:2026-10-16(夜版改三态主题,见文末 Revision)

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
