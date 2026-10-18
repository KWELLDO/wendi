"""问底 · 站内消息（最小实现）。

用户反馈的站内追问/回复走这里。独立建表，不把消息混进 feedback。
- 用户只能读自己的消息；
- 管理端按 user_id 发消息；
- 正文最长 1000 字；不保存联系方式（联系方式仍在 feedback 表，只有站长可见）。
"""
from __future__ import annotations

import db

MAX_BODY = 1000
SOURCES = ("agent", "admin")


def init() -> None:
    with db._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS user_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            feedback_id INTEGER,
            body TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent',
            created_at TEXT NOT NULL,
            read_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_messages_user
            ON user_messages(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_user_messages_unread
            ON user_messages(user_id, read_at);
        """)


def send(user_id: int, body: str, source: str = "agent",
         feedback_id: int | None = None) -> int:
    text = str(body or "").strip()
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id 必须是正整数")
    if len(text) < 2:
        raise ValueError("消息太短")
    if len(text) > MAX_BODY:
        raise ValueError(f"消息不能超过 {MAX_BODY} 字")
    if source not in SOURCES:
        raise ValueError("source 只能是 agent / admin")
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO user_messages (user_id, feedback_id, body, source, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, feedback_id, text, source, db.now()))
        return int(cur.lastrowid)


def list_for_user(user_id: int, limit: int = 50) -> list[dict]:
    with db._conn() as c:
        rows = c.execute(
            "SELECT id, feedback_id, body, source, created_at, read_at "
            "FROM user_messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(200, int(limit))))).fetchall()
    return [dict(r) for r in rows]


def unread_count(user_id: int) -> int:
    with db._conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM user_messages WHERE user_id=? AND read_at IS NULL",
            (user_id,)).fetchone()
    return int(row["n"])


def mark_read(user_id: int, ids: list[int] | None = None, all_: bool = False) -> int:
    with db._conn() as c:
        if all_:
            return c.execute(
                "UPDATE user_messages SET read_at=? "
                "WHERE user_id=? AND read_at IS NULL",
                (db.now(), user_id)).rowcount
        clean: list[int] = []
        for x in ids or []:
            if len(clean) >= 200:
                break
            try:
                clean.append(int(x))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        marks = ",".join("?" for _ in clean)
        return c.execute(
            f"UPDATE user_messages SET read_at=? "
            f"WHERE user_id=? AND read_at IS NULL AND id IN ({marks})",
            (db.now(), user_id, *clean)).rowcount
