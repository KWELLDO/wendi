"""问底 · 数据层（SQLite）

表：users / sessions / codes（兑换码）/ syncs（云同步）
配额：匿名按 IP（内存，server.py 管）；登录用户按账号（本模块管）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("WENDI_DB", str(Path.home() / ".wendi" / "wendi.db")))

# —— 配额（次/天） ——
QUOTA_ANON = int(os.environ.get("WENDI_QUOTA_ANON", "20"))    # 未登录，按 IP
QUOTA_FREE = int(os.environ.get("WENDI_QUOTA_FREE", "40"))    # 免费账号
QUOTA_PRO = int(os.environ.get("WENDI_QUOTA_PRO", "300"))     # 付费账号


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            pw_salt TEXT NOT NULL,
            pw_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            plan_until TEXT,
            day_key TEXT NOT NULL DEFAULT '',
            day_used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            days INTEGER NOT NULL,
            note TEXT DEFAULT '',
            used_by INTEGER,
            used_at TEXT,
            batch TEXT NOT NULL DEFAULT '',
            voided INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS syncs (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meter_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            day_key TEXT NOT NULL,
            user_id INTEGER,
            plan TEXT NOT NULL,
            mode TEXT NOT NULL,
            style TEXT,
            stream INTEGER NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            prompt_cache_hit_tokens INTEGER NOT NULL,
            prompt_cache_miss_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost_cents INTEGER NOT NULL,
            ok INTEGER NOT NULL,
            depth INTEGER NOT NULL DEFAULT -1,
            turns INTEGER NOT NULL DEFAULT -1
        );
        CREATE INDEX IF NOT EXISTS idx_meter_requests_day ON meter_requests(day_key);
        CREATE INDEX IF NOT EXISTS idx_meter_requests_user_day ON meter_requests(user_id, day_key);
        CREATE TABLE IF NOT EXISTS meter_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'other',
            body TEXT NOT NULL,
            contact TEXT NOT NULL DEFAULT '',
            can_reply INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER,
            ip TEXT NOT NULL DEFAULT '',
            ua TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, id);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            day_key TEXT NOT NULL,
            event TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            user_id INTEGER,
            code TEXT NOT NULL DEFAULT '',
            room_id TEXT NOT NULL DEFAULT '',
            card_id TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT -1,
            meta TEXT NOT NULL DEFAULT '{}',
            ip_hash TEXT NOT NULL DEFAULT '',
            ua_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_day_event ON events(day_key, event);
        CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_code ON events(code, ts);
        CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, ts);
        CREATE TABLE IF NOT EXISTS daily_metrics (
            day TEXT PRIMARY KEY,
            active_users INTEGER NOT NULL DEFAULT 0,
            new_users INTEGER NOT NULL DEFAULT 0,
            sessions INTEGER NOT NULL DEFAULT 0,
            app_opens INTEGER NOT NULL DEFAULT 0,
            explains INTEGER NOT NULL DEFAULT 0,
            explain_errors INTEGER NOT NULL DEFAULT 0,
            drill_downs INTEGER NOT NULL DEFAULT 0,
            go_backs INTEGER NOT NULL DEFAULT 0,
            shares INTEGER NOT NULL DEFAULT 0,
            exports INTEGER NOT NULL DEFAULT 0,
            feedbacks INTEGER NOT NULL DEFAULT 0,
            llm_calls INTEGER NOT NULL DEFAULT 0,
            cost_cents INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            d1_return INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT ''
        );
        """)
        try:
            c.execute("ALTER TABLE users ADD COLUMN day_key TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        for ddl in ("ALTER TABLE codes ADD COLUMN batch TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE codes ADD COLUMN voided INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE meter_requests ADD COLUMN depth INTEGER NOT NULL DEFAULT -1",
                    "ALTER TABLE meter_requests ADD COLUMN turns INTEGER NOT NULL DEFAULT -1",
                    "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN invite_code TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE codes ADD COLUMN kind TEXT NOT NULL DEFAULT 'pro'",
                    "ALTER TABLE codes ADD COLUMN tester_label TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE meter_requests ADD COLUMN session_id TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE meter_requests ADD COLUMN room_id TEXT NOT NULL DEFAULT ''"):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # —— 经济测算默认单价（美元/百万 token，来自 docs/plans/2026-09-23-usage-pricing-probe.md） ——
        defaults = {
            "flash_in_hit_offpeak": "0.003",
            "flash_in_hit_peak": "0.006",
            "flash_in_miss_offpeak": "0.15",
            "flash_in_miss_peak": "0.30",
            "flash_out_offpeak": "0.60",
            "flash_out_peak": "1.20",
            "usd_to_cny": "7.2",
            "cost_cents_cap": "3000",  # ¥30/天 → 超了当日全员降级
            "cost_cents_hard": "6000",  # ¥60/天 → 2×cap，超了硬停 429
            # —— 降级档（P0：超配额/超cap不断头，缩短输出+降速） ——
            "degrade_max_tokens": "400",       # 降级时 explain/followup 输出上限
            "degrade_max_tokens_chat": "1000",  # 降级时 chat 输出上限
            "degrade_min_interval": "15",       # 降级时同一用户最小间隔（秒）
        }
        c.executemany("INSERT OR IGNORE INTO meter_config (key, value) VALUES (?,?)",
                      list(defaults.items()))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ================= 用户 =================
def create_user(email: str, salt: str, pw_hash: str) -> dict | None:
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users (email, pw_salt, pw_hash, created_at) VALUES (?,?,?,?)",
                (email.lower().strip(), salt, pw_hash, now()))
            return {"id": cur.lastrowid, "email": email.lower().strip()}
    except sqlite3.IntegrityError:
        return None


def get_user_by_email(email: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()


def get_user(uid: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_invite_code(uid: int, code: str) -> None:
    """记下账号归属的邀请码（归因/传播链用；注册成功后写一次）。"""
    with _conn() as c:
        c.execute("UPDATE users SET invite_code=? WHERE id=?", ((code or "").strip().upper(), uid))


def set_plan(uid: int, plan: str, until: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET plan=?, plan_until=? WHERE id=?", (plan, until, uid))


def reset_password(uid: int, salt: str, pw_hash: str) -> None:
    """改密码并踢掉该用户全部会话（重置后旧 token 立即失效）。"""
    with _conn() as c:
        c.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?", (salt, pw_hash, uid))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


# ================= 会话 =================
def create_session(token: str, uid: int, days: int = 90) -> None:
    exp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + days * 86400))
    with _conn() as c:
        c.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                  (token, uid, now(), exp))


def get_session_user(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token=? AND s.expires_at > ?", (token, now())).fetchone()
        return row


def delete_session(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# ================= 配额 =================
def effective_plan(user: sqlite3.Row) -> str:
    """pro 到期则回落 free。"""
    if user["plan"] == "pro":
        if not user["plan_until"] or user["plan_until"] >= today():
            return "pro"
    return "free"


def quota_limit(user: sqlite3.Row) -> int:
    return QUOTA_PRO if effective_plan(user) == "pro" else QUOTA_FREE


def consume_quota(uid: int, limit: int) -> tuple[bool, int, int]:
    """扣一次配额。返回 (allowed, used, limit)。跨天自动重置。"""
    d = today()
    with _conn() as c:
        row = c.execute("SELECT day_key, day_used FROM users WHERE id=?", (uid,)).fetchone()
        used = row["day_used"] if row["day_key"] == d else 0
        if used >= limit:
            return False, used, limit
        used += 1
        c.execute("UPDATE users SET day_key=?, day_used=? WHERE id=?", (d, used, uid))
        return True, used, limit


def quota_state(user: sqlite3.Row) -> dict:
    d = today()
    used = user["day_used"] if user["day_key"] == d else 0
    return {"used": used, "limit": quota_limit(user), "date": d}


def refund_quota(uid: int) -> None:
    """LLM 调用失败时退回一次配额。"""
    with _conn() as c:
        c.execute("UPDATE users SET day_used = CASE WHEN day_used > 0 THEN day_used - 1 ELSE 0 END "
                  "WHERE id=?", (uid,))


# ================= 兑换码 =================
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混字符


def gen_codes(n: int, days: int, note: str = "", batch: str | None = None,
              kind: str = "pro", tester_label: str = "") -> tuple[str, list[str]]:
    """生成 n 个码，返回 (batch, codes)。batch 缺省自动生成 YYYYMMDD-XXXX。

    kind='pro'    → 登录后兑换 Pro；
    kind='invite' → 注册邀请码（days>0 则注册即得 Pro 天数，tester_label 记「谁」）。
    撞库（INSERT OR IGNORE 未插入）时重试，绝不把已存在的码返回给调用方。
    """
    import secrets as _s
    if not batch:
        batch = time.strftime("%Y%m%d") + "-" + "".join(_s.choice(_CODE_ALPHABET) for _ in range(4))
    out: list[str] = []
    with _conn() as c:
        while len(out) < n:
            code = "WD-" + "-".join("".join(_s.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(3))
            cur = c.execute(
                "INSERT OR IGNORE INTO codes (code, days, note, batch, kind, tester_label) "
                "VALUES (?,?,?,?,?,?)",
                (code, days, note, batch, kind, tester_label))
            if cur.rowcount:  # 0 = 撞库，重试
                out.append(code)
    return batch, out


def list_codes(batch: str | None = None, kind: str | None = None) -> list[dict]:
    """列码（可按批次/类型过滤），带兑换人邮箱与 status：unused / used / voided。"""
    q = ("SELECT c.code, c.days, c.note, c.batch, c.kind, c.tester_label, c.used_by, "
         "c.used_at, c.voided, u.email AS used_by_email "
         "FROM codes c LEFT JOIN users u ON u.id = c.used_by WHERE 1=1")
    args: list = []
    if batch:
        q += " AND c.batch=?"
        args.append(batch)
    if kind:
        q += " AND c.kind=?"
        args.append(kind)
    q += " ORDER BY c.batch DESC, c.code"
    out = []
    with _conn() as c:
        for r in c.execute(q, tuple(args)):
            d = dict(r)
            d["status"] = "voided" if d["voided"] else ("used" if d["used_by"] else "unused")
            out.append(d)
    return out


def void_codes(codes: list[str] | None = None, batch: str | None = None) -> int:
    """作废未使用的码（按码列表和/或整批）。返回实际作废数量；已用/已废的跳过。"""
    with _conn() as c:
        n = 0
        if batch:
            n += c.execute("UPDATE codes SET voided=1 WHERE batch=? AND used_by IS NULL AND voided=0",
                           (batch,)).rowcount
        for code in codes or []:
            n += c.execute("UPDATE codes SET voided=1 WHERE code=? AND used_by IS NULL AND voided=0",
                           (code.strip().upper(),)).rowcount
        return n


def code_stats(batch: str | None = None) -> dict:
    """批次转化账：总数/已兑/已废/未用/兑过码的用户数。used 与 voided 天然不相交。"""
    q = ("SELECT COUNT(*) AS n, "
         "COALESCE(SUM(used_by IS NOT NULL),0) AS used, "
         "COALESCE(SUM(voided),0) AS voided, "
         "COUNT(DISTINCT used_by) AS redeemers FROM codes")
    args: tuple = ()
    if batch:
        q += " WHERE batch=?"
        args = (batch,)
    with _conn() as c:
        row = c.execute(q, args).fetchone()
    n, used, voided = row["n"], row["used"], row["voided"]
    return {"n": n, "used": used, "voided": voided, "unused": n - used - voided,
            "redeemers": row["redeemers"]}


def redeem(code: str, uid: int) -> tuple[bool, str, str]:
    """兑换。返回 (ok, message, new_plan_until)。"""
    code = code.strip().upper()
    with _conn() as c:
        row = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if not row:
            return False, "兑换码不存在", ""
        if row["voided"]:
            return False, "兑换码已作废", ""
        if row["used_by"]:
            return False, "兑换码已被使用", ""
        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        base = user["plan_until"] if (user["plan_until"] and user["plan_until"] > today()) else today()
        t = time.mktime(time.strptime(base, "%Y-%m-%d")) + row["days"] * 86400
        until = time.strftime("%Y-%m-%d", time.localtime(t))
        c.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=?", (uid, now(), code))
        c.execute("UPDATE users SET plan='pro', plan_until=? WHERE id=?", (until, uid))
        return True, f"兑换成功，Pro 有效期至 {until}", until


# ================= 云同步 =================
def sync_get(uid: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT data, updated_at FROM syncs WHERE user_id=?", (uid,)).fetchone()
        return dict(row) if row else None


def sync_delete(uid: int) -> bool:
    """删除云端副本（数据主权：这份数据是用户的，随时可撤）。"""
    with _conn() as c:
        return c.execute("DELETE FROM syncs WHERE user_id=?", (uid,)).rowcount > 0


def sync_put(uid: int, data: str) -> str:
    ts = now()
    with _conn() as c:
        c.execute("INSERT INTO syncs (user_id, data, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                  (uid, data, ts))
    return ts


# ================= 经济测算（metering） =================
def meter_record(user_id, plan: str, mode: str, style, stream: bool,
                 prompt_cache_hit_tokens: int, prompt_cache_miss_tokens: int,
                 completion_tokens: int, cost_cents: int, ok: bool,
                 depth: int = -1, turns: int = -1,
                 session_id: str = "", room_id: str = "") -> None:
    """记一笔 LLM 调用的真实 token 消耗与折算成本。

    depth/turns 为行为信号（-1=未上报）；session_id/room_id 用于把成本关联到一次前端会话/一层房间。
    """
    with _conn() as c:
        c.execute(
            "INSERT INTO meter_requests (ts, day_key, user_id, plan, mode, style, stream, "
            "prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
            "completion_tokens, cost_cents, ok, depth, turns, session_id, room_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), today(), user_id, plan, mode, style,
             1 if stream else 0,
             prompt_cache_hit_tokens + prompt_cache_miss_tokens,
             prompt_cache_hit_tokens, prompt_cache_miss_tokens,
             completion_tokens, cost_cents, 1 if ok else 0, depth, turns,
             (session_id or "")[:64], (room_id or "")[:64]))


def meter_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO meter_config (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def meter_get(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM meter_config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def meter_totals(day: str) -> dict:
    """全局某天的总账：总次数、token、成本，按 plan / mode 分组。"""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(prompt_cache_hit_tokens),0) AS hit, "
            "COALESCE(SUM(prompt_cache_miss_tokens),0) AS miss, "
            "COALESCE(SUM(completion_tokens),0) AS comp, "
            "COALESCE(SUM(cost_cents),0) AS cost "
            "FROM meter_requests WHERE day_key=?", (day,)).fetchone()
        by_plan = {r["plan"]: r["n"] for r in c.execute(
            "SELECT plan, COUNT(*) AS n FROM meter_requests WHERE day_key=? GROUP BY plan", (day,))}
        by_mode = {r["mode"]: r["n"] for r in c.execute(
            "SELECT mode, COUNT(*) AS n FROM meter_requests WHERE day_key=? GROUP BY mode", (day,))}
    return {"day": day, "requests": row["n"], "hit_tokens": row["hit"],
            "miss_tokens": row["miss"], "completion_tokens": row["comp"],
            "cost_cents": row["cost"], "by_plan": by_plan, "by_mode": by_mode}


def meter_user_day(uid: int, day: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(prompt_cache_hit_tokens),0) AS hit, "
            "COALESCE(SUM(prompt_cache_miss_tokens),0) AS miss, "
            "COALESCE(SUM(completion_tokens),0) AS comp, "
            "COALESCE(SUM(cost_cents),0) AS cost "
            "FROM meter_requests WHERE user_id=? AND day_key=?", (uid, day)).fetchone()
    return {"day": day, "requests": row["n"], "hit_tokens": row["hit"],
            "miss_tokens": row["miss"], "completion_tokens": row["comp"],
            "cost_cents": row["cost"]}


def meter_user_ndays(uid: int, n: int) -> list[dict]:
    """单用户近 n 天的逐日账（定价 drill-down）。返回按 day_key 升序。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT day_key, COUNT(*) AS n, "
            "COALESCE(SUM(prompt_cache_hit_tokens),0) AS hit, "
            "COALESCE(SUM(prompt_cache_miss_tokens),0) AS miss, "
            "COALESCE(SUM(completion_tokens),0) AS comp, "
            "COALESCE(SUM(cost_cents),0) AS cost "
            "FROM meter_requests WHERE user_id=? "
            "GROUP BY day_key ORDER BY day_key DESC LIMIT ?", (uid, n)).fetchall()
    return [{"day": r["day_key"], "requests": r["n"], "hit_tokens": r["hit"],
             "miss_tokens": r["miss"], "completion_tokens": r["comp"],
             "cost_cents": r["cost"]} for r in rows]


# ================= 反馈 =================
FEEDBACK_KINDS = ("bug", "idea", "other")
FEEDBACK_STATUSES = ("new", "read", "done")


def feedback_add(kind: str, body: str, contact: str = "", can_reply: bool = False,
                 user_id: int | None = None, ip: str = "", ua: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO feedback (ts, kind, body, contact, can_reply, user_id, ip, ua) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now(), kind, body, contact, 1 if can_reply else 0, user_id, ip, ua[:200]))
        return cur.lastrowid


def feedback_list(status: str | None = None, limit: int = 200) -> list[dict]:
    """反馈列表（新→旧）；带登录用户邮箱，未登录只有 IP。"""
    sql = "SELECT f.*, u.email FROM feedback f LEFT JOIN users u ON u.id = f.user_id"
    args: list = []
    if status:
        sql += " WHERE f.status=?"
        args.append(status)
    sql += " ORDER BY f.id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def feedback_set_status(fid: int, status: str, note: str | None = None) -> bool:
    with _conn() as c:
        if note is None:
            cur = c.execute("UPDATE feedback SET status=? WHERE id=?", (status, fid))
        else:
            cur = c.execute("UPDATE feedback SET status=?, note=? WHERE id=?", (status, note, fid))
        return cur.rowcount > 0


def feedback_stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"]
        rows = c.execute("SELECT status, COUNT(*) AS n FROM feedback GROUP BY status").fetchall()
    out = {"total": total, "new": 0, "read": 0, "done": 0}
    for r in rows:
        if r["status"] in out:
            out[r["status"]] = r["n"]
    return out


# ================= 用户（管理端） =================
def list_users(q: str | None = None, limit: int = 200) -> list[dict]:
    """用户列表（新→旧），含生效套餐、今日用量、云同步体积。禁用用户的会话另有 drop_sessions。"""
    sql = ("SELECT u.id, u.email, u.created_at, u.plan, u.plan_until, u.day_key, u.day_used, "
           "u.disabled, s.data AS sync_data, s.updated_at AS sync_at "
           "FROM users u LEFT JOIN syncs s ON s.user_id = u.id")
    args: list = []
    if q:
        sql += " WHERE u.email LIKE ?"
        args.append(f"%{q.strip().lower()}%")
    sql += " ORDER BY u.id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        data = d.pop("sync_data") or ""
        d["sync_bytes"] = len(data.encode("utf-8")) if data else 0
        d["plan_effective"] = effective_plan(r)
        d["day_used"] = r["day_used"] if r["day_key"] == today() else 0
        out.append(d)
    return out


def set_disabled(uid: int, disabled: bool) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
        return cur.rowcount > 0


def drop_sessions(uid: int) -> int:
    """踢掉某用户全部登录会话，返回删除条数。"""
    with _conn() as c:
        return c.execute("DELETE FROM sessions WHERE user_id=?", (uid,)).rowcount


def user_stats() -> dict:
    """总览：总用户 / 已禁 / 生效 Pro / 近 7 天新增 / 云同步份数。"""
    d7 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(disabled),0) AS disabled, "
            "COALESCE(SUM(CASE WHEN plan='pro' AND (plan_until IS NULL OR plan_until >= ?) "
            "THEN 1 ELSE 0 END),0) AS pro, "
            "COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END),0) AS new7 "
            "FROM users", (today(), d7)).fetchone()
        syncs = c.execute("SELECT COUNT(*) AS n FROM syncs").fetchone()["n"]
    return {"total": row["total"], "disabled": row["disabled"], "pro": row["pro"],
            "new7": row["new7"], "syncs": syncs}


# ================= 管理审计 =================
def admin_log(action: str, detail: str = "", ip: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT INTO admin_log (ts, action, detail, ip) VALUES (?,?,?,?)",
                  (now(), action, detail[:500], ip))


def admin_log_list(limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ================= 事件流水（内测采集：只记动作，不记正文） =================
EVENTS = (
    "app_open", "select_text", "explain_request", "explain_success", "explain_error",
    "drill_down", "go_back", "export", "share_create", "feedback", "register", "login",
    "system_error",
)
MAX_META_CHARS = 1000

# 事件身份口径：码 > 账号 > IP 哈希。一条事件只算一个身份，避免同一人在 active_users 里被算两次。
_IDENT = ("COALESCE(NULLIF(code,''), CASE WHEN user_id IS NOT NULL THEN 'u' || user_id "
          "ELSE 'i' || ip_hash END)")


def ip_hash(ip: str) -> str:
    """IP 只留哈希：sha256(salt + ip) 前 16 hex。salt 存 meter_config（首次自动生成）。"""
    salt = meter_get("ip_hash_salt")
    if not salt:
        salt = secrets.token_hex(16)
        meter_set("ip_hash_salt", salt)
    return hashlib.sha256((salt + (ip or "")).encode("utf-8")).hexdigest()[:16]


def _ua_hash(ua: str) -> str:
    return hashlib.sha256((ua or "").encode("utf-8")).hexdigest()[:16]


def event_add(event: str, session_id: str = "", user_id: int | None = None, code: str = "",
              room_id: str = "", card_id: str = "", depth: int = -1, meta: dict | None = None,
              ip: str = "", ua: str = "") -> None:
    """写一条事件。调用方负责吞异常（埋点绝不阻塞主干，但错误要能被看见）。"""
    ts = int(time.time() * 1000)
    with _conn() as c:
        c.execute(
            "INSERT INTO events (ts, day_key, event, session_id, user_id, code, room_id, "
            "card_id, depth, meta, ip_hash, ua_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, time.strftime("%Y-%m-%d"), event, (session_id or "")[:64], user_id,
             (code or "").strip().upper()[:32], (room_id or "")[:64], (card_id or "")[:64],
             int(depth), json.dumps(meta or {}, ensure_ascii=False)[:MAX_META_CHARS],
             ip_hash(ip), _ua_hash(ua)))


def events_list(day: str | None = None, event: str | None = None, session_id: str | None = None,
                code: str | None = None, limit: int = 200) -> list[dict]:
    """事件流（新→旧）。meta 已解析成对象，方便直接喂看板/agent。"""
    q = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if day:
        q += " AND day_key=?"
        args.append(day)
    if event:
        q += " AND event=?"
        args.append(event)
    if session_id:
        q += " AND session_id=?"
        args.append(session_id)
    if code:
        q += " AND code=?"
        args.append(code.strip().upper())
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        try:
            r["meta"] = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            r["meta"] = {}
    return rows


def event_day_counts(day: str) -> dict:
    """某日各事件条数 + 独立会话数（漏斗的原始素材）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT event, COUNT(*) AS n, COUNT(DISTINCT session_id) AS s "
            "FROM events WHERE day_key=? GROUP BY event", (day,)).fetchall()
    return {r["event"]: {"n": r["n"], "sessions": r["s"]} for r in rows}


def _metric_row(day: str) -> dict:
    """按 events + meter_requests + users 重算某日聚合（纯函数，rollup_day 调它）。"""
    with _conn() as c:
        e = dict(c.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions, "
            f"COUNT(DISTINCT {_IDENT}) AS active "
            "FROM events WHERE day_key=?", (day,)).fetchone())
        counts = {r["event"]: r["n"] for r in c.execute(
            "SELECT event, COUNT(*) AS n FROM events WHERE day_key=? GROUP BY event", (day,))}
        new_users = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE substr(created_at,1,10)=?", (day,)).fetchone()["n"]
        m = dict(c.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_cents),0) AS cost, "
            "COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failed "
            "FROM meter_requests WHERE day_key=?", (day,)).fetchone())
        prev = c.execute(
            f"SELECT DISTINCT {_IDENT} AS k FROM events WHERE day_key=? AND event='app_open'",
            (day,)).fetchall()
        yesterday = _day_shift(day, -1)
        prevday = {r["k"] for r in c.execute(
            f"SELECT DISTINCT {_IDENT} AS k FROM events WHERE day_key=? AND event='app_open'",
            (yesterday,)).fetchall()}
    today_keys = {r["k"] for r in prev}
    return {
        "day": day,
        "active_users": e["active"], "new_users": new_users, "sessions": e["sessions"],
        "app_opens": counts.get("app_open", 0),
        "explains": counts.get("explain_success", 0),
        "explain_errors": counts.get("explain_error", 0),
        "drill_downs": counts.get("drill_down", 0),
        "go_backs": counts.get("go_back", 0),
        "shares": counts.get("share_create", 0),
        "exports": counts.get("export", 0),
        "feedbacks": counts.get("feedback", 0),
        "llm_calls": m["calls"], "cost_cents": m["cost"],
        "errors": counts.get("system_error", 0) + m["failed"],
        "d1_return": len(today_keys & prevday),
    }


def _day_shift(day: str, delta: int) -> str:
    t = time.mktime(time.strptime(day, "%Y-%m-%d")) + delta * 86400
    return time.strftime("%Y-%m-%d", time.localtime(t))


def rollup_day(day: str) -> dict:
    """幂等重算某日聚合并写入 daily_metrics（先删后插，可安全重复跑）。"""
    row = _metric_row(day)
    with _conn() as c:
        c.execute("DELETE FROM daily_metrics WHERE day=?", (day,))
        c.execute(
            "INSERT INTO daily_metrics (day, active_users, new_users, sessions, app_opens, "
            "explains, explain_errors, drill_downs, go_backs, shares, exports, feedbacks, "
            "llm_calls, cost_cents, errors, d1_return, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["day"], row["active_users"], row["new_users"], row["sessions"], row["app_opens"],
             row["explains"], row["explain_errors"], row["drill_downs"], row["go_backs"],
             row["shares"], row["exports"], row["feedbacks"], row["llm_calls"],
             row["cost_cents"], row["errors"], row["d1_return"], int(time.time() * 1000)))
    return row


def metrics_get(day: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM daily_metrics WHERE day=?", (day,)).fetchone()
    return dict(r) if r else None


def metrics_range(days: int = 14) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM daily_metrics ORDER BY day DESC LIMIT ?",
                         (int(days),)).fetchall()
    return [dict(r) for r in rows]


def funnel(day: str) -> dict:
    """当日漏斗（独立会话数）：划词 → 解释成功 → 下钻 → 回退 → 分享。"""
    out = {}
    for ev in ("select_text", "explain_success", "drill_down", "go_back", "share_create"):
        with _conn() as c:
            out[ev] = c.execute(
                "SELECT COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id "
                "                          ELSE 'e' || ip_hash END) AS n "
                "FROM events WHERE day_key=? AND event=? AND event != 'system_error'",
                (day, ev)).fetchone()["n"]
    return out


def prune_events(keep_days: int = 90) -> int:
    """删掉保留期外的事件流水（聚合指标永久保留）。返回删除条数。"""
    cutoff = _day_shift(time.strftime("%Y-%m-%d"), -int(keep_days))
    with _conn() as c:
        return c.execute("DELETE FROM events WHERE day_key < ?", (cutoff,)).rowcount


def tester_rows() -> list[dict]:
    """每人一行：邀请码 → 账号 → 事件统计 → 成本 → D1/D7 回访。"""
    with _conn() as c:
        base = [dict(r) for r in c.execute(
            "SELECT c.code, c.kind, c.days, c.batch, c.note, c.tester_label, c.voided, "
            "c.used_at, u.id AS user_id, u.email, u.plan, u.plan_until, u.disabled, "
            "u.created_at AS registered_at, u.invite_code "
            "FROM codes c LEFT JOIN users u ON u.invite_code = c.code "
            "WHERE c.kind='invite' ORDER BY c.code").fetchall()]
        stats = {r["code"]: dict(r) for r in c.execute(
            "SELECT code, COUNT(*) AS n, COUNT(DISTINCT day_key) AS days_active, "
            "MAX(ts) AS last_ts, MIN(day_key) AS first_day, MAX(day_key) AS last_day, "
            "SUM(event='app_open') AS opens, SUM(event='explain_success') AS explains, "
            "SUM(event='drill_down') AS drills, SUM(event='go_back') AS backs "
            "FROM events WHERE code != '' GROUP BY code").fetchall()}
        cost = {r["code"]: r["cost"] for r in c.execute(
            "SELECT u.invite_code AS code, COALESCE(SUM(m.cost_cents),0) AS cost "
            "FROM meter_requests m JOIN users u ON u.id = m.user_id "
            "WHERE u.invite_code != '' GROUP BY u.invite_code").fetchall()}
        open_days: dict[str, list[str]] = {}
        for r in c.execute("SELECT DISTINCT code, day_key FROM events "
                           "WHERE code != '' AND event='app_open' ORDER BY day_key"):
            open_days.setdefault(r["code"], []).append(r["day_key"])
    for row in base:
        code = row["code"]
        st = stats.get(code, {})
        row["events"] = st.get("n") or 0
        row["days_active"] = st.get("days_active") or 0
        row["explains"] = st.get("explains") or 0
        row["drills"] = st.get("drills") or 0
        row["opens"] = st.get("opens") or 0
        row["last_ts"] = st.get("last_ts")
        row["first_day"] = st.get("first_day")
        row["last_day"] = st.get("last_day")
        row["cost_cents"] = cost.get(code) or 0
        days = open_days.get(code, [])
        row["returned"] = len(days) >= 2
        row["d1"] = _within(days, 1, 2)
        row["d7"] = _within(days, 6, 8)
    return base


def _within(days: list[str], lo: int, hi: int) -> bool:
    """首次打开之后，是否在第 lo–hi 天之间又打开过（回访信号，带一天容差）。"""
    if len(days) < 2:
        return False
    first = time.mktime(time.strptime(days[0], "%Y-%m-%d"))
    return any(lo <= (time.mktime(time.strptime(d, "%Y-%m-%d")) - first) / 86400 <= hi
               for d in days[1:])


def meter_session_cost(session_id: str, within_s: int = 900) -> int | None:
    """该会话最近一次 LLM 调用的折算成本（分）。给 explain_success 事件附着用；没有则 None。

    这样前端只需上报「解释成功 + 耗时」，成本由服务端从计量表取真值——成本不经过前端。
    """
    if not session_id:
        return None
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - within_s))
    with _conn() as c:
        row = c.execute("SELECT cost_cents FROM meter_requests WHERE session_id=? AND ts>=? "
                        "ORDER BY id DESC LIMIT 1", (session_id, cutoff)).fetchone()
    return row["cost_cents"] if row else None


# ================= 邀请码（kind='invite'：注册门槛，可携带赠送天数） =================
def register_with_invite(email: str, salt: str, pw_hash: str,
                         code: str = "") -> tuple[dict | None, str, int]:
    """一条事务里完成注册：校验邀请码 → 建号 → 记归属 → （days>0 时）赠 Pro。

    返回 (user | None, err, days)。err='' 表示成功；code 为空且允许空码时也可建号。
    """
    email = email.lower().strip()
    code = (code or "").strip().upper()
    with _conn() as c:
        if code:
            row = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
            if not row:
                return None, "邀请码不存在", 0
            if row["voided"]:
                return None, "邀请码已作废", 0
            if row["used_by"]:
                return None, "邀请码已经被用过了", 0
            if (row["kind"] or "pro") != "invite":
                return None, "这是兑换码，不是邀请码", 0
            days = int(row["days"] or 0)
        else:
            days = 0
        try:
            cur = c.execute(
                "INSERT INTO users (email, pw_salt, pw_hash, created_at, invite_code) "
                "VALUES (?,?,?,?,?)", (email, salt, pw_hash, now(), code))
        except sqlite3.IntegrityError:
            return None, "这个邮箱已经注册过了", 0
        uid = cur.lastrowid
        if code:
            c.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=?", (uid, now(), code))
        if days:
            until = time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
            c.execute("UPDATE users SET plan='pro', plan_until=? WHERE id=?", (until, uid))
        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(user), "", days
def invite_claim(code: str, uid: int) -> tuple[bool, str, int]:
    """注册时用邀请码：校验 → 标记用掉 → 返回 (ok, message, 赠送天数)。账号归属由调用方写。"""
    code = (code or "").strip().upper()
    if not code:
        return False, "需要邀请码", 0
    with _conn() as c:
        row = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if not row:
            return False, "邀请码不存在", 0
        if row["voided"]:
            return False, "邀请码已作废", 0
        if row["used_by"]:
            return False, "邀请码已被使用", 0
        if (row["kind"] or "pro") != "invite":
            return False, "这是兑换码，不是邀请码", 0
        c.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=?", (uid, now(), code))
        return True, "ok", int(row["days"] or 0)
