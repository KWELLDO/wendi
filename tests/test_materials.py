"""材料：上传（流式、绕过 MAX_BODY）/ 解析 / 归属 / 全文与分块读取。

运行：python3 -m unittest discover -s tests -v
真解析用例需要 kreuzberg；本机可用 /tmp/kztest/bin/python 跑：
    /tmp/kztest/bin/python -m unittest tests.test_materials -v
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
_tmpdir = tempfile.mkdtemp(prefix="wendi-materials-")

import db  # noqa: E402
import materials  # noqa: E402
import server  # noqa: E402

PDF_SAMPLE = Path.home() / "Documents/Obsidian-knowledge/附件/pdf/2_共产主义原理.pdf"
PDF_ENCRYPTED = Path.home() / "Documents/纳税记录.pdf"
_DEFAULT_TOKEN = object()


class Handler(server.Handler):
    """直调 handler：只补 HTTP 输入（headers/rfile/path）与 _json 输出。"""

    def __init__(self, token=None, body: bytes = b"", headers: dict | None = None,
                 path: str = "/api/materials", ip: str = "203.0.113.45"):
        self.status, self.payload = None, None
        self.headers = dict(headers or {})
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.rfile = io.BytesIO(body)
        self.client_address = (ip, 1234)
        self.path = path
        self._uid = None
        self._error = ""
        self._cost_cents = 0
        self._t0 = 0.0
        self._sid_hint = "s_materials"

    def _json(self, code, obj):
        self.status, self.payload = code, obj

    def send_response(self, code, message=None):
        self.status = code

    def _qs(self):
        return {}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_db = db.DB_PATH
        cls._old_dir = server.MATERIALS_DIR
        db.DB_PATH = Path(_tmp.name)
        db.init()
        server.MATERIALS_DIR = Path(_tmpdir)

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._old_db
        server.MATERIALS_DIR = cls._old_dir
        shutil.rmtree(_tmpdir, ignore_errors=True)

    def setUp(self):
        for t in ("material_chunks", "materials", "sessions", "users", "events"):
            with db._conn() as c:
                c.execute(f"DELETE FROM {t}")
        self.uid = db.create_user(f"m{os.urandom(4).hex()}@example.com", "s", "h")["id"]
        self.token = f"tok-{os.urandom(8).hex()}"
        db.create_session(self.token, self.uid)

    # —— helpers ——
    def upload(self, data: bytes, filename: str | None = "a.txt",
               mime: str = "text/plain", token=_DEFAULT_TOKEN,
               headers: dict | None = None) -> Handler:
        h = Handler(token=self.token if token is _DEFAULT_TOKEN else token, body=data)
        h.headers.update(headers or {})
        h.headers["Content-Length"] = str(len(data))
        if filename is not None:
            h.headers["X-Wendi-Filename"] = filename
        if mime:
            h.headers["Content-Type"] = mime
        h._do_POST()
        return h

    def add_material(self, uid: int, name: str = "a.txt") -> tuple[int, Path]:
        row_dir = Path(_tmpdir) / str(uid)
        row_dir.mkdir(parents=True, exist_ok=True)
        path = row_dir / f"{os.urandom(6).hex()}.txt"
        path.write_text("hello world", encoding="utf-8")
        chunks = [{"idx": 0, "content": "hello world", "byte_start": 0, "byte_end": 11,
                   "char_start": 0, "char_end": 11, "first_page": 0, "last_page": 0,
                   "token_count": None}]
        mid = db.material_save(uid, name, "text/plain", "txt", 11, "sha", str(path),
                               "hello world", 0, chunks)
        return mid, path

    def get(self, path: str, token=_DEFAULT_TOKEN) -> Handler:
        h = Handler(token=self.token if token is _DEFAULT_TOKEN else token, path=path)
        h._do_GET()
        return h

    def delete(self, path: str, token=_DEFAULT_TOKEN) -> Handler:
        h = Handler(token=self.token if token is _DEFAULT_TOKEN else token, path=path)
        h._do_DELETE()
        return h


class UploadTests(Base):
    def test_requires_login(self):
        h = self.upload(b"x", token=None)
        self.assertEqual(h.status, 401)
        self.assertEqual(db.material_count(self.uid), 0)

    def test_requires_filename(self):
        h = self.upload(b"x", filename=None)
        self.assertEqual(h.status, 400)
        self.assertEqual(h.payload["code"], "filename_required")

    def test_requires_content_length(self):
        h = Handler(token=self.token, body=b"")
        h.headers["X-Wendi-Filename"] = "a.txt"
        h._do_POST()
        self.assertEqual(h.status, 411)

    def test_rejects_oversize_without_reading(self):
        old = server.UPLOAD_MAX_BYTES
        server.UPLOAD_MAX_BYTES = 10
        try:
            h = self.upload(b"x" * 11)
        finally:
            server.UPLOAD_MAX_BYTES = old
        self.assertEqual(h.status, 413)
        self.assertEqual(h.payload["code"], "file_too_large")
        self.assertEqual(h.rfile.tell(), 0, "超限时不应读取请求体")

    def test_upload_bypasses_max_body_and_saves(self):
        data = b"x" * (server.MAX_BODY + 123)
        parsed = materials.ParsedMaterial(content="正文", mime="text/plain", page_count=1,
                                          chunks=[{"idx": 0, "content": "正文",
                                                   "byte_start": 0, "byte_end": 6,
                                                   "char_start": 0, "char_end": 2,
                                                   "first_page": 1, "last_page": 1,
                                                   "token_count": None}])
        with mock.patch.object(server.materials, "parse_file", return_value=parsed):
            h = self.upload(data, filename="长 文档.txt")
        self.assertEqual(h.status, 201, h.payload)
        self.assertEqual(h.payload["filename"], "长 文档.txt")
        self.assertEqual(h.payload["size"], len(data))
        mid = h.payload["id"]
        row = db.material_get(self.uid, mid)
        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "正文")
        self.assertEqual(row["char_count"], 2)
        self.assertEqual(row["chunk_count"], 1)
        self.assertTrue(Path(row["path"]).is_file())
        self.assertEqual(Path(row["path"]).read_bytes(), data)
        self.assertEqual(db.material_chunks(mid)[0]["char_end"], 2)

    def test_parse_failure_cleans_file_and_row(self):
        with mock.patch.object(server.materials, "parse_file",
                               side_effect=materials.PasswordProtected()):
            h = self.upload(b"%PDF encrypted")
        self.assertEqual(h.status, 422)
        self.assertEqual(h.payload["code"], "password_protected")
        self.assertEqual(db.material_count(self.uid), 0)
        uid_dir = Path(_tmpdir) / str(self.uid)
        self.assertEqual(list(uid_dir.glob("*")) if uid_dir.exists() else [], [])

    def test_parser_unavailable_is_503(self):
        with mock.patch.object(server.materials, "parse_file",
                               side_effect=materials.ParserUnavailable("没装")):
            h = self.upload(b"x")
        self.assertEqual(h.status, 503)
        self.assertEqual(h.payload["code"], "parser_unavailable")
        self.assertEqual(db.material_count(self.uid), 0)

    def test_material_count_limit(self):
        old = server.MATERIALS_MAX
        server.MATERIALS_MAX = 0
        try:
            h = self.upload(b"x")
        finally:
            server.MATERIALS_MAX = old
        self.assertEqual(h.status, 429)
        self.assertEqual(h.payload["code"], "material_limit")

    def test_material_bytes_limit(self):
        self.add_material(self.uid)  # 已有 11 字节
        old = server.MATERIALS_MAX_BYTES
        server.MATERIALS_MAX_BYTES = 10
        try:
            h = self.upload(b"x" * 5)
        finally:
            server.MATERIALS_MAX_BYTES = old
        self.assertEqual(h.status, 429)
        self.assertEqual(h.payload["code"], "material_quota")

    def test_filename_path_traversal_is_stripped(self):
        parsed = materials.ParsedMaterial(content="x", mime="text/plain")
        with mock.patch.object(server.materials, "parse_file", return_value=parsed):
            h = self.upload(b"x", filename="..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(h.status, 201, h.payload)
        self.assertEqual(h.payload["filename"], "passwd")
        row = db.material_get(self.uid, h.payload["id"])
        self.assertTrue(str(row["path"]).startswith(str(Path(_tmpdir) / str(self.uid))))


class ReadTests(Base):
    def test_list_is_scoped_to_user(self):
        mid, _ = self.add_material(self.uid, "mine.txt")
        h = self.get("/api/materials")
        self.assertEqual(h.status, 200)
        self.assertEqual([m["id"] for m in h.payload["items"]], [mid])
        self.assertEqual(h.payload["total"], 1)
        self.assertNotIn("path", h.payload["items"][0])

        other = db.create_user(f"o{os.urandom(4).hex()}@example.com", "s", "h")["id"]
        other_token = f"tok-{os.urandom(8).hex()}"
        db.create_session(other_token, other)
        h2 = self.get("/api/materials", token=other_token)
        self.assertEqual(h2.payload["items"], [])

    def test_detail_returns_content_and_hides_path(self):
        mid, _ = self.add_material(self.uid)
        h = self.get(f"/api/materials/{mid}")
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["content"], "hello world")
        self.assertNotIn("path", h.payload["material"])
        h2 = self.get(f"/api/materials/{mid}?content=0")
        self.assertNotIn("content", h2.payload)

    def test_other_users_material_is_404(self):
        mid, _ = self.add_material(self.uid)
        other = db.create_user(f"o{os.urandom(4).hex()}@example.com", "s", "h")["id"]
        other_token = f"tok-{os.urandom(8).hex()}"
        db.create_session(other_token, other)
        self.assertEqual(self.get(f"/api/materials/{mid}", token=other_token).status, 404)
        self.assertEqual(self.get(f"/api/materials/{mid}/chunks", token=other_token).status, 404)

    def test_chunks_pagination(self):
        mid, _ = self.add_material(self.uid)
        for i in range(1, 4):
            with db._conn() as c:  # 同一个事务里插入并提交，避免泄漏未提交连接
                c.execute("INSERT INTO material_chunks (material_id, idx, content, byte_start, "
                          "byte_end, char_start, char_end, first_page, last_page) "
                          "VALUES (?,?,?,?,?,?,?,?,?)",
                          (mid, i, f"c{i}", i, i + 1, i, i + 1, i + 1, i + 1))
                c.execute("UPDATE materials SET chunk_count=4 WHERE id=?", (mid,))
        h = self.get(f"/api/materials/{mid}/chunks?offset=1&limit=2")
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["total"], 4)
        self.assertEqual([c["idx"] for c in h.payload["chunks"]], [1, 2])
        self.assertEqual(h.payload["chunks"][0]["first_page"], 2)

    def test_requires_login(self):
        self.assertEqual(self.get("/api/materials", token=None).status, 401)


class DeleteTests(Base):
    def test_delete_removes_row_and_file(self):
        mid, path = self.add_material(self.uid)
        h = self.delete(f"/api/materials/{mid}")
        self.assertEqual(h.status, 200)
        self.assertFalse(path.exists())
        self.assertIsNone(db.material_get(self.uid, mid))

    def test_cannot_delete_other_users_material(self):
        mid, path = self.add_material(self.uid)
        other = db.create_user(f"o{os.urandom(4).hex()}@example.com", "s", "h")["id"]
        other_token = f"tok-{os.urandom(8).hex()}"
        db.create_session(other_token, other)
        h = self.delete(f"/api/materials/{mid}", token=other_token)
        self.assertEqual(h.status, 404)
        self.assertTrue(path.exists())
        self.assertIsNotNone(db.material_get(self.uid, mid))


@unittest.skipUnless(materials.available(), "kreuzberg 未安装（用 /tmp/kztest/bin/python 跑真解析用例）")
class RealParserTests(Base):
    def test_txt_parse_has_offsets(self):
        p = Path(_tmpdir) / f"real-{os.urandom(4).hex()}.txt"
        p.write_text("第一段。" * 80 + "\n\n" + "第二段。" * 80, encoding="utf-8")
        parsed = materials.parse_file(p)
        self.assertTrue(parsed.content)
        self.assertGreaterEqual(len(parsed.chunks), 1)
        for c in parsed.chunks:
            self.assertLessEqual(c["char_start"], c["char_end"])
            self.assertLessEqual(c["char_end"], len(parsed.content))

    def test_upload_txt_end_to_end(self):
        data = ("问底材料测试。" * 50).encode("utf-8")
        h = self.upload(data, filename="材料.txt")
        self.assertEqual(h.status, 201, h.payload)
        self.assertGreater(h.payload["char_count"], 0)
        detail = self.get(f"/api/materials/{h.payload['id']}")
        self.assertEqual(detail.payload["content"], data.decode("utf-8"))

    @unittest.skipUnless(PDF_SAMPLE.exists(), f"缺少样例 {PDF_SAMPLE}")
    def test_pdf_pages_and_chunk_pages(self):
        parsed = materials.parse_file(PDF_SAMPLE)
        self.assertEqual(parsed.mime, "application/pdf")
        self.assertEqual(parsed.page_count, 18)
        self.assertGreater(parsed.page_count, 0)
        self.assertTrue(all(1 <= c["first_page"] <= parsed.page_count for c in parsed.chunks))
        for c in parsed.chunks:
            self.assertLessEqual(c["first_page"], c["last_page"])
            self.assertLessEqual(c["last_page"], parsed.page_count)

    @unittest.skipUnless(PDF_ENCRYPTED.exists(), f"缺少样例 {PDF_ENCRYPTED}")
    def test_encrypted_pdf_reports_password_protected(self):
        with self.assertRaises(materials.PasswordProtected) as ctx:
            materials.parse_file(PDF_ENCRYPTED)
        self.assertEqual(ctx.exception.code, "password_protected")
        self.assertEqual(ctx.exception.status, 422)

    @unittest.skipUnless(PDF_ENCRYPTED.exists(), f"缺少样例 {PDF_ENCRYPTED}")
    def test_encrypted_upload_returns_422(self):
        h = self.upload(PDF_ENCRYPTED.read_bytes(), filename="纳税记录.pdf",
                        mime="application/pdf")
        self.assertEqual(h.status, 422, h.payload)
        self.assertEqual(h.payload["code"], "password_protected")
        self.assertEqual(db.material_count(self.uid), 0)


if __name__ == "__main__":
    unittest.main()
