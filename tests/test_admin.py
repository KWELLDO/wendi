"""受控建号 / 密码重置 / 兑换码批次管理的测试。运行：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

# 把 db 指向临时文件，避免污染真实数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["WENDI_DB"] = _tmp.name

import auth  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


class BaseAdmin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        cls._old_token = server.ADMIN_TOKEN
        server.ADMIN_TOKEN = "test-token"  # 打开 admin 鉴权

    @classmethod
    def tearDownClass(cls):
        server.ADMIN_TOKEN = cls._old_token

    def _mkuser(self, email: str, pw: str = "secret1") -> dict:
        salt, h = auth.hash_password(pw)
        u = db.create_user(email, salt, h)
        self.assertIsNotNone(u, f"建号失败 {email}")
        return u


class TestGenCodes(BaseAdmin):
    def test_returns_batch_and_unique_codes(self):
        batch, codes = db.gen_codes(5, 30, "测一批")
        self.assertEqual(len(codes), 5)
        self.assertEqual(len(set(codes)), 5, "码必须互不相同")
        self.assertTrue(all(c.startswith("WD-") for c in codes))
        rows = db.list_codes(batch)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["batch"] == batch for r in rows))
        self.assertTrue(all(r["status"] == "unused" for r in rows))

    def test_auto_batch_id_format(self):
        batch, _ = db.gen_codes(1, 7)
        self.assertRegex(batch, r"^\d{8}-[A-Z2-9]{4}$")

    def test_explicit_batch_reused(self):
        batch, _ = db.gen_codes(2, 7, batch="MY-BATCH")
        self.assertEqual(batch, "MY-BATCH")
        self.assertEqual(db.code_stats("MY-BATCH")["n"], 2)


class TestVoidAndStats(BaseAdmin):
    def test_void_then_redeem_rejected(self):
        batch, codes = db.gen_codes(2, 30)
        u = self._mkuser("void@example.com")
        self.assertEqual(db.void_codes([codes[0]]), 1)
        ok, msg, _ = db.redeem(codes[0], u["id"])
        self.assertFalse(ok)
        self.assertIn("作废", msg)
        # 同批另一个码仍可用
        ok, _, _ = db.redeem(codes[1], u["id"])
        self.assertTrue(ok)

    def test_void_batch_only_unused(self):
        batch, codes = db.gen_codes(3, 30, batch="VBATCH")
        u = self._mkuser("vb@example.com")
        db.redeem(codes[0], u["id"])          # 先兑 1 个
        voided = db.void_codes(batch="VBATCH")  # 整批作废 → 只应命中未用的 2 个
        self.assertEqual(voided, 2)
        st = db.code_stats("VBATCH")
        self.assertEqual(st, {"n": 3, "used": 1, "voided": 2, "unused": 0, "redeemers": 1})

    def test_stats_and_list_status(self):
        batch, codes = db.gen_codes(3, 30, batch="SBATCH")
        u1 = self._mkuser("s1@example.com")
        u2 = self._mkuser("s2@example.com")
        db.redeem(codes[0], u1["id"])
        db.redeem(codes[1], u2["id"])
        db.void_codes([codes[2]])
        st = db.code_stats("SBATCH")
        self.assertEqual(st, {"n": 3, "used": 2, "voided": 1, "unused": 0, "redeemers": 2})
        rows = {r["code"]: r for r in db.list_codes("SBATCH")}
        self.assertEqual(rows[codes[0]]["used_by_email"], "s1@example.com")
        self.assertEqual(rows[codes[0]]["status"], "used")
        self.assertEqual(rows[codes[2]]["status"], "voided")

    def test_used_code_cannot_be_voided(self):
        batch, codes = db.gen_codes(1, 30)
        u = self._mkuser("used@example.com")
        db.redeem(codes[0], u["id"])
        self.assertEqual(db.void_codes([codes[0]]), 0, "已用的码不该能作废")


class TestResetPassword(BaseAdmin):
    def test_reset_revokes_sessions_and_new_pw_works(self):
        u = self._mkuser("reset@example.com", "oldpass1")
        tok1, tok2 = "t-reset-1", "t-reset-2"
        db.create_session(tok1, u["id"])
        db.create_session(tok2, u["id"])
        self.assertIsNotNone(db.get_session_user(tok1))

        salt, h = auth.hash_password("newpass2")
        db.reset_password(u["id"], salt, h)

        self.assertIsNone(db.get_session_user(tok1), "重置后旧 token 必须失效")
        self.assertIsNone(db.get_session_user(tok2), "全部会话都要被踢")
        row = db.get_user_by_email("reset@example.com")
        self.assertTrue(auth.verify_password("newpass2", row["pw_salt"], row["pw_hash"]))

    def test_other_users_sessions_untouched(self):
        a = self._mkuser("keep-a@example.com")
        b = self._mkuser("keep-b@example.com")
        db.create_session("t-keep-a", a["id"])
        db.create_session("t-keep-b", b["id"])
        salt, h = auth.hash_password("brandnew1")
        db.reset_password(a["id"], salt, h)
        self.assertIsNone(db.get_session_user("t-keep-a"))
        self.assertIsNotNone(db.get_session_user("t-keep-b"), "不能误伤别人的会话")


class TestAdminHandlers(BaseAdmin):
    """直接调 handler 方法（绕过 HTTP），验证鉴权与参数校验。"""

    class FakeHandler(server.Handler):
        def __init__(self, method="POST", body=None, token="test-token"):
            self.body = body or {}
            self.status = None
            self.payload = None
            self.headers = {"Authorization": f"Bearer {token}"} if token else {}
            self.path = "/api/admin/x"
            self._method = method

        def _json(self, code, obj):
            self.status, self.payload = code, obj

        def _body(self):
            return self.body

        def _qs(self):
            return {}

    def test_no_token_rejected(self):
        server.ADMIN_TOKEN = ""
        try:
            h = self.FakeHandler(token="")
            h._admin_gen_codes({"n": 1, "days": 7})
            self.assertEqual(h.status, 401)
        finally:
            server.ADMIN_TOKEN = "test-token"

    def test_missing_token_rejected(self):
        h = self.FakeHandler(token="")
        h._admin_gen_codes({"n": 1, "days": 7})
        self.assertEqual(h.status, 401)

    def test_wrong_token_rejected(self):
        h = self.FakeHandler(token="wrong")
        h._admin_gen_codes({"n": 1, "days": 7})
        self.assertEqual(h.status, 401)

    def test_create_user_no_email_format_required(self):
        h = self.FakeHandler()
        h._admin_create_user({"email": "test", "password": "testpw"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["email"], "test")
        self.assertEqual(h.payload["plan"], "free")

    def test_create_pro_user_with_days(self):
        h = self.FakeHandler()
        h._admin_create_user({"email": "pro@example.com", "password": "procpw1",
                              "plan": "pro", "days": 30})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["plan"], "pro")
        self.assertTrue(h.payload["plan_until"])

    def test_create_duplicate_user_409(self):
        h = self.FakeHandler()
        h._admin_create_user({"email": "dup@example.com", "password": "duppw11"})
        self.assertEqual(h.status, 200)
        h2 = self.FakeHandler()
        h2._admin_create_user({"email": "dup@example.com", "password": "duppw11"})
        self.assertEqual(h2.status, 409)

    def test_create_short_password_400(self):
        h = self.FakeHandler()
        h._admin_create_user({"email": "sp@example.com", "password": "123"})
        self.assertEqual(h.status, 400)

    def test_reset_password_404_unknown_user(self):
        h = self.FakeHandler()
        h._admin_reset_password({"email": "nobody@example.com", "password": "newpw11"})
        self.assertEqual(h.status, 404)

    def test_gen_codes_bad_params_400(self):
        h = self.FakeHandler()
        h._admin_gen_codes({"n": 0, "days": 7})
        self.assertEqual(h.status, 400)
        h._admin_gen_codes({"n": "abc", "days": 7})
        self.assertEqual(h.status, 400)

    def test_void_requires_target(self):
        h = self.FakeHandler()
        h._admin_void_codes({})
        self.assertEqual(h.status, 400)

    def test_llmkey_requires_token(self):
        h = self.FakeHandler(token="wrong")
        h._admin_llmkey_get()
        self.assertEqual(h.status, 401)
        h._admin_llmkey_set({"keys": "sk-abc123456789012345678"})
        self.assertEqual(h.status, 401)

    def test_llmkey_set_get_masked(self):
        h = self.FakeHandler()
        h._admin_llmkey_set({"keys": "sk-abc123456789012345678,  sk-def987654321098765432"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["count"], 2)
        self.assertEqual(h.payload["keys"], ["sk-abc***5678", "sk-def***5432"])
        g = self.FakeHandler(method="GET")
        g._admin_llmkey_get()
        self.assertEqual(g.status, 200)
        self.assertEqual(g.payload["count"], 2)
        self.assertNotIn("sk-abc123456789012345678", str(g.payload), "完整密钥绝不能回显")
        self.assertIsNone(g.payload["active"], "刚换 key 后不应还缓存着旧 active")

    def test_llmkey_set_resets_active_cache(self):
        server._ACTIVE_KEY = ("env:WENDI_API_KEY", "sk-old000000000000000000000")
        try:
            h = self.FakeHandler()
            h._admin_llmkey_set({"keys": "sk-new123456789012345678"})
            self.assertEqual(h.status, 200)
            self.assertIsNone(server._ACTIVE_KEY, "换 key 必须作废旧缓存，否则新 key 不生效")
        finally:
            server._ACTIVE_KEY = None

    def test_llmkey_set_base_url_and_model(self):
        h = self.FakeHandler()
        h._admin_llmkey_set({"keys": "sk-abc123456789012345678",
                             "base_url": "https://api.moonshot.cn/", "model": "kimi-k2"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["base_url"], "https://api.moonshot.cn", "结尾斜杠应去掉")
        self.assertEqual(h.payload["model"], "kimi-k2")
        g = self.FakeHandler(method="GET")
        g._admin_llmkey_get()
        self.assertEqual(g.payload["base_url"], "https://api.moonshot.cn")
        self.assertEqual(g.payload["model"], "kimi-k2")

    def test_llmkey_bad_inputs_400(self):
        h = self.FakeHandler()
        h._admin_llmkey_set({"keys": ""})
        self.assertEqual(h.status, 400)
        h._admin_llmkey_set({"keys": "short"})
        self.assertEqual(h.status, 400, "过短密钥疑似粘贴不完整")
        h._admin_llmkey_set({"keys": "sk-abc123456789012345678", "base_url": "ftp://x"})
        self.assertEqual(h.status, 400)

    def test_llmkey_clear(self):
        db.meter_set("llm_api_keys", "sk-abc123456789012345678")
        db.meter_set("llm_base_url", "https://api.moonshot.cn")
        db.meter_set("llm_model", "kimi-k2")
        server._ACTIVE_KEY = ("db:llm_api_keys[0]", "sk-abc123456789012345678")
        try:
            h = self.FakeHandler()
            h._admin_llmkey_set({"action": "clear"})
            self.assertEqual(h.status, 200)
            self.assertEqual(h.payload["count"], 0)
            self.assertIsNone(server._ACTIVE_KEY)
            self.assertEqual(db.meter_get("llm_api_keys"), "")
            self.assertEqual(db.meter_get("llm_base_url"), "", "清除要连 base_url 一起")
            self.assertEqual(db.meter_get("llm_model"), "", "清除要连 model 一起")
        finally:
            server._ACTIVE_KEY = None


class TestKeySources(BaseAdmin):
    """_existing_keys 的来源优先级：面板(DB) > 环境变量 > ~/.secrets。"""

    def _clean_env(self):
        return mock.patch.dict(os.environ, {}, clear=False)

    def test_db_key_first_when_no_env(self):
        db.meter_set("llm_api_keys", "sk-dbkey123456789012345678")
        try:
            with mock.patch.dict(os.environ, {"HOME": tempfile.mkdtemp()}, clear=False):
                os.environ.pop("WENDI_API_KEY", None)
                os.environ.pop("WENDI_KEYS", None)
                keys = server._existing_keys()
            self.assertEqual(keys[0], ("db:llm_api_keys[0]", "sk-dbkey123456789012345678"))
        finally:
            db.meter_set("llm_api_keys", "")

    def test_db_and_env_merged_db_first(self):
        db.meter_set("llm_api_keys", "sk-dbkey123456789012345678")
        try:
            with mock.patch.dict(os.environ,
                                 {"HOME": tempfile.mkdtemp(),
                                  "WENDI_API_KEY": "sk-envkey123456789012345"},
                                 clear=False):
                os.environ.pop("WENDI_KEYS", None)
                keys = server._existing_keys()
            self.assertEqual([k for _, k in keys],
                             ["sk-dbkey123456789012345678", "sk-envkey123456789012345"])
        finally:
            db.meter_set("llm_api_keys", "")

    def test_raises_with_actionable_message_when_nothing(self):
        db.meter_set("llm_api_keys", "")
        with mock.patch.dict(os.environ, {"HOME": tempfile.mkdtemp()}, clear=False):
            os.environ.pop("WENDI_API_KEY", None)
            os.environ.pop("WENDI_KEYS", None)
            with self.assertRaises(RuntimeError) as cm:
                server._existing_keys()
        self.assertIn("密钥", str(cm.exception))

    def test_llm_request_uses_panel_overrides(self):
        db.meter_set("llm_base_url", "http://127.0.0.1:9/")
        db.meter_set("llm_model", "test-model-x")
        try:
            req = server._llm_request("sk-k", [{"role": "user", "content": "hi"}], 5, False)
            self.assertTrue(req.full_url.startswith("http://127.0.0.1:9/chat/completions"))
            payload = json.loads(req.data.decode())
            self.assertEqual(payload["model"], "test-model-x")
        finally:
            db.meter_set("llm_base_url", "")
            db.meter_set("llm_model", "")


if __name__ == "__main__":
    unittest.main()
