#!/usr/bin/env python3
"""一次性清理：作废所有未使用的邀请码（邀请制已取消，kind='invite' 不再有消费方）。

在服务器 /opt/wendi 下运行：python3 void-invites.py
只作废未使用且未作废的邀请码；已注册账号的 users.invite_code 历史归因不受影响。
"""
import sys
sys.path.insert(0, ".")
import db  # noqa: E402

db.init()
with db._conn() as c:
    n = c.execute("UPDATE codes SET voided=1 "
                  "WHERE kind='invite' AND used_by IS NULL AND voided=0").rowcount
print(f"[void-invites] 已作废未使用邀请码 {n} 个")
