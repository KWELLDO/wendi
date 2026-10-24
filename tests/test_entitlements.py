"""entitlements:增值权益闸口的取值链与降级行为。"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile

import db
import entitlements
import messages


class FakeHandler:
    def __init__(self):
        self.status = None
        self.payload = None

    def _json(self, status, payload):
        self.status = status
        self.payload = payload


class TestEntitlements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        cls._old_db = db.DB_PATH
        db.DB_PATH = Path(cls._tmp.name)
        db.init()
        messages.init()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._old_db
        os.unlink(cls._tmp.name)

    def setUp(self):
        with db._conn() as c:
            for t in ("users", "meter_config"):
                c.execute(f"DELETE FROM {t}")
        self._old_env = os.environ.pop("WENDI_FEATURE_SYNC_FREE", None)
        salt, h = db.__dict__ and __import__("auth").hash_password("secret1")
        self.pro, _ = db.register("pro@example.com", salt, h)
        self.free, _ = db.register("free@example.com", salt, h)
        db.set_plan(self.pro["id"], "pro", "2999-12-31")
        self.pro = db.get_user(self.pro["id"])     # 行快照要跟库一致（服务端 _user() 每次现读）

    def tearDown(self):
        if self._old_env is not None:
            os.environ["WENDI_FEATURE_SYNC_FREE"] = self._old_env
        else:
            os.environ.pop("WENDI_FEATURE_SYNC_FREE", None)

    def test_free_by_default_everyone(self):
        for u in (self.pro, self.free):
            for feature in entitlements.FEATURES:
                self.assertTrue(entitlements.has(u, feature))

    def test_meter_config_off_makes_it_pro_only(self):
        db.meter_set("feature_sync_free", "0")
        self.assertTrue(entitlements.has(self.pro, "sync"))
        self.assertFalse(entitlements.has(self.free, "sync"))

    def test_env_fallback_when_meter_config_empty(self):
        os.environ["WENDI_FEATURE_SYNC_FREE"] = "0"
        self.assertFalse(entitlements.has(self.free, "sync"))
        db.meter_set("feature_sync_free", "1")     # meter_config 优先于 env
        self.assertTrue(entitlements.has(self.free, "sync"))

    def test_expired_pro_counts_as_free(self):
        db.meter_set("feature_export_free", "0")
        db.set_plan(self.pro["id"], "pro", "2020-01-01")
        self.assertFalse(entitlements.has(db.get_user(self.pro["id"]), "export"))

    def test_unknown_feature_open_by_default(self):
        self.assertTrue(entitlements.has(self.free, "no-such-feature"))

    def test_deny_payload_shape(self):
        h = FakeHandler()
        entitlements.deny(h, "sync")
        self.assertEqual(h.status, 403)
        self.assertEqual(h.payload["entitlement"], "sync")
        self.assertTrue(h.payload["upgrade"])


if __name__ == "__main__":
    unittest.main()
