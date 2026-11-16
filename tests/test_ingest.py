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

    def test_request_embedding_models_override(self):
        result = self.service.ingest(
            IngestRequest(
                filename="own.md",
                area=Area.PERSONAL,
                text="текст с моделью папки",
                embedding_models=("m-own",),
            )
        )
        self.assertEqual(result.status, IngestResult.INDEXED)
        self.assertEqual(
            [item["model"] for item in self.store.pending_embeddings(limit=100)],
            ["m-own"],
        )

    def test_request_embedding_models_empty_disables_semantics(self):
        result = self.service.ingest(
            IngestRequest(
                filename="no-sem.md",
                area=Area.PERSONAL,
                text="только морфология",
                embedding_models=(),
            )
        )
        self.assertEqual(result.status, IngestResult.INDEXED)
        self.assertEqual(self.store.pending_embeddings(limit=100), [])

    def test_duplicate_uses_request_models(self):
        text = "общий текст с моделью папки"
        first = self.service.ingest(
            IngestRequest(
                filename="a.md",
                area=Area.PERSONAL,
                text=text,
                embedding_models=("m-own",),
            )
        )
        second = self.service.ingest(
            IngestRequest(
                filename="b.md",
                area=Area.FOREIGN,
                text=text,
                source_path="/vault/b.md",
                embedding_models=("m-own",),
            )
        )
        self.assertEqual(first.document_id, second.document_id)
        self.assertEqual(
            sorted(item["model"] for item in self.store.pending_embeddings(limit=100)),
            ["m-own", "m-own"],
        )

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


class CacheTextPolicyTest(unittest.TestCase):
    """D27: текст не всех экстракторов хранится в кэше.

    ``cache_text=False`` — текст воспроизводим из оригинала: на диске не
    дублируется, но документ индексируется и эмбеддинги ставятся как обычно
    (воркер перечитает файл). Дубликаты делят один файл кэша, поэтому
    кэшируемый источник «поднимает» флаг и для документа без кэша.
    """

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

    def _ingest(self, filename: str, text: str, *, cache_text: bool) -> IngestResult:
        return self.service.ingest(
            IngestRequest(
                filename=filename,
                area=Area.PERSONAL,
                text=text,
                source_path=f"/vault/{filename}",
                cache_text=cache_text,
            )
        )

    def test_uncached_text_is_not_stored_but_indexed(self):
        result = self._ingest("note.md", "заметка без кэша", cache_text=False)
        self.assertEqual(result.status, IngestResult.INDEXED)
        doc_id = result.document_id
        assert doc_id is not None
        self.assertFalse(self.textstore.exists(doc_id))
        self.assertEqual(self.store.text_cached_docs([doc_id]), set())
        self.assertEqual(self.store.counts()["documents"], 1)
        # семантика не выключается: воркер перечитает оригинал (D27)
        self.assertEqual(len(self.store.pending_embeddings()), 1)

    def test_cached_by_default(self):
        result = self._ingest("note.md", "обычная заметка", cache_text=True)
        doc_id = result.document_id
        assert doc_id is not None
        self.assertTrue(self.textstore.exists(doc_id))
        self.assertEqual(self.store.text_cached_docs([doc_id]), {doc_id})

    def test_cachable_duplicate_upgrades_uncached_document(self):
        text = "одинаковый текст в двух файлах"
        first = self._ingest("a.md", text, cache_text=False)
        self.assertEqual(first.status, IngestResult.INDEXED)
        doc_id = first.document_id
        assert doc_id is not None
        self.assertFalse(self.textstore.exists(doc_id))

        second = self._ingest("b.pdf", text, cache_text=True)

        self.assertEqual(second.status, IngestResult.ALREADY)
        self.assertEqual(second.document_id, doc_id)
        self.assertTrue(
            self.textstore.exists(doc_id),
            "кэшируемому источнику текст нужен на диске",
        )
        self.assertEqual(self.store.text_cached_docs([doc_id]), {doc_id})

    def test_uncachable_duplicate_keeps_cached_text(self):
        text = "текст уже лежит в кэше"
        first = self._ingest("a.pdf", text, cache_text=True)
        doc_id = first.document_id
        assert doc_id is not None

        self._ingest("b.md", text, cache_text=False)

        self.assertTrue(self.textstore.exists(doc_id))
        self.assertEqual(self.store.text_cached_docs([doc_id]), {doc_id})

    def test_rescan_with_disabled_cache_drops_text(self):
        """Тот же файл перечитан с `cache_text=False` — текст уходит с диска."""
        text = "переключаем кэш выключен"
        first = self._ingest("note.md", text, cache_text=True)
        doc_id = first.document_id
        assert doc_id is not None
        self.assertTrue(self.textstore.exists(doc_id))

        again = self._ingest("note.md", text, cache_text=False)

        self.assertEqual(again.status, IngestResult.ALREADY)
        self.assertEqual(again.document_id, doc_id)
        self.assertFalse(self.textstore.exists(doc_id))
        self.assertFalse(self.store.document_wants_cache(doc_id))

    def test_removing_last_cached_source_drops_text(self):
        """Остался только некэшируемый источник — кэш больше не нужен."""
        text = "общий текст двух источников"
        first = self._ingest("a.pdf", text, cache_text=True)
        doc_id = first.document_id
        assert doc_id is not None
        self._ingest("b.md", text, cache_text=False)

        result = self.service.remove_source(Area.PERSONAL, "a.pdf")

        self.assertTrue(result.found)
        self.assertFalse(self.textstore.exists(doc_id))
        self.assertFalse(self.store.document_wants_cache(doc_id))


class IngestDuplicateQueueTest(unittest.TestCase):
    """Повторная встреча файла не пересчитывает уже готовые эмбеддинги.

    Сайдкар — признак, что документ отработан под моделью для этого источника:
    watcher на каждом проходе снова видит те же файлы, и без этой проверки
    очередь пополнялась бы постоянным пересчётом.
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
        self.assertEqual(self.store.source_path(Area.PERSONAL.value, src.name), "")

        again = self._ingest(src, text)
        self.assertEqual(again.status, IngestResult.ALREADY)
        self.assertEqual(
            self.store.source_path(Area.PERSONAL.value, src.name), str(src)
        )

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
