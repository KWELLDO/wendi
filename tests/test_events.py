"""内测第一阶段：事件采集 / 邀请码注册 / 日聚合 / 看板端点的测试。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import auth  # noqa: E402
import db  # noqa: E402
import messages  # noqa: E402
import server  # noqa: E402


class Handler(server.Handler):
    """直调 handler：只补 _json / _ip / HTTP 响应三处依赖。"""

    def __init__(self, token="test-token", ip="203.0.113.9", sid="s_test"):
        self.status, self.payload = None, None
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        if sid:
            self.headers["X-Wendi-Session"] = sid
        self.client_address = (ip, 1234)
        self.path = "/api/x"
        self._uid = None
        self._error = ""
        self._cost_cents = 0
        self._t0 = 0.0

    def _json(self, code, obj):
        self.status, self.payload = code, obj

    def _body(self):
        return {}

    def _qs(self):
        return {}

    def _ok_empty(self):
        self.status = 200

    # 有些用例要看重定向后的 stderr（请求日志）
    def send_response(self, code, message=None):
        self.status = code


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_db = db.DB_PATH
        db.DB_PATH = Path(_tmp.name)
        db.init()
        messages.init()  # /api/me 返回 unread_messages，需要 user_messages 表
        cls._old_token = server.ADMIN_TOKEN
        server.ADMIN_TOKEN = "test-token"
        cls._old_require = server.REQUIRE_INVITE
        server.REQUIRE_INVITE = True

    @classmethod
    def tearDownClass(cls):
        server.ADMIN_TOKEN = cls._old_token
        server.REQUIRE_INVITE = cls._old_require

    def setUp(self):
        server._feedback_rate.clear()
        server._event_rate.clear()
        for t in ("events", "daily_metrics", "admin_log", "users", "sessions",
                  "codes", "syncs", "meter_requests", "user_messages"):
            self._exec(f"DELETE FROM {t}")

    @staticmethod
    def _exec(sql: str, args: tuple = ()) -> None:
        """一次性连接（用完就关，别在测试里堆 sqlite 句柄）。"""
        c = db._conn()
        try:
            c.execute(sql, args)
            c.commit()
        finally:
            c.close()

    def invite(self, days=30, label="A 研究者") -> str:
        _, codes = db.gen_codes(1, days, batch="ITEST", kind="invite", tester_label=label)
        return codes[0]

    def mkuser(self, email, code="") -> dict:
        salt, h = auth.hash_password("secret1")
        user, err, _ = db.register_with_invite(email, salt, h, code)
        self.assertIsNotNone(user, f"建号失败 {email}: {err}")
        return user


# ===================== 事件采集 =====================
class TestEventIngest(Base):
    def test_whitelist_and_silent_response(self):
        h = Handler()
        h._event_add({"event": "app_open", "meta": {"ref": "WD-X"}})
        self.assertEqual(h.status, 200)
        self.assertEqual(len(db.events_list()), 1)
        h2 = Handler()
        h2._event_add({"event": "evil_event"})
        self.assertEqual(h2.status, 200, "未知事件名也要静默 200（不打扰用户）")
        self.assertEqual(len(db.events_list()), 1, "未知事件名不落库")

    def test_raw_ip_never_stored(self):
        h = Handler(ip="198.51.100.42")
        h._event_add({"event": "app_open"})
        row = db.events_list()[0]
        self.assertEqual(len(row["ip_hash"]), 16)
        self.assertNotIn("198.51.100.42", json.dumps(row, ensure_ascii=False))

    def test_meta_capped(self):
        h = Handler()
        h._event_add({"event": "explain_success", "meta": {"blob": "x" * 5000}})
        raw = db.events_list()[0]["meta"]
        self.assertLessEqual(len(json.dumps(raw, ensure_ascii=False)), db.MAX_META_CHARS + 10)

    def test_body_never_recorded(self):
        """事件里不允许出现正文：只传动作与元数据，落库也只有这些。"""
        h = Handler()
        h._event_add({"event": "explain_success", "meta": {"latency_ms": 900,
                                                          "body": "这是提问正文不该被存"}})
        row = db.events_list()[0]
        self.assertEqual(row["meta"]["latency_ms"], 900)
        self.assertEqual(len(db.events_list(event="explain_success")), 1)

    def test_rate_limit_drops_silently(self):
        old = server.EVENT_PER_MIN
        server.EVENT_PER_MIN = 3
        try:
            for _ in range(3):
                Handler()._event_add({"event": "app_open"})
            h = Handler()
            h._event_add({"event": "app_open"})
            self.assertEqual(h.status, 200)
            self.assertEqual(len(db.events_list()), 3, "超频事件静默丢弃")
        finally:
            server.EVENT_PER_MIN = old

    def test_session_and_code_attribution(self):
        code = self.invite()
        u = self.mkuser("attr@example.com", code)
        tok = "tok-attr"
        db.create_session(tok, u["id"])
        h = Handler(token="")
        h.headers = {"Authorization": "Bearer " + tok, "X-Wendi-Session": "s_1"}
        h._event_add({"event": "explain_success"})          # 不带 code → 回落到账号的邀请码
        row = db.events_list()[0]
        self.assertEqual(row["user_id"], u["id"])
        self.assertEqual(row["code"], code)
        self.assertEqual(row["session_id"], "s_1")

    def test_depth_out_of_range_normalized(self):
        h = Handler()
        h._event_add({"event": "drill_down", "depth": 9999})
        self.assertEqual(db.events_list()[0]["depth"], -1)


# ===================== 聚合 / 漏斗 / 回访 =====================
class TestRollup(Base):
    def _seed(self, day, code, user_id=None, events=("app_open", "select_text", "explain_success")):
        for ev in events:
            db.event_add(ev, session_id="s_" + day, user_id=user_id, code=code)
        # 事件落在指定日期（造 D1 用）
        self._exec("UPDATE events SET day_key=? WHERE day_key!=?", (day, day))

    def test_rollup_counts_and_idempotent(self):
        code = self.invite()
        u = self.mkuser("roll@example.com", code)
        self._seed(db.today(), code, u["id"])
        db.meter_record(u["id"], "pro", "explain", "standard", True, 100, 10, 5, 3, True)
        m1 = db.rollup_day(db.today())
        m2 = db.rollup_day(db.today())      # 幂等：再算一次结果一致
        self.assertEqual(m1, m2)
        self.assertEqual(m1["app_opens"], 1)
        self.assertEqual(m1["explains"], 1)
        self.assertEqual(m1["sessions"], 1)
        self.assertEqual(m1["active_users"], 1, "同一个人不能算两次")
        self.assertEqual(m1["new_users"], 1)
        self.assertEqual(m1["llm_calls"], 1)
        self.assertEqual(m1["cost_cents"], 3)

    def test_funnel_distinct_sessions(self):
        db.event_add("select_text", session_id="s_a", code="WD-A")
        db.event_add("explain_success", session_id="s_a", code="WD-A")
        db.event_add("select_text", session_id="s_b", code="WD-A")
        f = db.funnel(db.today())
        self.assertEqual(f["select_text"], 2)
        self.assertEqual(f["explain_success"], 1)

    def test_d1_return_across_days(self):
        code = self.invite()
        yesterday = db._day_shift(db.today(), -1)
        db.event_add("app_open", session_id="s1", code=code)
        db.event_add("app_open", session_id="s2", code=code)
        self._exec("UPDATE events SET day_key=? WHERE session_id='s1'", (yesterday,))
        m = db.rollup_day(db.today())
        self.assertEqual(m["app_opens"], 1)
        self.assertEqual(m["d1_return"], 1, "昨天来过、今天又来 = D1 回访")

    def test_errors_counted(self):
        db.event_add("system_error", meta={"error": "boom"})
        self.assertEqual(db.rollup_day(db.today())["errors"], 1)

    def test_prune_keeps_recent(self):
        db.event_add("app_open")
        self._exec("INSERT INTO events (ts, day_key, event) VALUES (0,'2000-01-01','app_open')")
        self.assertEqual(db.prune_events(90), 1)
        self.assertEqual(len(db.events_list()), 1)

    def test_tester_rows_signals(self):
        code = self.invite(days=30, label="B 学生")
        u = self.mkuser("tester@example.com", code)
        d1 = db._day_shift(db.today(), -2)
        for day, sid in ((d1, "s1"), (db.today(), "s2")):
            db.event_add("app_open", session_id=sid, code=code, user_id=u["id"])
            db.event_add("explain_success", session_id=sid, code=code, user_id=u["id"])
            self._exec("UPDATE events SET day_key=? WHERE session_id=?", (day, sid))
        db.meter_record(u["id"], "pro", "explain", "standard", True, 0, 0, 0, 7, True)
        row = next(r for r in db.tester_rows() if r["code"] == code)
        self.assertEqual(row["tester_label"], "B 学生")
        self.assertEqual(row["email"], "tester@example.com")
        self.assertEqual(row["explains"], 2)
        self.assertTrue(row["returned"])
        self.assertTrue(row["d1"], "隔两天再回来应算回访")
        self.assertEqual(row["cost_cents"], 7)


# ===================== 邀请码注册 =====================
class TestInviteRegister(Base):
    def test_requires_invite_code(self):
        h = Handler(token="")
        h._register({"email": "no@code.cn", "password": "secret1"})
        self.assertEqual(h.status, 403)
        self.assertTrue(h.payload["need_invite"])
        self.assertEqual(len(db.list_users()), 0)

    def test_bad_and_reused_code(self):
        h = Handler(token="")
        h._register({"email": "a@code.cn", "password": "secret1", "invite": "WD-NOPE-NOPE-NOPE"})
        self.assertEqual(h.status, 403)
        code = self.invite()
        h2 = Handler(token="")
        h2._register({"email": "b@code.cn", "password": "secret1", "invite": code})
        self.assertEqual(h2.status, 200)
        h3 = Handler(token="")
        h3._register({"email": "c@code.cn", "password": "secret1", "invite": code})
        self.assertEqual(h3.status, 403)
        self.assertIn("用过", h3.payload["error"])

    def test_pro_code_rejected_as_invite(self):
        _, codes = db.gen_codes(1, 30, kind="pro")
        h = Handler(token="")
        h._register({"email": "d@code.cn", "password": "secret1", "invite": codes[0]})
        self.assertEqual(h.status, 403)
        self.assertIn("兑换码", h.payload["error"])

    def test_success_grants_pro_and_records_attribution(self):
        code = self.invite(days=30)
        h = Handler(token="")
        h._register({"email": "test1@example.com", "password": "secret1", "invite": code})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["plan"], "pro")
        self.assertEqual(h.payload["invite_days"], 30)
        user = db.get_user_by_email("test1@example.com")
        self.assertEqual(user["invite_code"], code)
        self.assertTrue(user["plan_until"])
        rows = db.list_codes(kind="invite")
        self.assertEqual(rows[0]["status"], "used")
        # 注册事件落表（带码，便于按人看路径）
        ev = db.events_list(event="register")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["code"], code)

    def test_zero_day_invite_gives_free_account(self):
        code = self.invite(days=0)
        h = Handler(token="")
        h._register({"email": "free@example.com", "password": "secret1", "invite": code})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["plan"], "free")

    def test_open_registration_when_switch_off(self):
        server.REQUIRE_INVITE = False
        try:
            h = Handler(token="")
            h._register({"email": "open@example.com", "password": "secret1"})
            self.assertEqual(h.status, 200)
        finally:
            server.REQUIRE_INVITE = True

    def test_duplicate_email_conflict(self):
        code1, code2 = self.invite(), self.invite()
        a = Handler(token="")
        a._register({"email": "dup@example.com", "password": "secret1", "invite": code1})
        self.assertEqual(a.status, 200)
        b = Handler(token="")
        b._register({"email": "dup@example.com", "password": "secret1", "invite": code2})
        self.assertEqual(b.status, 409)
        self.assertEqual(next(c for c in db.list_codes(kind="invite")
                              if c["code"] == code2)["status"], "unused",
                         "注册失败的码不该被消耗")


# ===================== 配额响应形状（登录/注册/兑换 vs /api/me） =====================
class TestQuotaShape(Base):
    def _register_pro(self, email="pro-quota@example.com"):
        code = self.invite(days=30)
        h = Handler(token="")
        h._register({"email": email, "password": "secret1", "invite": code})
        self.assertEqual(h.status, 200)
        return h

    def test_register_pro_quota_has_plan(self):
        h = self._register_pro()
        self.assertEqual(h.payload["plan"], "pro")
        self.assertEqual(h.payload["quota"]["plan"], "pro")
        self.assertEqual(h.payload["quota"]["limit"], db.QUOTA_PRO)

    def test_login_quota_matches_me(self):
        self._register_pro("match@example.com")
        h = Handler(token="")
        h._login({"email": "match@example.com", "password": "secret1"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["quota"]["plan"], "pro")

        me = Handler(token=h.payload["token"])
        me.path = "/api/me"
        me._do_GET()
        self.assertEqual(me.status, 200)
        self.assertEqual(h.payload["quota"], me.payload["quota"],
                         "登录与 /api/me 的 quota 必须同形同值（前端读 quota.plan）")

    def test_free_and_expired_pro_report_free(self):
        code = self.invite(days=0)
        h = Handler(token="")
        h._register({"email": "free-quota@example.com", "password": "secret1", "invite": code})
        self.assertEqual(h.payload["quota"]["plan"], "free")

        u = self.mkuser("expired-quota@example.com")
        db.set_plan(u["id"], "pro", "2020-01-01")
        h2 = Handler(token="")
        h2._login({"email": "expired-quota@example.com", "password": "secret1"})
        self.assertEqual(h2.payload["plan"], "free")
        self.assertEqual(h2.payload["quota"]["plan"], "free")
        self.assertEqual(h2.payload["quota"]["limit"], db.QUOTA_FREE)

    def test_redeem_quota_has_plan(self):
        u = self.mkuser("redeem-quota@example.com")
        tok = "tok-redeem-quota"
        db.create_session(tok, u["id"])
        _, codes = db.gen_codes(1, 30, kind="pro")
        h = Handler(token=tok)
        h._redeem({"code": codes[0]})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["quota"]["plan"], "pro")
        self.assertEqual(h.payload["quota"]["limit"], db.QUOTA_PRO)


# ===================== 看板端点 =====================
class TestAdminIntakeEndpoints(Base):
    def test_all_require_token(self):
        for call in (lambda h: h._admin_metrics({}),
                     lambda h: h._admin_overview()):
            g = Handler(token="wrong")
            call(g)
            self.assertEqual(g.status, 401)

    def test_metrics_shape_and_bad_day(self):
        db.event_add("app_open", session_id="s1", code="WD-X")
        h = Handler()
        h._admin_metrics({})
        self.assertEqual(h.status, 200)
        d = h.payload
        for k in ("day", "metrics", "funnel", "recent", "state", "cap_cents",
                  "cost_today_cents", "thresholds"):
            self.assertIn(k, d)
        self.assertEqual(d["metrics"]["app_opens"], 1)
        bad = Handler()
        bad._admin_metrics({"day": ["2026/09/27"]})
        self.assertEqual(bad.status, 400)

    def test_metrics_persist_to_table(self):
        db.event_add("explain_success", session_id="s1")
        h = Handler()
        h._admin_metrics({})
        self.assertIsNotNone(db.metrics_get(db.today()))
        self.assertEqual(db.metrics_get(db.today())["explains"], 1)

    def test_codes_kind_filter_and_generation(self):
        h = Handler()
        h.payload = None
        h._admin_gen_codes({"n": 2, "days": 30, "kind": "invite", "tester_label": "C 老师"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["kind"], "invite")
        rows = db.list_codes(kind="invite")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["tester_label"] == "C 老师" for r in rows))
        self.assertEqual(db.list_codes(kind="pro"), [])
        bad = Handler()
        bad._admin_gen_codes({"n": 1, "days": 30, "kind": "weird"})
        self.assertEqual(bad.status, 400)

    def test_invite_code_can_be_voided(self):
        code = self.invite()
        db.void_codes([code])
        h = Handler(token="")
        h._register({"email": "v@example.com", "password": "secret1", "invite": code})
        self.assertEqual(h.status, 403)
        self.assertIn("作废", h.payload["error"])


# ===================== 请求日志 =====================
class TestRequestLog(Base):
    def test_json_line_shape(self):
        h = Handler(ip="203.0.113.9")
        h.command, h.path, h._status = "POST", "/api/explain?x=1", 200
        h._uid, h._cost_cents = 7, 12
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            h.log_request(200, 128)
        line = json.loads(buf.getvalue().strip())
        self.assertEqual(line["kind"], "http")
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["path"], "/api/explain", "路径不带查询串")
        self.assertEqual(line["status"], 200)
        self.assertEqual(line["user_id"], 7)
        self.assertEqual(line["cost_cents"], 12)
        self.assertEqual(line["session_id"], "s_test")
        self.assertIn("latency_ms", line)

    def test_error_line_flags_error(self):
        h = Handler()
        h.command, h.path, h._status, h._error = "GET", "/api/x", 500, "ValueError: boom"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            h.log_request(500, 0)
        line = json.loads(buf.getvalue().strip())
        self.assertEqual(line["level"], "error")
        self.assertIn("boom", line["error"])


# ===================== explain_success 附着成本 =====================
class TestCostAttach(Base):
    def test_success_event_gets_server_side_cost(self):
        u = self.mkuser("cost@example.com")
        db.meter_record(u["id"], "free", "explain", "standard", True, 100, 20, 10, 9, True,
                        session_id="s_cost")
        h = Handler(sid="s_cost")
        h._event_add({"event": "explain_success", "meta": {"latency_ms": 800}})
        row = db.events_list(event="explain_success")[0]
        self.assertEqual(row["meta"]["cost_cents"], 9, "成本由服务端附着，前端不传")
        self.assertEqual(row["meta"]["latency_ms"], 800)

    def test_no_cost_when_session_unknown(self):
        h = Handler(sid="s_other")
        h._event_add({"event": "explain_success", "meta": {}})
        self.assertNotIn("cost_cents", db.events_list(event="explain_success")[0]["meta"])

    def test_cost_not_attached_to_other_events(self):
        u = self.mkuser("cost2@example.com")
        db.meter_record(u["id"], "free", "explain", "standard", True, 0, 0, 0, 5, True,
                        session_id="s_x")
        h = Handler(sid="s_x")
        h._event_add({"event": "drill_down", "meta": {}})
        self.assertNotIn("cost_cents", db.events_list(event="drill_down")[0]["meta"])


# ===================== 流式路径也要落 session_id（回归：曾在闭包里引用 body 直接 500） =====================
class _FakeResp:
    """假的上游 SSE 响应：可迭代的字节行 + close()。"""

    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class StreamHandler(server.Handler):
    def __init__(self, sid="s_stream"):
        self.status, self.payload = None, None
        self.headers = {"X-Wendi-Session": sid}
        self.path = "/api/explain"
        self.client_address = ("203.0.113.30", 1234)
        self._cost_cents, self._error = 0, ""
        self.sent = []
        self.wfile = self

    # wfile 替身
    def write(self, data):
        self.sent.append(data)

    def flush(self):
        pass

    # HTTP 替身
    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def _json(self, code, obj):
        self.status, self.payload = code, obj

    def _quota_payload(self, user):
        return {"plan": "free", "used": 1, "limit": 40}


class TestStreamUsage(Base):
    def test_stream_records_session_and_room(self):
        u = self.mkuser("stream@example.com")
        rows = [
            b'data: {"choices":[{"delta":{"content":"\\u71b5"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"\\u51cf"}}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b' "usage":{"prompt_tokens":300000,"prompt_cache_hit_tokens":200000,'
            b'"prompt_cache_miss_tokens":100000,"completion_tokens":100000}}\n',
            b"data: [DONE]\n",
        ]
        h = StreamHandler(sid="s_stream")
        with mock.patch.object(server, "call_llm_stream", return_value=_FakeResp(rows)):
            h._explain_stream(u, [{"role": "user", "content": "x"}], 500,
                              mode="explain", term="熵", depth=2, turns=0,
                              session_id="s_stream", room_id="room-1")
        self.assertIsNone(h.payload, "正常路径不该出错误响应")
        self.assertTrue(any(b'"done": true' in b for b in h.sent), "应发出 done 事件")
        row = db._conn().execute(
            "SELECT session_id, room_id, depth, ok FROM meter_requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["session_id"], "s_stream")
        self.assertEqual(row["room_id"], "room-1")
        self.assertEqual(row["depth"], 2)
        self.assertEqual(row["ok"], 1)
        self.assertGreater(h._cost_cents, 0, "记录成本应回写到 handler（给请求日志用）")

    def test_stream_without_session_still_works(self):
        u = self.mkuser("stream2@example.com")
        h = StreamHandler()
        with mock.patch.object(server, "call_llm_stream",
                                        return_value=_FakeResp([b"data: [DONE]\n"])):
            h._explain_stream(u, [], 100, session_id="", room_id="")
        self.assertEqual(h.status, 200)


# ===================== 邀请制闸门：内测期未登录不给用 =====================
class TestInviteGate(Base):
    def _explain_handler(self):
        class H(Handler):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.body = {}
        return H(token="")

    def test_anonymous_explain_blocked_when_invite_only(self):
        h = self._explain_handler()
        h._explain({"mode": "chat", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(h.status, 403)
        self.assertTrue(h.payload["need_invite"])
        # 被门挡住也算信号：落一条 gate_blocked，能看到「有多少人想用但没码」
        ev = db.events_list(event="gate_blocked")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["meta"]["mode"], "chat")
        row = db._conn().execute("SELECT COUNT(*) AS n FROM meter_requests").fetchone()
        self.assertEqual(row["n"], 0, "被闸门拦住不该产生 LLM 调用")

    def test_anonymous_allowed_when_switch_off(self):
        server.REQUIRE_INVITE = False
        try:
            h = self._explain_handler()
            # 打桩 LLM：不花钱，只验证闸门放行到配额/调用逻辑
            with mock.patch.object(server, "call_llm",
                                   return_value=("答案", {"prompt_tokens": 10,
                                                          "completion_tokens": 5}, False)):
                h._explain({"mode": "chat", "messages": [{"role": "user", "content": "hi"}],
                            "turns": 1})
            self.assertEqual(h.status, 200, f"开关关掉后匿名应可用：{h.payload}")
            self.assertEqual(db.events_list(event="gate_blocked"), [])
        finally:
            server.REQUIRE_INVITE = True

    def test_feedback_still_open_without_code(self):
        """没码的人至少能留言要码——闸门不能把反馈也关掉。"""
        h = Handler(token="")
        h._feedback_add({"body": "我想要一个邀请码", "contact": "me@example.com", "can_reply": True})
        self.assertEqual(h.status, 200)
