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
for f in index.html graph2d.html meter.html admin.html about.html privacy.html terms.html feedback.html site.css; do
  scp -q "web/$f" "$HOST:/tmp/wendi-$f"
  ssh $HOST "sudo cp /tmp/wendi-$f /var/www/kwelldo/know/$f && rm -f /tmp/wendi-$f"
done
# 第三方静态资源（KaTeX）；目录结构原样同步，缺它前端会退化回原始 LaTeX 文本
rsync -az --delete web/vendor/ "$HOST:/tmp/wendi-vendor/"
ssh $HOST 'sudo rsync -a --delete /tmp/wendi-vendor/ /var/www/kwelldo/know/vendor/ && rm -rf /tmp/wendi-vendor'

echo "== 3) 密钥（仅首次创建） =="
if ssh $HOST 'test -f /etc/wendi.env'; then
  echo "   /etc/wendi.env 已存在，跳过"
else
  [ -n "${WENDI_API_KEY:-}" ] || { echo "找不到 WENDI_API_KEY，中止"; exit 1; }
  ADMIN_TOKEN="${WENDI_ADMIN_TOKEN:-$(openssl rand -hex 16)}"
  # WENDI_REQUIRE_INVITE=1 表示注册必须邀请码（内测默认）；设 0 可临时放开公开注册
  printf 'WENDI_API_KEY=%s\nWENDI_ADMIN_TOKEN=%s\nWENDI_REQUIRE_INVITE=1\n' "$WENDI_API_KEY" "$ADMIN_TOKEN" \
    | ssh $HOST 'sudo bash -c "umask 077; cat > /etc/wendi.env; chmod 600 /etc/wendi.env"'
  echo "   /etc/wendi.env 已创建（600）"
  echo "   管理 token（请自行保存，用于 /api/meter）：$ADMIN_TOKEN"
fi

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
