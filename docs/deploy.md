# 部署要点（Debian 13 + systemd + nginx）

本文是「材料上传 / kreuzberg 解析」上线时的部署口径。**部署脚本只改仓库，不自动上线**；
执行上线时按下面的顺序来。

## 0. 前置条件（一次性人工准备）

- Debian 13（PEP 668 `externally-managed`），有 `sudo`；nginx + systemd 已按现有方式跑着。
- `python3-venv`：Debian 13 默认可能没装。二选一：
  ```bash
  sudo apt install -y python3-venv          # 推荐：人工装一次
  # 或让 deploy.sh 代装：WENDI_HOST=ubuntu@your-server WENDI_INSTALL_VENV=1 ./deploy/deploy.sh
  ```
- 离线 wheel（推荐主路径）：本机参考件
  `/tmp/opencode/kreuz-dl/kreuzberg-4.10.4-cp310-abi3-manylinux_2_28_x86_64.whl`。
  `deploy.sh` 会 `scp` 到服务器 `/tmp` 后用 venv pip 安装，不经过系统 Python。
- 磁盘：材料原始文件默认在 `/home/ubuntu/.wendi/materials`（单用户上限 500MB），
  加上 SQLite；上线前确认剩余空间。
- DNS / TLS 已就绪（`know.kwelldo.cn`）。

## 1. 本机执行顺序

```bash
# 1) 合并 frontend-design → product-dev，跑测试（本仓库）
python3 -m unittest discover -s tests

# 2) 指定部署机；脚本会 rsync 后端 + 前端、建 venv、装依赖、重启服务、更新 nginx
WENDI_HOST=ubuntu@your-server ./deploy/deploy.sh
```

`deploy.sh` 现在按这个顺序做（幂等，可反复跑）：

1. 后端 rsync：`server.py prompts.py db.py auth.py kernel.py materials.py requirements.txt`
   → `/opt/wendi/`。**漏 `materials.py` 会导致 systemd 启动 ImportError。**
2. venv 引导：`/opt/wendi/.venv` 不存在才创建；Python < 3.10 直接报错。
3. 依赖：默认优先离线 wheel；已装 4.10.4 则跳过；wheel 找不到/装失败则回退在线 pip。
4. 前端 rsync（`web/` + `web/vendor/`）。
5. 密钥（缺失才补，不覆盖已有）、systemd unit、日报 timer、nginx、健康检查。

### 依赖安装的两种路径

| 路径 | 触发 | 说明 |
|---|---|---|
| **离线 wheel（默认主路径）** | `WENDI_WHEEL_DIR` 下存在 `kreuzberg-4.10.4-*.whl` | 已验证的 wheel，避免生产访问 PyPI；只在未安装时 scp（不重复传 38MB） |
| 在线 pip（兜底） | 没有 wheel，或 `WENDI_USE_PIP=1` 时优先 | `venv/bin/pip install -r requirements.txt`，使用服务器 pip 配置的索引 |

自定义 wheel 目录：

```bash
WENDI_WHEEL_DIR=/path/to/wheels WENDI_HOST=ubuntu@your-server ./deploy/deploy.sh
# 强制先在线、失败再回退 wheel：
WENDI_USE_PIP=1 WENDI_HOST=ubuntu@your-server ./deploy/deploy.sh
```

手动兜底（脚本外）：

```bash
scp /tmp/opencode/kreuz-dl/kreuzberg-4.10.4-*.whl ubuntu@your-server:/tmp/
ssh ubuntu@your-server '/opt/wendi/.venv/bin/pip install --no-index /tmp/kreuzberg-4.10.4-*.whl'
```

## 2. systemd

`deploy/wendi.service` 的 `ExecStart` 已改为 venv 解释器：

```ini
ExecStart=/opt/wendi/.venv/bin/python -u /opt/wendi/server.py
```

- 不要改回 `/usr/bin/python3`：Debian 13 的 PEP 668 会拒绝系统级 pip，且 kreuzberg 只装在 venv。
- `ReadWritePaths=/opt/wendi -/home/ubuntu/.wendi` 已覆盖默认材料目录；
  若把 `WENDI_MATERIALS_DIR` 指到别处，需同步加进 `ReadWritePaths`。
- 部署脚本在 `systemctl restart wendi` 后检查 `systemctl is-active wendi`。

## 3. nginx

`deploy/nginx-know.py` 会把 `/api/` 块升级为：

```nginx
location /api/ {
    client_max_body_size 35m;
    proxy_pass http://127.0.0.1:8899;
    ...
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_cache off;
}
```

原因：

- **`client_max_body_size 35m`**：nginx 默认 1MB，30MB 上传会在到达后端前被 413 挡掉。
  35MB 是 30MB 上限 + 头部/余量。
- **`proxy_read_timeout` / `proxy_send_timeout 300s`**：上传 + 解析可能超过默认 60s。
- **`proxy_request_buffering off`**：让 nginx 边收边转发，配合后端 64KB 流式落盘；
  不设也能用，但 nginx 会先把 30MB 缓冲到磁盘再转发。
- **`proxy_buffering off`**：保持现有 SSE 不缓冲行为。

脚本按「`client_max_body_size 35m` + `proxy_request_buffering off` 都在」判定已升级；
老部署即使已有 SSE 版也会被替换。执行后 `sudo nginx -t && sudo systemctl reload nginx` 由
`deploy.sh` 完成。

## 4. 部署后验证

```bash
# 1) 服务与模型
curl -s https://know.kwelldo.cn/api/health

# 2) 登录拿 token
TOKEN=$(curl -s -X POST https://know.kwelldo.cn/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 3) 上传小 PDF 冒烟（应 201，带 page_count/chunk_count）
curl -sS -X POST https://know.kwelldo.cn/api/materials \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/pdf' \
  -H 'X-Wendi-Filename: smoke.pdf' -H 'Expect:' \
  --data-binary @small.pdf

# 4) 读取全文 / 分块
curl -sS https://know.kwelldo.cn/api/materials/1 -H "Authorization: Bearer $TOKEN"
curl -sS 'https://know.kwelldo.cn/api/materials/1/chunks?offset=0&limit=2' \
  -H "Authorization: Bearer $TOKEN"

# 5) 加密 PDF 错误路径（应 422 password_protected）
curl -sS -w '\nHTTP %{http_code}\n' -X POST https://know.kwelldo.cn/api/materials \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/pdf' \
  -H 'X-Wendi-Filename: encrypted.pdf' -H 'Expect:' \
  --data-binary @encrypted.pdf

# 6) 清理冒烟材料
curl -sS -X DELETE https://know.kwelldo.cn/api/materials/1 -H "Authorization: Bearer $TOKEN"
```

### 「未装引擎」的语义

- 服务**仍能启动**（`materials.py` 懒加载 kreuzberg），`/api/health` 正常。
- 上传接口返回 **503 `parser_unavailable`**；其他 LLM 功能不受影响。
- 部署后如果上传仍是 503，说明 venv 依赖没装好：

```bash
ssh ubuntu@your-server '/opt/wendi/.venv/bin/pip show kreuzberg'
ssh ubuntu@your-server '/opt/wendi/.venv/bin/python -c "import kreuzberg; print(kreuzberg.__version__)"'
sudo journalctl -u wendi -n 80 -o cat
```

## 5. 数据与回滚

- 启动时 `db.init()` 只做 `CREATE TABLE IF NOT EXISTS` / 加列，**材料表是增量迁移**，
  不需要手工改库；旧数据不受影响。
- 备份：`/home/ubuntu/.wendi/wendi.db` 与 `/home/ubuntu/.wendi/materials/` 一起备份。
- 回滚：`git revert` 对应提交后重跑 `deploy.sh`；venv 可保留。
  材料表/文件不会被旧代码主动删除（旧接口只是不暴露）。
- `deploy.sh` 不删除 `/opt/wendi` 旧文件，也不清理已有 venv；依赖版本由 requirements/wheel 控制。

## 6. 需要人工确认的点

- 服务器是否已装 `python3-venv`（否则 `sudo apt install python3-venv`，或 `WENDI_INSTALL_VENV=1`）。
- 离线 wheel 是否在部署机的 `WENDI_WHEEL_DIR`（默认 `/tmp/opencode/kreuz-dl`）；
  没有就确保服务器能访问 PyPI，或用 `WENDI_USE_PIP=1` 明确走在线。
- nginx 配置路径是否仍是 `/etc/nginx/sites-enabled/kwelldo.conf`（`nginx-know.py` 的 `P`）。
- `sudo`：创建 `/opt/wendi`、写 systemd unit、reload nginx、可选 apt 安装都需要。
