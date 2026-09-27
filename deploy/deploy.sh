#!/usr/bin/env bash
# 部署/更新「问底」到 know.kwelldo.cn（幂等，可反复跑）
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${WENDI_HOST:?请设 WENDI_HOST（ssh 主机别名或 user@host）}"

echo "== 1) 后端 -> /opt/wendi =="
ssh $HOST 'sudo mkdir -p /opt/wendi && sudo chown ubuntu:ubuntu /opt/wendi'
rsync -az server.py prompts.py db.py auth.py kernel.py "$HOST:/opt/wendi/"
rsync -az deploy/gencode.py "$HOST:/opt/wendi/"

echo "== 2) 前端 -> /var/www/kwelldo/know/ =="
# 整目录一次 rsync。原来 9 个文件 × (scp + ssh) = 18 次连接，腾讯云 sshd 在连接突发里会直接
# 断开（2026-09-28 实测：Connection closed by ... 部署跑到一半 abort，前一次还撞 300s 超时）。
rsync -az --exclude 'vendor/' --exclude 'graph3d-test.html' web/ "$HOST:/tmp/wendi-web/"
ssh $HOST 'sudo rsync -a --exclude vendor/ /tmp/wendi-web/ /var/www/kwelldo/know/ && rm -rf /tmp/wendi-web'
# 第三方静态资源（KaTeX）；目录结构原样同步，缺它前端会退化回原始 LaTeX 文本
rsync -az --delete web/vendor/ "$HOST:/tmp/wendi-vendor/"
ssh $HOST 'sudo rsync -a --delete /tmp/wendi-vendor/ /var/www/kwelldo/know/vendor/ && rm -rf /tmp/wendi-vendor'

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
scp -q deploy/upsert-env.py "$HOST:/tmp/wendi-upsert-env.py"
printf 'WENDI_API_KEY=%s\nWENDI_ADMIN_TOKEN=%s\nWENDI_REQUIRE_INVITE=%s\n' \
  "$API_KEY" "$ADMIN_TOKEN" "${WENDI_REQUIRE_INVITE:-1}" \
  | ssh $HOST 'sudo python3 /tmp/wendi-upsert-env.py /etc/wendi.env && rm -f /tmp/wendi-upsert-env.py'

echo "== 4) systemd 服务 =="
scp -q deploy/wendi.service "$HOST:/tmp/wendi.service"
ssh $HOST 'sudo mv /tmp/wendi.service /etc/systemd/system/wendi.service && sudo systemctl daemon-reload && sudo systemctl enable wendi >/dev/null 2>&1 || true; sudo systemctl restart wendi && sleep 1 && systemctl is-active wendi'

echo "== 4b) 内测日报（脚本 + 每日 timer） =="
scp -q scripts/daily_report.py "$HOST:/tmp/wendi-daily_report.py"
scp -q deploy/wendi-report.service deploy/wendi-report.timer "$HOST:/tmp/"
ssh $HOST 'sudo mv /tmp/wendi-daily_report.py /opt/wendi/daily_report.py && sudo mkdir -p /opt/wendi/reports && sudo chown -R ubuntu:ubuntu /opt/wendi/reports && sudo mv /tmp/wendi-report.service /tmp/wendi-report.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now wendi-report.timer >/dev/null && systemctl is-active wendi-report.timer'

echo "== 5) nginx 反代（幂等） =="
scp -q deploy/nginx-know.py "$HOST:/tmp/nginx-know.py"
ssh $HOST 'sudo python3 /tmp/nginx-know.py && sudo nginx -t 2>&1 | tail -1 && sudo systemctl reload nginx && rm -f /tmp/nginx-know.py'

echo "== 6) 验证 =="
sleep 1
curl -s -m 12 https://know.kwelldo.cn/api/health && echo
echo "✓ 完成：https://know.kwelldo.cn"
