#!/usr/bin/env python3
"""抓取 DeepSeek 实时定价。

尝试官方 API 文档 pricing 页，打印 deepseek-flash / deepseek-v4-pro 的输入/输出单价、
缓存命中输入价、离峰折扣等信息。
用法：python3 scripts/fetch_pricing.py
"""
from __future__ import annotations

import re
import urllib.request

SOURCES = [
    # 官方 API 文档 pricing 页（HTML）
    "https://api-docs.deepseek.com/quick_start/pricing/",
    # 官方首页
    "https://api-docs.deepseek.com/",
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def extract_prices(html: str):
    """从 HTML 里抓形如 $0.14 / $0.27 / ¥1 的价格片段和上下文。"""
    # 抓价格 token
    prices = re.findall(r"[\$¥€]\s?\d+(?:\.\d+)?", html)
    # 抓 "per 1M tokens" 之类的单位上下文
    units = re.findall(r"per\s+1M\s+tokens?", html, re.IGNORECASE)
    print("  发现价格片段:", prices[:20], "..." if len(prices) > 20 else "")
    print("  发现 'per 1M tokens' 单位:", len(units), "处")


def main():
    for url in SOURCES:
        print(f"== {url} ==")
        try:
            html = fetch(url)
            print(f"  长度 {len(html)} 字符")
            extract_prices(html)
        except Exception as e:  # noqa: BLE001
            print(f"  失败: {e}")
        print()


if __name__ == "__main__":
    main()
