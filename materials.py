"""问底 · 材料解析（kreuzberg 4.10.x，懒加载）

上传的文件在这里变成「材料」：markdown 全文 + 带位置的分块（页码 / 字节偏移 / 字符偏移）。

kreuzberg 是问底第一个第三方依赖（Rust 核，wheel 自带运行时，无 Python 依赖）。
它**只在真正解析时导入**：没装引擎时服务仍能零依赖启动，上传接口返回 503。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 分块参数：1000 字符 / 150 重叠，兼顾「划词定位」与「喂给 LLM 的上下文完整度」
CHUNK_MAX_CHARS = int(os.environ.get("WENDI_CHUNK_MAX_CHARS", "1000"))
CHUNK_OVERLAP = int(os.environ.get("WENDI_CHUNK_OVERLAP", "150"))
# 单次解析超时（秒）；超时由 kreuzberg 自己中断
PARSE_TIMEOUT = int(os.environ.get("WENDI_PARSE_TIMEOUT", "120"))
# 落库正文上限（字符）。30MB 的 PDF 理论输出可能更大；截断时返回 truncated 标记。
MAX_TEXT_CHARS = int(os.environ.get("WENDI_MATERIAL_MAX_CHARS", "8000000"))


class ParserUnavailable(RuntimeError):
    """解析引擎未安装 / 无法导入。"""


class ParseFailed(RuntimeError):
    """解析失败。code 给接口用，status 是建议的 HTTP 状态码。"""

    def __init__(self, message: str, code: str = "parse_failed", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


class UnsupportedFormat(ParseFailed):
    def __init__(self, message: str):
        super().__init__(message, code="unsupported_format", status=415)


class PasswordProtected(ParseFailed):
    def __init__(self, message: str = "文件已加密，需要密码才能解析"):
        super().__init__(message, code="password_protected", status=422)


@dataclass
class ParsedMaterial:
    """解析结果（与 DB / HTTP 层解耦）。"""

    content: str
    chunks: list[dict[str, Any]] = field(default_factory=list)
    mime: str = ""
    page_count: int = 0
    truncated: bool = False


def engine() -> Any:
    """导入并返回 kreuzberg 模块；失败抛 ParserUnavailable。"""
    try:
        return importlib.import_module("kreuzberg")
    except Exception as e:  # noqa: BLE001 - ImportError / ABI 不匹配都归为「引擎不可用」
        raise ParserUnavailable(f"解析引擎不可用：{type(e).__name__}: {e}") from e


def available() -> bool:
    try:
        engine()
        return True
    except ParserUnavailable:
        return False


def _char_offsets(content: str, points: list[int]) -> dict[int, int]:
    """把 UTF-8 字节偏移映射成字符偏移。

    kreuzberg 的 chunk.metadata 给的是**字节**偏移（相对全文）。前端划词锚点用字符
    更稳，所以这里换算。按偏移升序逐段解码，避免每个 chunk 都从头扫一遍。
    """
    data = content.encode("utf-8")
    pts = sorted({min(max(int(p), 0), len(data)) for p in points})
    out: dict[int, int] = {}
    prev_b = prev_c = 0
    for b in pts:
        prev_c += len(data[prev_b:b].decode("utf-8", "ignore"))
        out[b] = prev_c
        prev_b = b
    return out


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _map_error(e: Exception) -> ParseFailed:
    """把 kreuzberg 的异常翻译成用户能看懂、接口能分支的错误。"""
    msg = f"{type(e).__name__}: {e}"
    low = msg.lower()
    if "password" in low or "encrypted" in low:
        return PasswordProtected("PDF 已加密，需要密码才能解析")
    if "timeout" in low or "timed out" in low:
        return ParseFailed("解析超时（文件可能过大或结构复杂）", code="parse_timeout")
    if "unsupported" in low or type(e).__name__ in ("ValidationError", "UnsupportedFormatError"):
        # ValidationError 的 message 常常只有一个 mime（如 application/octet-stream）
        detail = str(e).strip()
        return UnsupportedFormat(f"不支持的文件格式{f'（{detail}）' if detail else ''}")
    if "missing" in low and "depend" in low:
        return ParseFailed(f"解析所需组件缺失：{e}", code="parser_missing_dependency", status=503)
    return ParseFailed(f"解析失败：{e}")


def _build_chunks(result: Any, content: str, page_count: int) -> list[dict[str, Any]]:
    raw_chunks = list(getattr(result, "chunks", None) or [])
    points: list[int] = []
    for c in raw_chunks:
        md = getattr(c, "metadata", None) or {}
        points.extend([_as_int(md.get("byte_start")) or 0, _as_int(md.get("byte_end")) or 0])
    cmap = _char_offsets(content, points) if points else {}
    data_len = len(content.encode("utf-8"))

    chunks: list[dict[str, Any]] = []
    for c in raw_chunks:
        md = getattr(c, "metadata", None) or {}
        text = getattr(c, "content", "") or ""
        b0 = min(max(_as_int(md.get("byte_start")) or 0, 0), data_len)
        b1 = min(max(_as_int(md.get("byte_end")) or b0, b0), data_len)
        # 截断后超出正文的块直接丢弃（避免前端拿到空块）
        if content and b0 >= data_len and text:
            continue
        first_page = _as_int(md.get("first_page"))
        last_page = _as_int(md.get("last_page"))
        if first_page is None:
            first_page = 1 if page_count else 0
        if last_page is None:
            last_page = first_page
        chunks.append({
            "idx": len(chunks),
            "content": text,
            "byte_start": b0,
            "byte_end": b1,
            "char_start": cmap.get(b0, 0),
            "char_end": cmap.get(b1, cmap.get(b0, 0)),
            "first_page": max(first_page, 0),
            "last_page": max(last_page, 0),
            "token_count": _as_int(md.get("token_count")),
        })

    if not chunks and content.strip():
        # 极短文档 / 某些格式引擎不给 chunk：整篇兜一块，保证划词有落点。
        chunks = [{
            "idx": 0, "content": content, "byte_start": 0, "byte_end": data_len,
            "char_start": 0, "char_end": len(content),
            "first_page": 1 if page_count else 0, "last_page": page_count,
            "token_count": None,
        }]
    for i, c in enumerate(chunks):  # idx 连续，前端分页稳定
        c["idx"] = i
    return chunks


def parse_file(path: str | Path) -> ParsedMaterial:
    """解析一个本地文件。失败抛 ParseFailed 子类；引擎缺失抛 ParserUnavailable。"""
    k = engine()
    path = Path(path)
    try:
        config = k.ExtractionConfig(
            chunking=k.ChunkingConfig(max_chars=CHUNK_MAX_CHARS, max_overlap=CHUNK_OVERLAP),
            extraction_timeout_secs=PARSE_TIMEOUT,
            use_cache=False,
        )
        result = k.extract_file_sync(str(path), config=config)
    except Exception as e:  # noqa: BLE001 - 统一翻译，不把 Rust 异常抛给 HTTP 层
        raise _map_error(e) from e

    content = getattr(result, "content", "") or ""
    truncated = False
    if len(content) > MAX_TEXT_CHARS:
        content = content[:MAX_TEXT_CHARS]
        truncated = True

    meta = getattr(result, "metadata", None) or {}
    page_count = _as_int(meta.get("page_count")) or 0
    if not page_count:
        pages = getattr(result, "pages", None) or []
        page_count = len(pages)
    mime = str(getattr(result, "mime_type", "") or "")

    if not content.strip():
        raise ParseFailed("没有解析出可用文字（可能是扫描件或空文档；OCR 未开启）",
                          code="empty_text")

    chunks = _build_chunks(result, content, page_count)
    return ParsedMaterial(content=content, chunks=chunks, mime=mime,
                          page_count=page_count, truncated=truncated)
