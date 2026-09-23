#!/usr/bin/env python3
"""在服务器上给 know.kwelldo.cn 的 server 块配置 /api/ 反代（幂等，含 SSE 免缓冲）。"""
import re
import sys

P = "/etc/nginx/sites-enabled/kwelldo.conf"
BLOCK = """    location /api/ {
        proxy_pass http://127.0.0.1:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
        proxy_cache off;
    }

"""
ANCHOR = "    server_name know.kwelldo.cn;\n    root /var/www/kwelldo/know;\n    index index.html;\n"

s = open(P, encoding="utf-8").read()
if "proxy_buffering off" in s:
    print("nginx: 已是 SSE 版，跳过")
    sys.exit(0)
m = re.search(r"    location /api/ \{.*?\n    \}\n\n?", s, re.S)
if m:
    open(P, "w", encoding="utf-8").write(s[:m.start()] + BLOCK + s[m.end():])
    print("nginx: /api/ 块已升级为 SSE 版")
elif ANCHOR in s:
    open(P, "w", encoding="utf-8").write(s.replace(ANCHOR, ANCHOR + "\n" + BLOCK, 1))
    print("nginx: /api/ 反代已加入（SSE 版）")
else:
    print("nginx: 未找到锚点，请手工处理", file=sys.stderr)
    sys.exit(1)
