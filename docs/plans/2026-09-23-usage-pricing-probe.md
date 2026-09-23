# 问底 · usage 与 DeepSeek 定价实测报告

- 日期：2026-09-23
- 性质：经济测算系统（metering）的**事实基线**。所有成本/计价实现与定价决策必须以此为准，不再采信任何拍脑袋的占位值。
- 来源：`scripts/probe_usage.py`（usage 实测）+ `scripts/fetch_pricing.py`（定价抓取，页面 https://api-docs.deepseek.com/quick_start/pricing/）
- 实测 key：`deepseek_n8n_api_key`（`~/.secrets`）

---

## 一、结论先行（四件事，都推翻了此前的假设）

1. **流式响应默认就返回 usage**，不需要 `stream_options.include_usage`。
2. **usage 里自带缓存命中拆分**（`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`），输入成本必须按 hit/miss 分开计价，否则误差可达 50 倍。
3. **模型已换代**：`deepseek-chat` 名称已退役，被服务端静默路由到 `deepseek-flash`（DeepSeek-V4.1-Flash）。计费器与单价必须对齐 `deepseek-flash`。
4. **价格分峰谷**（2 倍差），且峰时正是中国用户白天活跃时段（详见 §3）。

---

## 二、usage 实测结果

### 2.1 三种请求形态的 usage 返回情况

| 请求形态 | 返回 usage？ | 位置与备注 |
|---|---|---|
| 非流式 `stream=false` | ✅ 有 | 正常 JSON body 的 `usage` 字段 |
| 流式（**不带** `stream_options`） | ✅ **有** | `[DONE]` 前最后一个 chunk 里带完整 `usage` |
| 流式（带 `include_usage=true`） | ✅ 有 | 同上，与不带参数**无差别** |

> **结论：`_llm_request` 无需加 `stream_options`。** 流式解析时，在遇到 `[DONE]` 前捕获最后一个带 `usage` 的 chunk 即可。

### 2.2 usage 字段完整结构（实测样例）

```json
{
  "prompt_tokens": 10,
  "completion_tokens": 60,
  "total_tokens": 70,
  "prompt_tokens_details": {"cached_tokens": 0},
  "prompt_cache_hit_tokens": 0,
  "prompt_cache_miss_tokens": 10
}
```

字段说明：

- `prompt_tokens` / `completion_tokens` / `total_tokens`：标准字段，直接可用。
- `prompt_tokens_details.cached_tokens`：缓存 token 数（与下二者冗余之一）。
- `prompt_cache_hit_tokens`：**命中缓存的输入 token 数**（按 Cache Hit 价计价）。
- `prompt_cache_miss_tokens`：**未命中缓存的输入 token 数**（按 Cache Miss 价计价）。

> 计量系统应同时落库 `completion_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens` 三个数，成本由后两者分别乘对应单价、加输出乘输出价得出。

### 2.3 意外发现：模型名被重定向

请求体 `model=deepseek-chat`，响应体 `model=deepseek-flash`。DeepSeek 官方文档确认：legacy 名 `deepseek-v4-flash` 等已退役，由 DeepSeek-V4.1-Flash 服务并按 Flash 价计费。

### 2.4 空 body 现象（运维注意）

非流式偶发返回空 body（ELB 负载均衡下）。实测脚本需加**重试**；生产代码 `_request_once` 同样应把"空 body / JSON 解析失败"当可重试错误处理，而非直接 500。

---

## 三、DeepSeek 实时定价（2026-09-23 抓取）

计价单位为**每 1M tokens**，币种**美元**。

| 维度 | deepseek-flash | deepseek-v4-pro |
|---|---|---|
| 输入 · 缓存命中 · 谷价 | $0.003 | $0.022 |
| 输入 · 缓存命中 · 峰价 | $0.006 | $0.044 |
| 输入 · 缓存未命中 · 谷价 | $0.15 | $0.66 |
| 输入 · 缓存未命中 · 峰价 | $0.30 | $1.32 |
| 输出 · 谷价 | $0.60 | $1.98 |
| 输出 · 峰价 | $1.20 | $3.96 |

其他事实：

- **峰时定义**：UTC 周一至周五 01:00–04:00、06:00–10:00（不含中国法定节假日）。其余为谷价（周末与节假日全天谷价）。
  - 换算北京时间（UTC+8）：**周一至五 09:00–12:00、14:00–18:00**——正是中国用户白天活跃时段，几乎必然踩峰价。
- **峰谷比 = 2x**，缓存命中/未命中比 ≈ 50x。
- 模型选型：产品默认走 **`deepseek-flash`**（便宜、当前被定向的目标）。`deepseek-v4-pro` 仅作对照，v1 不启用。

### 3.1 折算成人民币的计价常量（建议入 `meter_config`）

以 flash 为例（按 ¥≈7.2 近似，实现时以实际汇率为准，这里只列美元）：

| 常量名 | 建议值（美元/百万 token） |
|---|---|
| `flash_in_hit_offpeak` | 0.003 |
| `flash_in_hit_peak` | 0.006 |
| `flash_in_miss_offpeak` | 0.15 |
| `flash_in_miss_peak` | 0.30 |
| `flash_out_offpeak` | 0.60 |
| `flash_out_peak` | 1.20 |

计费公式：

```
输入成本 = hit_tokens × in_hit_price(峰谷) + miss_tokens × in_miss_price(峰谷)
输出成本 = completion_tokens × out_price(峰谷)
总成本  = 输入成本 + 输出成本
```

---

## 四、对经济测算系统设计的影响（务必回写进实施计划）

1. **成本折算函数** `_cost_cents` 必须升级为**六常量 + 峰谷判定**，不能再是"输入价/输出价"两常量。
2. **`meter_config` 表**要存这六个单价常量 + 峰谷时区规则，而不是两个占位值。
3. **计量落库字段**从 `prompt_tokens/completion_tokens` 扩展为 **`completion_tokens` + `prompt_cache_hit_tokens` + `prompt_cache_miss_tokens`**（三者即可算全成本；`prompt_tokens` 可作为校验冗余保留）。
4. **峰谷判定**需要服务器本地时区 → UTC 的换算；或直接用 UTC `time.gmtime`，避免 `memo.md §二.8` 已记录的"本地时区依赖"问题。
5. **模型名**：`server.py` 默认 `MODEL` 应改为 `deepseek-flash`（或至少在计量侧按 flash 计价并注释明确）。
6. **空 body 重试**：计量落库前，LLM 层需对空响应/解析失败做重试，否则偶发空 body 会污染计量（记为 0 token 但实际可能已计费）。

---

## 五、脚本清单

- `scripts/probe_usage.py` —— usage 实测（含重试与三种形态）。
- `scripts/fetch_pricing.py` —— 定价页抓取（当前为雏形，抽取了价格片段；完整表格见本报告 §3）。
