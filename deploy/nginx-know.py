#!/usr/bin/env python3
"""在服务器上给 know.kwelldo.cn 的 server 块配置 /api/ 反代（幂等，含 SSE 免缓冲）。"""
import re
import sys

P = "/etc/nginx/sites-enabled/kwelldo.conf"
BLOCK = """    location /api/ {
        # 材料上传上限 30MB（server.py WENDI_UPLOAD_MAX_MB 默认 30）；留 5MB 余量给请求头/边界。
        client_max_body_size 35m;
        proxy_pass http://127.0.0.1:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 解析 + 上传可能超过 60s；与 server.py 的 UPLOAD_TIMEOUT/PARSE_TIMEOUT 对齐。
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        # 边收边转给后端，避免 nginx 先落盘再转发 30MB；SSE 仍保持不缓冲。
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_cache off;
    }

"""
ANCHOR = "    server_name know.kwelldo.cn;\n    root /var/www/kwelldo/know;\n    index index.html;\n"

s = open(P, encoding="utf-8").read()
m = re.search(r"    location /api/ \{.*?\n    \}\n\n?", s, re.S)
if m:
    # 幂等标记只看 /api/ 块内：避免别处的同名指令让本脚本误判为已升级。
    block = m.group(0)
    if "client_max_body_size 35m" in block and "proxy_request_buffering off" in block:
        print("nginx: 已是材料上传版，跳过")
        sys.exit(0)
    open(P, "w", encoding="utf-8").write(s[:m.start()] + BLOCK + s[m.end():])
    print("nginx: /api/ 块已升级（35m / 300s / 请求流式）")
elif ANCHOR in s:
    open(P, "w", encoding="utf-8").write(s.replace(ANCHOR, ANCHOR + "\n" + BLOCK, 1))
    print("nginx: /api/ 反代已加入（35m / 300s / 请求流式）")
else:
    print("nginx: 未找到锚点，请手工处理", file=sys.stderr)
    sys.exit(1)
