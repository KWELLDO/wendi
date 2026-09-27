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

echo "== 3) 密钥（仅首次创建） =="
if ssh $HOST 'test -f /etc/wendi.env'; then
  echo "   /etc/wendi.env 已存在，跳过"
else
  [ -n "${WENDI_API_KEY:-}" ] || { echo "找不到 WENDI_API_KEY，中止"; exit 1; }
  ADMIN_TOKEN="${WENDI_ADMIN_TOKEN:-$(openssl rand -hex 16)}"
  printf 'WENDI_API_KEY=%s\nWENDI_ADMIN_TOKEN=%s\n' "$WENDI_API_KEY" "$ADMIN_TOKEN" \
    | ssh $HOST 'sudo bash -c "umask 077; cat > /etc/wendi.env; chmod 600 /etc/wendi.env"'
  echo "   /etc/wendi.env 已创建（600）"
  echo "   管理 token（请自行保存，用于 /api/meter）：$ADMIN_TOKEN"
fi

echo "== 4) systemd 服务 =="
scp -q deploy/wendi.service "$HOST:/tmp/wendi.service"
ssh $HOST 'sudo mv /tmp/wendi.service /etc/systemd/system/wendi.service && sudo systemctl daemon-reload && sudo systemctl enable wendi >/dev/null 2>&1 || true; sudo systemctl restart wendi && sleep 1 && systemctl is-active wendi'

echo "== 5) nginx 反代（幂等） =="
scp -q deploy/nginx-know.py "$HOST:/tmp/nginx-know.py"
ssh $HOST 'sudo python3 /tmp/nginx-know.py && sudo nginx -t 2>&1 | tail -1 && sudo systemctl reload nginx && rm -f /tmp/nginx-know.py'

echo "== 6) 验证 =="
sleep 1
curl -s -m 12 https://know.kwelldo.cn/api/health && echo
echo "✓ 完成：https://know.kwelldo.cn"
