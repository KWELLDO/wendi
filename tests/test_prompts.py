"""prompts.py 的测试。运行：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import unittest

from prompts import explain_messages


class TestExplainMessages(unittest.TestCase):
    def test_doubt_reaches_user_message(self):
        msgs = explain_messages("熵", "这里熵增意味着什么", "standard", "为什么不是能量守恒？")
        user = msgs[1]["content"]
        self.assertIn("为什么不是能量守恒？", user)
        self.assertIn("疑惑", user)                       # 带上了「这是读者的疑惑」的框
        self.assertIn("**熵**", user)
        self.assertLess(user.index("疑惑"), user.index("解释它是什么意思"))  # 疑惑在通用指令之前

    def test_no_doubt_leaves_message_unchanged(self):
        for q in ("", "   ", None):
            user = explain_messages("熵", "ctx", question=q)[1]["content"]
            self.assertNotIn("疑惑", user)
            self.assertEqual(user.count("解释它是什么意思"), 1)

    def test_system_carries_style(self):
        sys_prompt = explain_messages("x", "y", "rigorous")[0]["content"]
        self.assertIn("严谨", sys_prompt)

    def test_source_meta_uses_context_and_low_confidence_without_doubt(self):
        msgs = explain_messages("熵", "系统的熵在这里增加", include_source_meta=True)
        system, user = msgs[0]["content"], msgs[1]["content"]
        self.assertIn("[[WENDI_SOURCE_META]]", system)
        self.assertIn("context_reading", system)
        self.assertIn("likely_need", system)
        self.assertIn("没有提供疑惑", system)
        self.assertIn("低把握", system)
        self.assertIn("[[WENDI_EXPLANATION]]", system)
        self.assertIn("系统的熵在这里增加", user)

    def test_source_meta_uses_user_doubt_as_evidence_not_as_fact(self):
        msgs = explain_messages("熵", "系统的熵在这里增加", question="为什么不是能量守恒？",
                               include_source_meta=True)
        system, user = msgs[0]["content"], msgs[1]["content"]
        self.assertIn("为什么不是能量守恒？", user)
        self.assertIn("根据读者的疑惑", system)
        self.assertIn("可能", system)


if __name__ == "__main__":
    unittest.main()
