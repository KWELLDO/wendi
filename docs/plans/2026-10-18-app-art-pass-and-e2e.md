# /app 美术打磨 + E2E 回归（2026-10-18）

分支：`frontend-design`（小步提交；本轮**不部署**，等用户确认）
状态：两条车道已回收并验收（E2E 14 passed），改动已小步提交；未部署。

## 背景
- 用户反馈 /app：「方向对但糙」——输入框太小、整体偏暗、层级糊、空态像巨洞。
- 视觉方向冻结：纸感编辑体（ADR 0001）——0 radius / 0 shadow / 发丝线 / 单一红。本轮只提工艺，不换方向。

## 实测基线（改前）
- `.inputrow input` 36px 高 / 13.5px 字（桌面宽 548px；390px 宽 216px 同高）；发送钮 36px/12.5px；select 36px/12px；`.inputrow` 55px；header 42px；crumbbar 30px
- 对比度：亮 `--ink-3 on --paper` 3.5；`--surface vs --paper` 亮 1.04 / 暗 1.07
- 暗色 token：paper `#1a1713` / surface `#211d18` / surface-2 `#14110d`（surface-2 比纸更暗 → 层级反转）
- 空态：主区 714×816、侧栏 691×816 → 巨洞感

## 车道
### A（@designer，独占 web/app/index.html + web/tokens.css）——已完成
- 输入区：input 46px/15px（390 下 16px）；发送 71×46；select 84×46 自绘箭头；inputrow 71（390：121）
- 暗色 token：纸面四阶重排，surface 比 paper 亮、surface-2 在 surface 之上；两块映射逐条同步（单源 `--dk-*`）
- 对比度：亮 ink-3 5.27、暗 6.02；<13px 文字全量审计仅禁用态「← 返回」低于 4.5（WCAG 豁免）
- 空态：主区「小标 + 大标题 + 01/02/03 阶梯 + 版口」，侧栏 L1/L2/L3 阶梯，预览两页纸线框；不加功能
- 契约：JS 行为未动（仅三处空态模板字符串），0 radius / 0 shadow（页面内），零外链

### B（@fixer，独占 e2e/**）——已完成
- Playwright：真实 `python3 server.py` @8901 + 独立测试库 `e2e/.tmp/wendi-e2e.db` + 上游黑洞防漏打桩
- 覆盖：落地页、注册/登录（真实 API + 邀请码）、/app 对话→划词解释→追问、分享、三态主题、390×844
- 诊断夹具：pageerror / console error / 请求失败自动断言

## 验收结果（独立复核，本地 8899）
- 数值：input 46px/15px（390 为 16px）；发送 71×46；select 84×46；inputrow 71（390：121）；header 52/50；crumbbar 36/34
- 对比度：ink-3 on paper 亮 5.27 / 暗 6.02；ink-3 on surface 5.69 / 5.39；正文 16.88 / 13.88；暗 surface vs paper 1.12、surface-2 vs paper 1.25
- 1440 / 390 均无横向溢出；可见元素 border-radius / box-shadow 计数 0/0；`.blank` / `.blank-list` / `.blank-prev` 生效（旧 `.lempty` 已移除）
- 暗色两块映射 24 条声明完全一致；4 个 script 块仅主脚本内空态模板字符串变化
- E2E：`cd e2e && npx playwright test` → **14 passed（16.3s）**；`--repeat-each=2` → 28 passed
- 数据隔离：`~/.wendi/wendi.db` 仍 0 用户 / 0 邀请码；测试数据全在 `e2e/.tmp/wendi-e2e.db`

## 已知缺陷（未修，e2e 以 test.fail 留证）
- 登录/注册响应 `quota` 缺 `plan` 字段 → Pro 用户登录瞬间 tooltip 显示「免费」，刷新走 `/api/me` 后恢复。
- 修复后该用例会翻转成「意外通过」，提醒移除 `test.fail` 标注。

## 环境事实（本轮核实）
- 8899 = 本工作树 `python3 server.py`（pid 422751），实时读盘
- 8901/8902 空闲；Playwright chromium-1248 已装；系统 Chrome 在 /opt/google/chrome/chrome
- 子代理默认模型不可用：`opencode-go/kimi-k2.5` 已下架、`opencode-go/deepseek-v4-pro` 触发 Go 用量上限 → 两条车道实际以 `deepseek/deepseek-flash` 运行（用户指示）

## 遗留/已知
- iOS 真机验证待用户（原生 select 宽度、安全区）
- 单条叶子解释时，右侧预览「子解释 0 /（空）」会拉满整列 → 随内容收缩/折叠
- 390 输入区 121px 双行，667px 短屏会挤阅读区；档位可考虑收进「更多」
- 禁用态「← 返回」对比 2.2 / 2.96（WCAG 豁免）观感偏虚
- 档位下拉展开仍是系统原生菜单（需自绘 listbox，属结构改动）
- 落地页 390px 导航 5 项挤一行：本轮范围外
- meter 真实链路需真实 `WENDI_ADMIN_TOKEN`；真实 LLM 链路未验
