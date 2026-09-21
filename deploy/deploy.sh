#!/usr/bin/env bash
# 部署/更新「问底」到 know.kwelldo.cn（幂等，可反复跑）
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=tencent

echo "== 1) 后端 -> /opt/wendi =="
ssh $HOST 'sudo mkdir -p /opt/wendi && sudo chown ubuntu:ubuntu /opt/wendi'
rsync -az server.py prompts.py "$HOST:/opt/wendi/"

echo "== 2) 前端 -> /var/www/kwelldo/know/ =="
scp -q web/index.html "$HOST:/tmp/wendi-index.html"
ssh $HOST 'sudo cp /tmp/wendi-index.html /var/www/kwelldo/know/index.html && rm -f /tmp/wendi-index.html'

echo "== 3) 密钥（仅首次创建） =="
if ssh $HOST 'test -f /etc/wendi.env'; then
  echo "   /etc/wendi.env 已存在，跳过"
else
  KEY=$(grep '^deepseek_n8n_api_key=' ~/.secrets | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
  [ -n "$KEY" ] || { echo "找不到 deepseek_n8n_api_key，中止"; exit 1; }
  printf 'WENDI_API_KEY=%s\n' "$KEY" | ssh $HOST 'sudo bash -c "umask 077; cat > /etc/wendi.env; chmod 600 /etc/wendi.env"'
  echo "   /etc/wendi.env 已创建（600）"
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
