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

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import db  # noqa: E402
import entitlements  # noqa: E402
import materials  # noqa: E402
import messages  # noqa: E402
from auth import hash_password, new_token, verify_password  # noqa: E402
from kernel import Kernel  # noqa: E402
from prompts import chat_messages, explain_messages, followup_messages  # noqa: E402
from explain_output import ExplainOutputParser, split_explain_output  # noqa: E402

PORT = int(os.environ.get("WENDI_PORT", "8899"))
MODEL = os.environ.get("WENDI_MODEL", "deepseek-flash")
BASE_URL = os.environ.get("WENDI_BASE_URL", "https://api.deepseek.com")
TIMEOUT = float(os.environ.get("WENDI_TIMEOUT", "90"))
WEB_DIR = HERE / "web"

# —— 输出长度（主对话更容易被截断，给得宽些） ——
MAX_TOKENS = int(os.environ.get("WENDI_MAX_TOKENS", "3000"))
MAX_TOKENS_CHAT = int(os.environ.get("WENDI_MAX_TOKENS_CHAT", "8000"))

# —— 限流 ——
LIMIT_ANON_PER_MIN = int(os.environ.get("WENDI_ANON_PER_MIN", "6"))
LIMIT_GLOBAL_DAY = int(os.environ.get("WENDI_LIMIT_GLOBAL_DAY", "4000"))
MAX_BODY = 64 * 1024
MAX_SYNC_BYTES = 1024 * 1024  # 云同步单用户上限 1MB

# —— 材料上传（单独走流式路由，不受 MAX_BODY 限制；上限默认 30MB） ——
MATERIALS_DIR = Path(os.environ.get(
    "WENDI_MATERIALS_DIR", str(Path.home() / ".wendi" / "materials")))
UPLOAD_MAX_MB = int(os.environ.get("WENDI_UPLOAD_MAX_MB", "30"))
UPLOAD_MAX_BYTES = UPLOAD_MAX_MB * 1024 * 1024
UPLOAD_CHUNK = 64 * 1024                 # 边读边写的块大小
UPLOAD_TIMEOUT = float(os.environ.get("WENDI_UPLOAD_TIMEOUT", "120"))  # 单次 socket 读超时
MATERIALS_MAX = int(os.environ.get("WENDI_MATERIALS_MAX", "200"))      # 单用户材料条数上限
MATERIALS_MAX_MB = int(os.environ.get("WENDI_MATERIALS_MAX_MB", "500"))  # 单用户原始文件总额
MATERIALS_MAX_BYTES = MATERIALS_MAX_MB * 1024 * 1024
MATERIAL_NAME_MAX = 200
ADMIN_TOKEN = os.environ.get("WENDI_ADMIN_TOKEN", "")  # 空则 meter 端点拒绝一切请求
EVENT_PER_MIN = int(os.environ.get("WENDI_EVENT_PER_MIN", "90"))   # 每 IP 每分钟事件上限（超出静默丢弃）
# 可选的管理端 IP 白名单（逗号分隔；空=不限制）。只认 X-Real-IP/X-Forwarded-For 的第一跳。
ADMIN_IPS = [ip.strip() for ip in os.environ.get("WENDI_ADMIN_IPS", "").split(",") if ip.strip()]
FEEDBACK_MAX = 2000                     # 单条反馈正文字数上限
FEEDBACK_PER_DAY = int(os.environ.get("WENDI_FEEDBACK_PER_DAY", "5"))   # 每 IP 每天
FEEDBACK_INTERVAL = int(os.environ.get("WENDI_FEEDBACK_INTERVAL", "30"))  # 同一 IP 最小间隔（秒）

# 允许站长后台在线热改的 meter_config 键（llm_* 密钥类不在此端点暴露，走「服务配置」面板）
METER_CONFIG_KEYS = (
    "cost_cents_cap", "cost_cents_hard", "degrade_min_interval",
    "degrade_max_tokens", "degrade_max_tokens_chat",
    "flash_in_hit_offpeak", "flash_in_hit_peak", "flash_in_miss_offpeak",
    "flash_in_miss_peak", "flash_out_offpeak", "flash_out_peak", "usd_to_cny",
)

# —— 内核（kernel.py：事件 + 服务 + 作用域；graph/notes/share 模块的挂载点） ——
kernel = Kernel()

_anon: dict[str, dict] = defaultdict(lambda: {"minute": [], "day": 0, "daykey": ""})
_global = {"day": 0, "daykey": ""}
_cost_global = {"day": 0, "daykey": "", "cost_cents": 0, "alerted": False, "hard_alerted": False}
_degrade_last: dict[str, float] = {}  # 降级档：同一用户上次放行时刻（内存态，与 _anon 同债）
_feedback_rate: dict[str, dict] = defaultdict(lambda: {"day": 0, "daykey": "", "last": 0.0})
_event_rate: dict[str, list] = defaultdict(list)   # ip -> [最近一分钟的事件时刻]
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
                  depth: int = -1, turns: int = -1,
                  session_id: str = "", room_id: str = "") -> int:
    """落库一次计量 + 成本闸推进，返回折算成本（分）。任何内部异常都不向上抛。"""
    try:
        hit, miss, comp = _usage_split(usage)
        cost = _cost_cents(hit, miss, comp) if ok else 0
        plan = db.effective_plan(user) if user else "anon"
        uid = user["id"] if user else None
        db.meter_record(uid, plan, mode, style, stream, hit, miss, comp, cost,
                        ok, depth, turns, session_id, room_id)
        _tick_cost_gate(cost)
        return cost
    except Exception:  # noqa: BLE001
        log.exception("计量落库失败（已忽略，不影响主干）")
        return 0


def _cost_caps() -> tuple[int, int]:
    """(cap, hard)：cap 超了当日全员降级，hard(=2×cap) 超了硬停。均自 meter_config 热读。"""
    cap = int(float(db.meter_get("cost_cents_cap") or 3000))
    hard = int(float(db.meter_get("cost_cents_hard") or cap * 2))
    return cap, hard


def _cost_roll_today() -> None:
    today = _today()
    if _cost_global["daykey"] != today:
        # 跨天/进程重启：内存计数不可信，从计量表恢复当日累计（堵住"重启绕过硬停"）。
        # 恢复值只反映已成功落库的调用，与内存累加的偏差 ≤ 最近一笔未落库请求，可忽略。
        try:
            restored = int(db.meter_totals(today)["cost_cents"])
        except Exception:  # noqa: BLE001
            restored = 0
        _cost_global.update(daykey=today, cost_cents=restored,
                            alerted=False, hard_alerted=False)


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


def _db_keys() -> list[tuple[str, str]]:
    """管理端「密钥」面板配置进库的密钥（meter_config.llm_api_keys，逗号/空白分隔）。"""
    raw = db.meter_get("llm_api_keys") or ""
    keys = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
    return [(f"db:llm_api_keys[{i}]", k) for i, k in enumerate(keys)]


def _llm_model() -> str:
    """模型名：面板配置优先，回落环境变量。"""
    return db.meter_get("llm_model") or MODEL


def _llm_base_url() -> str:
    """OpenAI 兼容端点：面板配置优先，回落环境变量。"""
    return (db.meter_get("llm_base_url") or BASE_URL).rstrip("/")


def _existing_keys() -> list[tuple[str, str]]:
    """返回 (名称, 密钥) 候选列表，首个可用者优先，401 自动轮换下一个。

    来源优先级（可共存，合并为候选列表）：
      1. 管理端「密钥」面板配置（meter_config.llm_api_keys）——UI 显式操作，最优先；
      2. WENDI_API_KEY 环境变量（单个）；
      3. WENDI_KEYS 环境变量（逗号分隔的多个 key，用于轮换）；
      4. ~/.secrets 里名为 deepseek_api_key 的一行（可选便利，非必需）。
    """
    candidates: list[tuple[str, str]] = []
    candidates += _db_keys()
    env = os.environ.get("WENDI_API_KEY")
    if env:
        candidates.append(("env:WENDI_API_KEY", env))
    multi = os.environ.get("WENDI_KEYS")
    if multi:
        candidates += [(f"env:WENDI_KEYS[{i}]", k) for i, k in
                       enumerate(k.strip() for k in multi.split(",") if k.strip())]
    secrets_file = Path.home() / ".secrets"
    if secrets_file.exists():
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("deepseek_api_key="):
                value = line.partition("=")[2].strip().strip('"').strip("'")
                if value:
                    candidates.append(("~/.secrets:deepseek_api_key", value))
    if not candidates:
        raise RuntimeError("无可用 API key：站长在 web 右上角「服务配置」填入密钥即可（热生效），或设 WENDI_API_KEY 环境变量")
    seen: set[str] = set()
    return [(name, k) for name, k in candidates if not (k in seen or seen.add(k))]


def _llm_request(key: str, messages: list[dict], max_tokens: int, stream: bool) -> urllib.request.Request:
    payload = {"model": _llm_model(), "messages": messages, "temperature": 0.4,
               "max_tokens": max_tokens, "stream": stream}
    return urllib.request.Request(
        f"{_llm_base_url()}/chat/completions",
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
        finish = data["choices"][0].get("finish_reason") or ""
        return text, data.get("usage", {}) or {}, finish
    raise RuntimeError(f"LLM 连续空响应/坏 JSON: {last_err}")


def call_llm(messages: list[dict], max_tokens: int = MAX_TOKENS) -> tuple[str, dict, bool]:
    global _ACTIVE_KEY
    keys = _existing_keys()
    if _ACTIVE_KEY:
        keys = [_ACTIVE_KEY] + [k for k in keys if k[0] != _ACTIVE_KEY[0]]
    errors: list[str] = []
    for name, key in keys:
        try:
            text, usage, finish = _request_once(key, messages, max_tokens)
            if not _ACTIVE_KEY or _ACTIVE_KEY[0] != name:
                print(f"[key] 使用: {name}")
                _ACTIVE_KEY = (name, key)
            return text, usage, finish == "length"
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
    finish = ""
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
            choice = obj["choices"][0]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta", {}).get("content")
        except (KeyError, IndexError, AttributeError):
            continue
        if delta:
            yield delta
    yield {"usage": usage, "finish": finish}


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # ---------- 日志与埋点（都不允许影响响应） ----------
    def _sid(self) -> str:
        """前端会话 id：优先请求里显式给的值（埋点从 body 来），退回 X-Wendi-Session 头。

        用于把请求日志、事件、成本串成一条线（同一次浏览 = 同一个 session_id）。
        """
        return (getattr(self, "_sid_hint", "") or self.headers.get("X-Wendi-Session") or "")[:64]

    def _track(self, event: str, code: str = "", meta: dict | None = None,
               user_id: int | None = None, depth: int = -1) -> None:
        """服务端自己记一条事件（注册/登录/反馈/系统错误）。异常吞掉。"""
        try:
            uid = user_id if user_id is not None else getattr(self, "_uid", None)
            db.event_add(event, session_id=self._sid(), user_id=uid, code=code,
                         depth=depth, meta=meta, ip=self._ip(),
                         ua=self.headers.get("User-Agent", ""))
        except Exception:  # noqa: BLE001
            log.exception("事件落库失败（已忽略）")

    def log_request(self, code="-", size="-"):
        """每请求一行 JSON（含延迟/用户/会话/成本），journal 里可被 agent 直接解析。"""
        self._http_log(code)

    def log_message(self, fmt, *args):
        """http.server 的其它日志（含 send_error）也走 JSON，附原文便于排障。"""
        self._http_log(getattr(self, "_status", None), extra=(fmt % args) if args else str(fmt))

    def _http_log(self, status=None, extra=""):
        try:
            line = {
                "ts": int(time.time() * 1000),
                "level": "error" if (self._error or (isinstance(status, int) and status >= 500)) else (
                    "warn" if (isinstance(status, int) and status >= 400) else "info"),
                "kind": "http", "method": getattr(self, "command", ""),
                "path": (self.path or "").split("?")[0],
                "status": status if status is not None else getattr(self, "_status", None),
                "latency_ms": int((time.time() - getattr(self, "_t0", time.time())) * 1000),
                "user_id": getattr(self, "_uid", None), "session_id": self._sid(),
                "cost_cents": getattr(self, "_cost_cents", 0),
            }
            if extra:
                line["msg"] = extra[:300]
            if self._error:
                line["error"] = self._error[:300]
            sys.stderr.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def do_GET(self):
        self._t0, self._status, self._error, self._cost_cents = time.time(), 200, "", 0
        try:
            self._do_GET()
        finally:
            self._flush_error_event()

    def do_POST(self):
        self._t0, self._status, self._error, self._cost_cents = time.time(), 200, "", 0
        try:
            self._do_POST()
        finally:
            self._flush_error_event()

    def do_DELETE(self):
        self._t0, self._status, self._error, self._cost_cents = time.time(), 200, "", 0
        try:
            self._do_DELETE()
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._json(500, {"error": str(e)})
        finally:
            self._flush_error_event()

    def _flush_error_event(self):
        """5xx/异常时补一条 system_error 事件（日志之外的行为面留痕）。"""
        if not self._error:
            return
        try:
            db.event_add("system_error", session_id=self._sid(),
                         user_id=getattr(self, "_uid", None),
                         meta={"path": (self.path or "").split("?")[0], "error": self._error[:200]},
                         ip=self._ip())
        except Exception:  # noqa: BLE001
            pass

    def log_message_raw(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- 工具 ----------
    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok_empty(self):
        """埋点端点的唯一响应：200 + 最小 JSON 正文。

        为什么不是 204、也不是 Content-Length: 0：http.server 走 HTTP/1.0，不带正文的
        响应被 Chromium 记成 net::ERR_ABORTED（其实服务端已处理完）。给一个一两字节的
        JSON 正文，浏览器就当成正常完成的请求，控制台不再有假错误。
        """
        self._json(200, {"ok": True})

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
        user = db.get_session_user(token)
        # 被禁用的账号：已有会话立即失效（登录入口也会拒），退回匿名身份（按 IP 限额）
        user = None if (user and user["disabled"]) else user
        self._uid = user["id"] if user else None   # 请求日志要用
        return user

    def _quota_payload(self, user) -> dict:
        if user:
            # 单一形状来源：登录/注册/兑换/me 都消费 db.quota_state()
            return db.quota_state(user)
        h = _anon[self._ip()]
        used = h["day"] if h["daykey"] == _today() else 0
        return {"plan": "anon", "used": used, "limit": db.QUOTA_ANON}

    # ---------- GET ----------
    def _do_GET(self):
        from urllib.parse import urlparse, parse_qs
        path = self.path.split("?")[0]
        qs = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api/health":
                self._json(200, {"ok": True, "model": _llm_model(), "used_today": _global["day"]})
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
            if path == "/api/meter/config":
                if not self._admin_ok(qs):
                    return
                cap, hard = _cost_caps()
                self._json(200, {
                    "day": _today(),
                    "state": _cost_state(),
                    "cap_cents": cap, "hard_cents": hard,
                    "today_cents": db.meter_totals(_today())["cost_cents"],
                    "degrade": {"explain_tokens": _degrade_max_tokens("explain"),
                                "chat_tokens": _degrade_max_tokens("chat"),
                                "min_interval": int(float(db.meter_get("degrade_min_interval") or 15))},
                    # 可热改的全量白名单值（站长后台「闸门」页直接编辑）
                    "config": {k: db.meter_get(k) for k in METER_CONFIG_KEYS},
                })
                return
            if path == "/api/admin/codes":
                if not self._admin_ok(qs):
                    return
                batch = qs.get("batch", [None])[0] or None
                kind = qs.get("kind", [None])[0] or None
                self._json(200, {"codes": db.list_codes(batch, kind),
                                 "summary": db.code_stats(batch)})
                return
            if path == "/api/admin/llmkey":
                self._admin_llmkey_get()
                return
            if path == "/api/admin/overview":
                self._admin_overview()
                return
            if path == "/api/admin/users":
                if not self._admin_ok(qs):
                    return
                self._json(200, {"users": db.list_users(qs.get("q", [""])[0] or None,
                                                         self._qint(qs, "limit", 200, hi=1000)),
                                 "stats": db.user_stats()})
                return
            if path == "/api/admin/feedback":
                if not self._admin_ok(qs):
                    return
                status = qs.get("status", [""])[0] or None
                if status and status not in db.FEEDBACK_STATUSES:
                    self._json(400, {"error": "status 只能是 new / read / done"})
                    return
                self._json(200, {"items": db.feedback_list(status, self._qint(qs, "limit", 200, hi=1000)),
                                 "stats": db.feedback_stats()})
                return
            if path == "/api/admin/metrics":
                self._admin_metrics(qs)
                return
            if path == "/api/admin/events":
                if not self._admin_ok(qs):
                    return
                self._json(200, {"items": db.events_list(
                    day=qs.get("day", [None])[0] or None,
                    event=qs.get("event", [None])[0] or None,
                    session_id=qs.get("session_id", [None])[0] or None,
                    code=qs.get("code", [None])[0] or None,
                    limit=self._qint(qs, "limit", 200, hi=1000))})
                return
            if path == "/api/admin/audit":
                if not self._admin_ok(qs):
                    return
                self._json(200, {"items": db.admin_log_list(self._qint(qs, "limit", 200, hi=1000))})
                return
            if path == "/api/messages":
                user = self._user()
                if not user:
                    self._json(401, {"error": "未登录"})
                    return
                limit = self._qint(qs, "limit", 50, hi=200)
                self._json(200, {
                    "items": messages.list_for_user(user["id"], limit),
                    "unread": messages.unread_count(user["id"]),
                })
                return
            if path == "/api/me":
                user = self._user()
                if not user:
                    self._json(401, {"error": "未登录"})
                    return
                self._json(200, {"email": user["email"], "created_at": user["created_at"],
                                 "plan_until": user["plan_until"],
                                 "unread_messages": messages.unread_count(user["id"]),
                                 "quota": self._quota_payload(user)})
                return
            if path == "/api/sync":
                user = self._user()
                if not user:
                    self._json(401, {"error": "未登录"})
                    return
                if not self._entitled(user, "sync"):
                    return
                data = db.sync_get(user["id"])
                self._json(200, data if data else {"data": None, "updated_at": None})
                return
            if path == "/api/materials":
                self._material_list(qs)
                return
            m = re.fullmatch(r"/api/materials/(\d+)", path)
            if m:
                self._material_detail(int(m.group(1)), qs)
                return
            m = re.fullmatch(r"/api/materials/(\d+)/chunks", path)
            if m:
                self._material_chunks(int(m.group(1)), qs)
                return
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._json(500, {"error": str(e)})
            return
        super().do_GET()

    def _qs(self) -> dict:
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    @staticmethod
    def _qint(qs: dict, key: str, default: int, lo: int = 1, hi: int = 1000) -> int:
        """查询串里的整数参数：越界夹紧、非数字回落默认（不因一个坏参数 500）。"""
        try:
            return max(lo, min(hi, int(qs.get(key, [str(default)])[0])))
        except (TypeError, ValueError):
            return default

    def _entitled(self, user, feature: str) -> bool:
        """增值权益闸:通过返回 True;否则发 403 并返回 False。"""
        if entitlements.has(user, feature):
            return True
        entitlements.deny(self, feature)
        return False

    def _admin_ok(self, qs: dict | None = None) -> bool:
        """校验 ?token= 或 Authorization: Bearer 是否匹配 WENDI_ADMIN_TOKEN。不匹配 → 401 并返回 False。
        另可设 WENDI_ADMIN_IPS 白名单（逗号分隔）；白名单外一律 403。"""
        if ADMIN_IPS and self._ip() not in ADMIN_IPS:
            self._json(403, {"error": "admin ip not allowed"})
            return False
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
    def _do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/materials":
            # 上传单独分流：绝不经过 _body() 的 64KB 上限，边读边写临时文件。
            try:
                self._material_upload()
            except Exception as e:  # noqa: BLE001
                self._error = f"{type(e).__name__}: {e}"
                log.exception("材料上传失败")
                self._json(500, {"error": "上传失败，请稍后重试", "code": "upload_failed"})
            return
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
            elif path == "/api/feedback":
                self._feedback_add(body)
            elif path == "/api/messages/read":
                self._messages_read(body)
            elif path == "/api/admin/user-message":
                self._admin_user_message(body)
            elif path == "/api/event":
                self._event_add(body)
            elif path == "/api/admin/codes":
                self._admin_gen_codes(body)
            elif path == "/api/admin/codes/void":
                self._admin_void_codes(body)
            elif path == "/api/admin/users":
                self._admin_create_user(body)
            elif path == "/api/admin/users/password":
                self._admin_reset_password(body)
            elif path == "/api/sync/delete":
                self._sync_delete()
            elif path == "/api/admin/llmkey":
                self._admin_llmkey_set(body)
            elif path == "/api/admin/users/disable":
                self._admin_user_disable(body)
            elif path == "/api/admin/feedback/status":
                self._admin_feedback_status(body)
            elif path == "/api/admin/meter/config":
                self._admin_meter_config_set(body)
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._json(500, {"error": str(e)})

    # ---------- 账户 ----------
    def _register(self, body: dict):
        """注册(开放注册,无需任何码)。"""
        email = str(body.get("email", "")).strip()
        pw = str(body.get("password", ""))
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "邮箱格式不正确"})
            return
        if len(pw) < 6:
            self._json(400, {"error": "密码至少 6 位"})
            return
        salt, h = hash_password(pw)
        user, err = db.register(email, salt, h)
        if not user:
            self._json(409, {"error": err})
            return
        token = new_token()
        db.create_session(token, user["id"])
        row = db.get_user(user["id"])
        self._uid = user["id"]
        self._track("register", meta={"plan": db.effective_plan(row)})
        self._json(200, {"token": token, "email": user["email"],
                         "plan": db.effective_plan(row), "plan_until": row["plan_until"],
                         "quota": db.quota_state(row)})

    def _login(self, body: dict):
        email = str(body.get("email", "")).strip()
        pw = str(body.get("password", ""))
        row = db.get_user_by_email(email)
        if not row or not verify_password(pw, row["pw_salt"], row["pw_hash"]):
            self._json(401, {"error": "邮箱或密码不正确"})
            return
        if row["disabled"]:
            self._json(403, {"error": "这个账号已被停用。若有疑问请在反馈页留言。"})
            return
        token = new_token()
        db.create_session(token, row["id"])
        self._uid = row["id"]
        self._track("login", user_id=row["id"])
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

    # ---------- 反馈（公开；按 IP 限流，落库进站长后台） ----------
    def _feedback_add(self, body: dict):
        ip = self._ip()
        h = _feedback_rate[ip]
        day = _today()
        if h["daykey"] != day:
            h["daykey"], h["day"], h["last"] = day, 0, 0.0
        now = time.time()
        if now - h["last"] < FEEDBACK_INTERVAL:
            self._json(429, {"error": f"刚收到你的反馈，{FEEDBACK_INTERVAL} 秒后可再发一条。"})
            return
        if h["day"] >= FEEDBACK_PER_DAY:
            self._json(429, {"error": "今天的反馈条数已满，请明天再来或直接发邮件。"})
            return
        kind = str(body.get("kind", "other")).strip() or "other"
        if kind not in db.FEEDBACK_KINDS:
            self._json(400, {"error": "kind 只能是 bug / idea / other"})
            return
        text = str(body.get("body", "")).strip()
        if len(text) < 2:
            self._json(400, {"error": "内容太短了，多写两句吧"})
            return
        if len(text) > FEEDBACK_MAX:
            self._json(400, {"error": f"内容请在 {FEEDBACK_MAX} 字以内"})
            return
        contact = str(body.get("contact", "")).strip()[:200]
        can_reply = bool(body.get("can_reply"))
        if can_reply and not contact:
            self._json(400, {"error": "勾了「可以联系我」就得留个邮箱或微信"})
            return
        user = self._user()
        fid = db.feedback_add(kind, text, contact, can_reply,
                              user["id"] if user else None, ip,
                              self.headers.get("User-Agent", ""))
        h["day"] += 1
        h["last"] = now
        self._track("feedback", meta={"kind": kind, "can_reply": can_reply})   # 只记动作，不记内容
        kernel.emit("feedback.new", id=fid, kind=kind, user_id=user["id"] if user else None)
        self._json(200, {"ok": True, "id": fid})

    # ---------- 站内消息（用户只读自己的；管理端按 user_id 发） ----------
    def _messages_read(self, body: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        ids = body.get("ids")
        if ids is not None and not isinstance(ids, list):
            self._json(400, {"error": "ids 必须是数组"})
            return
        all_ = bool(body.get("all"))
        if not all_ and not ids:
            self._json(400, {"error": "需要 ids 数组或 all=true"})
            return
        updated = messages.mark_read(user["id"], ids=ids, all_=all_)
        self._json(200, {"ok": True, "updated": updated})

    def _admin_user_message(self, body: dict):
        if not self._admin_ok():
            return
        try:
            uid = int(body.get("user_id"))
        except (TypeError, ValueError):
            self._json(400, {"error": "user_id 必须是整数"})
            return
        text = str(body.get("body", "")).strip()
        if not 2 <= len(text) <= messages.MAX_BODY:
            self._json(400, {"error": f"消息长度必须在 2-{messages.MAX_BODY} 字"})
            return
        user = db.get_user(uid)
        if not user:
            self._json(404, {"error": "用户不存在"})
            return
        if user["disabled"]:
            self._json(409, {"error": "用户已停用，不能发消息"})
            return
        feedback_id = body.get("feedback_id")
        if feedback_id is not None:
            try:
                feedback_id = int(feedback_id)
            except (TypeError, ValueError):
                self._json(400, {"error": "feedback_id 必须是整数"})
                return
        mid = messages.send(uid, text, source="agent", feedback_id=feedback_id)
        db.admin_log("user.message", f"user_id={uid} message_id={mid} "
                                     f"feedback_id={feedback_id}", self._ip())
        self._json(200, {"ok": True, "id": mid, "user_id": uid})

    # ---------- 埋点（公开；白名单 + 限流；任何情况都空 200，绝不打扰用户） ----------
    def _event_add(self, body: dict):
        try:
            ip = self._ip()
            now = time.time()
            hits = _event_rate[ip]
            hits[:] = [t for t in hits if now - t < 60]
            if len(hits) < EVENT_PER_MIN:
                hits.append(now)
                event = str(body.get("event", ""))[:40]
                if event in db.EVENTS:
                    user = self._user()
                    code = str(body.get("code", "")).strip().upper()
                    if not code and user:
                        code = user["invite_code"] or ""
                    meta = body.get("meta")
                    meta = dict(meta) if isinstance(meta, dict) else {}
                    depth = body.get("depth")
                    # 会话 id：优先 body（sendBeacon 不能带头），退回 X-Wendi-Session 头
                    sid = str(body.get("session_id", "")).strip() or self._sid()
                    self._sid_hint = sid          # 请求日志也用这个 id
                    if event == "explain_success" and sid:
                        # 成本不经过前端：服务端从计量表取该会话最近一次的真实成本
                        cost = db.meter_session_cost(sid)
                        if cost is not None:
                            meta["cost_cents"] = cost
                    db.event_add(
                        event, session_id=sid,
                        user_id=user["id"] if user else None, code=code,
                        room_id=str(body.get("room_id", "")), card_id=str(body.get("card_id", "")),
                        depth=depth if isinstance(depth, int) and -1 <= depth <= 99 else -1,
                        meta=meta, ip=ip, ua=self.headers.get("User-Agent", ""))
        except Exception:  # noqa: BLE001
            log.exception("事件落库失败（已忽略）")
        finally:
            self._ok_empty()

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
        db.admin_log("codes.gen", f"batch={batch} n={len(codes)} days={days} note={note}",
                     self._ip())
        self._json(200, {"batch": batch, "codes": codes, "kind": "pro", "days": days})

    def _admin_void_codes(self, body: dict):
        if not self._admin_ok():
            return
        codes = [str(c) for c in body.get("codes", []) if str(c).strip()] or None
        batch = str(body.get("batch", "")).strip() or None
        if not codes and not batch:
            self._json(400, {"error": "需要 codes 列表或 batch"})
            return
        voided = db.void_codes(codes, batch)
        db.admin_log("codes.void", f"batch={batch} codes={len(codes) if codes else 0} voided={voided}",
                     self._ip())
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
        db.admin_log("user.create", f"user_id={user['id']} email={email} plan={plan}", self._ip())
        row = db.get_user(user["id"])
        self._json(200, {"id": row["id"], "email": row["email"],
                         "plan": db.effective_plan(row), "plan_until": row["plan_until"]})

    # —— LLM 密钥/端点/模型（「密钥」面板；热生效，无需重启） ——
    @staticmethod
    def _mask_key(k: str) -> str:
        return k if len(k) <= 12 else k[:6] + "***" + k[-4:]

    def _admin_llmkey_get(self):
        if not self._admin_ok():
            return
        keys = _db_keys()
        self._json(200, {"count": len(keys),
                         "keys": [self._mask_key(k) for _, k in keys],
                         "base_url": db.meter_get("llm_base_url") or "",
                         "model": db.meter_get("llm_model") or "",
                         "active": _ACTIVE_KEY[0] if _ACTIVE_KEY else None})

    def _admin_llmkey_set(self, body: dict):
        global _ACTIVE_KEY
        if not self._admin_ok():
            return
        if str(body.get("action", "set")) == "clear":
            db.meter_set("llm_api_keys", "")
            db.meter_set("llm_base_url", "")
            db.meter_set("llm_model", "")
            _ACTIVE_KEY = None  # 立即停止使用已缓存的 key
            kernel.emit("admin.llmkey_cleared")
            db.admin_log("llmkey.clear", "", self._ip())
            self._json(200, {"ok": True, "count": 0, "keys": []})
            return
        raw = body.get("keys", "")
        if isinstance(raw, list):
            raw = "\n".join(str(k) for k in raw)
        keys: list[str] = []
        for k in re.split(r"[,\s;]+", str(raw)):
            if k and k not in keys:
                keys.append(k)
        if not keys:
            self._json(400, {"error": "keys 为空"})
            return
        if len(keys) > 20:
            self._json(400, {"error": "最多 20 个密钥"})
            return
        short = next((k for k in keys if len(k) < 16), None)
        if short:
            self._json(400, {"error": f"有密钥只有 {len(short)} 字符，疑似复制不完整（正常 ≥ 20）"})
            return
        base_url = str(body.get("base_url", "")).strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            self._json(400, {"error": "接口地址必须以 http(s):// 开头"})
            return
        if base_url:
            base_url = base_url.rstrip("/")  # 统一形态：库存的、回显的、请求用的必须是同一个
        model = str(body.get("model", "")).strip()
        db.meter_set("llm_api_keys", ",".join(keys))
        db.meter_set("llm_base_url", base_url)
        db.meter_set("llm_model", model)
        _ACTIVE_KEY = None
        kernel.emit("admin.llmkey_set", n=len(keys), has_base_url=bool(base_url),
                    model=model or None)
        db.admin_log("llmkey.set", f"n={len(keys)} base_url={base_url} model={model}", self._ip())
        self._json(200, {"ok": True, "count": len(keys),
                         "keys": [self._mask_key(k) for k in keys],
                         "base_url": base_url, "model": model})

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
        db.admin_log("user.password_reset", f"user_id={row['id']} email={row['email']}", self._ip())
        self._json(200, {"ok": True, "email": row["email"], "sessions_revoked": True})

    # ---------- 站长后台：总览 / 用户 / 反馈 / 闸门热改 ----------
    def _admin_overview(self):
        """一屏看板：成本闸状态 + 当日账 + 用户/反馈/兑换码计数 + 最新 5 条未读反馈。"""
        if not self._admin_ok():
            return
        day = _today()
        cap, hard = _cost_caps()
        meter = db.meter_totals(day)
        codes = db.code_stats()
        self._json(200, {
            "day": day, "model": _llm_model(), "state": _cost_state(),
            "cost_cents": meter["cost_cents"], "cap_cents": cap, "hard_cents": hard,
            "calls_today": meter["requests"],
            "users": db.user_stats(),
            "feedback": db.feedback_stats(),
            "codes": {"n": codes["n"], "used": codes["used"],
                      "unused": codes["unused"], "voided": codes["voided"]},
            "meter": meter,
            "funnel": db.funnel(day),
            "events_today": sum(v["n"] for v in db.event_day_counts(day).values()),
            "feedback_new": db.feedback_list("new", 5),
        })

    def _admin_metrics(self, qs: dict):
        """看板：当日聚合（现算并落 daily_metrics）+ 漏斗 + 近 14 天 + 成本闸状态。"""
        if not self._admin_ok(qs):
            return
        day = qs.get("day", [_today()])[0] or _today()
        try:
            time.strptime(day, "%Y-%m-%d")
        except ValueError:
            self._json(400, {"error": "day 形如 2026-09-27"})
            return
        row = db.rollup_day(day)
        db.rollup_day(db._day_shift(day, -1))      # 昨天重算一次，D1 才准
        cap, hard = _cost_caps()
        self._json(200, {
            "day": day, "metrics": row, "funnel": db.funnel(day),
            "recent": db.metrics_range(self._qint(qs, "days", 14, hi=90)),
            "state": _cost_state(), "cap_cents": cap, "hard_cents": hard,
            "cost_today_cents": db.meter_totals(day)["cost_cents"],
            "by_mode": db.meter_totals(day)["by_mode"],
            "by_plan": db.meter_totals(day)["by_plan"],
            "thresholds": {"cost_cents_day": 5000, "error_rate": 0.05, "d1_zero": True},
        })

    def _admin_user_disable(self, body: dict):
        """停用/恢复账号。停用同时踢掉该用户全部会话（在建会话立即失效）。"""
        if not self._admin_ok():
            return
        if "user_id" not in body:
            self._json(400, {"error": "缺 user_id"})
            return
        try:
            uid = int(body["user_id"])
        except (TypeError, ValueError):
            self._json(400, {"error": "user_id 必须是整数"})
            return
        if "disabled" not in body:
            self._json(400, {"error": "缺 disabled（true=停用 / false=恢复）"})
            return
        row = db.get_user(uid)
        if not row:
            self._json(404, {"error": "用户不存在"})
            return
        disabled = bool(body.get("disabled"))
        db.set_disabled(uid, disabled)
        kicked = db.drop_sessions(uid) if disabled else 0
        db.admin_log("user.disable" if disabled else "user.enable",
                     f"user_id={uid} email={row['email']} reason={str(body.get('reason', ''))[:200]}",
                     self._ip())
        kernel.emit("admin.user_disabled", user_id=uid, disabled=disabled)
        self._json(200, {"ok": True, "user_id": uid, "email": row["email"],
                         "disabled": disabled, "sessions_dropped": kicked})

    def _admin_feedback_status(self, body: dict):
        if not self._admin_ok():
            return
        if "id" not in body:
            self._json(400, {"error": "缺 id"})
            return
        try:
            fid = int(body["id"])
        except (TypeError, ValueError):
            self._json(400, {"error": "id 必须是整数"})
            return
        status = str(body.get("status", "")).strip()
        if status not in db.FEEDBACK_STATUSES:
            self._json(400, {"error": "status 只能是 new / read / done"})
            return
        note = body.get("note")
        note = None if note is None else str(note)[:500]
        if not db.feedback_set_status(fid, status, note):
            self._json(404, {"error": "反馈不存在"})
            return
        db.admin_log("feedback.status", f"id={fid} status={status}", self._ip())
        self._json(200, {"ok": True, "id": fid, "status": status})

    def _admin_meter_config_set(self, body: dict):
        """热改成本闸/降级档/单价（白名单键，见 METER_CONFIG_KEYS）。保存即生效，无需重启。"""
        if not self._admin_ok():
            return
        patch = body.get("set")
        if not isinstance(patch, dict) or not patch:
            self._json(400, {"error": 'body 形如 {"set": {"cost_cents_cap": 3000}}'})
            return
        unknown = [k for k in patch if k not in METER_CONFIG_KEYS]
        if unknown:
            self._json(400, {"error": "不允许改的键：" + "、".join(unknown)})
            return
        vals: dict[str, str] = {}
        for k, v in patch.items():
            try:
                f = float(v)
            except (TypeError, ValueError):
                self._json(400, {"error": f"{k} 必须是数字"})
                return
            if not (f >= 0) or f == float("inf"):  # NaN / 负数 / 无穷 一律拒
                self._json(400, {"error": f"{k} 必须是非负有限数"})
                return
            vals[k] = str(int(f)) if k.startswith(("cost_cents", "degrade_")) else str(f)
        cap = float(vals.get("cost_cents_cap") or db.meter_get("cost_cents_cap") or 0)
        hard = float(vals.get("cost_cents_hard") or db.meter_get("cost_cents_hard") or 0)
        if cap and hard and hard <= cap:
            self._json(400, {"error": f"hard 必须大于 cap（改了会是 cap={cap:g} / hard={hard:g}）"})
            return
        for k, v in vals.items():
            db.meter_set(k, v)
        db.admin_log("meter.config", json.dumps(vals, ensure_ascii=False), self._ip())
        kernel.emit("admin.meter_config", keys=sorted(vals))
        self._json(200, {"ok": True, "set": vals})

    # ---------- 云同步 ----------
    def _sync_put(self, body: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        if not entitlements.has(user, "sync"):
            entitlements.deny(self, "sync")
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

    def _sync_delete(self):
        """删除自己的云端副本（本地数据不动）。"""
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        if not entitlements.has(user, "sync"):
            entitlements.deny(self, "sync")
            return
        deleted = db.sync_delete(user["id"])
        self._json(200, {"ok": True, "deleted": deleted})

    # ---------- 材料：上传 / 列表 / 全文 / 分块 / 删除 ----------
    def _upload_filename(self) -> str:
        """从 X-Wendi-Filename 或 ?name= 取原始文件名（支持 percent-encoding）。"""
        from urllib.parse import parse_qs, unquote, urlparse
        raw = self.headers.get("X-Wendi-Filename", "")
        if not raw:
            qs = parse_qs(urlparse(self.path).query)
            raw = (qs.get("name") or [""])[0]
        name = unquote(str(raw or "")).strip()
        name = name.replace("\\", "/").rsplit("/", 1)[-1]       # 去掉路径
        name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()     # 去掉控制字符
        if name in ("", ".", ".."):
            return ""
        if len(name) > MATERIAL_NAME_MAX:                        # 截断但保留扩展名
            suffix = Path(name).suffix[:16]
            name = Path(name).stem[:max(1, MATERIAL_NAME_MAX - len(suffix))] + suffix
        return name

    def _material_upload(self):
        """POST /api/materials：流式收文件 → kreuzberg 解析 → 原始文件落盘 + 全文/分块入库。

        刻意不调用 _body()：MAX_BODY 是全局反滥用限制，上传只受 UPLOAD_MAX_BYTES 约束。
        """
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录", "code": "unauthorized"})
            return
        if not self._entitled(user, "materials"):
            return

        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._json(411, {"error": "需要 Content-Length（不支持 chunked 上传）",
                             "code": "length_required"})
            return
        try:
            size = int(raw_len)
        except (TypeError, ValueError):
            self._json(400, {"error": "Content-Length 不合法", "code": "bad_length"})
            return
        if size <= 0:
            self._json(400, {"error": "文件为空", "code": "empty_file"})
            return
        if size > UPLOAD_MAX_BYTES:
            self._json(413, {"error": f"文件超过 {UPLOAD_MAX_MB}MB 上限",
                             "code": "file_too_large", "max_bytes": UPLOAD_MAX_BYTES})
            return
        count, used_bytes = db.material_usage(user["id"])
        if count >= MATERIALS_MAX:
            self._json(429, {"error": f"材料数量已达上限（{MATERIALS_MAX} 条），请先删除旧材料",
                             "code": "material_limit", "used": count, "limit": MATERIALS_MAX})
            return
        if used_bytes + size > MATERIALS_MAX_BYTES:
            self._json(429, {"error": f"材料存储空间不足（上限 {MATERIALS_MAX_MB}MB）",
                             "code": "material_quota", "used_bytes": used_bytes,
                             "limit_bytes": MATERIALS_MAX_BYTES})
            return

        filename = self._upload_filename()
        if not filename:
            self._json(400, {"error": "缺少文件名：请带 X-Wendi-Filename 头（或 ?name=）",
                             "code": "filename_required"})
            return
        ext = Path(filename).suffix.lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,10}", ext or ""):
            ext = ""

        client_mime = (self.headers.get("Content-Type") or "").split(";")[0].strip()[:120]
        if client_mime in ("application/octet-stream", "binary/octet-stream"):
            client_mime = ""

        uid_dir = MATERIALS_DIR / str(user["id"])
        uid_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = uuid.uuid4().hex
        tmp = uid_dir / f".{token}.part"
        dest = uid_dir / (f"{token}.{ext}" if ext else token)

        conn = getattr(self, "connection", None)
        if conn is not None:  # 防慢速上传占住线程；http.server 默认没有 socket 超时
            try:
                conn.settimeout(UPLOAD_TIMEOUT)
            except OSError:
                pass

        sha = hashlib.sha256()
        written = 0
        try:
            with open(tmp, "wb") as f:
                remaining = size
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        raise ValueError("上传中断（实际字节数少于 Content-Length）")
                    f.write(chunk)
                    sha.update(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
        except TimeoutError:
            tmp.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)
            self._json(408, {"error": "上传超时", "code": "upload_timeout"})
            return
        except Exception:
            tmp.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)
            raise

        started = time.time()
        try:
            parsed = materials.parse_file(dest)
        except materials.ParserUnavailable as e:
            self._safe_unlink(str(dest))
            self._json(503, {"error": str(e), "code": "parser_unavailable"})
            return
        except materials.ParseFailed as e:
            self._safe_unlink(str(dest))
            self._track("material_upload_failed", code=e.code,
                        meta={"size": written, "ext": ext})
            self._json(e.status, {"error": str(e), "code": e.code})
            return
        except Exception:
            self._safe_unlink(str(dest))
            raise

        mime = (parsed.mime or client_mime or "application/octet-stream")[:120]
        try:
            mid = db.material_save(user["id"], filename, mime, ext, written, sha.hexdigest(),
                                   str(dest), parsed.content, parsed.page_count, parsed.chunks)
        except Exception:
            self._safe_unlink(str(dest))
            raise

        elapsed_ms = int((time.time() - started) * 1000)
        self._track("material_upload", meta={
            "material_id": mid, "size": written,
            "page_count": parsed.page_count, "chunk_count": len(parsed.chunks)})
        self._json(201, {
            "id": mid, "filename": filename, "mime": mime, "size": written,
            "sha256": sha.hexdigest(), "char_count": len(parsed.content),
            "page_count": parsed.page_count, "chunk_count": len(parsed.chunks),
            "truncated": parsed.truncated, "parse_ms": elapsed_ms,
        })

    def _material_list(self, qs: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        if not self._entitled(user, "materials"):
            return
        limit = self._qint(qs, "limit", 200, hi=1000)
        self._json(200, {"items": db.materials_list(user["id"], limit),
                         "total": db.material_count(user["id"])})

    def _material_detail(self, mid: int, qs: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        if not self._entitled(user, "materials"):
            return
        with_content = str((qs.get("content") or ["1"])[0]).lower() not in ("0", "false", "no")
        row = db.material_get(user["id"], mid, with_content=with_content)
        if not row:
            self._json(404, {"error": "材料不存在", "code": "not_found"})
            return
        row.pop("path", None)
        payload: dict = {"material": row}
        if with_content:
            payload["content"] = row.pop("content", "")
        self._json(200, payload)

    def _material_chunks(self, mid: int, qs: dict):
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        if not self._entitled(user, "materials"):
            return
        row = db.material_get(user["id"], mid, with_content=False)
        if not row:
            self._json(404, {"error": "材料不存在", "code": "not_found"})
            return
        offset = self._qint(qs, "offset", 0, lo=0, hi=10_000_000)
        limit = self._qint(qs, "limit", 100, hi=500)
        self._json(200, {
            "material_id": mid, "total": row["chunk_count"],
            "offset": offset, "limit": limit,
            "chunks": db.material_chunks(mid, offset, limit),
        })

    def _do_DELETE(self):
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/api/materials/(\d+)", path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        user = self._user()
        if not user:
            self._json(401, {"error": "未登录"})
            return
        mid = int(m.group(1))
        row = db.material_delete(user["id"], mid)
        if not row:
            self._json(404, {"error": "材料不存在", "code": "not_found"})
            return
        self._safe_unlink(str(row.get("path") or ""))
        self._track("material_delete", meta={"material_id": mid})
        self._json(200, {"ok": True, "id": mid})

    @staticmethod
    def _safe_unlink(path: str) -> None:
        """只删 MATERIALS_DIR 里的文件（防库被改后误删任意路径）。"""
        if not path:
            return
        try:
            p = Path(path).resolve()
            p.relative_to(MATERIALS_DIR.resolve())
        except (ValueError, OSError):
            log.warning("拒删材料目录之外的文件：%s", path)
            return
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.warning("删除材料文件失败：%s", path, exc_info=True)

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
        source_meta_enabled = mode == "explain" and body.get("source_meta") is True
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
                                        body.get("style", "standard"),
                                        str(body.get("question", "") or ""),
                                        include_source_meta=source_meta_enabled)
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
                                            consumed=consumed,
                                            source_meta_enabled=source_meta_enabled,
                                            session_id=self._sid(),
                                            room_id=str(body.get("room_id", "")))
            text, usage, truncated = call_llm(msgs, max_tokens)
            source_meta = None
            if source_meta_enabled:
                source_meta, text = split_explain_output(text)
        except Exception as e:  # noqa: BLE001
            if user and consumed:
                db.refund_quota(user["id"])
            kernel.emit("explain.error", mode=mode, user_id=user["id"] if user else None,
                        term=term, error=str(e))
            _record_usage(user, mode, body.get("style", "standard"), False, {}, ok=False,
                          depth=depth, turns=turns, session_id=self._sid(),
                          room_id=str(body.get("room_id", "")))
            self._json(500, {"error": str(e)})
            return
        self._cost_cents = _record_usage(user, mode, body.get("style", "standard"), False,
                                        usage, ok=True, depth=depth, turns=turns,
                                        session_id=self._sid(),
                                        room_id=str(body.get("room_id", "")))
        kernel.emit("explain.done", mode=mode, user_id=user["id"] if user else None,
                    term=term, parent_term=None, style=body.get("style", "standard"),
                    ms=round((time.time() - t0) * 1000), stream=False, chunks=1,
                    truncated=truncated, depth=depth, turns=turns, degraded=degraded)
        payload = {"text": text,
                   "quota": self._quota_payload(db.get_user(user["id"]) if user else None)}
        if source_meta is not None:
            payload["source_meta"] = source_meta
        if truncated:
            payload["truncated"] = True
        if degraded:
            payload["degraded"] = True
            payload["reason"] = degrade_reason
        self._json(200, payload)

    def _explain_stream(self, user, msgs: list[dict], max_tokens: int,
                        mode: str = "explain", term: str = "", t0: float = 0.0,
                        depth: int = -1, turns: int = -1,
                        degraded: bool = False, degrade_reason: str | None = None,
                        consumed: bool = True, source_meta_enabled: bool = False,
                        session_id: str = "", room_id: str = ""):
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
        received = 0
        usage: dict = {}
        finish = ""
        splitter = ExplainOutputParser() if source_meta_enabled and mode == "explain" else None
        try:
            for delta in sse_deltas(resp):
                if isinstance(delta, dict) and "usage" in delta:
                    usage = delta["usage"]
                    finish = delta.get("finish") or ""
                    continue
                received += 1
                if splitter:
                    source_meta, body_delta = splitter.feed(delta)
                    if source_meta is not None:
                        emit({"source_meta": source_meta})
                    if not body_delta:
                        continue
                    delta = body_delta
                sent += 1
                emit({"delta": delta})
            if splitter:
                source_meta, body_delta = splitter.finish()
                if source_meta is not None:
                    emit({"source_meta": source_meta})
                if body_delta:
                    sent += 1
                    emit({"delta": body_delta})
            done = {"done": True,
                    "quota": self._quota_payload(db.get_user(user["id"]) if user else None)}
            if finish == "length":
                done["truncated"] = True
            if not finish:
                # 上游没给 finish_reason 就断流：回答不完整，按失败上报（已出的内容保留）
                done["error"] = "连接中断，回答可能不完整（可重试）"
                if not sent and user and consumed:
                    db.refund_quota(user["id"])
            if degraded:
                done["degraded"] = True
                done["reason"] = degrade_reason
            emit(done)
            self._cost_cents = _record_usage(user, mode, "standard", True, usage,
                                             ok=bool(finish), depth=depth, turns=turns,
                                             session_id=session_id, room_id=room_id)
            kernel.emit("explain.done", mode=mode, user_id=user["id"] if user else None,
                        term=term, parent_term=None, style="standard",
                        ms=round((time.time() - t0) * 1000), stream=True, chunks=received,
                        truncated=(finish == "length"), depth=depth, turns=turns, degraded=degraded)
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端中途离开
        except Exception as e:  # noqa: BLE001
            self._cost_cents = _record_usage(user, mode, "standard", True, usage, ok=False,
                                             depth=depth, turns=turns,
                                             session_id=session_id, room_id=room_id)
            kernel.emit("explain.error", mode=mode, user_id=user["id"] if user else None,
                        term=term, error=str(e))
            try:
                emit({"error": "连接中断，内容可能不完整（" + str(e) + "）"})
                if not sent and user and consumed:  # 一个字都没出且真扣了次 → 退款
                    db.refund_quota(user["id"])
            except OSError:
                pass
        finally:
            resp.close()


def main():
    db.init()
    messages.init()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"问底后端: http://127.0.0.1:{PORT} (model={MODEL}, db={db.DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
