#!/usr/bin/env python3
"""问底 WenDi · 后端（薄代理 + 限流）

- 静态：本目录 web/（index.html）
- POST /api/explain   mode=explain | followup | chat
- GET  /api/health

密钥：环境变量 WENDI_API_KEY（生产，由 systemd EnvironmentFile 注入）。
     缺失时回退到 ~/.secrets 候选键（仅供本地开发）。
隐私：不记录对话内容；只记录请求计数与错误。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from prompts import chat_messages, explain_messages, followup_messages  # noqa: E402

PORT = int(os.environ.get("WENDI_PORT", "8899"))
MODEL = os.environ.get("WENDI_MODEL", "deepseek-chat")
BASE_URL = os.environ.get("WENDI_BASE_URL", "https://api.deepseek.com")
TIMEOUT = float(os.environ.get("WENDI_TIMEOUT", "90"))
WEB_DIR = HERE / "web"

# —— 限流配置（防白嫖/防失控） ——
LIMIT_PER_MIN = int(os.environ.get("WENDI_LIMIT_PER_MIN", "8"))        # 每 IP 每分钟
LIMIT_PER_DAY = int(os.environ.get("WENDI_LIMIT_PER_DAY", "80"))       # 每 IP 每天
LIMIT_GLOBAL_DAY = int(os.environ.get("WENDI_LIMIT_GLOBAL_DAY", "4000"))  # 全局每天
MAX_BODY = 80 * 1024  # 单请求体上限

_hits: dict[str, dict] = defaultdict(lambda: {"minute": [], "day": 0, "daykey": ""})
_global = {"day": 0, "daykey": ""}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def check_limits(ip: str) -> tuple[bool, str]:
    now = time.time()
    today = _today()
    h = _hits[ip]
    if h["daykey"] != today:
        h["daykey"] = today
        h["day"] = 0
        h["minute"] = []
    h["minute"] = [t for t in h["minute"] if now - t < 60]

    if _global["daykey"] != today:
        _global["daykey"] = today
        _global["day"] = 0

    if len(h["minute"]) >= LIMIT_PER_MIN:
        return False, "请求太快了，歇口气再试。"
    if h["day"] >= LIMIT_PER_DAY:
        return False, f"今天的用量用完了（每台设备每天 {LIMIT_PER_DAY} 次）。问底还在早期，以此保护服务。"
    if _global["day"] >= LIMIT_GLOBAL_DAY:
        return False, "今天服务总量已达上限，明天再来。"

    h["minute"].append(now)
    h["day"] += 1
    _global["day"] += 1
    return True, ""


# —— 密钥 ——
_KEY_CACHE: str | None = None


def load_key() -> str:
    global _KEY_CACHE
    if _KEY_CACHE:
        return _KEY_CACHE
    env = os.environ.get("WENDI_API_KEY")
    if env:
        _KEY_CACHE = env
        return env
    # 本地开发回退：~/.secrets 候选
    secrets = Path.home() / ".secrets"
    candidates = ["deepseek_api_key", "deepseek_Prime-Agent", "deepseek_codex_api_key",
                  "deepseek_n8n_api_key", "deepseek_nvim_api_key"]
    found: dict[str, str] = {}
    if secrets.exists():
        for line in secrets.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                found[k.strip()] = v.strip().strip('"').strip("'")
    for name in candidates:
        if found.get(name):
            _KEY_CACHE = found[name]
            return _KEY_CACHE
    raise RuntimeError("无可用 API key（请设 WENDI_API_KEY）")


def call_llm(messages: list[dict]) -> str:
    payload = {"model": MODEL, "messages": messages, "temperature": 0.4,
               "max_tokens": 800, "stream": False}
    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {load_key()}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("服务密钥失效，请联系维护者") from e
        raise RuntimeError(f"LLM HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("LLM 响应异常") from e


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/health":
            self._json(200, {"ok": True, "model": MODEL, "used_today": _global["day"]})
            return
        super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/explain":
            self._json(404, {"error": "not found"})
            return
        ip = (self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For")
              or self.client_address[0] or "").split(",")[0].strip()
        ok, msg = check_limits(ip or "unknown")
        if not ok:
            self._json(429, {"error": msg})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY:
                self._json(400, {"error": "请求体过大"})
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": f"bad request: {e}"})
            return
        try:
            mode = body.get("mode", "explain")
            if mode == "explain":
                msgs = explain_messages(body.get("term", ""), body.get("context", ""),
                                        body.get("style", "standard"))
            elif mode == "followup":
                msgs = followup_messages(body.get("term", ""), body.get("previous", ""),
                                         body.get("question", ""))
            elif mode == "chat":
                msgs = chat_messages(body.get("messages", []))
            else:
                self._json(400, {"error": f"unknown mode: {mode}"})
                return
            self._json(200, {"text": call_llm(msgs)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"问底后端: http://127.0.0.1:{PORT} (model={MODEL})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
