# 问底 WenDi

> **遇到不懂的词，就划它。解释里还有不懂的，继续划。一层层到底。**
> *Get to the bottom of it.*

在线体验：<https://know.kwelldo.cn>

---

## 这是什么

一个**递归解释器**。

用 AI 学习新领域时，最大的障碍往往不是信息不够，而是**术语过载**——AI 默认你已经拿着它的术语表。传统的"追问"是线性的：问完一个词，原来的句子已经滚走了；解释里又冒出新的词，问两轮就迷路。

「问底」把这件事做成一个**可回退的空间**：

- **划选**任何不懂的词 → 就地分屏给出解释（结合所处句子的语境，不是词典义）
- 解释卡里的词**还能继续划**，一层层向下
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
# —— 或者不设环境变量，启动后打开 web 页面点右上角「服务配置」（站长入口）在线配置，需 WENDI_ADMIN_TOKEN ——
export WENDI_ADMIN_TOKEN=换一个自己的管理令牌

python3 server.py        # → http://127.0.0.1:8899
```

零依赖：只用 Python 3 标准库。

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
| `WENDI_LIMIT_PER_MIN` | 8 | 每 IP 每分钟请求上限 |
| `WENDI_LIMIT_PER_DAY` | 80 | 每 IP 每天上限 |
| `WENDI_LIMIT_GLOBAL_DAY` | 4000 | 全局每天上限（成本闸） |
| `WENDI_MAX_TOKENS` | 3000 | 划词解释/追问的单次输出上限 |
| `WENDI_MAX_TOKENS_CHAT` | 8000 | 主对话的单次输出上限（长答不再截断；降级档另有上限） |

### 部署

`deploy/` 里是完整的生产部署件（systemd unit + nginx 反代脚本 + 幂等部署脚本）。
把 `deploy.sh` 顶部的 `HOST` 改成你的服务器即可。

## 结构

```
server.py        薄代理：转发 LLM 调用 + 限流（防滥用/成本闸）
kernel.py        内核：事件总线 + 服务注册 + 作用域生命周期（借鉴 cordis，MIT）
prompts.py       解释器人格：划词解释 / 卡内追问 / 主对话 三个提示词
web/index.html   全部前端（单文件、零构建）：分形房间 + 摄像机 + 键盘导航
web/about.html   关于 wendi（立场、数据主权、合规状态）
web/privacy.html 隐私政策（收集什么/不收集什么、保留期、你的权利）
web/terms.html   服务条款（测试期口径、兑换码、免责）
web/feedback.html 反馈表单（进站长后台，匿名可发）
web/admin.html   站长后台（总览 / 反馈 / 用户 / 兑换码 / 成本闸 / 审计）
web/meter.html   计量台（逐日账、单用户 drill-down）
web/site.css     文档页共用样式（大报编辑体 token）
deploy/          部署件（systemd / nginx / 部署脚本）
tests/           测试（python3 -m unittest discover -s tests）
docs/            理论文档 + 架构图（wendi-architecture.html，可交互）
```

前端交互要点：划词气泡、`Esc` 返回上层、`↑↓` 切换子解释、`Enter` 进入、SSE 流式输出（边生成边显示）、导出 Markdown。

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

## 账户与配额

| 身份 | 每日额度 | 说明 |
|---|---|---|
| 未登录 | 20 次 | 按 IP，防止滥用 |
| 免费账号 | 40 次 | 注册即得 |
| Pro | 300 次 | 兑换码激活，含云同步 |

云同步：登录后点顶栏「同步↑」把本地数据（对话 + 解释卡）备份到云端；换设备登录时会询问是否载入。

### 部署方：生成兑换码

```bash
ssh your-server 'cd /opt/wendi && python3 gencode.py 10 30 launch'
# 参数：数量 天数 备注
```

## 许可

**AGPL-3.0-or-later**。

你可以自由使用、修改、自部署；如果你把它做成网络服务提供给他人，衍生作品也需要同样开源。
