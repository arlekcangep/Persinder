"""Тесты HTTP-сервиса приёма."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlencode

from persinder.api import make_server
from persinder.ingest import IngestService
from persinder.morphology import NoopStemmer
from persinder.search import SearchService
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        ingest = IngestService(
            self.store, self.texts, NoopStemmer(), chunk_size=40, chunk_overlap=0
        )
        search = SearchService(self.store, NoopStemmer(), textstore=self.texts)
        self.server = make_server(ingest, search, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        addr = self.server.server_address
        self.base = f"http://{addr[0]}:{addr[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self._tmp.cleanup()

    def _post(self, path: str, payload: dict[str, object]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request)

    def test_health(self):
        with urllib.request.urlopen(self.base + "/health") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.load(resp)["status"], "ok")

    def test_ingest_ok(self):
        with self._post(
            "/ingest", {"filename": "a.txt", "area": "personal", "text": "Привет мир"}
        ) as resp:
            body = json.load(resp)
        self.assertEqual(body["status"], "indexed")
        self.assertEqual(body["chunks"], 1)

    def test_ingest_bad_area(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/ingest", {"filename": "a.txt", "area": "nope", "text": "x"})
        self.assertEqual(ctx.exception.code, 400)

    def test_ingest_empty_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/ingest", {"filename": "a.txt", "area": "web", "text": "  "})
        self.assertEqual(ctx.exception.code, 422)

    def _search(self, query: str) -> Any:
        params = urlencode({"q": query, "limit": "5"})
        with urllib.request.urlopen(f"{self.base}/search?{params}") as resp:
            return json.load(resp)

    def test_search_hit_has_context_three_lines_around(self):
        text = (
            "line one\nline two\nline three\nline four\nline five\n"
            "line six\nline seven\nline eight\nline nine\n"
        )
        with self._post(
            "/ingest", {"filename": "ctx.txt", "area": "personal", "text": text}
        ) as resp:
            self.assertEqual(json.load(resp)["status"], "indexed")

        body = self._search("four")
        self.assertTrue(body["hits"])
        hit = body["hits"][0]
        self.assertEqual(hit["chunk_index"], 0)
        self.assertEqual(
            hit["context"],
            "line one\nline two\nline three\nline four\n"
            "line five\nline six\nline seven\nline eight",
        )

    def test_search_context_is_null_without_raw_text(self):
        with self._post(
            "/ingest",
            {"filename": "gone.txt", "area": "personal", "text": "исчезнувший текст"},
        ) as resp:
            doc_id = json.load(resp)["document_id"]
        self.texts.delete(doc_id)

        body = self._search("исчезнувший")
        self.assertTrue(body["hits"])
        self.assertIsNone(body["hits"][0]["context"])


if __name__ == "__main__":
    unittest.main()
