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

    def test_search_invalid_min_score(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/search?q=x&min_score=abc")
        self.assertEqual(ctx.exception.code, 400)

    def test_search_invalid_layer_limit(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/search?q=x&morph_limit=abc")
        self.assertEqual(ctx.exception.code, 400)

    def test_search_morph_limit(self):
        for i in range(3):
            with self._post(
                "/ingest",
                {
                    "filename": f"n{i}.txt",
                    "area": "personal",
                    "text": f"котик номер {i}",
                },
            ) as resp:
                self.assertEqual(json.load(resp)["status"], "indexed")

        params = urlencode({"q": "котик", "limit": "10", "morph_limit": "2"})
        url = f"{self.base}/search?{params}"
        with urllib.request.urlopen(url) as resp:
            body = json.load(resp)

        self.assertEqual(len(body["hits"]), 2, "морфология ограничена morph_limit")

    def test_search_hit_has_context_three_sentences_around(self):
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
        self.assertEqual(hit["area"], "personal")
        self.assertEqual(hit["filename"], "ctx.txt")
        self.assertEqual(hit["source_path"], "", "HTTP-вставка — виртуальный документ")
        # Контекст — от найденного слова, по 3 предложения назад и вперёд,
        # а не весь чанк (строки с 4-й по 7-ю не попадают).
        self.assertEqual(
            hit["context"],
            "line one\nline two\nline three\nline four\n"
            "line five\nline six\nline seven",
        )

    def test_search_hit_context_is_bounded_not_whole_chunk(self):
        # Чанк маленький, но контекст всё равно только вокруг найденного слова:
        # ничего дальше 3 предложений от хита не выводится.
        text = (
            "alpha one\nalpha two\nцелевое слово\n"
            "delta one\ndelta two\ndelta three\ndelta four\ndelta five\n"
            "delta six\ndelta seven\ndelta eight\ndelta nine\n"
        )
        with self._post(
            "/ingest", {"filename": "ctx2.txt", "area": "personal", "text": text}
        ) as resp:
            self.assertEqual(json.load(resp)["status"], "indexed")

        body = self._search("слово")
        self.assertTrue(body["hits"])
        hit = body["hits"][0]
        self.assertIn("целевое слово", hit["context"])
        self.assertNotIn("delta eight", hit["context"])
        self.assertNotIn("delta nine", hit["context"])

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
