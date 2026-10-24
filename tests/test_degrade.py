"""P0（降级续命 + 成本闸三档 + 行为埋点）的测试。运行：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 独立 temp DB：不与 test_admin/test_metering 共享，避免互相污染断言
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
_OLD_DB = None

import auth  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


def _reset_state():
    server._anon.clear()
    server._degrade_last.clear()
    server._global.update(day=0, daykey=server._today())
    server._cost_global.update(daykey=server._today(), cost_cents=0,
                               alerted=False, hard_alerted=False)


class P0Base(unittest.TestCase):
    _seq = 0

    @classmethod
    def setUpClass(cls):
        global _OLD_DB
        _OLD_DB = db.DB_PATH
        db.DB_PATH = Path(_tmp.name)
        db.init()
        cls._old_token = server.ADMIN_TOKEN

    def setUp(self):
        _reset_state()

    @classmethod
    def tearDownClass(cls):
        server.ADMIN_TOKEN = cls._old_token
        db.DB_PATH = _OLD_DB

    def _mkuser(self, email: str | None = None, pw: str = "secret1") -> dict:
        type(self)._seq += 1
        email = email or f"p0-{type(self)._seq}-{os.getpid()}@example.com"
        salt, h = auth.hash_password(pw)
        u = db.create_user(email, salt, h)
        self.assertIsNotNone(u, f"建号失败 {email}")
        return u

    def _exhaust(self, uid: int, limit: int) -> None:
        """把用户当日配额打满。"""
        for _ in range(limit + 1):
            db.consume_quota(uid, limit)


class FakeExplainHandler(server.Handler):
    """直接调 _explain（绕过 HTTP），LLM 一律打桩。"""

    def __init__(self, body=None, user=None, ip="10.0.0.1"):
        self.body = body or {}
        self.status = None
        self.payload = None
        self.headers = {}
        self.path = "/api/explain"
        self.client_address = (ip, 12345)
        self._user_obj = user
        self.calls = []  # (max_tokens)

    def _json(self, code, obj):
        self.status, self.payload = code, obj

    def _body(self):
        return self.body

    def _user(self):
        return self._user_obj

    def _qs(self):
        return {}

    def _quota_payload(self, user):
        if user:
            st = db.quota_state(user)
            return {"plan": db.effective_plan(user), "used": st["used"], "limit": st["limit"]}
        return {"plan": "anon", "used": 0, "limit": db.QUOTA_ANON}

    def _ip(self):
        return self.client_address[0]


class TestCheckAnon(P0Base):
    def test_fresh_ip_ok(self):
        st, msg = server.check_anon("1.1.1.1")
        self.assertEqual(st, "ok")

    def test_over_quota_degrades_not_blocks(self):
        # 直接预置超限状态，避免连打撞上 6/min 限流（那条路径单独测）
        h = server._anon["2.2.2.2"]
        h.update(day=db.QUOTA_ANON, daykey=server._today(), minute=[])
        st, _ = server.check_anon("2.2.2.2")
        self.assertEqual(st, "degraded", "超体验额度必须降级而非 429")
        st, _ = server.check_anon("2.2.2.2")
        self.assertEqual(st, "degraded")

    def test_minute_rate_blocked(self):
        for _ in range(server.LIMIT_ANON_PER_MIN):
            server.check_anon("3.3.3.3")
        st, msg = server.check_anon("3.3.3.3")
        self.assertEqual(st, "blocked")
        self.assertIn("太快", msg)

    def test_global_day_limit_blocked(self):
        server._global.update(day=server.LIMIT_GLOBAL_DAY, daykey=server._today())
        st, msg = server.check_anon("4.4.4.4")
        self.assertEqual(st, "blocked")
        self.assertIn("总量", msg)


class TestCostGate(P0Base):
    def test_state_normal_below_cap(self):
        self.assertEqual(server._cost_state(), "normal")

    def test_state_degraded_over_cap(self):
        server._cost_global["cost_cents"] = 3001  # cap=3000
        self.assertEqual(server._cost_state(), "degraded")

    def test_state_hard_over_2x_cap(self):
        server._cost_global["cost_cents"] = 6001  # hard=6000
        self.assertEqual(server._cost_state(), "hard")

    def test_day_rollover_resets(self):
        # 日切/重启后内存计数不可信，应从计量表恢复；账本为空即 0。
        # （mock 掉计量表：本模块其他用例会往同一个 temp DB 写记录，断言不该随执行顺序漂移）
        server._cost_global.update(daykey="2000-01-01", cost_cents=99999,
                                   alerted=True, hard_alerted=True)
        with mock.patch.object(db, "meter_totals", return_value={"cost_cents": 0}):
            self.assertEqual(server._cost_state(), "normal")
        self.assertEqual(server._cost_global["cost_cents"], 0)

    def test_tick_emits_events_once(self):
        over, hard = [], []
        d1 = server.kernel.on("meter.cost_over", lambda **kw: over.append(kw))
        d2 = server.kernel.on("meter.cost_hard", lambda **kw: hard.append(kw))
        try:
            server._tick_cost_gate(2500)
            server._tick_cost_gate(600)   # 3100 > cap → over 发一次
            server._tick_cost_gate(3000)  # 6100 > hard → hard 发一次
            server._tick_cost_gate(5000)  # 继续超，不再重复发
            self.assertEqual(len(over), 1)
            self.assertEqual(len(hard), 1)
            self.assertEqual(over[0]["cap"], 3000)
            self.assertEqual(hard[0]["hard"], 6000)
        finally:
            d1(); d2()

    def test_caps_hot_editable(self):
        db.meter_set("cost_cents_cap", "100")
        db.meter_set("cost_cents_hard", "200")
        try:
            self.assertEqual(server._cost_caps(), (100, 200))
            server._cost_global["cost_cents"] = 150
            self.assertEqual(server._cost_state(), "degraded")
            server._cost_global["cost_cents"] = 250
            self.assertEqual(server._cost_state(), "hard")
        finally:
            db.meter_set("cost_cents_cap", "3000")
            db.meter_set("cost_cents_hard", "6000")


class TestDegradeParams(P0Base):
    def test_min_interval(self):
        self.assertTrue(server._degrade_ok("u1"))
        self.assertFalse(server._degrade_ok("u1"), "15s 内第二次必须被限")
        self.assertTrue(server._degrade_ok("u2"), "不同用户互不影响")

    def test_min_interval_hot_editable(self):
        db.meter_set("degrade_min_interval", "0")
        try:
            self.assertTrue(server._degrade_ok("u9"))
            self.assertTrue(server._degrade_ok("u9"))
        finally:
            db.meter_set("degrade_min_interval", "15")

    def test_degrade_max_tokens(self):
        self.assertEqual(server._degrade_max_tokens("explain"), 400)
        self.assertEqual(server._degrade_max_tokens("followup"), 400)
        self.assertEqual(server._degrade_max_tokens("chat"), 1000)
        db.meter_set("degrade_max_tokens", "250")
        try:
            self.assertEqual(server._degrade_max_tokens("explain"), 250)
        finally:
            db.meter_set("degrade_max_tokens", "400")


class TestExplainDegrade(P0Base):
    """_explain 入口行为：超限降级 / 硬停 / 输出截短 / 退款门闩。"""

    def _run(self, body, user=None, llm_result=None):
        h = FakeExplainHandler(body=body, user=user)
        result = llm_result or ("好答案" * 100, {"prompt_tokens": 10, "completion_tokens": 50}, False)
        with mock.patch.object(server, "call_llm", return_value=result) as m:
            h._explain(body)
            h.llm_calls = m.call_args_list
        return h

    def _fresh_user_exhausted(self):
        u = self._mkuser()
        self._exhaust(u["id"], db.QUOTA_FREE)
        row = db.get_user(u["id"])
        return u, row

    def test_over_quota_returns_200_degraded_not_429(self):
        u, row = self._fresh_user_exhausted()
        h = self._run({"mode": "explain", "term": "量子"}, user=row)
        self.assertEqual(h.status, 200, f"超限必须 200 降级，实际 {h.status}: {h.payload}")
        self.assertTrue(h.payload.get("degraded"))
        self.assertEqual(h.payload.get("reason"), "quota")

    def test_over_quota_output_capped_at_400(self):
        u, row = self._fresh_user_exhausted()
        h = self._run({"mode": "explain", "term": "量子"}, user=row)
        self.assertEqual(h.llm_calls[0].args[1], 400)

    def test_over_quota_chat_capped_at_1000(self):
        u, row = self._fresh_user_exhausted()
        h = self._run({"mode": "chat", "messages": []}, user=row)
        self.assertEqual(h.llm_calls[0].args[1], 1000)

    def test_within_quota_no_degrade_full_tokens(self):
        u = self._mkuser("fresh@example.com")
        row = db.get_user(u["id"])
        h = self._run({"mode": "explain", "term": "量子"}, user=row)
        self.assertEqual(h.status, 200)
        self.assertNotIn("degraded", h.payload)
        self.assertEqual(h.llm_calls[0].args[1], server.MAX_TOKENS)

    def test_degraded_second_hit_rate_limited_429(self):
        u, row = self._fresh_user_exhausted()
        self._run({"mode": "explain", "term": "一"}, user=row)
        h = FakeExplainHandler(body={"mode": "explain", "term": "二"}, user=db.get_user(u["id"]))
        with mock.patch.object(server, "call_llm", return_value=("x", {}, False)):
            h._explain(h.body)
        self.assertEqual(h.status, 429)
        self.assertTrue(h.payload.get("degraded"))
        self.assertIn("15", h.payload["error"])

    def test_hard_cap_blocks_everyone_with_429(self):
        server._cost_global["cost_cents"] = 6001
        u = self._mkuser("hardcap@example.com")
        row = db.get_user(u["id"])
        h = FakeExplainHandler(body={"mode": "explain", "term": "x"}, user=row)
        with mock.patch.object(server, "call_llm") as m:
            h._explain(h.body)
            m.assert_not_called()
        self.assertEqual(h.status, 429)
        self.assertEqual(h.payload.get("cost"), "hard")

    def test_cost_cap_degrades_even_fresh_quota(self):
        """配额没超但全局超 cap → 也降级（全员）。"""
        server._cost_global["cost_cents"] = 3001
        u = self._mkuser("costdeg@example.com")
        row = db.get_user(u["id"])
        h = self._run({"mode": "explain", "term": "x"}, user=row)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.payload.get("degraded"))
        self.assertEqual(h.payload.get("reason"), "cost_cap")
        self.assertEqual(h.llm_calls[0].args[1], 400)

    def test_failed_degraded_request_no_refund(self):
        """降级档没真扣次 → 失败不退款，否则失败一次反赚一个槽位。"""
        u, row = self._fresh_user_exhausted()
        before = db.quota_state(row)["used"]
        h = FakeExplainHandler(body={"mode": "explain", "term": "x"}, user=db.get_user(u["id"]))
        with mock.patch.object(server, "call_llm", side_effect=RuntimeError("LLM 挂了")):
            h._explain(h.body)
        self.assertEqual(h.status, 500)
        after = db.quota_state(db.get_user(u["id"]))["used"]
        self.assertEqual(after, before, "未扣次的失败请求不能退款")

    def test_failed_normal_request_refunds(self):
        u = self._mkuser("refund@example.com")
        row = db.get_user(u["id"])
        db.consume_quota(u["id"], db.QUOTA_FREE)
        mid = db.quota_state(db.get_user(u["id"]))["used"]
        h = FakeExplainHandler(body={"mode": "explain", "term": "x"}, user=db.get_user(u["id"]))
        with mock.patch.object(server, "call_llm", side_effect=RuntimeError("LLM 挂了")):
            h._explain(h.body)
        after = db.quota_state(db.get_user(u["id"]))["used"]
        self.assertEqual(after, mid, "失败请求把本次扣的 1 次退回（用前=用后，没有多退）")
        self.assertGreater(mid, 0)


class TestSourceMetadata(P0Base):
    def test_opt_in_metadata_is_split_from_explanation_body(self):
        user = self._mkuser("source-meta@example.com")
        row = db.get_user(user["id"])
        body = {
            "mode": "explain", "term": "熵", "context": "系统的熵在这里增加",
            "question": "为什么不是能量守恒？", "source_meta": True,
        }
        model_output = (
            '[[WENDI_SOURCE_META]]{"context_reading":"说明语境","likely_need":"可能在辨析概念边界"}'
            '[[/WENDI_SOURCE_META]][[WENDI_EXPLANATION]]解释正文'
        )
        h = FakeExplainHandler(body=body, user=row)
        with mock.patch.object(server, "call_llm", return_value=(model_output, {}, False)):
            with mock.patch.object(server, "explain_messages", wraps=server.explain_messages) as prompt:
                h._explain(body)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["text"], "解释正文")
        self.assertEqual(h.payload["source_meta"], {
            "context_reading": "说明语境", "likely_need": "可能在辨析概念边界",
        })
        self.assertTrue(prompt.call_args.kwargs["include_source_meta"])


class TestBehaviorMetering(P0Base):
    def test_depth_turns_recorded(self):
        u = self._mkuser("meter@example.com")
        row = db.get_user(u["id"])
        h = FakeExplainHandler(body={"mode": "explain", "term": "x", "depth": 3, "turns": 7}, user=row)
        with mock.patch.object(server, "call_llm", return_value=("ok", {"completion_tokens": 10}, False)):
            h._explain(h.body)
        with db._conn() as c:
            r = c.execute("SELECT depth, turns FROM meter_requests "
                          "WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        self.assertEqual(r["depth"], 3)
        self.assertEqual(r["turns"], 7)

    def test_missing_signals_default_minus_1(self):
        u = self._mkuser("meter2@example.com")
        row = db.get_user(u["id"])
        h = FakeExplainHandler(body={"mode": "explain", "term": "x"}, user=row)
        with mock.patch.object(server, "call_llm", return_value=("ok", {}, False)):
            h._explain(h.body)
        with db._conn() as c:
            r = c.execute("SELECT depth, turns FROM meter_requests "
                          "WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        self.assertEqual(r["depth"], -1)
        self.assertEqual(r["turns"], -1)

    def test_bad_signal_values_default_minus_1(self):
        u = self._mkuser("meter3@example.com")
        row = db.get_user(u["id"])
        h = FakeExplainHandler(body={"mode": "explain", "term": "x",
                                     "depth": "abc", "turns": -5}, user=row)
        with mock.patch.object(server, "call_llm", return_value=("ok", {}, False)):
            h._explain(h.body)
        with db._conn() as c:
            r = c.execute("SELECT depth, turns FROM meter_requests "
                          "WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        self.assertEqual(r["depth"], -1)
        self.assertEqual(r["turns"], -1)

    def test_meter_record_signature_backwards_compat(self):
        """老调用方（不带 depth/turns）不炸。"""
        db.meter_record(1, "free", "explain", "standard", False, 0, 5, 10, 3, True)
        with db._conn() as c:
            r = c.execute("SELECT depth, turns FROM meter_requests ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(r["depth"], -1)
        self.assertEqual(r["turns"], -1)


if __name__ == "__main__":
    unittest.main()
