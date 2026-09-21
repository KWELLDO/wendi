#!/usr/bin/env python3
"""问底 WenDi · 后端

功能：LLM 代理（划词解释 / 追问 / 对话）+ 账户 + 配额 + 兑换码 + 云同步。
依赖：Python 3 标准库（无第三方包）。

环境变量：
  WENDI_API_KEY          LLM 密钥（必需；本地开发可省，回退 ~/.secrets）
  WENDI_PORT             监听端口（默认 8899，只绑 127.0.0.1）
  WENDI_MODEL           模型名（默认 deepseek-chat）
  WENDI_BASE_URL        OpenAI 兼容端点（默认 https://api.deepseek.com）
  WENDI_DB              SQLite 路径（默认 ~/.wendi/wendi.db）
  WENDI_QUOTA_ANON/FREE/PRO  配额（默认 20/40/300 次每天）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import db  # noqa: E402
from auth import hash_password, new_token, verify_password  # noqa: E402
from prompts import chat_messages, explain_messages, followup_messages  # noqa: E402

PORT = int(os.environ.get("WENDI_PORT", "8899"))
MODEL = os.environ.get("WENDI_MODEL", "deepseek-chat")
BASE_URL = os.environ.get("WENDI_BASE_URL", "https://api.deepseek.com")
TIMEOUT = float(os.environ.get("WENDI_TIMEOUT", "90"))
WEB_DIR = HERE / "web"

# —— 限流 ——
LIMIT_ANON_PER_MIN = int(os.environ.get("WENDI_ANON_PER_MIN", "6"))
LIMIT_GLOBAL_DAY = int(os.environ.get("WENDI_LIMIT_GLOBAL_DAY", "4000"))
MAX_BODY = 64 * 1024
MAX_SYNC_BYTES = 1024 * 1024  # 云同步单用户上限 1MB

_anon: dict[str, dict] = defaultdict(lambda: {"minute": [], "day": 0, "daykey": ""})
_global = {"day": 0, "daykey": ""}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def check_anon(ip: str) -> tuple[bool, str]:
    """匿名配额（按 IP，内存）。返回 (ok, message)。"""
    now = time.time()
    today = _today()
    h = _anon[ip]
    if h["daykey"] != today:
        h["daykey"] = today
        h["day"] = 0
        h["minute"] = []
    h["minute"] = [t for t in h["minute"] if now - t < 60]
    if len(h["minute"]) >= LIMIT_ANON_PER_MIN:
        return False, "请求太快了，歇口气再试。"
    if h["day"] >= db.QUOTA_ANON:
        return False, f"未登录的每日体验额度用完了（{db.QUOTA_ANON} 次）。注册一个账号可以继续。"
    if not check_global():
        return False, "今天服务总量已达上限，明天再来。"
    h["minute"].append(now)
    h["day"] += 1
    return True, ""


def check_global() -> bool:
    today = _today()
    if _global["daykey"] != today:
        _global["daykey"] = today
        _global["day"] = 0
    if _global["day"] >= LIMIT_GLOBAL_DAY:
        return False
    _global["day"] += 1
    return True


# —— 密钥（支持多候选：401 自动换下一个，成功者缓存） ——
_ACTIVE_KEY: tuple[str, str] | None = None


class AuthError(RuntimeError):
    """401/403：该 key 不可用，应换下一个候选。"""


def _existing_keys() -> list[tuple[str, str]]:
    env = os.environ.get("WENDI_API_KEY")
    if env:
        return [("env:WENDI_API_KEY", env)]
    candidates = ["deepseek_api_key", "deepseek_Prime-Agent", "deepseek_codex_api_key",
                  "deepseek_n8n_api_key", "deepseek_nvim_api_key"]
    found: dict[str, str] = {}
    secrets_file = Path.home() / ".secrets"
    if secrets_file.exists():
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                found[k.strip()] = v.strip().strip('"').strip("'")
    out = [(n, found[n]) for n in candidates if found.get(n)]
    if not out:
        raise RuntimeError("无可用 API key（请设 WENDI_API_KEY）")
    return out


def _request_once(key: str, messages: list[dict]) -> str:
    payload = {"model": MODEL, "messages": messages, "temperature": 0.4,
               "max_tokens": 800, "stream": False}
    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"key 不可用 (HTTP {e.code})") from e
        raise RuntimeError(f"LLM HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("LLM 响应异常") from e


def call_llm(messages: list[dict]) -> str:
    global _ACTIVE_KEY
    keys = _existing_keys()
    if _ACTIVE_KEY:
        keys = [_ACTIVE_KEY] + [k for k in keys if k[0] != _ACTIVE_KEY[0]]
    errors: list[str] = []
    for name, key in keys:
        try:
            text = _request_once(key, messages)
            if not _ACTIVE_KEY or _ACTIVE_KEY[0] != name:
                print(f"[key] 使用: {name}")
                _ACTIVE_KEY = (name, key)
            return text
        except AuthError as e:
            errors.append(f"{name}: {e}")
            continue
    raise RuntimeError("所有候选 key 均不可用 → " + "; ".join(errors))


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- 工具 ----------
    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}

    def _ip(self) -> str:
        ip = (self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For")
              or self.client_address[0] or "unknown")
        return ip.split(",")[0].strip()

    def _user(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        return db.get_session_user(token)

    def _quota_payload(self, user) -> dict:
        if user:
            st = db.quota_state(user)
            return {"plan": db.effective_plan(user), "used": st["used"], "limit": st["limit"]}
        h = _anon[self._ip()]
        used = h["day"] if h["daykey"] == _today() else 0
        return {"plan": "anon", "used": used, "limit": db.QUOTA_ANON}

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/health":
                self._json(200, {"ok": True, "model": MODEL, "used_today": _global["day"]})
                return
            if path == "/api/me":
                user = self._user()
                if not user:
                    self._json(401, {"error": "未登录"})
                    return
                self._json(200, {"email": user["email"], "created_at": user["created_at"],
                                 "plan_until": user["plan_until"],
                                 "quota": self._quota_payload(user)})
                return
            if path == "/api/sync":
                user = self._user()
                if not user:
                    self._json(401, {"error": "未登录"})
                    return
                data = db.sync_get(user["id"])
                self._json(200, data if data else {"data": None, "updated_at": None})
                return
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})
            return
        super().do_GET()

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": f"bad request: {e}"})
            return
        try:
            if path == "/api/auth/register":
                self._register(body)
            elif path == "/api/auth/login":
                self._login(body)
            elif path == "/api/auth/logout":
                self._logout()
            elif path == "/api/redeem":
                self._redeem(body)
            elif path == "/api/sync":
                self._sync_put(body)
            elif path == "/api/explain":
                self._explain(body)
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    # ---------- 账户 ----------
    def _register(self, body: dict):
        email = str(body.get("email", "")).strip()
        pw = str(body.get("password", ""))
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "邮箱格式不正确"})
            return
        if len(pw) < 6:
            self._json(400, {"error": "密码至少 6 位"})
            return
        salt, h = hash_password(pw)
        user = db.create_user(email, salt, h)
        if not user:
            self._json(409, {"error": "这个邮箱已经注册过了"})
            return
        token = new_token()
        db.create_session(token, user["id"])
        row = db.get_user(user["id"])
        self._json(200, {"token": token, "email": user["email"],
                         "plan": db.effective_plan(row), "plan_until": None,
                         "quota": db.quota_state(row)})

    def _login(self, body: dict):
        email = str(body.get("email", "")).strip()
        pw = str(body.get("password", ""))
        row = db.get_user_by_email(email)
        if not row or not verify_password(pw, row["pw_salt"], row["pw_hash"]):
            self._json(401, {"error": "邮箱或密码不正确"})
            return
        token = new_token()
        db.create_session(token, row["id"])
        self._json(200, {"token": token, "email": row["email"],
                         "plan": db.effective_plan(row), "plan_until": row["plan_until"],
                         "quota": db.quota_state(row)})

    def _logout(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            db.delete_session(auth[7:].strip())
        self._json(200, {"ok": True})

    def _redeem(self, body: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "请先登录再兑换"})
            return
        ok, msg, until = db.redeem(str(body.get("code", "")), user["id"])
        if not ok:
            self._json(400, {"error": msg})
            return
        row = db.get_user(user["id"])
        self._json(200, {"ok": True, "message": msg, "plan": "pro", "plan_until": until,
                         "quota": db.quota_state(row)})

    # ---------- 云同步 ----------
    def _sync_put(self, body: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        data = body.get("data")
        if not isinstance(data, str):
            self._json(400, {"error": "data 必须是字符串"})
            return
        if len(data.encode("utf-8")) > MAX_SYNC_BYTES:
            self._json(413, {"error": "数据超过 1MB 上限"})
            return
        ts = db.sync_put(user["id"], data)
        self._json(200, {"ok": True, "updated_at": ts})

    # ---------- 核心：解释 ----------
    def _explain(self, body: dict):
        user = self._user()
        # 配额
        if user:
            limit = db.quota_limit(user)
            ok, used, limit = db.consume_quota(user["id"], limit)
            if not ok:
                self._json(429, {"error": f"今天的额度用完了（{limit} 次/天）。升级 Pro 可以获得更多额度。",
                                 "quota": {"plan": db.effective_plan(user), "used": used, "limit": limit}})
                return
        else:
            ok, msg = check_anon(self._ip())
            if not ok:
                self._json(429, {"error": msg})
                return

        mode = body.get("mode", "explain")
        try:
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
            text = call_llm(msgs)
        except Exception as e:  # noqa: BLE001
            if user:
                db.refund_quota(user["id"])
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"text": text, "quota": self._quota_payload(db.get_user(user["id"]) if user else None)})


def main():
    db.init()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"问底后端: http://127.0.0.1:{PORT} (model={MODEL}, db={db.DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
