#!/usr/bin/env python3
"""在服务器上给 know.kwelldo.cn 的 server 块加入 /api/ 反代（幂等）。"""
import sys

P = "/etc/nginx/sites-enabled/kwelldo.conf"
ANCHOR = "    server_name know.kwelldo.cn;\n    root /var/www/kwelldo/know;\n    index index.html;\n"
LOC = """\
    location /api/ {
        proxy_pass http://127.0.0.1:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

"""

s = open(P, encoding="utf-8").read()
if "location /api/" in s:
    print("nginx: /api/ 已存在，跳过")
elif ANCHOR in s:
    open(P, "w", encoding="utf-8").write(s.replace(ANCHOR, ANCHOR + "\n" + LOC, 1))
    print("nginx: 已加入 /api/ 反代")
else:
    print("nginx: 未找到锚点，请手工处理", file=sys.stderr)
    sys.exit(1)
