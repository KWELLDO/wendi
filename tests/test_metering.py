"""经济测算（metering）的测试。运行：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest

# 把 db 指向临时文件，避免污染真实数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["WENDI_DB"] = _tmp.name

import db  # noqa: E402
import server  # noqa: E402
from server import _cost_cents, _is_peak_utc, _usage_split  # noqa: E402


class TestIsPeakUtc(unittest.TestCase):
    def test_weekend_offpeak(self):
        # gmtime 的 struct_time：tm_year..tm_wday
        import time
        # 选一个已知周六的 UTC 时间，直接构造 struct_time
        # 2026-09-26 是周六
        g = time.struct_time((2026, 9, 26, 2, 0, 0, 5, 269, 0))
        self.assertFalse(_is_peak_utc(g))

    def test_weekday_peak_windows(self):
        import time
        # 2026-09-23 是周三（tm_wday=2），02:00 UTC 在 01:00-04:00 峰窗内
        g = time.struct_time((2026, 9, 23, 2, 0, 0, 2, 266, 0))
        self.assertTrue(_is_peak_utc(g))

    def test_weekday_offpeak(self):
        import time
        # 周三 05:00 UTC，在峰窗之外
        g = time.struct_time((2026, 9, 23, 5, 0, 0, 2, 266, 0))
        self.assertFalse(_is_peak_utc(g))


class TestUsageSplit(unittest.TestCase):
    def test_full_usage(self):
        u = {"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 7, "completion_tokens": 10}
        self.assertEqual(_usage_split(u), (3, 7, 10))

    def test_missing_cache_split_falls_back_to_prompt_tokens(self):
        u = {"prompt_tokens": 12, "completion_tokens": 5}
        self.assertEqual(_usage_split(u), (0, 12, 5))

    def test_empty_usage(self):
        self.assertEqual(_usage_split({}), (0, 0, 0))


class TestCostCents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def test_cost_formula_offpeak(self):
        # 直接调 _cost_cents（依赖 meter_config 默认值，db.init 后已 seed）
        # 1000 hit + 1000 miss + 1000 completion，谷价
        # usd = 1000*0.003 + 1000*0.15 + 1000*0.60 = 3 + 150 + 600 = 753 美元
        # 换算 = 753 / 1e6 * 7.2 * 100 ≈ 0.542 分 → round = 1
        cents = _cost_cents(1000, 1000, 1000)
        self.assertGreater(cents, 0)
        # 精确验证（谷价常量固定）
        usd = 1000 * 0.003 + 1000 * 0.15 + 1000 * 0.60
        expected = round(usd / 1_000_000 * 7.2 * 100)
        self.assertEqual(cents, expected)

    def test_zero_cost(self):
        self.assertEqual(_cost_cents(0, 0, 0), 0)


class TestDbMeter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def test_record_and_totals(self):
        day = db.today()
        db.meter_record(1, "pro", "explain", "standard", False, 3, 7, 10, 123, True)
        db.meter_record(1, "pro", "chat", None, True, 0, 5, 20, 45, True)
        db.meter_record(None, "anon", "explain", "standard", False, 0, 2, 4, 10, True)

        totals = db.meter_totals(day)
        self.assertEqual(totals["requests"], 3)
        self.assertEqual(totals["hit_tokens"], 3)
        self.assertEqual(totals["miss_tokens"], 14)
        self.assertEqual(totals["completion_tokens"], 34)
        self.assertEqual(totals["cost_cents"], 178)
        self.assertEqual(totals["by_plan"], {"pro": 2, "anon": 1})
        self.assertEqual(totals["by_mode"], {"explain": 2, "chat": 1})

    def test_user_ndays(self):
        uid = 999
        db.meter_record(uid, "free", "explain", "standard", False, 1, 1, 1, 5, True)
        rows = db.meter_user_ndays(uid, 7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_cents"], 5)
        self.assertEqual(rows[0]["requests"], 1)

    def test_config_set_get(self):
        db.meter_set("flash_out_offpeak", "0.99")
        self.assertEqual(db.meter_get("flash_out_offpeak"), "0.99")
        # 还原，避免污染其他断言
        db.meter_set("flash_out_offpeak", "0.60")


class TestMeterConfigEndpoint(unittest.TestCase):
    """GET /api/meter/config：闸门参数 + 当日状态（报表页数据源）。直接调 do_GET。

    注意：pytest 单进程共享 server 模块与临时库，meter_requests 有别的用例累积的
    旧账——所以内存闸态显式设值（生产语义：内存=活闸门），today_cents 只断言
    与台账 SUM 透传一致，不钉死具体数值。
    """

    class FakeHandler(server.Handler):
        def __init__(self, token="test-token"):
            self.status = None
            self.payload = None
            self.headers = {"Authorization": f"Bearer {token}"} if token else {}
            self.path = "/api/meter/config"

        def _json(self, code, obj):
            self.status, self.payload = code, obj

    @classmethod
    def setUpClass(cls):
        db.init()
        cls._old_token = server.ADMIN_TOKEN
        server.ADMIN_TOKEN = "test-token"

    @classmethod
    def tearDownClass(cls):
        server.ADMIN_TOKEN = cls._old_token

    def tearDown(self):
        server._cost_global.update(daykey="2000-01-01", cost_cents=0)

    def _get(self):
        h = self.FakeHandler()
        h.do_GET()
        return h

    def test_no_token_rejected(self):
        h = self.FakeHandler(token="")
        h.do_GET()
        self.assertEqual(h.status, 401)

    def test_reports_caps_state_and_today_cost(self):
        server._cost_global.update(daykey=server._today(), cost_cents=100)
        h = self._get()
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["cap_cents"], 3000)
        self.assertEqual(h.payload["hard_cents"], 6000)
        self.assertEqual(h.payload["state"], "normal")
        self.assertEqual(h.payload["today_cents"],
                         db.meter_totals(server._today())["cost_cents"])
        self.assertEqual(h.payload["degrade"]["min_interval"], 15)
        self.assertRegex(h.payload["day"], r"^\d{4}-\d{2}-\d{2}$")

    def test_state_degraded_and_hard(self):
        server._cost_global.update(daykey=server._today(), cost_cents=3500)
        self.assertEqual(self._get().payload["state"], "degraded")
        server._cost_global.update(daykey=server._today(), cost_cents=6001)
        self.assertEqual(self._get().payload["state"], "hard")


if __name__ == "__main__":
    unittest.main()
