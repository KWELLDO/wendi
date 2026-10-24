"""Explain response metadata protocol tests."""
from __future__ import annotations

import unittest

from explain_output import ExplainOutputParser, split_explain_output


class TestExplainOutputParser(unittest.TestCase):
    def test_streams_only_body_after_complete_metadata_header(self):
        parser = ExplainOutputParser()
        meta, body = parser.feed('[[WENDI_SOURCE_META]]{"context_reading":"语境说明","likely_need":"可能想确认概念边界"}')
        self.assertIsNone(meta)
        self.assertEqual(body, "")

        meta, body = parser.feed('[[/WENDI_SOURCE_META]][[WENDI_EXPLANATION]]解释正文')
        self.assertEqual(meta, {
            "context_reading": "语境说明",
            "likely_need": "可能想确认概念边界",
        })
        self.assertEqual(body, "解释正文")

        meta, body = parser.feed("继续")
        self.assertIsNone(meta)
        self.assertEqual(body, "继续")

    def test_plain_legacy_response_falls_back_without_losing_text(self):
        parser = ExplainOutputParser()
        meta, body = parser.feed("普通解释")
        self.assertIsNone(meta)
        self.assertEqual(body, "普通解释")
        self.assertEqual(parser.finish(), (None, ""))

    def test_incomplete_metadata_falls_back_at_end(self):
        parser = ExplainOutputParser()
        meta, body = parser.feed("[[WENDI_SOURCE_META]]截断")
        self.assertIsNone(meta)
        self.assertEqual(body, "")
        self.assertEqual(parser.finish(), (None, "[[WENDI_SOURCE_META]]截断"))

    def test_complete_metadata_survives_when_body_marker_is_truncated(self):
        parser = ExplainOutputParser()
        parser.feed('[[WENDI_SOURCE_META]]{"context_reading":"语境","likely_need":"可能卡点"}'
                    '[[/WENDI_SOURCE_META]]')
        self.assertEqual(parser.finish(), ({"context_reading": "语境", "likely_need": "可能卡点"}, ""))

    def test_non_stream_split_removes_metadata_header(self):
        meta, body = split_explain_output(
            '[[WENDI_SOURCE_META]]{"context_reading":"此处说明语境","likely_need":"低把握：想确认词义"}'
            '[[/WENDI_SOURCE_META]][[WENDI_EXPLANATION]]正文'
        )
        self.assertEqual(meta["context_reading"], "此处说明语境")
        self.assertEqual(meta["likely_need"], "低把握：想确认词义")
        self.assertEqual(body, "正文")

    def test_untrusted_or_oversized_metadata_is_sanitized(self):
        meta, body = split_explain_output(
            '[[WENDI_SOURCE_META]]{"context_reading":42,"likely_need":"x"}'
            '[[/WENDI_SOURCE_META]][[WENDI_EXPLANATION]]正文'
        )
        self.assertEqual(meta, {"context_reading": "", "likely_need": "x"})
        self.assertEqual(body, "正文")


if __name__ == "__main__":
    unittest.main()
