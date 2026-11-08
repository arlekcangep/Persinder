"""Тесты воркера эмбеддингов: очередь → векторы → сайдкары (на модель, D24)."""

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
    sidecar_path,
    unpack_f16_vectors,
    virtual_sidecar_path,
)
from persinder.ingest import IngestRequest, IngestService
from persinder.interfaces import Embedder
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore
from persinder.vectorindex import UsannIndex


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
        self.worker = self._worker()

    def _worker(
        self,
        *,
        chunk_size: int = 1200,
        chunk_overlap: int = 200,
        embedder: Embedder | None = None,
        ann_dir: Path | None = None,
    ) -> EmbedWorker:
        embedder = embedder if embedder is not None else self.embedder

        def factory(model: str) -> Embedder:
            # ключ задачи → эмбеддер: по умолчанию один и тот же на любую модель
            return embedder

        return EmbedWorker(
            self.store,
            self.textstore,
            factory,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            virtual_dir=self.virtual,
            ann_dir=ann_dir,
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
        data = read_sidecar(file_sidecar_path(src, "placeholder-v1"))
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.fingerprint.model, "placeholder-v1")
        self.assertEqual(data.fingerprint.dim, self.embedder.dim)
        self.assertEqual(self.store.counts()["pending_embeddings"], 0)

    def test_worker_materializes_embeddings_in_store(self):
        """Модуль 3: воркер пишет векторы чанков в таблицу embeddings под моделью."""
        text = "заметка про квантовый флакс"
        self._file("books/one.md", text)
        self.worker.run_once()
        rows = self.store.embeddings_for_model("placeholder-v1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["doc_id"],
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(rows[0]["chunk_index"], 0)
        vector = unpack_f16_vectors(rows[0]["vector"], self.embedder.dim)[0]
        self.assertEqual(len(vector), self.embedder.dim)
        # другие модели не смешиваются
        self.assertEqual(self.store.embeddings_for_model("placeholder-v2"), [])

    def test_worker_maintains_ann_index(self):
        """Модуль 3: воркер кладёт векторы в ANN-индекс (ключи = id строк)."""
        ann_dir = self.data / "ann"
        ann_dir.mkdir()
        worker = self._worker(ann_dir=ann_dir)
        self._file("books/one.md", "заметка про квантовый флакс")
        worker.run_once()

        index_path = ann_dir / "placeholder-v1.usearch"
        self.assertTrue(index_path.exists())
        index = UsannIndex(index_path, self.embedder.dim)
        live = {
            row["id"] for row in self.store.embeddings_ids_for_model("placeholder-v1")
        }
        self.assertEqual(index.keys(), live)

    def test_worker_syncs_stale_keys_on_next_pass(self):
        """Реконсиляция: удалённый документ уходит из индекса на следующем проходе."""
        ann_dir = self.data / "ann"
        ann_dir.mkdir()
        worker = self._worker(ann_dir=ann_dir)
        text = "документ, который потом удалят"
        self._file("books/one.md", text)
        worker.run_once()
        index_path = ann_dir / "placeholder-v1.usearch"
        index = UsannIndex(index_path, self.embedder.dim)
        self.assertEqual(index.size, 1)

        # удаляем документ каскадом; в файле индекса ключ пока «протух»
        doc_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.store.delete_document(doc_id)
        self.assertEqual(index.size, 1, "индекс чистится не мгновенно, а на проходе")

        # новый файл под ту же модель заставляет воркер открыть индекс и сделать sync
        self._file("books/two.md", "второй документ под той же моделью")
        worker.run_once()
        live = {
            row["id"] for row in self.store.embeddings_ids_for_model("placeholder-v1")
        }
        reopened = UsannIndex(index_path, self.embedder.dim)
        self.assertEqual(reopened.keys(), live)
        self.assertEqual(reopened.size, 1)

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
        self.assertIsNotNone(
            read_sidecar(virtual_sidecar_path(self.virtual, doc_id, "placeholder-v1"))
        )
        # рядом с файлом сайдкара нет — файла нет
        self.assertEqual(list(self.root.glob("**/.persinder")), [])

    def test_archive_part_sidecar_in_archive_folder(self):
        """Часть архива: source_path = полный виртуальный путь, сайдкар в
        .persinder/<модель>/<архив>/ папку; при удалении источника уходит вместе."""
        container = self.root / "arch"
        container.mkdir(parents=True, exist_ok=True)
        zip_path = container / "bundle.zip"
        zip_path.write_text("", encoding="utf-8")  # контейнер существует на диске
        text = "внутренний документ архива про векторный поиск"
        part_path = Path(zip_path) / "inner.md"
        self.ingest.ingest(
            IngestRequest(
                filename="bundle.zip/inner.md",
                area=Area.PERSONAL,
                text=text,
                source_path=str(part_path),
            )
        )
        stats = self.worker.run_once(limit=10)
        self.assertEqual((stats.docs, stats.sidecars, stats.errors), (1, 1, 0))

        sidecar = sidecar_path(part_path, "placeholder-v1")
        self.assertTrue(sidecar.exists(), f"нет сайдкара: {sidecar}")
        # папка на архив внутри .persinder
        self.assertEqual(
            sidecar.parent,
            container / ".persinder" / "placeholder-v1" / "bundle.zip",
        )
        # в глобальную virtual_dir не ушёл
        self.assertEqual(list(self.virtual.glob("*.pdr")), [])

        self.ingest.remove_source(Area.PERSONAL, "bundle.zip/inner.md")
        self.assertFalse(sidecar.exists(), "сайдкар не удалён вместе с источником")

    def test_duplicate_gets_its_own_sidecar(self):
        text = "общий текст про котиков и васильки"
        src1 = self._file("books/a.md", text)
        src2 = self._file("books/b.md", text)
        # два источника — по очереди на каждый (уникальная пара doc+path+model)
        self.assertEqual(self.store.counts()["pending_embeddings"], 2)
        stats = self.worker.run_once()
        self.assertEqual(stats.sidecars, 2)
        self.assertIsNotNone(read_sidecar(file_sidecar_path(src1, "placeholder-v1")))
        self.assertIsNotNone(read_sidecar(file_sidecar_path(src2, "placeholder-v1")))

    def test_empty_queue_path_resolves_to_real_source(self):
        """Старая запись без source_path: сайдкар ложится рядом с файлом.

        У пользователя база могла быть создана до появления колонки
        ``source_path``; задачи в очередь попадали с пустым путём, и векторы
        писались в ``virtual/`` — со стороны выглядело, что «папки модели нет».
        """
        src = self.root / "notes/old.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        text = "старый документ без записанного пути"
        src.write_text(text, encoding="utf-8")
        outcome = self.ingest.ingest(
            IngestRequest(filename="notes/old.md", area=Area.PERSONAL, text=text)
        )
        doc_id = outcome.document_id
        assert doc_id is not None
        # База «починилась»: у источника теперь есть реальный путь.
        self.store.add_source(doc_id, Area.PERSONAL.value, "notes/old.md", str(src))

        stats = self.worker.run_once(limit=10)
        self.assertEqual((stats.docs, stats.sidecars, stats.errors), (1, 1, 0))
        self.assertTrue(file_sidecar_path(src, "placeholder-v1").exists())
        self.assertEqual(
            list(self.virtual.rglob("*.pdr")),
            [],
            "сайдкар не должен уходить в virtual/, если есть путь файла",
        )

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
        before = read_sidecar(file_sidecar_path(src, "placeholder-v1"))
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
        after = read_sidecar(file_sidecar_path(src, "placeholder-v1"))
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        # сайдкар перезаписан: содержимое вектора соответствует новой версии
        expected = self.embedder.embed(["вторая версия, совсем другая"])[0]
        for a, b in zip(after.chunks[0].vector, expected, strict=True):
            self.assertAlmostEqual(a, b, places=3)

    def test_remove_source_deletes_sidecar(self):
        src = self._file("books/one.md", "удаляемый документ")
        self.worker.run_once()
        sidecar = file_sidecar_path(src, "placeholder-v1")
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
        sidecar = virtual_sidecar_path(self.virtual, doc_id, "placeholder-v1")
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
        worker = self._worker(chunk_size=10, chunk_overlap=2)
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
        data = read_sidecar(file_sidecar_path(src, "placeholder-v1"))
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

    def test_dual_model_sidecars_live_side_by_side(self):
        """Миграция (D24): файл считается двумя моделями — два сайдкара рядом."""

        class V2(PlaceholderEmbedder):
            name = "placeholder-v2"

        ingest = IngestService(
            self.store,
            self.textstore,
            NoopStemmer(),
            virtual_dir=self.virtual,
            embedding_models=("placeholder-v1", "placeholder-v2"),
        )
        src = self.root / "migrate.md"
        text = "файл, который живёт под двумя моделями сразу"
        src.write_text(text, encoding="utf-8")
        ingest.ingest(
            IngestRequest(
                filename="migrate.md",
                area=Area.PERSONAL,
                text=text,
                source_path=str(src),
            )
        )
        # по задаче на модель на чанк
        self.assertEqual(self.store.counts()["pending_embeddings"], 2)

        v2 = V2()

        def factory(model: str) -> Embedder:
            return v2 if model == "placeholder-v2" else self.embedder

        worker = EmbedWorker(
            self.store,
            self.textstore,
            factory,
            virtual_dir=self.virtual,
        )
        stats = worker.run_once()
        self.assertEqual(stats.sidecars, 2)

        data1 = read_sidecar(file_sidecar_path(src, "placeholder-v1"))
        data2 = read_sidecar(file_sidecar_path(src, "placeholder-v2"))
        self.assertIsNotNone(data1)
        self.assertIsNotNone(data2)
        assert data1 is not None and data2 is not None
        self.assertEqual(data1.fingerprint.model, "placeholder-v1")
        self.assertEqual(data2.fingerprint.model, "placeholder-v2")
        self.assertEqual(self.store.counts()["pending_embeddings"], 0)

    def test_search_switch_keeps_old_model_sidecar(self):
        """После переключения search старый сайдкар остаётся (до remove модели)."""
        src = self._file("books/one.md", "текст под старой моделью")
        self.worker.run_once()
        old = file_sidecar_path(src, "placeholder-v1")
        self.assertTrue(old.exists())

        # новые файлы после `model use` пишут только новую модель, старый
        # сайдкар не трогается — его уберёт только `model remove`
        ingest = IngestService(
            self.store,
            self.textstore,
            NoopStemmer(),
            virtual_dir=self.virtual,
            embedding_models=("placeholder-v2",),
        )

        class V2(PlaceholderEmbedder):
            name = "placeholder-v2"

        src2 = self.root / "books/two.md"
        src2.write_text("новый файл под новой моделью", encoding="utf-8")
        ingest.ingest(
            IngestRequest(
                filename="books/two.md",
                area=Area.PERSONAL,
                text="новый файл под новой моделью",
                source_path=str(src2),
            )
        )

        def factory(model: str) -> Embedder:
            return V2()

        worker = EmbedWorker(
            self.store,
            self.textstore,
            factory,
            virtual_dir=self.virtual,
        )
        stats = worker.run_once()
        self.assertEqual(stats.sidecars, 1)
        self.assertTrue(old.exists(), "старый сайдкар живёт, пока модель установлена")
        self.assertIsNotNone(read_sidecar(file_sidecar_path(src2, "placeholder-v2")))


class EmbedWorkerRobustnessTest(unittest.TestCase):
    """Устойчивость фонового воркера: кэш эмбеддера и изоляция ошибок."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.virtual = root / "virtual"
        self.store = SqliteStore(root / "db.sqlite")
        self.textstore = FsTextStore(root / "pending_texts")
        self.ingest = IngestService(
            self.store, self.textstore, NoopStemmer(), virtual_dir=self.virtual
        )
        self.good = PlaceholderEmbedder()

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

    def test_embedder_factory_called_once_per_model(self):
        """Модель не перезагружается на каждый документ/проход (кэш)."""
        calls: list[str] = []

        def factory(model: str) -> Embedder:
            calls.append(model)
            return self.good

        worker = EmbedWorker(
            self.store, self.textstore, factory, virtual_dir=self.virtual
        )
        self._file("a.md", "первый документ про котиков")
        self._file("b.md", "второй документ про собак")
        worker.run_once(limit=10)
        worker.run_once(limit=10)
        self.assertEqual(calls, ["placeholder-v1"], "сессия модели — один раз")

    def test_one_document_error_does_not_block_others(self):
        """Падение одного документа не мешает посчитать остальные."""
        boom = self._file("0boom.md", "этот документ взорвёт эмбеддер boom")
        good = self._file("zgood.md", "обычный документ про спокойный флакс")

        class BoomEmbedder(PlaceholderEmbedder):
            def embed(self, texts: list[str]) -> list[list[float]]:
                if any("boom" in text for text in texts):
                    raise RuntimeError("модель упала")
                return super().embed(texts)

        worker = EmbedWorker(
            self.store,
            self.textstore,
            lambda model: BoomEmbedder(),
            virtual_dir=self.virtual,
        )
        stats = worker.run_once(limit=10)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.docs, 1, "второй документ всё равно посчитан")
        self.assertTrue(
            read_sidecar(file_sidecar_path(good, "placeholder-v1")) is not None
        )
        self.assertFalse(
            file_sidecar_path(boom, "placeholder-v1").exists(),
            "у упавшего документа сайдкара нет",
        )


if __name__ == "__main__":
    unittest.main()
