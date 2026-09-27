"""prompts.py 的测试。运行：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import unittest

from prompts import explain_messages


class TestExplainMessages(unittest.TestCase):
    def test_doubt_reaches_user_message(self):
        msgs = explain_messages("熵", "这里熵增意味着什么", "standard", "为什么不是能量守恒？")
        user = msgs[1]["content"]
        self.assertIn("为什么不是能量守恒？", user)
        self.assertIn("正面回应这个疑惑", user)
        self.assertIn('**熵**', user)

    def test_no_doubt_leaves_message_unchanged(self):
        for q in ("", "   ", None):
            user = explain_messages("熵", "ctx", question=q)[1]["content"]
            self.assertNotIn("疑惑", user)
            self.assertEqual(user.count("解释它是什么意思"), 1)

    def test_system_carries_style(self):
        sys_prompt = explain_messages("x", "y", "rigorous")[0]["content"]
        self.assertIn("严谨", sys_prompt)


if __name__ == "__main__":
    unittest.main()
