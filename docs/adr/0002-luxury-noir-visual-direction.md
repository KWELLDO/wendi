# ADR 0002 · 视觉方向改由「黑金(luxury-noir)」承接

日期:2026-10-25 · 状态:已采纳 · 取代:ADR 0001 的纸感视觉契约(结构/Layout 契约不变)

## 背景

ADR 0001 定下的「纸底 + 近黑墨 + 单一编辑红 + 发丝线 + 方角」(editorial-broadsheet 应用壳变体)
服役约一个月后,经三轮候选评审(两轮自造方向被否),用户从自有美术库
(`~/.pi/agent/skills/design-library/design-picks.md`,2026-09-27 亲选「第一级别喜欢」)
中选定 `luxury-noir`(黑金奢华)为工作台与全站的新视觉方向。

## 决定

1. **色板**:暗为原色——近黑 `#0a0a0b` 纸 + 暖白 `#eae6dd` 墨 + 金 `#c9a86a` 唯一强调;
   亮色为「暖金白」变体(`#f4f1e9` 纸 / `#a07d3f` 金),由本壳首定义(spec 原文未定义亮色)。
2. **金的使用纪律**(spec 禁忌,照单全收):金面积 <5%,只给线、字与当前态;
   正文永远暖白;不加大面积金色填充。
3. **字体**:Cormorant Garamond(西文衬线)+ LXGW WenKai(中文衬线,霞鹜文楷 OFL,
   unicode-range 拆分 webfont)+ Cinzel(小型大写眉标,`--caps` 新 token)+ Inter 300/400/500(正文黑体)。
   全部 @fontsource 经 npmmirror vendor 进 `web/vendor/fonts/`,零外网 CDN。
4. **纸纹退役**:黑金无纹理——留白与金线就是全部质感。`--paper-tex:none`,羊皮纸云斑退役。
5. **动效预算两处**:报头分层入场一次(1.6s,`prefers-reduced-motion` 兜底)+ 顶部金晕(静态)。
   其余静止;已有 `.room.enter` 相机动画属交互反馈,不计入。
6. **Layout 不变**:三栏 tmux 式工作台、分栏拖拽、右下预览扩展等交互契约原封未动。

## 影响

- `web/tokens.css`:亮色 token、`--dk-*` 暗色真源、字体栈、纸纹全部换血;
  新增 `--caps`(眉标 Cinzel)与 `--gold-hi`(金高亮)。
- `web/app/index.html`:黑金组件样式并入本体 `<style>`(原评审皮肤层移除)。
- `web/graph2d.html`、落地页、文档页:token 单源自动继承;主要页面补字体 `<link>`。
- `web/vendor/fonts/`:新增 cormorant-garamond / cinzel / inter / lxgw-wenkai。

## 后果

- 正面:与产品「让视线慢下来」的阅读气质一致;暗色首读体验大幅提升;
  视觉与既有美术库(站群)可追溯、可复用。
- 代价:亮色模式存在感下降(noir 暗为原色);Cormorant/Cinzel 只覆盖西文,
  中文衬线靠文楷与系统宋体兜底;金在低对比屏上偏弱——正文对比度实测仍 ≥15:1,
  金 on 黑 8.3:1(仅用于 ≥12px 或非正文)。
