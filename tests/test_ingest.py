"""Тесты сервиса приёма (модуль ingest)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.ingest import IngestService, chunk_text
from persinder.models import Area, IngestRequest, IngestResult
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class ChunkTextTest(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_text("abc", 100, 10), [(0, 3)])

    def test_overlap(self):
        spans = chunk_text("abcdefghij", 4, 2)
        self.assertEqual(spans[0], (0, 4))
        self.assertEqual(spans[1], (2, 6))
        self.assertEqual(spans[-1][1], 10)

    def test_rejects_bad_params(self):
        with self.assertRaises(ValueError):
            chunk_text("abc", 0, 0)
        with self.assertRaises(ValueError):
            chunk_text("abc", 4, 4)


class IngestServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteStore(root / "db.sqlite")
        self.textstore = FsTextStore(root / "texts")
        self.service = IngestService(
            self.store, self.textstore, NoopStemmer(), chunk_size=1000, chunk_overlap=100
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _request(
        self,
        text: str = "Привет мир",
        area: Area = Area.PERSONAL,
        filename: str = "a.txt",
    ) -> IngestRequest:
        return IngestRequest(filename=filename, area=area, text=text)

    def test_indexed(self):
        result = self.service.ingest(self._request())
        self.assertEqual(result.status, IngestResult.INDEXED)
        self.assertEqual(result.chunks, 1)
        self.assertIsNotNone(result.document_id)
        # текст ушёл во временный каталог
        doc_id = result.document_id
        assert doc_id is not None
        self.assertTrue(self.textstore.exists(doc_id))
        # морфологический поиск находит документ
        found = self.store.search_morph("привет")
        self.assertEqual(len(found), 1)
        # задача на эмбеддинги поставлена
        self.assertEqual(len(self.store.pending_embeddings()), 1)

    def test_duplicate_by_hash(self):
        first = self.service.ingest(self._request())
        second = self.service.ingest(self._request(area=Area.FOREIGN, filename="copy.txt"))
        self.assertEqual(second.status, IngestResult.ALREADY)
        self.assertEqual(second.document_id, first.document_id)
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["sources"], 2)

    def test_rejects_empty(self):
        result = self.service.ingest(self._request(text="   "))
        self.assertEqual(result.status, IngestResult.REJECTED)

    def test_chunking_many(self):
        long_text = "".join(f"слово{i} " for i in range(50))
        service = IngestService(
            self.store, self.textstore, NoopStemmer(), chunk_size=40, chunk_overlap=8
        )
        result = service.ingest(
            IngestRequest(filename="big.txt", area=Area.WEB, text=long_text)
        )
        self.assertEqual(result.status, IngestResult.INDEXED)
        self.assertGreater(result.chunks, 1)
        self.assertEqual(
            len(self.store.pending_embeddings(limit=1000)), result.chunks
        )


if __name__ == "__main__":
    unittest.main()
