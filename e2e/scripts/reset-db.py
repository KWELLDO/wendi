#!/usr/bin/env python3
"""重建 e2e 专用 SQLite 库（只建表 + 写 meter_config 默认值）。

调用方（global-setup.ts）负责先删库；这里再做一次路径防呆，
确保永远不会拿 ~/.wendi/wendi.db 当测试库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

raw = os.environ.get("WENDI_DB", "")
if not raw:
    raise SystemExit("[e2e] WENDI_DB 未设置，拒绝初始化数据库")

db_path = Path(raw).resolve()
allowed = (REPO_ROOT / "e2e" / ".tmp").resolve()
home_db = (Path.home() / ".wendi" / "wendi.db").resolve()

if db_path == home_db or allowed not in db_path.parents:
    raise SystemExit(f"[e2e] 拒绝初始化非测试数据库: {db_path}")

db_path.parent.mkdir(parents=True, exist_ok=True)

import db  # noqa: E402  （必须在 WENDI_DB 已就位后导入）
import messages  # noqa: E402

db.init()
messages.init()  # user_messages 表（global-setup 会删掉 server.py 启动时建的库，schema 以这里为准）
print(f"[e2e] 测试数据库已重建: {db.DB_PATH}")
