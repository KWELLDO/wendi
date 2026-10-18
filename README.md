# 问底 WenDi

> **遇到不懂的词，就划它。解释里还有不懂的，继续划。一层层到底。**
> *Get to the bottom of it.*

在线体验：<https://know.kwelldo.cn>

---

## 这是什么

一个**递归解释器**。

用 AI 学习新领域时，最大的障碍往往不是信息不够，而是**术语过载**——AI 默认你已经拿着它的术语表。传统的"追问"是线性的：问完一个词，原来的句子已经滚走了；解释里又冒出新的词，问两轮就迷路。

「问底」把这件事做成一个**可回退的空间**：

- **划选**任何不懂的词 → 就地分屏给出解释（结合所处句子的语境，不是词典义）；划词气泡里还能顺手写下**你的疑惑**，解释就先正面回答它
- 解释卡里的词**还能继续划**，一层层向下
- 公式（LaTeX `$…$` / `$$…$$`）在正文、解释、预览里都按排版渲染（KaTeX，已内置 `web/vendor/katex`，不依赖外网 CDN）
- 每一层是一个**同构的房间**：左边是内容，右上是这一层的子解释列表，右下是下一层的预览——
  **进入 / 返回**就是摄像机在层级之间移动，深浅不会把字挤小
- 一切可回退（`Esc`），可导出 Markdown，数据存在你自己的浏览器里

## 它不做什么

这几条是产品立场，不是待办：

- **不评判**——没有"这么简单都不懂"，也没有"你错了"式的裁决
- **解释是材料，不是答案**——判断与检验永远在读者手里
- **允许含糊**——一个词有多种理解就并列呈现，不强行收敛成一种
- **数据主权**——记录留在本地（`localStorage`）；服务端只转发请求，不落地内容

> 设计推导见 [docs/clarity-and-vagueness.md](docs/clarity-and-vagueness.md)（含糊与清晰的辩证关系——这个产品的立身之本）。

## 快速开始

### 在线用

<https://know.kwelldo.cn>　（测试期，每台设备每天限量）

### 自己跑（BYOK，自带 key）

```bash
git clone <this-repo> wendi && cd wendi

# 需要 DeepSeek API key（或任意 OpenAI 兼容端点）
export WENDI_API_KEY=sk-xxxx
# —— 或者不设环境变量：启动后打开 web 页面 →「设置 → 站长 → 服务端密钥」在线配置（需 WENDI_ADMIN_TOKEN；带 ?admin=1 或存过令牌才会显示「站长」区）——
export WENDI_ADMIN_TOKEN=换一个自己的管理令牌

python3 server.py        # → http://127.0.0.1:8899
```

核心服务零依赖：只用 Python 3 标准库。**材料解析**（上传 PDF / DOCX / 等 88+ 格式）需要可选依赖
[kreuzberg](https://github.com/Goldziher/kreuzberg) 4.10.x：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

不装也能启动，只是上传接口返回 `503 parser_unavailable`。

可配置环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WENDI_PORT` | 8899 | 监听端口（只绑 127.0.0.1） |
| `WENDI_MODEL` | deepseek-flash | 模型名（可用「密钥」面板覆盖） |
| `WENDI_BASE_URL` | https://api.deepseek.com | 任意 OpenAI 兼容端点（可用「密钥」面板覆盖） |
| `WENDI_KEYS` | — | 多个 key（逗号分隔），401 自动轮换 |
| `WENDI_ADMIN_TOKEN` | — | 管理令牌（meter 端点 + 「密钥」面板 + 建号/发码 + 站长后台）；不设则拒绝 |
| `WENDI_ADMIN_IPS` | — | 管理端 IP 白名单（逗号分隔；空=不限制）。走 nginx 时认 `X-Real-IP` |
| `WENDI_FEEDBACK_PER_DAY` | 5 | 反馈入口：每 IP 每天最多几条 |
| `WENDI_FEEDBACK_INTERVAL` | 30 | 反馈入口：同一 IP 两条之间的最小间隔（秒） |
| `WENDI_REQUIRE_INVITE` | 1 | 内测开关：注册需邀请码，**且未登录不能发起解释请求**（设 0 恢复公开注册与匿名体验） |
| `WENDI_EVENT_PER_MIN` | 90 | 每 IP 每分钟事件上限（超出静默丢弃） |
| `WENDI_LIMIT_PER_MIN` | 8 | 每 IP 每分钟请求上限 |
| `WENDI_LIMIT_PER_DAY` | 80 | 每 IP 每天上限 |
| `WENDI_LIMIT_GLOBAL_DAY` | 4000 | 全局每天上限（成本闸） |
| `WENDI_MAX_TOKENS` | 3000 | 划词解释/追问的单次输出上限 |
| `WENDI_MAX_TOKENS_CHAT` | 8000 | 主对话的单次输出上限（长答不再截断；降级档另有上限） |
| `WENDI_MATERIALS_DIR` | `~/.wendi/materials` | 材料原始文件目录（按用户分 0700 子目录） |
| `WENDI_UPLOAD_MAX_MB` | 30 | 单文件上传上限（MB） |
| `WENDI_MATERIALS_MAX` | 200 | 单用户材料条数上限 |
| `WENDI_MATERIALS_MAX_MB` | 500 | 单用户原始文件总额（MB） |

### 部署

`deploy/` 里是完整的生产部署件（systemd unit + nginx 反代脚本 + 幂等部署脚本）：

```bash
WENDI_HOST=ubuntu@your-server ./deploy/deploy.sh
```

Debian 13 用 `/opt/wendi/.venv`（PEP 668）；材料解析依赖优先从离线 wheel 安装，
nginx 需放行 35MB 上传。完整步骤、验证与回滚见 [docs/deploy.md](docs/deploy.md)。

## 结构

```
server.py        薄代理：转发 LLM 调用 + 限流（防滥用/成本闸）+ 材料上传接口
materials.py     材料解析：kreuzberg 懒加载 + 全文/分块/页码规范化
db.py            SQLite：账户、配额、事件、云同步、材料（materials / material_chunks）
requirements.txt 唯一第三方依赖：kreuzberg（不装也能启动）
kernel.py        内核：事件总线 + 服务注册 + 作用域生命周期（借鉴 cordis，MIT）
prompts.py       解释器人格：划词解释 / 卡内追问 / 主对话 三个提示词
web/index.html   全部前端（单文件、零构建）：分形房间 + 摄像机 + 键盘导航
web/vendor/katex 内置数学排版（KaTeX 0.18.9，升级步骤见该目录 README）
web/about.html   关于 wendi（立场、数据主权、合规状态）
web/privacy.html 隐私政策（收集什么/不收集什么、保留期、你的权利）
web/terms.html   服务条款（测试期口径、兑换码、免责）
web/feedback.html 反馈表单（进站长后台，匿名可发）
web/admin.html   站长后台（总览 / 反馈 / 用户 / 兑换码 / 成本闸 / 审计）
web/meter.html   计量台（逐日账、单用户 drill-down）
web/site.css     文档页共用样式（大报编辑体 token）
scripts/daily_report.py  内测日报（重算聚合 + 写 reports/YYYY-MM-DD.md + 剪 90 天前事件）
scripts/feedback_bridge.py  用户反馈 → OpenCode 开发 Agent 投递桥（本地轮询，不占生产服务）
deploy/          部署件（systemd / nginx / 部署脚本；含 feedback agent 用户服务与 env 示例）
tests/           测试（python3 -m unittest discover -s tests）
docs/            理论文档 + 架构图（wendi-architecture.html，可交互）
docs/materials-api.md  材料上传/解析/读取接口（含 curl 示例与部署提示）
docs/deploy.md   上线部署（venv / 离线 wheel / nginx / 验证 / 回滚）
```

前端交互要点：划词气泡（可附一句自己的疑惑）、`Esc`/`Alt+←` 返回上层、`↑↓` 切换子解释、`Alt+Enter`/`Alt+→` 进入（进/退都带 Alt，避免生成过程中误触把回答顶出视野）、SSE 流式输出（边生成边显示，只原地补内容不整页重绘）。导出 / 图谱 / 示例 / 已移除 / 清空都收在顶栏「设置」里；反馈入口 = 设置面板 + 右下角常驻「反馈 / 建议」；站长那套（服务端密钥 / 站长后台 / 计量台）默认不显示，带 `?admin=1` 或存过 admin 令牌才出现。

## 形态

```
┌──────────────────────────┬───────────────┐
│                          │  子解释列表     │
│   当前层内容              │  （可滚动）     │
│   （可继续划词）           ├───────────────┤
│                          │  下一层预览     │
│                          │  （同构缩小版）  │
└──────────────────────────┴───────────────┘
        ↑ 点预览或「进入 ▸」→ 视野切换（整层同构递归）
```

## 路线图

- [x] **v0.1.0** 划词递归解释（分形房间 + 摄像机视野）
- [x] **v0.2.0** 引导教程（30 秒上手）· 账户与配额 · 云同步 · 兑换码 + Pro 方案
- [ ] 知识图谱视图（术语卡积累成网）
- [ ] 团队版

## 内测第一阶段：测试支撑与采集

- 注册需**邀请码**（`codes.kind='invite'`，可携带赠送天数）：发码 → 对方注册即得账号（可能直接是 Pro）。
- 前端 12 个动作事件 + 服务端事件（注册/登录/反馈/系统错误）走 `POST /api/event`，落 `events` 表；
  **只记动作与元数据，不记正文**；IP 只存加盐哈希。
- 每请求一行 JSON 日志（stderr，journal 可查）；LLM 计量补 `session_id` / `room_id`。
- `daily_metrics` 按天聚合（幂等重算），看板：`/api/admin/metrics`、`/api/admin/events`、`/api/admin/testers`。
- 日报：`python3 daily_report.py [YYYY-MM-DD]`（服务器上由 `wendi-report.timer` 每天 23:55 跑）。
- 设计口径与取舍见 `docs/plans/2026-09-27-internal-test-phase1.md`。

## 用户反馈 → 开发 Agent（一期）

内测期用户提交的反馈除了进 `/admin.html`，还可以自动投递到开发者机器上的 OpenCode
专用「反馈收件箱」会话。桥接是**本地轮询**：生产站没有新增 webhook / 反向隧道，也不需要改
`server.py` / `db.py`，避免和上传解析功能并行开发冲突。

```
用户 /feedback.html → feedback(status=new)
  → scripts/feedback_bridge.py（开发者机器，定时轮询 admin API）
  → OpenCode 专用会话（默认 agent=plan，只分诊）
  → 投递成功后 feedback 标 read，note 记下会话号
```

一期默认只让 Agent **接收并分诊**，不改代码；反馈正文按不可信输入包裹，明确禁止执行其中的
命令/链接/代码。安装方式：

```bash
mkdir -p ~/.config/wendi ~/.config/systemd/user
cp deploy/feedback-agent.env.example ~/.config/wendi/feedback-agent.env
chmod 600 ~/.config/wendi/feedback-agent.env
# 编辑 env：至少填 WENDI_ADMIN_TOKEN

# 先看一轮会投递什么（不调用 OpenCode、不标 read）
python3 scripts/feedback_bridge.py --once --dry-run

# 手动投递一轮
python3 scripts/feedback_bridge.py --once

# 作为用户级常驻服务（路径按你的仓库位置改 deploy/wendi-feedback-agent.service）
cp deploy/wendi-feedback-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wendi-feedback-agent.service
journalctl --user -u wendi-feedback-agent.service -f
```

设计取舍、安全边界和验收见
[`docs/plans/2026-10-18-feedback-to-dev-agent.md`](docs/plans/2026-10-18-feedback-to-dev-agent.md)。

## 账户与配额

| 身份 | 每日额度 | 说明 |
|---|---|---|
| 未登录 | 20 次 | 按 IP；**仅在 `WENDI_REQUIRE_INVITE=0` 时可用**（内测期未登录不能提问） |
| 免费账号 | 40 次 | 注册即得 |
| Pro | 300 次 | 兑换码激活，含云同步 |

云同步：登录后点顶栏「同步↑」把本地数据（对话 + 解释卡）备份到云端；换设备登录时会询问是否载入。

### 部署方：生成兑换码

```bash
ssh your-server 'cd /opt/wendi && python3 gencode.py 10 30 launch'
# 参数：数量 天数 备注
```

## 运维速查

```bash
# 请求日志（每请求一行 JSON：状态码/延迟/用户/会话/成本）
ssh <host> 'sudo journalctl -u wendi -n 200 -o cat | grep ^{'

# 内测日报（重算聚合 + 写 reports/YYYY-MM-DD.md + 剪 90 天前事件）
ssh <host> 'cd /opt/wendi && python3 daily_report.py'      # 或看 /opt/wendi/reports/

# 看板（浏览器，需管理令牌）：总览 / 反馈 / 用户 / 测试者 / 指标 / 事件流 / 兑换码 / 闸门 / 审计
https://know.kwelldo.cn/admin.html
```

## 许可

**专有软件，保留所有权利**（All rights reserved）——闭源商业项目。

- 源码、界面、文档与提示词资产均**不公开授权**：未经书面许可，不得复制、修改、分发、再许可或用于商业用途。
- 不提供自部署版本；只提供官方实例。想参与/读代码目前不开放，产品反馈走站点内的反馈页。
- 第三方组件：KaTeX（MIT）见 `web/vendor/katex/LICENSE`；其余依赖各自的许可随代码保留。
