"""问底 · 数据层（SQLite）

表：users / sessions / codes（兑换码）/ syncs（云同步）
配额：匿名按 IP（内存，server.py 管）；登录用户按账号（本模块管）。
"""

from __future__ import annotations

import os
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
                    "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"):
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


def gen_codes(n: int, days: int, note: str = "", batch: str | None = None) -> tuple[str, list[str]]:
    """生成 n 个兑换码，返回 (batch, codes)。batch 缺省自动生成 YYYYMMDD-XXXX。

    撞库（INSERT OR IGNORE 未插入）时重试，绝不把已存在的码返回给调用方。
    """
    import secrets as _s
    if not batch:
        batch = time.strftime("%Y%m%d") + "-" + "".join(_s.choice(_CODE_ALPHABET) for _ in range(4))
    out: list[str] = []
    with _conn() as c:
        while len(out) < n:
            code = "WD-" + "-".join("".join(_s.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(3))
            cur = c.execute("INSERT OR IGNORE INTO codes (code, days, note, batch) VALUES (?,?,?,?)",
                            (code, days, note, batch))
            if cur.rowcount:  # 0 = 撞库，重试
                out.append(code)
    return batch, out


def list_codes(batch: str | None = None) -> list[dict]:
    """列兑换码（可按批次过滤），带兑换人邮箱与 status：unused / used / voided。"""
    q = ("SELECT c.code, c.days, c.note, c.batch, c.used_by, c.used_at, c.voided, "
         "u.email AS used_by_email FROM codes c LEFT JOIN users u ON u.id = c.used_by")
    args: tuple = ()
    if batch:
        q += " WHERE c.batch=?"
        args = (batch,)
    q += " ORDER BY c.batch DESC, c.code"
    out = []
    with _conn() as c:
        for r in c.execute(q, args):
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
                 depth: int = -1, turns: int = -1) -> None:
    """记一笔 LLM 调用的真实 token 消耗与折算成本。depth/turns 为行为信号（-1=未上报）。"""
    with _conn() as c:
        c.execute(
            "INSERT INTO meter_requests (ts, day_key, user_id, plan, mode, style, stream, "
            "prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
            "completion_tokens, cost_cents, ok, depth, turns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), today(), user_id, plan, mode, style,
             1 if stream else 0,
             prompt_cache_hit_tokens + prompt_cache_miss_tokens,
             prompt_cache_hit_tokens, prompt_cache_miss_tokens,
             completion_tokens, cost_cents, 1 if ok else 0, depth, turns))


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
