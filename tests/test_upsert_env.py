"""deploy/upsert-env.py（幂等合并 systemd EnvironmentFile）的测试。

它动的是线上密钥文件，所以逻辑要钉住：只改传入的键、保留注释、幂等、备份、600。
运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "upsert_env", Path(__file__).resolve().parent.parent / "deploy" / "upsert-env.py")
upsert = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upsert)


class TestMerge(unittest.TestCase):
    def test_replaces_appends_and_keeps_other_lines(self):
        existing = "# 注释\nWENDI_API_KEY=old\nOTHER=keep-me\n"
        content, replaced, added = upsert.merge(existing, [("WENDI_API_KEY", "new"),
                                                           ("WENDI_ADMIN_TOKEN", "tok")])
        self.assertEqual(replaced, ["WENDI_API_KEY"])
        self.assertEqual(added, ["WENDI_ADMIN_TOKEN"])
        self.assertEqual(content, "# 注释\nWENDI_API_KEY=new\nOTHER=keep-me\nWENDI_ADMIN_TOKEN=tok\n")

    def test_untouched_keys_survive(self):
        content, replaced, added = upsert.merge("A=1\nB=2\n", [("A", "9")])
        self.assertIn("B=2", content)
        self.assertEqual(replaced, ["A"])
        self.assertEqual(added, [])

    def test_idempotent(self):
        once, _, _ = upsert.merge("A=1\n", [("A", "2"), ("B", "3")])
        twice, _, _ = upsert.merge(once, [("A", "2"), ("B", "3")])
        self.assertEqual(once, twice)

    def test_value_with_equals_and_spaces_kept(self):
        content, _, _ = upsert.merge("", [("TOKEN", "a=b c=d")])
        self.assertEqual(content, "TOKEN=a=b c=d\n")

    def test_blank_and_missing_file(self):
        content, _, added = upsert.merge("", [("K", "v")])
        self.assertEqual(content, "K=v\n")
        self.assertEqual(added, ["K"])

    def test_parse_rejects_bad_key_and_empty(self):
        with self.assertRaises(SystemExit):
            upsert.parse_pairs("BAD KEY=1\n")
        with self.assertRaises(SystemExit):
            upsert.parse_pairs("# only comments\n")

    def test_parse_skips_comments_and_blanks(self):
        pairs = upsert.parse_pairs("# c\n\nA=1\n  B=2\n")
        self.assertEqual(pairs, [("A", "1"), ("B", "2")])


class TestEndToEnd(unittest.TestCase):
    def test_writes_file_with_backup_and_0600(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "env")
            Path(path).write_text("WENDI_API_KEY=old\n", encoding="utf-8")
            os.chmod(path, 0o600)
            orig_pairs = upsert.parse_pairs
            try:
                upsert.parse_pairs = lambda _t: [("WENDI_API_KEY", "new"), ("WENDI_ADMIN_TOKEN", "t")]
                import sys
                old_argv, sys.argv = sys.argv, ["upsert-env.py", path]
                old_stdin = sys.stdin
                try:
                    sys.stdin = type("S", (), {"read": staticmethod(lambda: "x")})()
                    upsert.main()
                finally:
                    sys.argv, sys.stdin = old_argv, old_stdin
            finally:
                upsert.parse_pairs = orig_pairs
            text = Path(path).read_text(encoding="utf-8")
            self.assertEqual(text, "WENDI_API_KEY=new\nWENDI_ADMIN_TOKEN=t\n")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(d).glob("env.bak-*"))), 1, "写入前必须留备份")


if __name__ == "__main__":
    unittest.main()
