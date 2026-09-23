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


if __name__ == "__main__":
    unittest.main()
