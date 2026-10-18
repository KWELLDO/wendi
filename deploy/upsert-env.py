#!/usr/bin/env python3
"""幂等地把 KEY=VALUE 合并进 systemd EnvironmentFile（默认 /etc/wendi.env）。

用法（值从 stdin 走，不经过命令行参数，避免出现在 ps / shell 历史里）：
    printf 'WENDI_API_KEY=%s\\nWENDI_ADMIN_TOKEN=%s\\n' "$K" "$T" \
      | sudo python3 upsert-env.py /etc/wendi.env

规则：
- **只改 stdin 里出现的键**：已有的键原地替换，缺的键追加，其它行（含注释）原样保留；
- 写入前备份到 <path>.bak-YYYYmmdd-HHMMSS（600），写好后原子替换（同目录 rename）；
- 只打印键名与备份文件名，**绝不回显值**；
- 幂等：同样的输入跑两次，第二次只更新修改时间，内容一致。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not KEY_RE.match(key):
            raise SystemExit(f"非法键名：{key!r}")
        pairs.append((key, value))
    if not pairs:
        raise SystemExit("stdin 里没有 KEY=VALUE")
    return pairs


def merge(existing: str, pairs: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
    """返回 (新内容, 被替换的键, 追加的键)。"""
    want = dict(pairs)
    replaced, added, out = [], [], []
    for line in existing.splitlines():
        stripped = line.lstrip()
        key = None
        if "=" in line and not stripped.startswith("#"):
            key = line.split("=", 1)[0].strip()
        if key and key in want:
            out.append(f"{key}={want[key]}")
            replaced.append(key)
            want.pop(key)
        else:
            out.append(line)
    for key, value in pairs:          # 保持输入顺序追加剩下的
        if key in want:
            out.append(f"{key}={value}")
            added.append(key)
    return "\n".join(out) + "\n", replaced, added


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/wendi.env"
    pairs = parse_pairs(sys.stdin.read())
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    else:
        existing = ""
    content, replaced, added = merge(existing, pairs)
    if os.path.exists(path):
        bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, bak)
        os.chmod(bak, 0o600)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"env 写入完成：替换 {replaced or '无'} · 新增 {added or '无'}")
    print("当前键名：" + " ".join(re.findall(r"(?m)^([A-Za-z_]+)=", content)))


if __name__ == "__main__":
    main()
