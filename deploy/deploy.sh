#!/usr/bin/env bash
# 部署/更新「问底」到 know.kwelldo.cn（幂等，可反复跑）
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${WENDI_HOST:?请设 WENDI_HOST（ssh 主机别名或 user@host）}"

# —— Python 依赖（材料解析）——
# Debian 13 是 PEP 668 externally-managed：依赖必须装进 venv，systemd 也指向它。
# 默认优先用本机已验证的离线 wheel；找不到时回退在线 pip。设 WENDI_USE_PIP=1 可强制先走在线。
VENV=/opt/wendi/.venv
WHEEL_DIR="${WENDI_WHEEL_DIR:-/tmp/opencode/kreuz-dl}"
WENDI_USE_PIP="${WENDI_USE_PIP:-0}"

# —— SSH 多路复用：整个脚本复用一条 TCP 连接 ——
# 腾讯云 sshd 会掐连接突发（2026-09-28 实测三次中断：一次 300s 超时、两次 Connection closed by …，
# 结果都是"半成品部署"）。复用后每个 ssh/scp 只花毫秒，也不再每次 5s 握手。
CTL="${TMPDIR:-/tmp}/wendi-deploy-$$.sock"
SSHOPT=(-o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=120
        -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
ssh_x()   { ssh "${SSHOPT[@]}" "$@"; }
scp_x()   { scp "${SSHOPT[@]}" "$@"; }
rsync_x() { rsync -e "ssh ${SSHOPT[*]}" "$@"; }
trap 'ssh -O exit -o ControlPath="$CTL" "$HOST" >/dev/null 2>&1 || true; rm -f "$CTL"' EXIT

# —— venv / 依赖辅助（deploy.sh 幂等：已就绪就跳过） ——
find_wheel() {
  find "$WHEEL_DIR" -maxdepth 1 -type f -name 'kreuzberg-4.10.4-*.whl' -print -quit 2>/dev/null || true
}
dep_ready() {
  ssh_x $HOST "$VENV/bin/pip show kreuzberg 2>/dev/null | grep -qx 'Version: 4.10.4'"
}
install_online() {
  echo "   在线 pip 安装 requirements.txt（备选方案）"
  ssh_x $HOST "$VENV/bin/pip install --disable-pip-version-check -r /opt/wendi/requirements.txt"
}
install_wheel() {
  local wheel="$1" name
  name="$(basename "$wheel")"
  echo "   离线 wheel：$name"
  # 必须保留原文件名 scp：pip 按文件名解析 wheel 元数据，改名会被拒（"not a valid wheel filename"）。
  # 2026-10-18 首上线实测：改名版失败→在线兜底；本修复让离线路径真正可用。
  scp_x -q "$wheel" "$HOST:/tmp/$name"
  ssh_x $HOST "$VENV/bin/pip install --no-index --disable-pip-version-check /tmp/$name; rc=\$?; rm -f /tmp/$name; exit \$rc"
}

echo "== 1) 后端 -> /opt/wendi =="
ssh_x $HOST 'sudo mkdir -p /opt/wendi && sudo chown ubuntu:ubuntu /opt/wendi'
# materials.py 必须同步，否则 systemd 启动时 ImportError；requirements.txt 供在线兜底安装。
rsync_x -az server.py prompts.py db.py auth.py kernel.py materials.py requirements.txt "$HOST:/opt/wendi/"
rsync_x -az deploy/gencode.py "$HOST:/opt/wendi/"

echo "== 1b) Python 依赖（venv；Debian 13 PEP 668） =="
if ! ssh_x $HOST "test -x $VENV/bin/python && test -x $VENV/bin/pip"; then
  if ! ssh_x $HOST 'python3 -c "import ensurepip" >/dev/null 2>&1'; then
    if [ "${WENDI_INSTALL_VENV:-0}" = "1" ]; then
      echo "   安装 python3-venv（WENDI_INSTALL_VENV=1）"
      ssh_x $HOST 'sudo apt-get update -qq && sudo apt-get install -y python3-venv'
    else
      echo "   缺少 python3-venv：先在服务器执行 sudo apt install python3-venv，"
      echo "   或设 WENDI_INSTALL_VENV=1 让脚本安装。"
      exit 1
    fi
  fi
  echo "   创建 $VENV"
  if ! ssh_x $HOST "python3 -m venv $VENV"; then
    echo "   创建 venv 失败：确认 venv 目录权限，以及 sudo apt install python3-venv 已执行。"
    exit 1
  fi
fi
# kreuzberg 的 wheel 是 cp310-abi3：venv Python 必须 >= 3.10（Debian 13 默认 3.13）。
ssh_x $HOST "$VENV/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'" \
  || { echo "   $VENV Python < 3.10，无法安装 kreuzberg abi3 wheel"; exit 1; }

if dep_ready; then
  ssh_x $HOST "$VENV/bin/python -c 'import kreuzberg'" \
    || { echo "   kreuzberg 已安装但 import 失败（venv/ABI 不兼容）"; exit 1; }
  echo "   kreuzberg 4.10.4 已就绪"
else
  WHEEL="$(find_wheel)"
  if [ "$WENDI_USE_PIP" = "1" ]; then
    if ! install_online; then
      echo "   在线安装失败，尝试离线 wheel（$WHEEL_DIR）"
      [ -n "$WHEEL" ] || { echo "   也没找到离线 wheel；见 docs/deploy.md"; exit 1; }
      install_wheel "$WHEEL"
    fi
  elif [ -n "$WHEEL" ]; then
    if ! install_wheel "$WHEEL"; then
      echo "   离线 wheel 安装失败，回退在线 pip"
      install_online
    fi
  else
    echo "   未找到离线 wheel（$WHEEL_DIR），改走在线 pip"
    install_online
  fi
  dep_ready || { echo "   安装后 kreuzberg 版本不是 4.10.4："; ssh_x $HOST "$VENV/bin/pip show kreuzberg || true"; exit 1; }
  ssh_x $HOST "$VENV/bin/python -c 'import kreuzberg'" \
    || { echo "   import kreuzberg 失败（venv/ABI 不兼容）"; exit 1; }
fi

echo "== 2) 前端 -> /var/www/kwelldo/know/ =="
# 整目录一次 rsync。原来 9 个文件 × (scp + ssh) = 18 次连接，腾讯云 sshd 在连接突发里会直接
# 断开（2026-09-28 实测：Connection closed by ... 部署跑到一半 abort，前一次还撞 300s 超时）。
rsync_x -az --exclude 'vendor/' --exclude 'graph3d-test.html' web/ "$HOST:/tmp/wendi-web/"
ssh_x $HOST 'sudo rsync -a --exclude vendor/ /tmp/wendi-web/ /var/www/kwelldo/know/ && rm -rf /tmp/wendi-web'
# 第三方静态资源（KaTeX）；目录结构原样同步，缺它前端会退化回原始 LaTeX 文本
rsync_x -az --delete web/vendor/ "$HOST:/tmp/wendi-vendor/"
ssh_x $HOST 'sudo rsync -a --delete /tmp/wendi-vendor/ /var/www/kwelldo/know/vendor/ && rm -rf /tmp/wendi-vendor'

echo "== 3) 密钥（补缺失，不覆盖已有；值走 stdin，不进命令行） =="
# 值来源：环境变量 > 本机 ~/.secrets（key get）> 缺则现生成管理令牌
API_KEY="${WENDI_API_KEY:-$(command -v key >/dev/null 2>&1 && key get wendi_API 2>/dev/null || true)}"
ADMIN_TOKEN="${WENDI_ADMIN_TOKEN:-$(command -v key >/dev/null 2>&1 && key get wendi_admin_token 2>/dev/null || true)}"
[ -n "$API_KEY" ] || { echo "找不到 API key：设 WENDI_API_KEY=…，或 key set wendi_API=…"; exit 1; }
if [ -z "$ADMIN_TOKEN" ]; then
  ADMIN_TOKEN="$(openssl rand -hex 32)"
  if command -v key >/dev/null 2>&1; then
    key set "wendi_admin_token=$ADMIN_TOKEN"
    key note wendi_admin_token note="问底站长令牌（deploy.sh 自动生成）" >/dev/null 2>&1 || true
    echo "   已生成管理令牌并写进 ~/.secrets：用 key copy wendi_admin_token 取"
  else
    echo "   已生成管理令牌（请立即保存，站长后台 /admin.html 用）：$ADMIN_TOKEN"
  fi
fi
scp_x -q deploy/upsert-env.py "$HOST:/tmp/wendi-upsert-env.py"
printf 'WENDI_API_KEY=%s\nWENDI_ADMIN_TOKEN=%s\nWENDI_REQUIRE_INVITE=%s\n' \
  "$API_KEY" "$ADMIN_TOKEN" "${WENDI_REQUIRE_INVITE:-1}" \
  | ssh_x $HOST 'sudo python3 /tmp/wendi-upsert-env.py /etc/wendi.env && rm -f /tmp/wendi-upsert-env.py'

echo "== 4) systemd 服务 =="
scp_x -q deploy/wendi.service "$HOST:/tmp/wendi.service"
ssh_x $HOST 'sudo mv /tmp/wendi.service /etc/systemd/system/wendi.service && sudo systemctl daemon-reload && sudo systemctl enable wendi >/dev/null 2>&1 || true; sudo systemctl restart wendi && sleep 1 && systemctl is-active wendi'

echo "== 4b) 内测日报（脚本 + 每日 timer） =="
scp_x -q scripts/daily_report.py "$HOST:/tmp/wendi-daily_report.py"
scp_x -q deploy/wendi-report.service deploy/wendi-report.timer "$HOST:/tmp/"
ssh_x $HOST 'sudo mv /tmp/wendi-daily_report.py /opt/wendi/daily_report.py && sudo mkdir -p /opt/wendi/reports && sudo chown -R ubuntu:ubuntu /opt/wendi/reports && sudo mv /tmp/wendi-report.service /tmp/wendi-report.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now wendi-report.timer >/dev/null && systemctl is-active wendi-report.timer'

echo "== 5) nginx 反代（幂等） =="
scp_x -q deploy/nginx-know.py "$HOST:/tmp/nginx-know.py"
ssh_x $HOST 'sudo python3 /tmp/nginx-know.py && sudo nginx -t 2>&1 | tail -1 && sudo systemctl reload nginx && rm -f /tmp/nginx-know.py'

echo "== 6) 验证 =="
sleep 1
curl -s -m 12 https://know.kwelldo.cn/api/health && echo
echo "✓ 完成：https://know.kwelldo.cn"
