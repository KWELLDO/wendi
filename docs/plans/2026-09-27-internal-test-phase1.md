# 内测第一阶段：测试支撑 + 数据采集

- 日期：2026-09-27
- 输入：`何以日志.md`（采集/日志/监控的需求稿）
- 目标：**5 个真人走完一遍之后，手里是「路径 + 成本 + 回访信号」，不是「感觉还行」**
- 交付面：用户系统可用（邀请制注册）· 邀请码机制 · 站长后台扩展 · 事件采集 + 请求日志 + 日聚合 + 日报

本文件只记**口径与取舍**；实现对不上时以本文件为准，改实现前先改这里。

---

## 0. 一句话设计

**一张事件流水表 + 一张日聚合表**，前端 12 个事件（能接几个接几个）走后端白名单端点；请求日志走 stderr 的 JSON 行；每天由 systemd timer 跑一次聚合与日报。不引外部系统，不采正文，埋点失败不影响用户。

---

## 1. 与需求稿的偏离（都是刻意的）

| 稿子里的 | 实际做法 | 为什么 |
|---|---|---|
| 新增 `events` / `test_batches` / `daily_metrics` 三表 | 只加 `events` + `daily_metrics`，**不建 `test_batches`** | `codes` 表已有 `batch` / `note` / `used_by` / `voided`，再加个 `tester_label` 就等价于 test_batches；少一张表少一套心智 |
| `events.code_id`（整数外键） | `events.code`（码字符串） | `codes` 的主键本来就是码字符串，可读、可直接 join、日报里不用再查一次 |
| `explain_success.meta.cost_cents`（前端上报） | 服务端在收到该事件时**附着**该 session 最近一次真实成本 | 成本是内部量，不该出现在前端可读的数据里（定价口径：用户侧只看「顺畅/降速」） |
| `material_load` 事件 | **不实现** | 当前产品没有「载入材料」这个动作，不为了埋点造交互 |
| `share_open` 事件 | **不实现**（分享链接分享的是 PNG/JSON 本体，站点没有分享落地页） | 同上：先把存在的动作采准 |
| `ip_hash` | 实现：`sha256(salt + ip)` 取前 16 hex，salt 存 `meter_config`（首次自动生成） | 匿名归属需要稳定标识，但没必要留原始 IP |
| 90 天事件保留 | 日聚合任务顺手删 >90 天的事件 | 聚合指标永久，流水有界 |

## 2. 采集口径

**采**：用户动作（划词/进入/回退/导出/分享/反馈/注册/打开）+ 系统调用（LLM 调用、成本、错误）。
**不采**：提问与解释正文、原始 IP、按键序列、鼠标轨迹。
**失败策略**：`/api/event` 永远返回空 200（不用 204：http.server 是 HTTP/1.0，204 不许带 Content-Length，Chromium 会把它记成 `net::ERR_ABORTED` 的控制台噪音），任何内部异常吞掉只写 stderr；前端 `track()` 不抛错。请求日志同样不影响响应。

事件清单（前端能接的）：

| 事件 | 触发点 | 关键 meta |
|---|---|---|
| `app_open` | 页面加载 | `ref`（邀请码）、`returning`（本地有数据） |
| `select_text` | 划词气泡弹出 | `chars`、`depth` |
| `explain_request` | 发起解释/追问/对话 | `mode`、`depth`、`style` |
| `explain_success` | 流式完成 | `latency_ms`、`mode`、`depth`、`truncated` |
| `explain_error` | 失败/断流 | `mode`、`error`（短码） |
| `drill_down` | 进入子解释 | `from_depth`、`to_depth` |
| `go_back` | Esc / 返回按钮 | `from_depth` |
| `export` | 导出 md/json | `format` |
| `share_create` | 生成分享卡 | `depth` |
| `feedback` | 反馈提交成功 | `kind`（内容不采） |
| `register` | 注册成功（服务端记） | `code`、`plan` |
| `login` | 登录成功（服务端记） | — |

服务端另记：`system_error`（异常时）。

## 3. 表结构

```sql
events(id, ts /*unix ms*/, day_key, event, session_id, user_id, code,
       room_id, card_id, depth, meta /*JSON*/, ip_hash, ua_hash)
  idx: ts, event, session_id, (code, ts), (user_id, ts)
daily_metrics(day PK, active_users, new_users, sessions, app_opens,
              explains, explain_errors, drill_downs, go_backs, shares, exports,
              feedbacks, llm_calls, cost_cents, errors, d1_return, updated_at)
codes  += kind('pro'|'invite'), tester_label
users  += invite_code
meter_requests += session_id, room_id
```

`daily_metrics` 由 `rollup_day(day)` 幂等重算（先 DELETE 该日再插入），`updated_at` 记重算时刻。

## 4. 邀请码机制

- **注册必须带邀请码**（`WENDI_REQUIRE_INVITE=1`，默认开；设 0 可临时放开）。
- 邀请码就是 `codes` 里 `kind='invite'` 的码：可携带 `days`（>0 则注册即得 Pro，等价于「测试账号」），可带 `tester_label`（「A 研究者」）与 `batch`。
- 注册成功：码标记 used（`used_by`/`used_at`）、`users.invite_code` 记下归属、写 `register` 事件；码不能用第二次。
- 匿名使用**保留**（零摩擦试用的入口，配额 20/次/天），但会在事件里带上 `?ref=` 的码，用于看传播链。
- 兑换码（`kind='pro'`）不变：登录后在账号弹窗兑换 Pro。

## 5. 归因与回访

- 前端：`?ref=CODE` → `localStorage['wendi-ref']` → 之后所有事件与注册都带上（注册成功后清除）。
- 登录用户的事件，服务端在 `code` 为空时回落到 `users.invite_code`。
- **D1/D7 回访**：同一 `code`（或同一 `session_id`，当没有 code 时）在次日/7 日内再次出现 `app_open` 即计入。
- 站长页「测试者」一行一人：标签 · 码 · 注册时间 · 事件数 · 解释次数 · 成本 · 最近出现 · D1 · D7。

## 6. 监控与日报

- `GET /api/admin/metrics?day=YYYY-MM-DD`：当日聚合 + 漏斗（划词→解释成功→下钻→回退→分享）+ 近 14 天 + D1。
- `GET /api/admin/events?...`：事件流，可按 day / event / session / code 过滤（meta 展开）。
- `GET /api/admin/testers`：上面的每人一行。
- `scripts/daily_report.py`：算昨天与今天的聚合，写 `reports/YYYY-MM-DD.md`（Markdown，早上看一眼）。
- 部署件：`wendi-report.service` + `wendi-report.timer`（每天 23:55）。阈值（成本 > ¥50/天、错误率 > 5%、D1=0）只在日报里**标红提示**，不做告警通道——没有外部告警是这个阶段的已知缺口。

## 7. 不做（本阶段）

- 不引 Kafka/ClickHouse/Prometheus/Grafana；不做实时大屏。
- 不采正文、不采原始 IP、不做按键记录。
- 不为了埋点改产品交互（事件都在已有动作的钩子上）。
- 不做邮件（找回密码仍走站长后台重置）。
- 不做 `/admin/actions` 只读动作清单：等真有 agent 消费再加，现在没有消费者。

## 8. 验收

1. 无邀请码注册被拒；有效码注册即得账号（+可选 Pro），码不可复用，`users.invite_code` 正确。
2. 前端 `track()` 覆盖上表事件；`/api/event` 对非法事件名/超长 meta/超频请求都不报错（静默空 200）。
3. 事件落表、`rollup_day` 幂等、90 天剪枝生效。
4. 站长后台能按人看路径、按日看漏斗与成本；日报脚本能跑出 Markdown。
5. 全量测试绿 + 本地真机跑一遍（注册→划词→下钻→回退→导出→反馈）。
