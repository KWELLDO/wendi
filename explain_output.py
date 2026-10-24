"""Parse the compact source metadata prelude from explanation model output."""
from __future__ import annotations

import json

SOURCE_META_START = "[[WENDI_SOURCE_META]]"
SOURCE_META_END = "[[/WENDI_SOURCE_META]]"
EXPLANATION_START = "[[WENDI_EXPLANATION]]"
MAX_META_LENGTH = 4096
MAX_FIELD_LENGTH = 240


def _decode_meta(raw: str) -> dict[str, str]:
    if len(raw) > MAX_META_LENGTH:
        return {"context_reading": "", "likely_need": ""}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"context_reading": "", "likely_need": ""}
    if not isinstance(value, dict):
        return {"context_reading": "", "likely_need": ""}
    result = {}
    for key in ("context_reading", "likely_need"):
        item = value.get(key)
        result[key] = item.strip()[:MAX_FIELD_LENGTH] if isinstance(item, str) else ""
    return result


class ExplainOutputParser:
    """Incrementally separate one metadata header from explanation body chunks.

    Until the complete header arrives, deltas are buffered. Legacy or malformed
    model output falls back to ordinary explanation text without being dropped.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._body_started = False
        self._finished = False

    def feed(self, chunk: str) -> tuple[dict[str, str] | None, str]:
        if self._finished:
            return None, ""
        if self._body_started:
            return None, chunk

        self._buffer += str(chunk or "")
        candidate = self._buffer.lstrip()
        if not candidate:
            return None, ""

        if not candidate.startswith(SOURCE_META_START):
            if SOURCE_META_START.startswith(candidate) and len(candidate) < len(SOURCE_META_START):
                return None, ""
            self._body_started = True
            body = self._buffer
            self._buffer = ""
            return None, body

        meta_end = candidate.find(SOURCE_META_END, len(SOURCE_META_START))
        if meta_end < 0:
            if len(candidate) <= MAX_META_LENGTH:
                return None, ""
            self._body_started = True
            body = self._buffer
            self._buffer = ""
            return None, body

        body_start = candidate.find(EXPLANATION_START, meta_end + len(SOURCE_META_END))
        if body_start < 0:
            if len(candidate) <= MAX_META_LENGTH:
                return None, ""
            self._body_started = True
            body = self._buffer
            self._buffer = ""
            return None, body

        raw_meta = candidate[len(SOURCE_META_START):meta_end].strip()
        body = candidate[body_start + len(EXPLANATION_START):]
        self._body_started = True
        self._buffer = ""
        return _decode_meta(raw_meta), body

    def finish(self) -> tuple[dict[str, str] | None, str]:
        if self._finished:
            return None, ""
        self._finished = True
        if self._body_started:
            return None, ""
        self._body_started = True
        candidate = self._buffer.lstrip()
        if candidate.startswith(SOURCE_META_START):
            meta_end = candidate.find(SOURCE_META_END, len(SOURCE_META_START))
            if meta_end >= 0:
                raw_meta = candidate[len(SOURCE_META_START):meta_end].strip()
                self._buffer = ""
                return _decode_meta(raw_meta), ""
        body = self._buffer
        self._buffer = ""
        return None, body


def split_explain_output(text: str) -> tuple[dict[str, str] | None, str]:
    """Non-streaming equivalent of :class:`ExplainOutputParser`."""
    parser = ExplainOutputParser()
    meta, body = parser.feed(text)
    end_meta, end_body = parser.finish()
    return (meta if meta is not None else end_meta), body + end_body
