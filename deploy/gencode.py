#!/usr/bin/env python3
"""生成兑换码（在服务器 /opt/wendi 下运行）

用法：python3 gencode.py [数量=10] [天数=30] [备注]
"""
import sys
sys.path.insert(0, ".")
import db  # noqa: E402

db.init()
n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
note = sys.argv[3] if len(sys.argv) > 3 else ""
for c in db.gen_codes(n, days, note):
    print(c)
