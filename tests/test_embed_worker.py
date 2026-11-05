"""Тесты воркера эмбеддингов: очередь → векторы → сайдкары."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.embed import (
    EmbedWorker,
    PlaceholderEmbedder,
    file_sidecar_path,
    read_sidecar,
    virtual_sidecar_path,
)
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class EmbedWorkerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.data = root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.virtual = self.data / "virtual"
        self.store = SqliteStore(self.data / "db.sqlite")
        self.textstore = FsTextStore(self.data / "pending_texts")
        self.embedder = PlaceholderEmbedder()
        self.ingest = IngestService(
            self.store,
            self.textstore,
            NoopStemmer(),
            virtual_dir=self.virtual,
        )
        self.worker = EmbedWorker(
            self.store,
            self.textstore,
            self.embedder,
            chunk_size=1200,
            chunk_overlap=200,
            virtual_dir=self.virtual,
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _file(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.ingest.ingest(
            IngestRequest(
                filename=name,
                area=Area.PERSONAL,
                text=text,
                source_path=str(path),
            )
        )
        return path

    def test_file_sidecar_written(self):
        src = self._file("books/one.md", "заметка про квантовый флакс")
        self.assertEqual(self.store.counts()["pending_embeddings"], 1)
        stats = self.worker.run_once(limit=10)
        self.assertEqual((stats.docs, stats.sidecars, stats.errors), (1, 1, 0))
        data = read_sidecar(file_sidecar_path(src))
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.fingerprint.model, "placeholder-v1")
        self.assertEqual(data.fingerprint.dim, self.embedder.dim)
        self.assertEqual(self.store.counts()["pending_embeddings"], 0)

    def test_virtual_sidecar_in_virtual_dir(self):
        text = "вставленный текст без файла на диске"
        outcome = self.ingest.ingest(
            IngestRequest(filename="note.md", area=Area.PERSONAL, text=text)
        )
        doc_id = outcome.document_id
        self.assertIsNotNone(doc_id)
        stats = self.worker.run_once()
        self.assertEqual(stats.sidecars, 1)
        assert doc_id is not None
        self.assertIsNotNone(read_sidecar(virtual_sidecar_path(self.virtual, doc_id)))
        # рядом с файлом сайдкара нет — файла нет
        self.assertEqual(list(self.root.glob("**/.persinder")), [])

    def test_duplicate_gets_its_own_sidecar(self):
        text = "общий текст про котиков и васильки"
        src1 = self._file("books/a.md", text)
        src2 = self._file("books/b.md", text)
        # два источника — по очереди на каждый (уникальная пара doc+path)
        self.assertEqual(self.store.counts()["pending_embeddings"], 2)
        stats = self.worker.run_once()
        self.assertEqual(stats.sidecars, 2)
        self.assertIsNotNone(read_sidecar(file_sidecar_path(src1)))
        self.assertIsNotNone(read_sidecar(file_sidecar_path(src2)))

    def test_missing_text_drops_queue(self):
        text = "документ, чей текст пропал до обработки"
        outcome = self.ingest.ingest(
            IngestRequest(filename="lost.md", area=Area.PERSONAL, text=text)
        )
        doc_id = outcome.document_id
        assert doc_id is not None
        self.textstore.delete(doc_id)
        stats = self.worker.run_once()
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.docs, 0)
        self.assertEqual(self.store.counts()["pending_embeddings"], 0)

    def test_second_run_is_idle(self):
        self._file("books/one.md", "текст для проверки идемпотентности")
        self.worker.run_once()
        stats = self.worker.run_once()
        self.assertEqual((stats.docs, stats.skipped), (0, 0))

    def test_modify_replaces_sidecar(self):
        src = self.root / "note.md"
        src.write_text("первая версия текста", encoding="utf-8")
        self.ingest.ingest(
            IngestRequest(
                filename="note.md",
                area=Area.PERSONAL,
                text="первая версия текста",
                source_path=str(src),
            )
        )
        self.worker.run_once()
        before = read_sidecar(file_sidecar_path(src))
        self.assertIsNotNone(before)

        src.write_text("вторая версия, совсем другая", encoding="utf-8")
        self.ingest.ingest(
            IngestRequest(
                filename="note.md",
                area=Area.PERSONAL,
                text="вторая версия, совсем другая",
                source_path=str(src),
            )
        )
        self.worker.run_once()
        after = read_sidecar(file_sidecar_path(src))
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        # сайдкар перезаписан: содержимое вектора соответствует новой версии
        expected = self.embedder.embed(["вторая версия, совсем другая"])[0]
        for a, b in zip(after.chunks[0].vector, expected, strict=True):
            self.assertAlmostEqual(a, b, places=3)

    def test_remove_source_deletes_sidecar(self):
        src = self._file("books/one.md", "удаляемый документ")
        self.worker.run_once()
        sidecar = file_sidecar_path(src)
        self.assertTrue(sidecar.exists())
        result = self.ingest.remove_source(Area.PERSONAL, "books/one.md")
        self.assertIsNotNone(result.document_id)
        self.assertFalse(sidecar.exists())
        self.assertFalse(self.textstore.exists(result.document_id or ""))

    def test_remove_virtual_deletes_sidecar(self):
        text = "виртуальный элемент, который удаляют"
        outcome = self.ingest.ingest(
            IngestRequest(filename="note.md", area=Area.PERSONAL, text=text)
        )
        doc_id = outcome.document_id
        assert doc_id is not None
        self.worker.run_once()
        sidecar = virtual_sidecar_path(self.virtual, doc_id)
        self.assertTrue(sidecar.exists())
        self.ingest.remove_source(Area.PERSONAL, "note.md")
        self.assertFalse(sidecar.exists())

    def test_multichunk_document(self):
        # маленький чанк → несколько векторов в сайдкаре
        ingest = IngestService(
            self.store,
            self.textstore,
            NoopStemmer(),
            chunk_size=10,
            chunk_overlap=2,
            virtual_dir=self.virtual,
        )
        worker = EmbedWorker(
            self.store,
            self.textstore,
            self.embedder,
            chunk_size=10,
            chunk_overlap=2,
            virtual_dir=self.virtual,
        )
        text = "очень длинный текст, который точно нарежется на несколько чанков"
        src = self.root / "books/multi.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        ingest.ingest(
            IngestRequest(
                filename="books/multi.md",
                area=Area.PERSONAL,
                text=text,
                source_path=str(src),
            )
        )
        stats = worker.run_once()
        self.assertGreater(stats.chunks, 1)
        data = read_sidecar(file_sidecar_path(src))
        assert data is not None
        self.assertGreater(len(data.chunks), 1)
        # диапазоны чанков покрывают текст
        self.assertEqual(data.chunks[-1].char_end, len(text))

    def test_doc_id_matches_content_hash(self):
        text = "проверка: doc_id это хэш текста"
        outcome = self.ingest.ingest(
            IngestRequest(filename="note.md", area=Area.PERSONAL, text=text)
        )
        self.assertEqual(
            outcome.document_id,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def test_fingerprint_recorded_and_rebuilt_on_model_switch(self):
        """Смена модели → пересчёт всех сайдкаров, отпечаток обновляется (D24)."""
        src = self._file("books/one.md", "текст для проверки смены модели")
        fp_path = self.data / "embed_fingerprint.json"
        worker = EmbedWorker(
            self.store,
            self.textstore,
            self.embedder,
            virtual_dir=self.virtual,
            fingerprint_path=fp_path,
        )
        stats = worker.run_once()
        self.assertEqual(stats.docs, 1)
        data = read_sidecar(file_sidecar_path(src))
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.fingerprint.model, "placeholder-v1")
        self.assertTrue(fp_path.exists())
        self.assertEqual(fp_path.read_text(encoding="utf-8").strip(), "placeholder-v1")

        # другая модель с другим отпечатком — тот же детерминированный алгоритм
        class NewModel(PlaceholderEmbedder):
            name = "placeholder-v2"

        worker2 = EmbedWorker(
            self.store,
            self.textstore,
            NewModel(),
            virtual_dir=self.virtual,
            fingerprint_path=fp_path,
        )
        stats = worker2.run_once()
        self.assertEqual(stats.docs, 1)
        data2 = read_sidecar(file_sidecar_path(src))
        self.assertIsNotNone(data2)
        assert data2 is not None
        self.assertEqual(data2.fingerprint.model, "placeholder-v2")
        self.assertEqual(fp_path.read_text(encoding="utf-8").strip(), "placeholder-v2")
        self.assertTrue(any("модель сменилась" in m for m in stats.messages))

    def test_no_rebuild_when_model_unchanged(self):
        """Та же модель и полный проход — вторая итерация простаивает."""
        self._file("books/one.md", "стабильный текст")
        fp_path = self.data / "embed_fingerprint.json"
        worker = EmbedWorker(
            self.store,
            self.textstore,
            self.embedder,
            virtual_dir=self.virtual,
            fingerprint_path=fp_path,
        )
        worker.run_once()
        stats = worker.run_once()
        self.assertEqual((stats.docs, stats.skipped, stats.errors), (0, 0, 0))
        self.assertFalse(any("модель сменилась" in m for m in stats.messages))
        self.assertEqual(fp_path.read_text(encoding="utf-8").strip(), "placeholder-v1")


if __name__ == "__main__":
    unittest.main()
