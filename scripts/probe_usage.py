#!/usr/bin/env python3
"""实测 DeepSeek 是否返回 usage 字段（非流式 + 流式）。

用途：验证经济测算系统的命门——usage 到底能不能拿到。
不写库、不改生产代码，只打印结果。
用法：python3 scripts/probe_usage.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("WENDI_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("WENDI_MODEL", "deepseek-flash")


def load_key() -> str:
    env = os.environ.get("WENDI_API_KEY")
    if env:
        return env
    f = Path.home() / ".secrets"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("deepseek_api_key="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    sys.exit("无 API key（设 WENDI_API_KEY 或 ~/.secrets 里放 deepseek_api_key）")


def req(payload: dict, key: str):
    return urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")


def probe_non_stream(key: str):
    print("=== 非流式 stream=false ===")
    p = {"model": MODEL, "messages": [
        {"role": "system", "content": "简短回答。"},
        {"role": "user", "content": "什么是波函数坍缩？"},
    ], "temperature": 0.4, "max_tokens": 50, "stream": False}
    with urllib.request.urlopen(req(p, key), timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    usage = data.get("usage")
    print("usage 字段:", json.dumps(usage, ensure_ascii=False))
    print("顶层 keys:", list(data.keys()))
    if usage:
        print(f"prompt_tokens={usage.get('prompt_tokens')} "
              f"completion_tokens={usage.get('completion_tokens')} "
              f"total_tokens={usage.get('total_tokens')}")
        for k in usage:
            if k not in ("prompt_tokens", "completion_tokens", "total_tokens"):
                print(f"  (附加字段) {k} = {usage[k]}")


def probe_stream_basic(key: str):
    print("\n=== 流式 stream=true（不带 stream_options）===")
    p = {"model": MODEL, "messages": [
        {"role": "system", "content": "简短回答。"},
        {"role": "user", "content": "什么是波函数坍缩？"},
    ], "temperature": 0.4, "max_tokens": 50, "stream": True}
    usage = None
    chunks = 0
    last_chunk = None
    with urllib.request.urlopen(req(p, key), timeout=60) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                obj = json.loads(d)
            except json.JSONDecodeError:
                continue
            chunks += 1
            last_chunk = obj
            u = obj.get("usage")
            if u:
                usage = u
    print(f"chunks 数: {chunks}")
    print("last chunk 是否含 usage:", json.dumps(last_chunk.get("usage") if last_chunk else None, ensure_ascii=False))
    print("整条流最终 usage:", json.dumps(usage, ensure_ascii=False))


def probe_stream_with_options(key: str):
    print("\n=== 流式 stream=true + stream_options.include_usage=true ===")
    p = {"model": MODEL, "messages": [
        {"role": "system", "content": "简短回答。"},
        {"role": "user", "content": "什么是波函数坍缩？"},
    ], "temperature": 0.4, "max_tokens": 50, "stream": True,
         "stream_options": {"include_usage": True}}
    usage = None
    chunks = 0
    last_chunk = None
    with urllib.request.urlopen(req(p, key), timeout=60) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                obj = json.loads(d)
            except json.JSONDecodeError:
                continue
            chunks += 1
            last_chunk = obj
            u = obj.get("usage")
            if u:
                usage = u
    print(f"chunks 数: {chunks}")
    print("last chunk 是否含 usage:", json.dumps(last_chunk.get("usage") if last_chunk else None, ensure_ascii=False))
    use_final = None
    if last_chunk and last_chunk.get("choices"):
        use_final = last_chunk["choices"][0].get("finish_reason")
    print("last finish_reason:", use_final)
    print("整条流最终 usage:", json.dumps(usage, ensure_ascii=False))


if __name__ == "__main__":
    key = load_key()
    try:
        probe_non_stream(key)
    except Exception as e:  # noqa: BLE001
        print("非流式失败:", e)
    try:
        probe_stream_basic(key)
    except Exception as e:  # noqa: BLE001
        print("流式(无options)失败:", e)
    try:
        probe_stream_with_options(key)
    except Exception as e:  # noqa: BLE001
        print("流式(带options)失败:", e)
