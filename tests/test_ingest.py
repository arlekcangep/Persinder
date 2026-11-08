"""Тесты сервиса приёма (модуль ingest)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.embed import file_sidecar_path
from persinder.ingest import IngestRequest, IngestResult, IngestService, chunk_text
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
            self.store,
            self.textstore,
            NoopStemmer(),
            chunk_size=1000,
            chunk_overlap=100,
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
        second = self.service.ingest(
            self._request(area=Area.FOREIGN, filename="copy.txt")
        )
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
        self.assertEqual(len(self.store.pending_embeddings(limit=1000)), result.chunks)


class IngestDuplicateQueueTest(unittest.TestCase):
    """Повторная встреча файла не пересчитывает уже готовые эмбеддинги.

    Сайдкар — признак, что документ отработан под моделью для этого источника.
    Иначе каждый запуск с ``--reindex`` (сброс журнала → все файлы снова
    попадают в ingest) ставил бы в очередь полный пересчёт — постоянный CPU.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        self.store = SqliteStore(root / "db.sqlite")
        self.textstore = FsTextStore(root / "texts")
        self.service = IngestService(
            self.store,
            self.textstore,
            NoopStemmer(),
            virtual_dir=root / "virtual",
            embedding_models=("placeholder-v1",),
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _ingest(self, src: Path, text: str) -> IngestResult:
        return self.service.ingest(
            IngestRequest(
                filename=src.name,
                area=Area.PERSONAL,
                text=text,
                source_path=str(src),
            )
        )

    def _fake_sidecar(self, src: Path) -> Path:
        path = file_sidecar_path(src, "placeholder-v1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sidecar")
        return path

    def test_sidecar_present_means_no_reenqueue(self):
        text = "документ про повторную индексацию"
        src = self.vault / "a.md"
        self._ingest(src, text)
        self.assertEqual(self.store.counts()["pending_embeddings"], 1)
        # Воркер разобрал очередь и оставил сайдкар.
        self.store.drop_queue_model("placeholder-v1")
        self._fake_sidecar(src)

        result = self._ingest(src, text)
        self.assertEqual(result.status, IngestResult.ALREADY)
        self.assertEqual(self.store.counts()["pending_embeddings"], 0)

    def test_reingest_fills_empty_source_path(self):
        """Старая запись без пути лечится: при повторной встрече путь пишется."""
        src = self.vault / "a.md"
        text = "документ, чей путь когда-то потерялся"
        first = self.service.ingest(
            IngestRequest(filename=src.name, area=Area.PERSONAL, text=text)
        )
        doc_id = first.document_id
        assert doc_id is not None
        self.assertEqual(self.store.source_paths_for_doc(doc_id), [])

        again = self._ingest(src, text)
        self.assertEqual(again.status, IngestResult.ALREADY)
        self.assertEqual(self.store.source_paths_for_doc(doc_id), [str(src)])

    def test_new_source_without_sidecar_is_enqueued(self):
        text = "общий текст двух копий"
        first = self.vault / "a.md"
        second = self.vault / "b.md"
        self._ingest(first, text)
        self.store.drop_queue_model("placeholder-v1")
        self._fake_sidecar(first)

        result = self._ingest(second, text)
        self.assertEqual(result.status, IngestResult.ALREADY)
        self.assertEqual(
            self.store.counts()["pending_embeddings"],
            1,
            "у нового источника своего сайдкара нет — задача нужна",
        )
        self.assertFalse(file_sidecar_path(second, "placeholder-v1").exists())


if __name__ == "__main__":
    unittest.main()
