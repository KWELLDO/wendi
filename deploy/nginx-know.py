#!/usr/bin/env python3
"""给 know.kwelldo.cn 的 server 块打补丁（幂等，两个补丁各自独立判断）：
  1) /api/ 反代（含 SSE 免缓冲）
  2) Cache-Control: no-cache（避免浏览器启发式缓存旧页面）
写配置前先备份；写完跑 nginx -t，失败则回滚备份并退出非 0（不把坏配置留在磁盘上）。"""
import re
import shutil
import subprocess
import sys

P = "/etc/nginx/sites-enabled/kwelldo.conf"
BAK = P + ".bak"
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
HDR = '    add_header Cache-Control "no-cache" always;\n'

s = open(P, encoding="utf-8").read()
changed = False

# —— 补丁 1：/api/ 反代升级为材料上传版（35m / 300s / 请求流式；独立幂等；已存在则跳过，不短路后续步骤）——
m = re.search(r"    location /api/ \{.*?\n    \}\n\n?", s, re.S)
if m:
    # 幂等标记只看 /api/ 块内：避免别处的同名指令让本脚本误判为已升级。
    block = m.group(0)
    if "client_max_body_size 35m" in block and "proxy_request_buffering off" in block:
        print("nginx: /api/ 已是材料上传版，跳过")
    else:
        s = s[:m.start()] + BLOCK + s[m.end():]
        print("nginx: /api/ 块已升级（35m / 300s / 请求流式）")
        changed = True
elif ANCHOR in s:
    s = s.replace(ANCHOR, ANCHOR + "\n" + BLOCK, 1)
    print("nginx: /api/ 反代已加入（35m / 300s / 请求流式）")
    changed = True
else:
    print("nginx: 未找到 /api/ 锚点，请手工处理", file=sys.stderr)
    sys.exit(1)

# —— 补丁 2：Cache-Control: no-cache（独立幂等）——
if 'Cache-Control "no-cache"' in s:
    print("nginx: Cache-Control: no-cache 已存在，跳过")
elif ANCHOR in s:
    s = s.replace(ANCHOR, ANCHOR + HDR, 1)
    print("nginx: Cache-Control: no-cache 已加入")
    changed = True
else:
    print("nginx: 未找到 server 块锚点，无法加 Cache-Control，请手工处理", file=sys.stderr)
    sys.exit(1)

if not changed:
    print("nginx: 无需改动")
    sys.exit(0)

# —— 写配置前先备份；写完 nginx -t，失败则回滚备份并报错退出 ——
shutil.copy2(P, BAK)
with open(P, "w", encoding="utf-8") as f:
    f.write(s)

t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
if t.returncode != 0:
    shutil.copy2(BAK, P)
    print("nginx: nginx -t 失败，已回滚备份，配置未生效", file=sys.stderr)
    sys.stderr.write(t.stdout)
    sys.stderr.write(t.stderr)
    sys.exit(1)

print("nginx: nginx -t 通过（原配置备份在 %s）" % BAK)
