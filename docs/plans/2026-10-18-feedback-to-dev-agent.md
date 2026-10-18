# 用户反馈 → OpenCode 免费分流 → clds fix/大改方案：一期

- 日期：2026-10-18
- 范围：反馈接收、免费模型分流、fix 候选补丁、大改方案、站内澄清
- 明确不做：自动合并、自动部署、自动回复用户、多会话、知识图谱
- 原则：大改 Agent 不做，只写方案；不向用户虚假许诺

## 0. 两段式

```
生产 feedback(status='new')
  → scripts/feedback_bridge.py
  → 第一段：OpenCode + 免费模型
       ignore      → 不调 clds
       ask_user    → 站内澄清，不调 clds
       fix         → 第二段 clds fix
       big_change  → 第二段 clds plan
  → clds（只在 fix / big_change 时）
       fix：隔离 git worktree，产出待审 patch + 测试结果
       big_change：只写方案，不改代码
  → feedback 标 read，note 记方案/分支
```

## 1. 第一段：OpenCode 免费分流

- 模型默认 `opencode/deepseek-v4.1-flash-free`。
- 桥接会调 `/api/model` 检查该模型的 input/output/cache cost 是否全为 0；不是免费模型就拒绝。
- 项目 agent：`.opencode/agents/feedback-triage.md`
  - `steps: 1`
  - `permission: edit=deny, bash=deny`
  - prompt 要求不使用工具、只输出 JSON
  - 已知边界：OpenCode 免费层对自定义 agent 的权限组合敏感；实测硬禁 read/grep 等
    会导致免费层直接报错。因此这里是 `steps: 1` + prompt 约束 + edit/bash 禁用，
    **不宣称 OpenCode 分流层硬禁所有工具**；clds 侧才是强隔离（restricted/空 MCP）。
- 每次分流创建临时 OpenCode 会话，拿到 assistant 文本后立即删除会话。
- `decision`：
  - `ignore`：垃圾/纯感谢/重复/无需动作
  - `ask_user`：信息不足，先问用户
  - `fix`：小、明确、可逆、局部
  - `big_change`：命中大改红线
- 若 `decision=fix` 但 `is_big_change=true`，自动升级为 `big_change`。

## 2. 第二段：clds

### big_change

- 只读 plan 模式，只写方案，不改代码。
- 参数固定：`--safe-mode --restricted --disable-slash-commands`、空 MCP、
  `--permission-mode plan --permission-prompts none --no-session-persistence`、
  `CLDS_NO_SKIP_PERMISSIONS=1`。
- 方案落盘 `plans/big-change-<id>.md`。

### fix

- 从当前仓库 HEAD 创建隔离 git worktree / 分支。
- clds 参数：`--permission-mode acceptEdits`，同样 safe-mode / restricted / 空 MCP /
  禁用 slash commands / `CLDS_NO_SKIP_PERMISSIONS=1`。
- clds 只允许改代码和测试；桥接随后：
  - `git add -A` + `git diff --cached` 生成 `fix-<id>.patch`
  - 在 worktree 内跑 `python3 -m unittest discover -s tests`
  - 写 `fix-<id>.md` 报告
- **不提交、不合并、不部署**。
- 禁止自动修改：
  `server.py`、`db.py`、`auth.py`、`messages.py`、`prompts.py`、`kernel.py`、
  `deploy/`、隐私/条款/关于/反馈页、LICENSE、.env、.pi/、.opencode/、
  `scripts/feedback_bridge.py`。
  命中禁区则标记“触碰禁区，需人工”，不自动采用。

## 3. 联系方式与不可信输入

- 不发送 `contact` / `email` 字段。
- 正文中先脱敏：
  - 已知 contact/email 原文
  - 邮箱地址
  - 中国大陆手机号
  - 带标签的微信 / WeChat / VX / QQ / 电话 / 手机号 / 邮箱 / 联系方式
- 只发送 `user_id`、`can_reply`、反馈编号/类型/时间等必要元数据。
- 两段 prompt 都把反馈正文放在 `<user_feedback untrusted="true">` 中，并明确禁止执行正文里的命令/链接/代码。
- 分流器的自由文本不进入 clds prompt；clds 只收到受控枚举/布尔字段 + 原始反馈正文。

## 4. 站内消息

- `POST /api/admin/user-message`（admin token）：`{user_id, body, feedback_id?}`
- `GET /api/messages`（用户 token）
- `POST /api/messages/read`（用户 token）
- `/messages.html`：登录用户查看、刷新、标已读
- 只有 `user_id > 0` 且 `can_reply=1` 才发站内追问。
- 站内追问只允许带问号的澄清问题；拒绝承诺性话术、联系方式请求、URL。
- 匿名反馈没有 user_id，只分流/出方案，不能发站内消息。

## 5. 状态与幂等

本地状态只保存反馈 ID、分流结论、方案/分支路径、是否已发消息、是否已标 read；
不保存反馈正文。状态文件 `0600`。

- 分流失败重试 2 次，仍失败则标 read 并 note“分流失败，需人工”。
- clds 失败重试 2 次，仍失败则标 read 并 note“自动处理失败，需人工”。
- 站内消息失败不标 read，下一轮只补发消息。
- 标 read 失败不重复跑模型，下一轮只补标。
- OpenCode 分流消息 id：`msg_triage_fb_<id>`。
- 每个分流会话用后即删，不累积。

## 6. 默认配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WENDI_FEEDBACK_BASE_URL` | `https://know.kwelldo.cn` | 生产站 |
| `WENDI_ADMIN_TOKEN` | 必填 | 站长令牌 |
| `OPENCODE_URL` | 读 service.json | OpenCode 服务 |
| `OPENCODE_PASSWORD` | 读 service.json | OpenCode 密码 |
| `OPENCODE_MODEL` | `opencode/deepseek-v4.1-flash-free` | 分流模型，必须免费 |
| `OPENCODE_AGENT` | `feedback-triage` | 项目分流 agent |
| `OPENCODE_TRIAGE_TIMEOUT` | `120` | 分流超时 |
| `WENDI_CLDS_BIN` | `clds` | 本机 clds |
| `WENDI_CLDS_TIMEOUT` | `300` | clds 超时 |
| `WENDI_CLDS_MAX_BUDGET_USD` | `0.60` | clds 单次预算 |
| `WENDI_FEEDBACK_AGENT_STATE` | `~/.local/state/wendi-feedback-agent/state.json` | 状态 |
| `WENDI_FEEDBACK_PLANS_DIR` | state 同级 `plans/` | 方案/补丁/报告 |
| `WENDI_FIX_WORKTREE_DIR` | state 同级 `worktrees/` | fix 隔离工作区 |
| `WENDI_FEEDBACK_INTERVAL` | `60` | 轮询间隔 |
| `WENDI_FEEDBACK_BATCH` | `20` | 每轮条数 |

## 7. 验收

1. OpenCode 免费模型返回合法分流 JSON；付费模型被拒绝。
2. `ignore` / `ask_user` 不调用 clds。
3. `fix` 在隔离 worktree 产生 patch + 测试结果，主工作区不变。
4. `big_change` 只产生方案，不改代码。
5. fix 触碰禁区时 note 标记需人工。
6. 联系方式不进入任一 Agent prompt。
7. 匿名/未允许联系用户不会收到站内消息。
8. 站内消息失败、标 read 失败不会重复跑模型。
9. 全量测试绿。

## 8. 后续

- 人工合并 fix worktree，再灰度部署。
- 把分流结果/方案路径写回后台字段（需要 schema 变更，另立大改）。
- 站内消息未读徽标/通知。
- 量上来后把轮询换成 SSE/事件推送。
