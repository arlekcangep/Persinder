"""Сквозной тест: HTTP-сервер → ingest → store/FTS → search.

Поднимается реальный сервер (``build_service`` + ``make_server``), текст
отправляется по HTTP, находится поиском, проверяется файл сырого текста
на диске и персистентность после переоткрытия БД.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from persinder.__main__ import build_service
from persinder.api import make_server
from persinder.config import Settings
from persinder.domain import Area
from persinder.store import SqliteStore

TEXT = (
    "Кот сидит на коврике в комнате. The quick brown fox jumps over the lazy dog. "
) * 5


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.data_dir = root / "data"
        settings = Settings(data_dir=self.data_dir)
        self.ingest, self.search, self.store = build_service(settings)
        self.server = make_server(self.ingest, self.search, host="127.0.0.1", port=0)
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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request)

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(self.base + path) as resp:
            return json.load(resp)

    def test_full_pipeline(self):
        # 1. POST /ingest по HTTP.
        with self._post(
            "/ingest", {"filename": "note.md", "area": "personal", "text": TEXT}
        ) as resp:
            body = json.load(resp)
        self.assertEqual(body["status"], "indexed")
        doc_id = body["document_id"]
        self.assertTrue(doc_id)
        self.assertGreater(body["chunks"], 0)

        # 2. Файл сырого текста лежит на диске.
        text_files = list((self.data_dir / "pending_texts").iterdir())
        self.assertEqual(len(text_files), 1)
        self.assertEqual(text_files[0].read_text(encoding="utf-8"), TEXT)

        # 3. GET /search находит документ (морфологический слой).
        result = self._get("/search?q=" + urllib.parse.quote("котов сидят"))
        hits = result["hits"]
        self.assertTrue(hits)
        self.assertIn(hits[0]["document_id"], {doc_id})
        self.assertEqual(hits[0]["chunk_index"], 0)

    def test_modified_file_replaces_document(self):
        # Файл изменился: старый документ вытесняется, в индексе только новый.
        with self._post(
            "/ingest",
            {
                "filename": "note.md",
                "area": "personal",
                "text": "первый вариант заметки про котиков",
            },
        ) as resp:
            first = json.load(resp)["document_id"]
        with self._post(
            "/ingest",
            {
                "filename": "note.md",
                "area": "personal",
                "text": "второй вариант заметки про флакс",
            },
        ) as resp:
            second = json.load(resp)["document_id"]
        self.assertNotEqual(first, second)
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["sources"], 1)
        old_hits = self._get("/search?q=" + urllib.parse.quote("котиков"))["hits"]
        self.assertEqual(old_hits, [])
        new_hits = self._get("/search?q=" + urllib.parse.quote("флакс"))["hits"]
        self.assertEqual(new_hits[0]["document_id"], second)
        # у вытесненного старого документа удалён и сырой текст
        self.assertEqual(len(list((self.data_dir / "pending_texts").iterdir())), 1)

    def test_deletion_through_pipeline(self):
        # Индексируем через HTTP, затем удаляем источник через ingest-сервис —
        # как это делает watcher при DELETED-событии.
        with self._post(
            "/ingest",
            {"filename": "note.md", "area": "personal", "text": TEXT},
        ) as resp:
            doc_id = json.load(resp)["document_id"]
        self.assertEqual(
            self.store.counts()["documents"], 1, "документ проиндексирован"
        )
        result = self.ingest.remove_source(Area.PERSONAL, "note.md")
        self.assertEqual(result.document_id, doc_id)
        self.assertEqual(self.store.counts()["documents"], 0)
        # сырой текст удалён вместе с документом
        self.assertEqual(list((self.data_dir / "pending_texts").iterdir()), [])

        with self._post(
            "/ingest", {"filename": "note.md", "area": "personal", "text": TEXT}
        ) as resp:
            doc_id = json.load(resp)["document_id"]
        self.store.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

        # Переоткрываем ту же БД — документ и поиск живут.
        reopened = SqliteStore(self.data_dir / "persinder.db")
        try:
            self.assertEqual(reopened.find_document_by_hash(doc_id), doc_id)
            counts = reopened.counts()
            self.assertEqual(counts["documents"], 1)
            self.assertGreater(counts["chunks"], 0)
            self.assertEqual(counts["pending_embeddings"], counts["chunks"])
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
