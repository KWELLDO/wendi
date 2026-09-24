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
import logging
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
from kernel import Kernel  # noqa: E402
from prompts import chat_messages, explain_messages, followup_messages  # noqa: E402

PORT = int(os.environ.get("WENDI_PORT", "8899"))
MODEL = os.environ.get("WENDI_MODEL", "deepseek-flash")
BASE_URL = os.environ.get("WENDI_BASE_URL", "https://api.deepseek.com")
TIMEOUT = float(os.environ.get("WENDI_TIMEOUT", "90"))
WEB_DIR = HERE / "web"

# —— 输出长度（主对话更容易被截断，给得宽些） ——
MAX_TOKENS = int(os.environ.get("WENDI_MAX_TOKENS", "800"))
MAX_TOKENS_CHAT = int(os.environ.get("WENDI_MAX_TOKENS_CHAT", "2000"))

# —— 限流 ——
LIMIT_ANON_PER_MIN = int(os.environ.get("WENDI_ANON_PER_MIN", "6"))
LIMIT_GLOBAL_DAY = int(os.environ.get("WENDI_LIMIT_GLOBAL_DAY", "4000"))
MAX_BODY = 64 * 1024
MAX_SYNC_BYTES = 1024 * 1024  # 云同步单用户上限 1MB
ADMIN_TOKEN = os.environ.get("WENDI_ADMIN_TOKEN", "")  # 空则 meter 端点拒绝一切请求

# —— 内核（kernel.py：事件 + 服务 + 作用域；graph/notes/share 模块的挂载点） ——
kernel = Kernel()

_anon: dict[str, dict] = defaultdict(lambda: {"minute": [], "day": 0, "daykey": ""})
_global = {"day": 0, "daykey": ""}
_cost_global = {"day": 0, "daykey": "", "cost_cents": 0, "alerted": False, "hard_alerted": False}
_degrade_last: dict[str, float] = {}  # 降级档：同一用户上次放行时刻（内存态，与 _anon 同债）
log = logging.getLogger("wendi.meter")


def _current_utc() -> time.struct_time:
    return time.gmtime()


def _is_peak_utc(now: time.struct_time | None = None) -> bool:
    """峰时判定（UTC，不依赖本地时区）。峰时 = 周一至五 01:00–04:00、06:00–10:00。

    已知简化：不判定中国法定节假日（节假日全天应算谷价），会导致节假日按峰价计、成本略高估。
    """
    g = now or _current_utc()
    if g.tm_wday >= 5:  # 周六/周日
        return False
    hh, mm = g.tm_hour, g.tm_min
    t = hh * 60 + mm
    return (60 <= t < 240) or (360 <= t < 600)


def _usage_split(usage: dict) -> tuple[int, int, int]:
    """从 usage 拆出 (hit, miss, completion)。缺省全 0（拿不到不报错）。"""
    try:
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    except (TypeError, ValueError):
        hit = miss = 0
    if not hit and not miss:
        try:
            miss = int(usage.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            miss = 0
    try:
        comp = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        comp = 0
    return hit, miss, comp


def _cost_cents(hit_tokens: int, miss_tokens: int, completion_tokens: int) -> int:
    """按缓存命中/未命中 + 峰谷 折算成本（人民币分）。单价来自 meter_config（可热改）。"""
    peak = _is_peak_utc()
    suffix = "peak" if peak else "offpeak"
    in_hit = float(db.meter_get(f"flash_in_hit_{suffix}") or 0.003)
    in_miss = float(db.meter_get(f"flash_in_miss_{suffix}") or 0.15)
    out = float(db.meter_get(f"flash_out_{suffix}") or 0.60)
    usd_cny = float(db.meter_get("usd_to_cny") or 7.2)
    usd = hit_tokens * in_hit + miss_tokens * in_miss + completion_tokens * out
    return round(usd / 1_000_000 * usd_cny * 100)


def _record_usage(user, mode: str, style, stream: bool, usage: dict, ok: bool,
                  depth: int = -1, turns: int = -1) -> None:
    """落库一次计量 + 成本闸推进。任何内部异常都不向上抛。"""
    try:
        hit, miss, comp = _usage_split(usage)
        cost = _cost_cents(hit, miss, comp) if ok else 0
        plan = db.effective_plan(user) if user else "anon"
        uid = user["id"] if user else None
        db.meter_record(uid, plan, mode, style, stream, hit, miss, comp, cost,
                        ok, depth, turns)
        _tick_cost_gate(cost)
    except Exception:  # noqa: BLE001
        log.exception("计量落库失败（已忽略，不影响主干）")


def _cost_caps() -> tuple[int, int]:
    """(cap, hard)：cap 超了当日全员降级，hard(=2×cap) 超了硬停。均自 meter_config 热读。"""
    cap = int(float(db.meter_get("cost_cents_cap") or 3000))
    hard = int(float(db.meter_get("cost_cents_hard") or cap * 2))
    return cap, hard


def _cost_roll_today() -> None:
    today = _today()
    if _cost_global["daykey"] != today:
        _cost_global.update(daykey=today, cost_cents=0, alerted=False, hard_alerted=False)


def _cost_state() -> str:
    """normal | degraded(超cap，全员降级) | hard(超2×cap，硬停)。请求入口每必调一次。"""
    _cost_roll_today()
    cap, hard = _cost_caps()
    if _cost_global["cost_cents"] > hard:
        return "hard"
    if _cost_global["cost_cents"] > cap:
        return "degraded"
    return "normal"


def _tick_cost_gate(add_cents: int) -> None:
    """累计全局日成本；越 cap/hard 各发一次事件。状态推进本身不阻断，闸门在 _explain 入口。"""
    _cost_roll_today()
    _cost_global["cost_cents"] += add_cents
    cap, hard = _cost_caps()
    cost = _cost_global["cost_cents"]
    if cost > cap and not _cost_global["alerted"]:
        _cost_global["alerted"] = True
        log.error("每日成本越界：%d 分 > %d 分（当日全员转入降级档）", cost, cap)
        kernel.emit("meter.cost_over", day=_today(), cost_cents=cost, cap=cap)
    if cost > hard and not _cost_global["hard_alerted"]:
        _cost_global["hard_alerted"] = True
        log.error("每日成本硬停：%d 分 > %d 分（当日停止服务，明日恢复）", cost, hard)
        kernel.emit("meter.cost_hard", day=_today(), cost_cents=cost, hard=hard)


def _degrade_ok(key: str) -> bool:
    """降级档限速：同一 key 间隔 ≥ degrade_min_interval 秒才放行（默认 15）。"""
    iv = float(db.meter_get("degrade_min_interval") or 15)
    now = time.time()
    if now - _degrade_last.get(key, 0.0) < iv:
        return False
    _degrade_last[key] = now
    return True


def _degrade_max_tokens(mode: str) -> int:
    """降级档输出上限：chat 1000 / 其他 400（meter_config 热改）。"""
    if mode == "chat":
        return int(float(db.meter_get("degrade_max_tokens_chat") or 1000))
    return int(float(db.meter_get("degrade_max_tokens") or 400))


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def check_anon(ip: str) -> tuple[str, str]:
    """匿名配额（按 IP，内存）。返回 (status, message)：
    ok=正常放行 · degraded=超体验额度，降级续命 · blocked=限流/全局日限，429。"""
    now = time.time()
    today = _today()
    h = _anon[ip]
    if h["daykey"] != today:
        h["daykey"] = today
        h["day"] = 0
        h["minute"] = []
    h["minute"] = [t for t in h["minute"] if now - t < 60]
    if len(h["minute"]) >= LIMIT_ANON_PER_MIN:
        return "blocked", "请求太快了，歇口气再试。"
    if not check_global():
        return "blocked", "今天服务总量已达上限，明天再来。"
    h["minute"].append(now)
    h["day"] += 1
    if h["day"] > db.QUOTA_ANON:
        return "degraded", ""
    return "ok", ""


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
    """返回 (名称, 密钥) 候选列表，首个可用者优先。

    来源优先级：
      1. WENDI_API_KEY 环境变量（单个）；
      2. WENDI_KEYS 环境变量（逗号分隔的多个 key，用于轮换）；
      3. ~/.secrets 里名为 deepseek_api_key 的一行（可选便利，非必需）。
    """
    env = os.environ.get("WENDI_API_KEY")
    if env:
        return [("env:WENDI_API_KEY", env)]
    multi = os.environ.get("WENDI_KEYS")
    if multi:
        keys = [k.strip() for k in multi.split(",") if k.strip()]
        if keys:
            return [(f"env:WENDI_KEYS[{i}]", k) for i, k in enumerate(keys)]
    secrets_file = Path.home() / ".secrets"
    if secrets_file.exists():
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("deepseek_api_key="):
                value = line.partition("=")[2].strip().strip('"').strip("'")
                if value:
                    return [("~/.secrets:deepseek_api_key", value)]
    raise RuntimeError("无可用 API key（请设 WENDI_API_KEY 或 WENDI_KEYS）")


def _llm_request(key: str, messages: list[dict], max_tokens: int, stream: bool) -> urllib.request.Request:
    payload = {"model": MODEL, "messages": messages, "temperature": 0.4,
               "max_tokens": max_tokens, "stream": stream}
    return urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")


def _request_once(key: str, messages: list[dict], max_tokens: int) -> tuple[str, dict]:
    """非流式调用。返回 (text, usage)。usage 缺省全 0（拿不到不报错）。

    DeepSeek 偶发返回空 body（ELB 负载下），做有限重试。
    """
    last_err: Exception | None = None
    for _ in range(3):
        req = _llm_request(key, messages, max_tokens, stream=False)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"key 不可用 (HTTP {e.code})") from e
            raise RuntimeError(f"LLM HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM 网络错误: {e.reason}") from e
        if not raw:
            last_err = RuntimeError("LLM 空响应（重试中）")
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            last_err = e
            continue
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError("LLM 响应异常") from e
        return text, data.get("usage", {}) or {}
    raise RuntimeError(f"LLM 连续空响应/坏 JSON: {last_err}")


def call_llm(messages: list[dict], max_tokens: int = MAX_TOKENS) -> tuple[str, dict]:
    global _ACTIVE_KEY
    keys = _existing_keys()
    if _ACTIVE_KEY:
        keys = [_ACTIVE_KEY] + [k for k in keys if k[0] != _ACTIVE_KEY[0]]
    errors: list[str] = []
    for name, key in keys:
        try:
            text, usage = _request_once(key, messages, max_tokens)
            if not _ACTIVE_KEY or _ACTIVE_KEY[0] != name:
                print(f"[key] 使用: {name}")
                _ACTIVE_KEY = (name, key)
            return text, usage
        except AuthError as e:
            errors.append(f"{name}: {e}")
            continue
    raise RuntimeError("所有候选 key 均不可用 → " + "; ".join(errors))


# —— 流式（SSE） ——
def _open_stream(key: str, messages: list[dict], max_tokens: int):
    """建立上游流式连接。401/403 抛 AuthError（此时还没给客户端发任何字节，可整体退款）。"""
    req = _llm_request(key, messages, max_tokens, stream=True)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"key 不可用 (HTTP {e.code})") from e
        raise RuntimeError(f"LLM HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e


def call_llm_stream(messages: list[dict], max_tokens: int):
    """同 call_llm 的多 key 轮换，但返回已建立的上游响应（迭代其行即得到 SSE 流）。

    返回 resp（一个可迭代的流对象）。usage 由调用方迭代 sse_deltas 时捕获。
    """
    global _ACTIVE_KEY
    keys = _existing_keys()
    if _ACTIVE_KEY:
        keys = [_ACTIVE_KEY] + [k for k in keys if k[0] != _ACTIVE_KEY[0]]
    errors: list[str] = []
    for name, key in keys:
        try:
            resp = _open_stream(key, messages, max_tokens)
            if not _ACTIVE_KEY or _ACTIVE_KEY[0] != name:
                print(f"[key] 使用: {name}")
                _ACTIVE_KEY = (name, key)
            return resp
        except AuthError as e:
            errors.append(f"{name}: {e}")
            continue
    raise RuntimeError("所有候选 key 均不可用 → " + "; ".join(errors))


def sse_deltas(resp):
    """解析上游 OpenAI 兼容 SSE，产出文本增量；结束时 yield 一个 `{"usage": {...}}`。

    DeepSeek 流式默认在 `[DONE]` 前最后一个 chunk 带 `usage`（无需 stream_options）。
    拿不到 usage 时 yield `{"usage": {}}`（调用方据此记 0 token，不报错）。
    """
    usage: dict = {}
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        try:
            delta = obj["choices"][0].get("delta", {}).get("content")
        except (KeyError, IndexError, AttributeError):
            continue
        if delta:
            yield delta
    yield {"usage": usage}


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
        from urllib.parse import urlparse, parse_qs
        path = self.path.split("?")[0]
        qs = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api/health":
                self._json(200, {"ok": True, "model": MODEL, "used_today": _global["day"]})
                return
            if path == "/api/meter":
                if not self._admin_ok(qs):
                    return
                day = qs.get("day", [_today()])[0]
                self._json(200, db.meter_totals(day))
                return
            if path == "/api/meter/user":
                if not self._admin_ok(qs):
                    return
                uid = int(qs.get("user_id", ["0"])[0])
                days = int(qs.get("days", ["7"])[0])
                self._json(200, {"user_id": uid, "days": db.meter_user_ndays(uid, days)})
                return
            if path == "/api/admin/codes":
                if not self._admin_ok(qs):
                    return
                batch = qs.get("batch", [None])[0] or None
                self._json(200, {"codes": db.list_codes(batch), "summary": db.code_stats(batch)})
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

    def _qs(self) -> dict:
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _admin_ok(self, qs: dict | None = None) -> bool:
        """校验 ?token= 或 Authorization: Bearer 是否匹配 WENDI_ADMIN_TOKEN。不匹配 → 401 并返回 False。"""
        if qs is None:
            qs = self._qs()
        token = qs.get("token", [""])[0]
        if not token:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
        if not ADMIN_TOKEN or token != ADMIN_TOKEN:
            self._json(401, {"error": "unauthorized"})
            return False
        return True

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
            elif path == "/api/admin/codes":
                self._admin_gen_codes(body)
            elif path == "/api/admin/codes/void":
                self._admin_void_codes(body)
            elif path == "/api/admin/users":
                self._admin_create_user(body)
            elif path == "/api/admin/users/password":
                self._admin_reset_password(body)
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

    # ---------- 管理端（WENDI_ADMIN_TOKEN；空则一律拒绝） ----------
    def _admin_gen_codes(self, body: dict):
        if not self._admin_ok():
            return
        try:
            n, days = int(body.get("n", 10)), int(body.get("days", 30))
        except (TypeError, ValueError):
            self._json(400, {"error": "n/days 必须是整数"})
            return
        if not 1 <= n <= 500 or not 1 <= days <= 3650:
            self._json(400, {"error": "n 范围 1-500，days 范围 1-3650"})
            return
        note = str(body.get("note", ""))[:200]
        batch = str(body.get("batch", "")).strip() or None
        batch, codes = db.gen_codes(n, days, note, batch)
        kernel.emit("admin.codes_generated", batch=batch, n=len(codes), days=days, note=note)
        self._json(200, {"batch": batch, "codes": codes})

    def _admin_void_codes(self, body: dict):
        if not self._admin_ok():
            return
        codes = [str(c) for c in body.get("codes", []) if str(c).strip()] or None
        batch = str(body.get("batch", "")).strip() or None
        if not codes and not batch:
            self._json(400, {"error": "需要 codes 列表或 batch"})
            return
        voided = db.void_codes(codes, batch)
        self._json(200, {"voided": voided})

    def _admin_create_user(self, body: dict):
        """受控建号：绕过公开注册的 EMAIL_RE，任意非空标识即可；可直接授予 pro。"""
        if not self._admin_ok():
            return
        email = str(body.get("email", "")).strip()
        pw = str(body.get("password", ""))
        if not email or len(email) > 254:
            self._json(400, {"error": "email 必填且不超过 254 字符"})
            return
        if len(pw) < 6:
            self._json(400, {"error": "密码至少 6 位"})
            return
        salt, h = hash_password(pw)
        user = db.create_user(email, salt, h)
        if not user:
            self._json(409, {"error": "这个邮箱已经注册过了"})
            return
        plan = str(body.get("plan", "free"))
        until = None
        if plan == "pro":
            try:
                days = int(body.get("days", 30))
            except (TypeError, ValueError):
                self._json(400, {"error": "days 必须是整数"})
                return
            if not 1 <= days <= 3650:
                self._json(400, {"error": "days 范围 1-3650"})
                return
            t = time.mktime(time.strptime(db.today(), "%Y-%m-%d")) + days * 86400
            until = time.strftime("%Y-%m-%d", time.localtime(t))
            db.set_plan(user["id"], "pro", until)
        kernel.emit("admin.user_created", user_id=user["id"], email=email, plan=plan)
        row = db.get_user(user["id"])
        self._json(200, {"id": row["id"], "email": row["email"],
                         "plan": db.effective_plan(row), "plan_until": row["plan_until"]})

    def _admin_reset_password(self, body: dict):
        """重置密码 + 踢掉该用户全部会话。忘记密码不再等于账号作废。"""
        if not self._admin_ok():
            return
        email = str(body.get("email", "")).strip()
        pw = str(body.get("password", ""))
        row = db.get_user_by_email(email)
        if not row:
            self._json(404, {"error": "用户不存在"})
            return
        if len(pw) < 6:
            self._json(400, {"error": "密码至少 6 位"})
            return
        salt, h = hash_password(pw)
        db.reset_password(row["id"], salt, h)
        kernel.emit("admin.password_reset", user_id=row["id"], email=email)
        self._json(200, {"ok": True, "email": row["email"], "sessions_revoked": True})

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
        # —— 成本闸（硬底线）：hard 硬停；degraded 当日全员降级 ——
        cost_state = _cost_state()
        if cost_state == "hard":
            self._json(429, {"error": "今日服务已达成本上限，明天恢复。", "cost": "hard"})
            return
        degraded = cost_state == "degraded"
        degrade_reason = "cost_cap" if degraded else None

        # —— 配额（P0：超限不断头，降级续命） ——
        consumed = False
        if user:
            limit = db.quota_limit(user)
            ok, used, limit = db.consume_quota(user["id"], limit)
            consumed = ok  # 未真正扣次的失败请求不退款，否则降级用户失败一次反赚一个槽位
            if not ok and not degraded:
                degraded, degrade_reason = True, "quota"
        else:
            st, msg = check_anon(self._ip())
            if st == "blocked":
                self._json(429, {"error": msg})
                return
            if st == "degraded" and not degraded:
                degraded, degrade_reason = True, "quota"

        mode = body.get("mode", "explain")
        # —— 行为信号（P0 埋点）：树深 + 对话轮数 ——
        depth = body.get("depth")
        turns = body.get("turns")
        depth = depth if isinstance(depth, int) and depth >= 0 else -1
        turns = turns if isinstance(turns, int) and turns >= 0 else -1
        stream = bool(body.get("stream"))
        term = str(body.get("term", ""))
        max_tokens = MAX_TOKENS

        # —— 降级档限速：超限后同一用户 15s 一发 ——
        if degraded:
            key = f"u{user['id']}" if user else f"ip:{self._ip()}"
            if not _degrade_ok(key):
                iv = int(float(db.meter_get("degrade_min_interval") or 15))
                self._json(429, {"error": f"你今天问得很多，慢一点再继续（降级间隔 {iv} 秒）。升级 Pro 即可不限速。",
                                 "degraded": True, "reason": degrade_reason,
                                 "quota": self._quota_payload(db.get_user(user["id"]) if user else None)})
                return

        t0 = time.time()
        try:
            if mode == "explain":
                msgs = explain_messages(term, body.get("context", ""),
                                        body.get("style", "standard"))
            elif mode == "followup":
                msgs = followup_messages(term, body.get("previous", ""),
                                         body.get("question", ""))
            elif mode == "chat":
                msgs = chat_messages(body.get("messages", []))
                max_tokens = MAX_TOKENS_CHAT
            else:
                self._json(400, {"error": f"unknown mode: {mode}"})
                return
            if degraded:
                max_tokens = min(max_tokens, _degrade_max_tokens(mode))
            if stream:
                return self._explain_stream(user, msgs, max_tokens, mode=mode, term=term, t0=t0,
                                            depth=depth, turns=turns,
                                            degraded=degraded, degrade_reason=degrade_reason,
                                            consumed=consumed)
            text, usage = call_llm(msgs, max_tokens)
        except Exception as e:  # noqa: BLE001
            if user and consumed:
                db.refund_quota(user["id"])
            kernel.emit("explain.error", mode=mode, user_id=user["id"] if user else None,
                        term=term, error=str(e))
            _record_usage(user, mode, body.get("style", "standard"), False, {}, ok=False,
                          depth=depth, turns=turns)
            self._json(500, {"error": str(e)})
            return
        _record_usage(user, mode, body.get("style", "standard"), False, usage, ok=True,
                      depth=depth, turns=turns)
        kernel.emit("explain.done", mode=mode, user_id=user["id"] if user else None,
                    term=term, parent_term=None, style=body.get("style", "standard"),
                    ms=round((time.time() - t0) * 1000), stream=False, chunks=1,
                    depth=depth, turns=turns, degraded=degraded)
        self._json(200, {"text": text,
                         "quota": self._quota_payload(db.get_user(user["id"]) if user else None),
                         **({"degraded": True, "reason": degrade_reason} if degraded else {})})

    def _explain_stream(self, user, msgs: list[dict], max_tokens: int,
                        mode: str = "explain", term: str = "", t0: float = 0.0,
                        depth: int = -1, turns: int = -1,
                        degraded: bool = False, degrade_reason: str | None = None,
                        consumed: bool = True):
        """SSE 流式解释。事件：data: {"delta": "…"} … data: {"done": true, "quota": {…}} / {"error": "…"}"""
        try:
            resp = call_llm_stream(msgs, max_tokens)
        except Exception as e:  # noqa: BLE001
            if user and consumed:
                db.refund_quota(user["id"])
            self._json(500, {"error": str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # 让 nginx 不缓冲，逐块透传
        self.end_headers()

        def emit(obj: dict):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        sent = 0
        usage: dict = {}
        try:
            for delta in sse_deltas(resp):
                if isinstance(delta, dict) and "usage" in delta:
                    usage = delta["usage"]
                    continue
                sent += 1
                emit({"delta": delta})
            done = {"done": True,
                    "quota": self._quota_payload(db.get_user(user["id"]) if user else None)}
            if degraded:
                done["degraded"] = True
                done["reason"] = degrade_reason
            emit(done)
            _record_usage(user, mode, "standard", True, usage, ok=True, depth=depth, turns=turns)
            kernel.emit("explain.done", mode=mode, user_id=user["id"] if user else None,
                        term=term, parent_term=None, style="standard",
                        ms=round((time.time() - t0) * 1000), stream=True, chunks=sent,
                        depth=depth, turns=turns, degraded=degraded)
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端中途离开
        except Exception as e:  # noqa: BLE001
            _record_usage(user, mode, "standard", True, usage, ok=False, depth=depth, turns=turns)
            kernel.emit("explain.error", mode=mode, user_id=user["id"] if user else None,
                        term=term, error=str(e))
            try:
                emit({"error": str(e)})
                if not sent and user and consumed:  # 一个字都没出且真扣了次 → 退款
                    db.refund_quota(user["id"])
            except OSError:
                pass
        finally:
            resp.close()


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
