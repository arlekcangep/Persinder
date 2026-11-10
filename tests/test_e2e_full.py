"""Сквозной e2e-пакет: watcher + индексация + эмбеддинги в сборе.

Повторяет реальный ``run_serve``: фоновый ``Watcher`` сканирует область,
``IndexingPipeline`` индексирует найденное, ``EmbedWorker`` разбирает очередь
в векторы и сайдкары. В отличие от ``test_e2e.py`` (HTTP → ingest → search)
здесь участвуют все три звена сразу, как при живом сервисе.

Покрывает:
* полный цикл: файл появляется → watcher находит → индексируется → embed
  пишет сайдкар;
* идемпотентность повторного скана: устроенные файлы не ставят задачи в
  очередь заново (нет цикла пересчёта);
* новый файл, добавленный при выключенном сервисе, подхватывается при старте;
* изменение файла → переиндексация и пересчёт эмбеддингов;
* удаление файла → документ, сырой текст и сайдкар убираются;
* ``--reindex``: сброс журнала, повторный пересчёт всех источников, затем
  сканы снова идемпотентны;
* часть архива: сайдкар в ``.persinder/<модель>/<архив>/`` папке.
"""

from __future__ import annotations

import time
import unittest
import zipfile
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.embed import (
    EmbedWorker,
    PlaceholderEmbedder,
    file_sidecar_path,
    pack_f16_vectors,
    read_sidecar,
)
from persinder.ingest import IngestService
from persinder.morphology import NoopStemmer
from persinder.sources import (
    AreaConfig,
    IndexingPipeline,
    Journal,
    ScanResult,
    WatchConfig,
    Watcher,
    default_registry,
)
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore
from persinder.vectorindex import UsannIndex

POLL = 0.2  # интервал скана watcher'а в тестах
SETTLE = 0.6  # сколько ждать после старта watcher'а
MODEL = "placeholder-v1"  # модель эмбеддингов в этих тестах


class WatcherEmbedE2ETest(unittest.TestCase):
    """Watcher + pipeline + embed в сборе на реальной файловой области."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.vault = self.root / "vault"
        self.vault.mkdir(parents=True)
        self.data.mkdir(parents=True)

        self.store = SqliteStore(self.data / "db.sqlite")
        self.texts = FsTextStore(self.data / "texts")
        self.ingest = IngestService(self.store, self.texts, NoopStemmer())
        self.registry = default_registry()
        ann_dir = self.data / "ann"
        ann_dir.mkdir(parents=True, exist_ok=True)
        self.worker = EmbedWorker(
            self.store,
            self.texts,
            lambda key: PlaceholderEmbedder(),
            virtual_dir=self.data / "virtual",
            ann_dir=ann_dir,
        )
        self.area = AreaConfig(
            path=self.vault, role=Area.PERSONAL, formats=("markdown", "text", "archive")
        )
        self.watch = WatchConfig(poll_interval=POLL, debounce=0.0)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    # --- помощники ---

    def _write(self, name: str, text: str) -> Path:
        path = self.vault / name
        path.write_text(text, encoding="utf-8")
        return path

    def _pending(self) -> int:
        return self.store.counts()["pending_embeddings"]

    def _ann_size(self) -> int:
        """Сколько ключей в ANN-индексе (должно совпадать с таблицей)."""
        return UsannIndex(self.data / "ann" / f"{MODEL}.usearch", dim=384).size

    def _run_watcher(
        self,
        journal: Journal,
        duration: float = SETTLE,
        during: Callable[[], None] | None = None,
    ) -> list[str]:
        """Запустить watcher на время, собрать отпечатки всех сканов.

        ``during`` выполняется, пока сервис работает, — так проверяются
        сценарии «файл изменился на живом сервисе», а не между запусками.
        """
        prints: list[str] = []

        def on_events(result: ScanResult):
            stats = pipeline.process(result)
            if (
                stats.indexed
                or stats.deleted
                or stats.duplicates
                or stats.rejected
                or stats.skipped
                or stats.errors
            ):
                prints.append(
                    f"indexed={stats.indexed} duplicates={stats.duplicates} "
                    f"deleted={stats.deleted}"
                )

        pipeline = IndexingPipeline(self.ingest, self.registry, journal=journal)
        watcher = Watcher((self.area,), self.watch, on_events, journal=journal)
        watcher.start()
        time.sleep(duration)
        if during is not None:
            during()
            time.sleep(duration)
        watcher.stop()
        return prints

    def _drain_embeddings(self) -> None:
        """Разобрать очередь до пустой (как фоновый embed-поток)."""
        while self._pending():
            self.worker.run_once(limit=100)

    # --- тесты ---

    def test_full_lifecycle_file_to_sidecar(self):
        """Файл в области: watcher индексирует, embed пишет сайдкар рядом."""
        self._write("note.md", "заметка про котиков и векторный поиск")
        journal = Journal(self.data / "journal.jsonl")

        prints = self._run_watcher(journal)
        self.assertEqual(prints, ["indexed=1 duplicates=0 deleted=0"])

        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertGreater(self._pending(), 0, "документ ждёт эмбеддингов")

        self._drain_embeddings()
        self.assertEqual(self._pending(), 0, "очередь разобрана до пустой")

        sidecar = self.vault / ".persinder" / "placeholder-v1" / "note.md.pdr"
        self.assertTrue(sidecar.exists(), "сайдкар лежит рядом с файлом")
        self.assertIsNotNone(read_sidecar(sidecar), "сайдкар читается")

    def test_repeat_scan_is_idempotent(self):
        """Устроенный файл не ставит задачи повторно: нет цикла пересчёта."""
        self._write("note.md", "заметка про флакс и эмбеддинги")
        journal = Journal(self.data / "journal.jsonl")

        self._run_watcher(journal, duration=SETTLE)
        self._drain_embeddings()
        self.assertEqual(self._pending(), 0)

        # Ещё несколько сканов: файл устроен — событий нет, очередь не растёт.
        prints = self._run_watcher(journal, duration=SETTLE)
        self.assertEqual(prints, [], "повторный скан не должен давать событий")
        self.assertEqual(self._pending(), 0, "очередь не пополняется повторно")

    def test_new_file_after_service_stop_is_picked_up(self):
        """Файл, добавленный при выключенном сервисе, индексируется при старте.

        Ключевой сценарий для ``--reindex``: журнал сброшен, поэтому watcher
        при старте видит файлы (и старые, и новый) и обрабатывает их.
        """
        old = self._write("old.md", "старый файл про котиков")
        journal = Journal(self.data / "journal.jsonl")
        self._run_watcher(journal)
        self._drain_embeddings()

        # Сервис «выключен»: watcher остановлен. Появляется новый файл.
        new = self._write("new.md", "новый файл про нейросети, добавленный в offline")

        # Запуск с --reindex: журнал сбрасывается, сканируется всё заново.
        journal_path = self.data / "journal.jsonl"
        journal_path.unlink()
        reindexed = Journal(journal_path)
        prints = self._run_watcher(reindexed)
        self.assertIn(
            "indexed=1", prints[0], "новый файл проиндексирован при переиндексации"
        )

        self._drain_embeddings()
        self.assertEqual(self._pending(), 0)
        # Оба документа живы, у каждого свой сайдкар.
        self.assertEqual(self.store.counts()["documents"], 2)
        for src in (old, new):
            self.assertTrue(
                (
                    src.parent / ".persinder" / "placeholder-v1" / f"{src.name}.pdr"
                ).exists()
            )

    def test_modified_file_is_reindexed(self):
        """Файл правится при живом watcher'е: индекс переезжает целиком.

        Раньше здесь проверялось только «документов по-прежнему один» — такой
        тест проходит и когда новый текст не попал в поиск, а векторы, ключи
        ANN и сайдкар остались от прежнего содержимого. Теперь сверяются все
        производные, и правка делается, пока сервис запущен.
        """
        source = self._write("note.md", "версия один про котиков и собак")
        journal = Journal(self.data / "journal.jsonl")
        self._run_watcher(journal)
        self._drain_embeddings()
        old_id = self.store.source_document(Area.PERSONAL.value, "note.md")
        self.assertIsNotNone(old_id)
        self.assertEqual(len(self.store.search_morph("котиков")), 1)

        time.sleep(0.05)  # иной mtime_ns — иначе правку не видно

        def _edit() -> None:
            self._write("note.md", "версия два про флакс")

        self._run_watcher(journal, during=_edit)
        self._drain_embeddings()

        new_id = self.store.source_document(Area.PERSONAL.value, "note.md")
        assert new_id is not None, "после правки у файла должен быть документ"
        self.assertNotEqual(new_id, old_id, "doc_id — хэш содержимого")
        self.assertEqual(self.store.counts()["documents"], 1)
        # 1. Морфология: старый текст ушёл, новый на месте.
        self.assertEqual(self.store.search_morph("котиков"), [])
        self.assertEqual(len(self.store.search_morph("флакс")), 1)
        # 2. Векторов старой версии не осталось.
        assert old_id is not None
        self.assertEqual(self.store.embeddings_for_doc(MODEL, old_id), [])
        chunks = self.store.counts()["chunks"]
        self.assertEqual(self.store.count_embeddings(MODEL), chunks)
        # 3. Ключи ANN совпадают с таблицей — старых нет.
        self.assertEqual(self._ann_size(), chunks, "в ANN остались ключи старой версии")
        # 4. Сайдкар пересчитан от нового содержимого, а не оставлен старым.
        sidecar = read_sidecar(file_sidecar_path(source, MODEL))
        assert sidecar is not None, "сайдкар должен перечитаться"
        rows = self.store.embeddings_for_doc(MODEL, new_id)
        self.assertEqual(len(sidecar.chunks), len(rows))
        self.assertEqual(
            pack_f16_vectors([list(sidecar.chunks[0].vector)], PlaceholderEmbedder.dim),
            bytes(rows[0]["vector"]),
            "вектор в сайдкаре тот же, что в таблице",
        )

    def test_deleted_file_cleans_document_and_sidecar(self):
        """Удалённый файл убирает документ, сырой текст и сайдкар."""
        self._write("note.md", "уникальный текст для удаления")
        journal = Journal(self.data / "journal.jsonl")
        self._run_watcher(journal)
        self._drain_embeddings()
        self.assertEqual(self.store.counts()["documents"], 1)

        (self.vault / "note.md").unlink()
        prints = self._run_watcher(journal)
        self.assertEqual(prints, ["indexed=0 duplicates=0 deleted=1"])

        counts = self.store.counts()
        self.assertEqual(counts["documents"], 0)
        self.assertEqual(counts["chunks"], 0)
        self.assertEqual(counts["pending_embeddings"], 0)
        self.assertEqual(list((self.data / "texts").iterdir()), [])
        self.assertFalse(
            (self.vault / ".persinder" / "placeholder-v1" / "note.md.pdr").exists(),
            "сайдкар удалён вместе с источником",
        )

    def test_reindex_requeues_every_source(self):
        """--reindex: журнал сброшен и в очередь поставлены ВСЕ источники."""
        self._write("a.md", "документ а про векторный поиск")
        self._write("b.md", "документ б про морфологию и стемминг")
        journal = Journal(self.data / "journal.jsonl")
        self._run_watcher(journal)
        self._drain_embeddings()
        self.assertEqual(self.store.counts()["documents"], 2)
        self.assertEqual(self._pending(), 0)

        # Как run_serve с --reindex: журнал сброшен и всё поставлено в очередь.
        journal_path = self.data / "journal.jsonl"
        journal_path.unlink()
        self.store.enqueue_all(["placeholder-v1"])
        self.assertEqual(
            self._pending(), 2, "пересчёт всех источников, а не только утраченных"
        )

        reindexed = Journal(journal_path)
        self._run_watcher(reindexed)
        self.assertEqual(
            self.store.counts()["documents"], 2, "документы целы после reindex"
        )
        self._drain_embeddings()
        self.assertEqual(self._pending(), 0)
        for name in ("a.md", "b.md"):
            self.assertTrue(
                (self.vault / ".persinder" / "placeholder-v1" / f"{name}.pdr").exists()
            )

        # Повторные сканы после переиндексации снова идемпотентны.
        more = self._run_watcher(reindexed)
        self.assertEqual(more, [], "после reindex повторные сканы пусты")
        self.assertEqual(self._pending(), 0)

    def test_reindex_picks_up_file_changed_while_service_off(self):
        """Файл, изменённый при выключенном сервисе, переиндексируется по хэшу."""
        src = self._write("note.md", "старая версия про котиков")
        journal = Journal(self.data / "journal.jsonl")
        self._run_watcher(journal)
        self._drain_embeddings()
        self.assertEqual(len(self.store.search_morph("котиков")), 1)

        # Сервис «выключен»: содержимое файла изменилось.
        time.sleep(0.05)
        src.write_text("новая версия про квантовый флакс", encoding="utf-8")

        # --reindex: журнал сброшен, все источники снова в очереди.
        journal_path = self.data / "journal.jsonl"
        journal_path.unlink()
        self.store.enqueue_all(["placeholder-v1"])
        reindexed = Journal(journal_path)
        prints = self._run_watcher(reindexed)
        self.assertIn("indexed=1", prints[0], "изменённый файл переиндексирован")

        self._drain_embeddings()
        self.assertEqual(self._pending(), 0)
        self.assertEqual(self.store.counts()["documents"], 1)
        self.assertEqual(
            len(self.store.search_morph("котиков")), 0, "старая версия удалена"
        )
        self.assertEqual(
            len(self.store.search_morph("флакс")), 1, "новая версия в индексе"
        )
        sidecar = file_sidecar_path(src, "placeholder-v1")
        self.assertTrue(sidecar.exists(), "сайдкар пересчитан для новой версии")
        self.assertIsNotNone(read_sidecar(sidecar))

    def test_archive_part_sidecar_in_archive_folder(self):
        """Часть zip-архива в области: сайдкар в .persinder/<архив>/ папке."""
        zip_path = self.vault / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("inner.md", "внутренний документ архива про векторный поиск")
        journal = Journal(self.data / "journal.jsonl")

        self._run_watcher(journal)
        self._drain_embeddings()

        sidecar = (
            self.vault / ".persinder" / "placeholder-v1" / "bundle.zip" / "inner.md.pdr"
        )
        self.assertTrue(sidecar.exists(), f"нет сайдкара части архива: {sidecar}")
        self.assertIsNotNone(read_sidecar(sidecar))

        # Удаление архива убирает и сайдкар части.
        zip_path.unlink()
        self._run_watcher(journal)
        self.assertFalse(sidecar.exists(), "сайдкар части удалён вместе с архивом")


if __name__ == "__main__":
    unittest.main()
