"""Тесты сверки журнала наблюдения с базой (``sources.reconcile``).

Сверка — лекарство от «сервис забыл файл»: журнал говорит «устроено», а в базе
нет документа или потерян сырой текст (или формат теперь поддержан). Без неё
скан не выдаёт событий (слепок не менялся), очередь пуста, сайдкара нет.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.sources import (
    AreaConfig,
    IndexingPipeline,
    Journal,
    JournalEvent,
    default_registry,
    reconcile,
)
from persinder.sources.extract import ExtractorRegistry, TextExtractor
from persinder.sources.models import WatchConfig
from persinder.sources.scanner import AreaScanner
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


def _area(root: Path, formats: tuple[str, ...] = ("text", "markdown")) -> AreaConfig:
    return AreaConfig(path=root, role=Area.PERSONAL, formats=formats)


def _stat(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.area = _area(root / "area")
        self.area.path.mkdir()
        self.journal = Journal(root / "journal.jsonl")
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.registry = default_registry()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _write(self, name: str, text: str = "текст заметки") -> Path:
        path = self.area.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _reconcile(self, area: AreaConfig | None = None):
        return reconcile(
            (area or self.area,),
            self.registry,
            self.journal,
            self.store,
            self.texts,
        )

    def test_skipped_file_now_supported_is_retried(self):
        """fb2/rtf, пропущенные до появления экстрактора, читаются заново."""
        path = self._write(
            "book.fb2", "<FictionBook><body><p>x</p></body></FictionBook>"
        )
        size, mtime = _stat(path)
        self.journal.record(
            self.area, "book.fb2", JournalEvent.SKIPPED, size=size, mtime_ns=mtime
        )
        # формат теперь разрешён в области — файл должен быть перечитан
        supported = _area(self.area.path, ("pub-formats",))

        stats = self._reconcile(supported)

        self.assertEqual([rel for rel, _ in stats.retried], ["book.fb2"])
        self.assertIn("формат теперь поддерживается", stats.retried[0][1])
        self.assertEqual(
            self.journal.snapshot((supported,)),
            {},
            "запись забыта — следующий скан сочтёт файл новым",
        )
        # скан действительно выдаёт событие создания (файл не «свежий» для
        # debounce: сдвигаем mtime назад)
        past = time.time() - 60
        os.utime(path, (past, past))
        scanner = AreaScanner(WatchConfig())
        _, events = scanner.scan(self.area, None)
        self.assertIn("book.fb2", [event.rel_path for event in events])

    def test_unsupported_file_is_reported_with_reason(self):
        """Формат не разрешён в formats области — сказать это, а не молчать."""
        path = self._write("page.html", "<html><body>текст</body></html>")
        size, mtime = _stat(path)
        self.journal.record(
            self.area, "page.html", JournalEvent.SKIPPED, size=size, mtime_ns=mtime
        )

        stats = self._reconcile()

        self.assertEqual(stats.retried, [])
        reasons = " ".join(stats.unsupported)
        self.assertIn("не разрешён в formats", reasons)
        self.assertIn("page.html", stats.unsupported[next(iter(stats.unsupported))])
        self.assertEqual(
            list(self.journal.snapshot((self.area,))),
            [self.area.path],
            "запись остаётся: файл по-прежнему не обслуживается",
        )

    def test_missing_extension_reason_is_reported(self):
        """Не установлен пакет — причина с подсказкой, что доставить."""
        registry = ExtractorRegistry(
            (TextExtractor(),), unavailable={".rtf": "RTF: нет пакета striprtf"}
        )
        path = self._write("book.rtf", r"{\rtf1 текст}")
        size, mtime = _stat(path)
        self.journal.record(
            self.area, "book.rtf", JournalEvent.SKIPPED, size=size, mtime_ns=mtime
        )

        stats = reconcile(
            (self.area,), registry, self.journal, self.store, self.texts
        )

        reason = next(iter(stats.unsupported))
        self.assertIn("striprtf", reason)

    def test_indexed_without_text_is_retried(self):
        """Текст потерян — документ невидим для семантики; нужен перечит файла."""
        path = self._write("note.md", "кот сидит на окне")
        size, mtime = _stat(path)
        ingest = IngestService(self.store, self.texts, NoopStemmer())
        outcome = ingest.ingest(
            IngestRequest(
                filename="note.md",
                area=Area.PERSONAL,
                text="кот сидит на окне",
                source_path=str(path),
            )
        )
        doc_id = outcome.document_id
        assert doc_id is not None
        self.journal.record(
            self.area, "note.md", JournalEvent.INDEXED, size=size, mtime_ns=mtime
        )
        self.texts.delete(doc_id)  # пользователь удалил текст

        stats = self._reconcile()

        self.assertEqual([rel for rel, _ in stats.retried], ["note.md"])
        self.assertIn("сырой текст потерян", stats.retried[0][1])

    def test_consistent_index_is_left_alone(self):
        path = self._write("note.md", "пёс бежит по дороге")
        size, mtime = _stat(path)
        ingest = IngestService(self.store, self.texts, NoopStemmer())
        ingest.ingest(
            IngestRequest(
                filename="note.md",
                area=Area.PERSONAL,
                text="пёс бежит по дороге",
                source_path=str(path),
            )
        )
        self.journal.record(
            self.area, "note.md", JournalEvent.INDEXED, size=size, mtime_ns=mtime
        )

        stats = self._reconcile()

        self.assertTrue(stats.empty)
        self.assertEqual(stats.checked, 1)

    def test_duplicate_record_without_text_is_retried(self):
        """Дубликат по хэшу тоже обязан иметь текст — иначе считать нечего."""
        first = self._write("a.md", "одинаковый текст")
        second = self._write("b.md", "одинаковый текст")
        ingest = IngestService(self.store, self.texts, NoopStemmer())
        outcome = ingest.ingest(
            IngestRequest(
                filename="a.md",
                area=Area.PERSONAL,
                text="одинаковый текст",
                source_path=str(first),
            )
        )
        doc_id = outcome.document_id
        assert doc_id is not None
        ingest.ingest(
            IngestRequest(
                filename="b.md",
                area=Area.PERSONAL,
                text="одинаковый текст",
                source_path=str(second),
            )
        )
        self.texts.delete(doc_id)
        for name, path in (("a.md", first), ("b.md", second)):
            size, mtime = _stat(path)
            self.journal.record(
                self.area, name, JournalEvent.DUPLICATE, size=size, mtime_ns=mtime
            )

        stats = self._reconcile()

        self.assertEqual(sorted(rel for rel, _ in stats.retried), ["a.md", "b.md"])
        self.assertTrue(all("текст" in reason for _, reason in stats.retried))

    def test_pipeline_skips_with_reason(self):
        """Конвейер пишет причину пропуска в журнал и в лог."""
        from persinder.api import IndexLog
        from persinder.sources.models import ChangeEvent, ChangeKind, ScanResult

        path = self._write("page.html", "<html><body>текст</body></html>")
        size, mtime = _stat(path)
        log = IndexLog()
        log.enable_console(False)
        pipeline = IndexingPipeline(
            IngestService(self.store, self.texts, NoopStemmer()),
            self.registry,
            journal=self.journal,
            log=log,
        )
        result = ScanResult(
            area=self.area,
            events=(
                ChangeEvent(
                    ChangeKind.CREATED,
                    self.area,
                    "page.html",
                    size=size,
                    mtime_ns=mtime,
                ),
            ),
        )
        pipeline.process(result)
        messages = [event.message for event in log.entries()]
        self.assertTrue(messages, "пропуск должен быть в журнале")
        self.assertIn("не разрешён в formats", " ".join(messages))
