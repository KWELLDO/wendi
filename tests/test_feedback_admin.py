"""反馈入口 + 站长后台（总览/用户/反馈/闸门/审计/云删）的测试。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# 独立 temp DB：不与其它测试文件共享，避免互相污染断言
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import auth  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


class Handler(server.Handler):
    """绕过 HTTP 直调 handler 方法；只补齐 _json/_body/_ip 三处依赖。"""

    def __init__(self, token="test-token", ip="203.0.113.9"):
        self.status = None
        self.payload = None
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client_address = (ip, 12345)
        self.path = "/api/x"

    def _json(self, code, obj):
        self.status, self.payload = code, obj

    def _body(self):
        return {}

    def _qs(self):
        return {}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_db = db.DB_PATH
        db.DB_PATH = Path(_tmp.name)
        db.init()
        cls._old_token = server.ADMIN_TOKEN
        server.ADMIN_TOKEN = "test-token"

    @classmethod
    def tearDownClass(cls):
        server.ADMIN_TOKEN = cls._old_token

    def setUp(self):
        server._feedback_rate.clear()
        # 每个用例一套干净数据：本文件大量断言计数，共享数据会让结果随执行顺序漂移
        with db._conn() as c:
            for t in ("feedback", "admin_log", "users", "sessions", "codes",
                      "syncs", "meter_requests", "meter_config"):
                c.execute(f"DELETE FROM {t}")
        db.init()   # 重新播回 meter_config 默认单价/闸门

    def mkuser(self, email, pw="secret1", plan="free", days=0) -> dict:
        salt, h = auth.hash_password(pw)
        u = db.create_user(email, salt, h)
        self.assertIsNotNone(u, f"建号失败 {email}")
        if plan == "pro":
            import time as _t
            ts = _t.mktime(_t.strptime(db.today(), "%Y-%m-%d")) + days * 86400
            db.set_plan(u["id"], "pro", _t.strftime("%Y-%m-%d", _t.localtime(ts)))
        return u


# ===================== 反馈（公开端点） =====================
class TestFeedbackSubmit(Base):
    def test_submit_stores_fields(self):
        h = Handler()
        h._feedback_add({"kind": "bug", "body": "气泡跑到屏幕外了", "contact": "a@b.cn", "can_reply": True})
        self.assertEqual(h.status, 200)
        self.assertTrue(h.payload["id"])
        rows = db.feedback_list()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["kind"], "bug")
        self.assertEqual(r["body"], "气泡跑到屏幕外了")
        self.assertEqual(r["contact"], "a@b.cn")
        self.assertEqual(r["can_reply"], 1)
        self.assertEqual(r["status"], "new")
        self.assertEqual(r["ip"], "203.0.113.9")
        self.assertIsNone(r["user_id"])
        self.assertEqual(db.feedback_stats()["new"], 1)

    def test_logged_in_feedback_carries_user(self):
        u = self.mkuser("fb@example.com")
        tok = "tok-fb"
        db.create_session(tok, u["id"])
        h = Handler(token="")                       # 用会话 token，不用管理 token
        h.headers = {"Authorization": "Bearer " + tok}
        h._feedback_add({"kind": "idea", "body": "希望解释卡能标出歧义", "can_reply": False})
        self.assertEqual(h.status, 200)
        row = db.feedback_list()[0]
        self.assertEqual(row["user_id"], u["id"])
        self.assertEqual(row["email"], "fb@example.com")

    def test_too_short_and_too_long_rejected(self):
        h = Handler()
        h._feedback_add({"body": "x"})
        self.assertEqual(h.status, 400)
        h = Handler()
        h._feedback_add({"body": "y" * (server.FEEDBACK_MAX + 1)})
        self.assertEqual(h.status, 400)
        self.assertEqual(db.feedback_list(), [], "被拒的请求不该落库")

    def test_bad_kind_rejected(self):
        h = Handler()
        h._feedback_add({"kind": "spam", "body": "正经内容"})
        self.assertEqual(h.status, 400)

    def test_can_reply_requires_contact(self):
        h = Handler()
        h._feedback_add({"body": "正经内容", "can_reply": True})
        self.assertEqual(h.status, 400)
        self.assertEqual(db.feedback_list(), [])

    def test_interval_and_daily_quota(self):
        h = Handler(ip="198.51.100.7")
        h._feedback_add({"body": "第一条"})
        self.assertEqual(h.status, 200)
        h2 = Handler(ip="198.51.100.7")
        h2._feedback_add({"body": "立刻又来一条"})
        self.assertEqual(h2.status, 429, "同一 IP 应有最小间隔")
        # 模拟跨过间隔：直接改内存里的 last
        server._feedback_rate["198.51.100.7"]["last"] = 0.0
        for i in range(server.FEEDBACK_PER_DAY - 1):
            hx = Handler(ip="198.51.100.7")
            hx._feedback_add({"body": f"第 {i} 条"})
            self.assertEqual(hx.status, 200)
            server._feedback_rate["198.51.100.7"]["last"] = 0.0
        hz = Handler(ip="198.51.100.7")
        hz._feedback_add({"body": "超出日额"})
        self.assertEqual(hz.status, 429, "超过每日条数应拒")
        self.assertEqual(len(db.feedback_list()), server.FEEDBACK_PER_DAY)


# ===================== 管理端点鉴权 =====================
class TestAdminAuth(Base):
    def test_each_admin_endpoint_rejects_bad_token(self):
        h = Handler(token="wrong")
        h._admin_overview()
        self.assertEqual(h.status, 401)
        for call in (
            lambda x: x._admin_user_disable({"user_id": 1, "disabled": True}),
            lambda x: x._admin_feedback_status({"id": 1, "status": "read"}),
            lambda x: x._admin_meter_config_set({"set": {"cost_cents_cap": 100}}),
        ):
            g = Handler(token="wrong")
            call(g)
            self.assertEqual(g.status, 401)

    def test_no_admin_token_configured_rejects(self):
        old = server.ADMIN_TOKEN
        server.ADMIN_TOKEN = ""      # 服务端没配令牌 → 一律拒绝（"空令牌=放行"是安全漏洞）
        try:
            h = Handler(token="")
            h._admin_overview()
            self.assertEqual(h.status, 401)
        finally:
            server.ADMIN_TOKEN = old


# ===================== 总览 / 用户 / 停用 =====================
class TestOverviewAndUsers(Base):
    def test_overview_shape(self):
        h = Handler()
        h._admin_overview()
        self.assertEqual(h.status, 200)
        d = h.payload
        for k in ("day", "model", "state", "cost_cents", "cap_cents", "hard_cents",
                  "calls_today", "users", "feedback", "codes", "meter", "feedback_new"):
            self.assertIn(k, d)
        self.assertEqual(db.feedback_stats()["new"], 0)
        self.assertEqual(d["users"]["total"], len(db.list_users()))

    def test_overview_lists_only_new_feedback(self):
        h = Handler()
        h._feedback_add({"body": "第一条"})
        f2 = Handler(ip="203.0.113.10")      # 换 IP：同一 IP 有最小间隔
        f2._feedback_add({"body": "第二条"})
        db.feedback_set_status(db.feedback_list()[1]["id"], "done")
        g = Handler()
        g._admin_overview()
        ids = [x["id"] for x in g.payload["feedback_new"]]
        self.assertEqual(len(ids), 1, "总览只该列出未读反馈")

    def test_user_list_effective_plan_and_search(self):
        u = self.mkuser("pro@example.com", plan="pro", days=30)
        self.mkuser("expired@example.com", plan="pro", days=-1)   # 已过期
        rows = {r["email"]: r for r in db.list_users()}
        self.assertEqual(rows["pro@example.com"]["plan_effective"], "pro")
        self.assertEqual(rows["expired@example.com"]["plan_effective"], "free",
                         "过期 Pro 应回落 free")
        self.assertEqual(len(db.list_users("pro@")), 1)
        self.assertEqual(db.list_users("pro@")[0]["id"], u["id"])

    def test_user_list_reports_today_usage_only(self):
        u = self.mkuser("usage@example.com")
        db.consume_quota(u["id"], 40)
        row = next(r for r in db.list_users() if r["id"] == u["id"])
        self.assertEqual(row["day_used"], 1)
        # 昨天的用量不该算进今天
        with db._conn() as c:
            c.execute("UPDATE users SET day_key='2000-01-01', day_used=39 WHERE id=?", (u["id"],))
        row = next(r for r in db.list_users() if r["id"] == u["id"])
        self.assertEqual(row["day_used"], 0)

    def test_disable_kills_sessions_and_blocks_login(self):
        u = self.mkuser("off@example.com")
        tok = "tok-off"
        db.create_session(tok, u["id"])
        h = Handler()
        h._admin_user_disable({"user_id": u["id"], "disabled": True, "reason": "滥用"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["sessions_dropped"], 1)
        # 已有 token 立即失效
        sess = Handler()
        sess.headers = {"Authorization": "Bearer " + tok}
        self.assertIsNone(sess._user(), "被停用账号的会话必须失效")
        # 重新登录被拒
        lg = Handler()
        lg._login({"email": "off@example.com", "password": "secret1"})
        self.assertEqual(lg.status, 403)
        # 恢复后能登录
        h2 = Handler()
        h2._admin_user_disable({"user_id": u["id"], "disabled": False})
        self.assertEqual(h2.status, 200)
        lg2 = Handler()
        lg2._login({"email": "off@example.com", "password": "secret1"})
        self.assertEqual(lg2.status, 200)
        self.assertTrue(lg2.payload["token"])

    def test_disable_bad_input(self):
        u = self.mkuser("bad@example.com")
        h = Handler()
        h._admin_user_disable({"disabled": True})
        self.assertEqual(h.status, 400, "缺 user_id")
        h = Handler()
        h._admin_user_disable({"user_id": u["id"]})
        self.assertEqual(h.status, 400, "缺 disabled")
        h = Handler()
        h._admin_user_disable({"user_id": 999999, "disabled": True})
        self.assertEqual(h.status, 404)


# ===================== 反馈管理 =====================
class TestFeedbackAdmin(Base):
    def test_status_transitions_and_note(self):
        h = Handler()
        h._feedback_add({"body": "一条反馈"})
        fid = db.feedback_list()[0]["id"]
        a = Handler()
        a._admin_feedback_status({"id": fid, "status": "done", "note": "已修，下版发"})
        self.assertEqual(a.status, 200)
        row = db.feedback_list()[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["note"], "已修，下版发")
        self.assertEqual(db.feedback_stats()["done"], 1)
        self.assertEqual(db.feedback_stats()["new"], 0)

    def test_bad_status_and_missing_id(self):
        h = Handler()
        h._feedback_add({"body": "一条反馈"})
        fid = db.feedback_list()[0]["id"]
        a = Handler()
        a._admin_feedback_status({"id": fid, "status": "closed"})
        self.assertEqual(a.status, 400)
        b = Handler()
        b._admin_feedback_status({"id": 99999, "status": "read"})
        self.assertEqual(b.status, 404)

    def test_note_preserves_status(self):
        h = Handler()
        h._feedback_add({"body": "一条反馈"})
        fid = db.feedback_list()[0]["id"]
        a = Handler()
        a._admin_feedback_status({"id": fid, "status": "read"})
        b = Handler()
        b._admin_feedback_status({"id": fid, "status": "read", "note": "看过，等复现"})
        self.assertEqual(b.status, 200)
        self.assertEqual(db.feedback_list()[0]["status"], "read")


# ===================== 闸门热改 =====================
class TestMeterConfigEndpoint(Base):
    def test_whitelist_only(self):
        h = Handler()
        h._admin_meter_config_set({"set": {"llm_api_keys": "sk-leak-000000000000"}})
        self.assertEqual(h.status, 400, "密钥类键必须不在闸门端点里")
        self.assertIsNone(db.meter_get("llm_api_keys"))

    def test_requires_set_object(self):
        for body in ({}, {"set": {}}, {"set": "cost_cents_cap=1"}):
            h = Handler()
            h._admin_meter_config_set(body)
            self.assertEqual(h.status, 400)

    def test_numeric_validation(self):
        h = Handler()
        h._admin_meter_config_set({"set": {"cost_cents_cap": "abc"}})
        self.assertEqual(h.status, 400)
        h = Handler()
        h._admin_meter_config_set({"set": {"cost_cents_cap": "-1"}})
        self.assertEqual(h.status, 400)
        h = Handler()
        h._admin_meter_config_set({"set": {"degrade_max_tokens": "nan"}})
        self.assertEqual(h.status, 400)

    def test_cap_below_existing_hard_is_allowed(self):
        h = Handler()
        h._admin_meter_config_set({"set": {"cost_cents_cap": 5000}})   # 默认 hard=6000 > 5000
        self.assertEqual(h.status, 200)
        self.assertEqual(db.meter_get("cost_cents_cap"), "5000")
        self.assertEqual(server._cost_caps(), (5000, 6000))

    def test_hard_not_greater_than_cap_rejected(self):
        h = Handler()
        h._admin_meter_config_set({"set": {"cost_cents_cap": 6000, "cost_cents_hard": 6000}})
        self.assertEqual(h.status, 400)
        self.assertEqual(db.meter_get("cost_cents_cap"), "3000", "非法请求不该改库")
        self.assertEqual(db.meter_get("cost_cents_hard"), "6000")

    def test_valid_patch_applies_and_is_readable(self):
        h = Handler()
        h._admin_meter_config_set({"set": {"cost_cents_cap": 500, "cost_cents_hard": 900,
                                           "degrade_min_interval": "30", "usd_to_cny": "7.15"}})
        self.assertEqual(h.status, 200)
        self.assertEqual(db.meter_get("cost_cents_cap"), "500")
        self.assertEqual(db.meter_get("degrade_min_interval"), "30")
        self.assertEqual(h.payload["set"]["cost_cents_hard"], "900")
        self.assertEqual(server._cost_caps(), (500, 900))

    def test_config_endpoint_exposes_whitelist_values(self):
        db.meter_set("cost_cents_cap", "1234")
        # GET /api/meter/config 的 config 字段就是这段拼装
        vals = {k: db.meter_get(k) for k in server.METER_CONFIG_KEYS}
        self.assertEqual(vals["cost_cents_cap"], "1234")
        self.assertIn("flash_in_miss_peak", vals)


# ===================== 审计日志 =====================
class TestAuditLog(Base):
    def test_mutations_are_logged(self):
        h = Handler()
        h._admin_gen_codes({"n": 2, "days": 7, "note": "审计"})
        self.assertEqual(h.status, 200)
        u = self.mkuser("audit@example.com")
        d = Handler()
        d._admin_user_disable({"user_id": u["id"], "disabled": True, "reason": "测试"})
        rows = {r["action"] for r in db.admin_log_list()}
        self.assertIn("codes.gen", rows)
        self.assertIn("user.disable", rows)
        top = db.admin_log_list()[0]
        self.assertEqual(top["ip"], "203.0.113.9")

    def test_audit_not_written_for_rejected_calls(self):
        h = Handler(token="wrong")
        h._admin_gen_codes({"n": 2, "days": 7})
        self.assertEqual(h.status, 401)
        self.assertEqual(db.admin_log_list(), [], "鉴权失败不该留管理动作")


# ===================== 云同步删除（数据主权） =====================
class TestSyncDelete(Base):
    def test_put_then_delete(self):
        u = self.mkuser("sync@example.com")
        tok = "tok-sync"
        db.create_session(tok, u["id"])
        put = Handler()
        put.headers = {"Authorization": "Bearer " + tok}
        put._sync_put({"data": '{"root":1}'})
        self.assertEqual(put.status, 200)
        self.assertTrue(db.sync_get(u["id"]))

        dele = Handler()
        dele.headers = {"Authorization": "Bearer " + tok}
        dele._sync_delete()
        self.assertEqual(dele.status, 200)
        self.assertTrue(dele.payload["deleted"])
        self.assertIsNone(db.sync_get(u["id"]))
        # 幂等：再删一次不报错、deleted=False
        again = Handler()
        again.headers = {"Authorization": "Bearer " + tok}
        again._sync_delete()
        self.assertEqual(again.status, 200)
        self.assertFalse(again.payload["deleted"])

    def test_delete_requires_login(self):
        h = Handler(token="")
        h.headers = {}
        h._sync_delete()
        self.assertEqual(h.status, 401)
