# 材料上传与解析 API

面向工作台的「材料」底座：上传文件 → kreuzberg 解析成 markdown → 存全文 + 带页码/偏移的分块 → 按 id 读取。

- 需要登录（`Authorization: Bearer <token>`）；材料归属登录用户，越权一律 404。
- 上传是**原始请求体**（不是 multipart），单文件一个请求；上限默认 30MB。
- 原始文件落盘 `~/.wendi/materials/<user_id>/<uuid>.<ext>`（`WENDI_MATERIALS_DIR` 可配），不进 Web 根。
- 全文与分块入库（SQLite：`materials` / `material_chunks`）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WENDI_MATERIALS_DIR` | `~/.wendi/materials` | 原始文件目录（按用户分 0700 子目录） |
| `WENDI_UPLOAD_MAX_MB` | `30` | 单文件上限（MB） |
| `WENDI_MATERIALS_MAX` | `200` | 单用户材料条数上限 |
| `WENDI_MATERIALS_MAX_MB` | `500` | 单用户原始文件总额（MB） |
| `WENDI_CHUNK_MAX_CHARS` | `1000` | 分块最大字符数 |
| `WENDI_CHUNK_OVERLAP` | `150` | 分块重叠字符数 |
| `WENDI_PARSE_TIMEOUT` | `120` | 单次解析超时（秒） |
| `WENDI_UPLOAD_TIMEOUT` | `120` | 上传 socket 读超时（秒） |

## POST /api/materials

上传并同步解析。文件名二选一：

- 请求头 `X-Wendi-Filename: <percent-encoded 文件名>`（推荐）
- 查询串 `?name=<percent-encoded 文件名>`（curl 方便）

`Content-Type` 可带文件 MIME；不带则由解析引擎判定。

```bash
TOKEN=...   # /api/auth/login 或 /api/auth/register 返回

# 上传（--data-binary 保证二进制不被改写；>1KB 时关掉 Expect 可少等 1 秒）
curl -sS -X POST http://127.0.0.1:8899/api/materials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/pdf" \
  -H "X-Wendi-Filename: $(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' 年报.pdf)" \
  -H "Expect:" \
  --data-binary @年报.pdf
```

```json
{
  "id": 1,
  "filename": "年报.pdf",
  "mime": "application/pdf",
  "size": 2410776,
  "sha256": "8edf…",
  "char_count": 12651,
  "page_count": 18,
  "chunk_count": 17,
  "truncated": false,
  "parse_ms": 253
}
```

失败：

| HTTP | code | 场景 |
|---|---|---|
| 400 | `filename_required` / `empty_file` / `bad_length` | 缺文件名 / 空文件 / Content-Length 非法 |
| 401 | `unauthorized` | 未登录 |
| 411 | `length_required` | 缺 Content-Length（不支持 chunked） |
| 413 | `file_too_large` | 超过单文件上限 |
| 415 | `unsupported_format` | 引擎不支持该格式 |
| 422 | `password_protected` | 加密 PDF（需密码） |
| 422 | `empty_text` | 扫描件/空文档（OCR 未开启） |
| 429 | `material_limit` / `material_quota` | 条数 / 总空间超限 |
| 503 | `parser_unavailable` | 未安装 kreuzberg |

## GET /api/materials

列出自己的材料（新→旧，不含正文与服务端路径）。

```bash
curl -sS http://127.0.0.1:8899/api/materials -H "Authorization: Bearer $TOKEN"
# {"items":[{"id":1,"filename":"年报.pdf","page_count":18,"chunk_count":17,...}],"total":1}
```

可选 `?limit=200`（1–1000）。

## GET /api/materials/{id}

元数据 + 全文。`?content=0` 只取元数据（列表/轮询用，避免传大字段）。

```bash
curl -sS "http://127.0.0.1:8899/api/materials/1" -H "Authorization: Bearer $TOKEN"
# {"material":{...},"content":"# …"}
```

## GET /api/materials/{id}/chunks?offset=0&limit=100

分块按 `idx` 升序。每块带：

- `char_start` / `char_end`：全文**字符**偏移（划词锚点）
- `byte_start` / `byte_end`：全文 UTF-8 **字节**偏移
- `first_page` / `last_page`：页码（非分页格式为 0）
- `content`、`token_count`

```bash
curl -sS "http://127.0.0.1:8899/api/materials/1/chunks?offset=0&limit=2" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{"material_id":1,"total":17,"offset":0,"limit":2,"chunks":[
  {"idx":0,"content":"…","char_start":0,"char_end":459,"first_page":1,"last_page":1,"token_count":null}
]}
```

## DELETE /api/materials/{id}

删除材料行 + 全部分块 + 原始文件。他人材料 404。

```bash
curl -sS -X DELETE http://127.0.0.1:8899/api/materials/1 -H "Authorization: Bearer $TOKEN"
# {"ok":true,"id":1}
```

## 前端接法（下一轮）

```js
const file = input.files[0];
await fetch("/api/materials", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": file.type || "application/octet-stream",
    "X-Wendi-Filename": encodeURIComponent(file.name),
  },
  body: file,   // 原始 File 作为 body，浏览器自动带 Content-Length
});
```

## 部署提示

- 生产用 venv：`python3 -m venv /opt/wendi/.venv && /opt/wendi/.venv/bin/pip install -r requirements.txt`，systemd `ExecStart` 指向 venv python。
- nginx 默认 `client_max_body_size 1m`，上传会被 nginx 先挡掉：`/api/` location 加 `client_max_body_size 35m;`，并适当放宽 `proxy_read_timeout`（解析 + 上传）。
