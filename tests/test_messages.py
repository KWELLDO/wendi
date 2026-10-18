"""站内消息（messages.py + server 路由）测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import auth  # noqa: E402
import db  # noqa: E402
import messages  # noqa: E402
import server  # noqa: E402


class Handler(server.Handler):
    def __init__(self, token="test-token", ip="203.0.113.9"):
        self.status = None
        self.payload = None
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client_address = (ip, 12345)
        self.path = "/api/x"
        self._uid = None

    def _json(self, code, obj):
        self.status, self.payload = code, obj

    def _qs(self):
        return {}

    def _ip(self):
        return self.client_address[0]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_db = db.DB_PATH
        db.DB_PATH = Path(_tmp.name)
        db.init()
        messages.init()
        cls._old_token = server.ADMIN_TOKEN
        server.ADMIN_TOKEN = "test-token"

    @classmethod
    def tearDownClass(cls):
        server.ADMIN_TOKEN = cls._old_token

    def setUp(self):
        with db._conn() as c:
            for t in ("user_messages", "users", "sessions", "admin_log", "feedback"):
                c.execute(f"DELETE FROM {t}")
        messages.init()

    def mkuser(self, email, pw="secret1"):
        salt, h = auth.hash_password(pw)
        u = db.create_user(email, salt, h)
        self.assertIsNotNone(u)
        tok = "tok-" + email
        db.create_session(tok, u["id"])
        return u, tok


class TestMessagesDb(Base):
    def test_send_list_and_user_isolation(self):
        u1, _ = self.mkuser("a@example.com")
        u2, _ = self.mkuser("b@example.com")
        m1 = messages.send(u1["id"], "你使用的是什么浏览器？", feedback_id=7)
        messages.send(u2["id"], "另一条消息")
        rows = messages.list_for_user(u1["id"])
        self.assertEqual([r["id"] for r in rows], [m1])
        self.assertEqual(rows[0]["feedback_id"], 7)
        self.assertEqual(messages.unread_count(u1["id"]), 1)
        self.assertEqual(messages.unread_count(u2["id"]), 1)

    def test_mark_read_is_user_scoped(self):
        u1, _ = self.mkuser("a@example.com")
        u2, _ = self.mkuser("b@example.com")
        m1 = messages.send(u1["id"], "给 u1 的问题？")
        m2 = messages.send(u2["id"], "给 u2 的问题？")
        n = messages.mark_read(u1["id"], ids=[m1, m2])
        self.assertEqual(n, 1, "只能标记自己的消息")
        self.assertEqual(messages.unread_count(u1["id"]), 0)
        self.assertEqual(messages.unread_count(u2["id"]), 1)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            messages.send(1, "x")
        with self.assertRaises(ValueError):
            messages.send(1, "x" * (messages.MAX_BODY + 1))
        with self.assertRaises(ValueError):
            messages.send(1, "正常消息", source="nope")


class TestMessageRoutes(Base):
    def test_get_messages_requires_login_and_returns_only_own(self):
        u1, tok1 = self.mkuser("a@example.com")
        u2, _ = self.mkuser("b@example.com")
        messages.send(u1["id"], "给你的问题？")
        messages.send(u2["id"], "给别人的问题？")

        h = Handler(token="")
        h.path = "/api/messages"
        h._do_GET()
        self.assertEqual(h.status, 401)

        h = Handler(token=tok1)
        h.path = "/api/messages"
        h._do_GET()
        self.assertEqual(h.status, 200)
        self.assertEqual(len(h.payload["items"]), 1)
        self.assertEqual(h.payload["items"][0]["body"], "给你的问题？")
        self.assertEqual(h.payload["unread"], 1)

    def test_mark_read_route(self):
        u, tok = self.mkuser("a@example.com")
        mid = messages.send(u["id"], "问题？")
        h = Handler(token=tok)
        h._messages_read({"ids": [mid]})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["updated"], 1)
        self.assertEqual(messages.unread_count(u["id"]), 0)

    def test_admin_send_message_and_audit(self):
        u, _ = self.mkuser("a@example.com")
        h = Handler(token="test-token")
        h._admin_user_message({"user_id": u["id"], "body": "你使用的是什么浏览器？",
                               "feedback_id": 7})
        self.assertEqual(h.status, 200)
        rows = messages.list_for_user(u["id"])
        self.assertEqual(rows[0]["body"], "你使用的是什么浏览器？")
        self.assertEqual(rows[0]["source"], "agent")
        self.assertEqual(rows[0]["feedback_id"], 7)
        self.assertEqual(db.admin_log_list()[0]["action"], "user.message")

    def test_admin_send_rejects_bad_token_user_or_body(self):
        h = Handler(token="wrong")
        h._admin_user_message({"user_id": 1, "body": "你好"})
        self.assertEqual(h.status, 401)

        u, _ = self.mkuser("a@example.com")
        h = Handler(token="test-token")
        h._admin_user_message({"user_id": 99999, "body": "你好"})
        self.assertEqual(h.status, 404)

        h = Handler(token="test-token")
        h._admin_user_message({"user_id": u["id"], "body": "x"})
        self.assertEqual(h.status, 400)

        db.set_disabled(u["id"], True)
        h = Handler(token="test-token")
        h._admin_user_message({"user_id": u["id"], "body": "你好"})
        self.assertEqual(h.status, 409)

    def test_me_includes_unread_count(self):
        u, tok = self.mkuser("a@example.com")
        messages.send(u["id"], "问题？")
        h = Handler(token=tok)
        h.path = "/api/me"
        h._do_GET()
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["unread_messages"], 1)


if __name__ == "__main__":
    unittest.main()
