"""Тесты фоновой переиндексации (``sources.reindex``, D25).

Своей сверки хэшей у переиндексации нет: все файлы идут через обычный
конвейер, а «текст не менялся» решает уже существующая проверка
``IngestService`` по ``documents.content_hash``. Здесь проверяется именно это:
неизменённый файл не переиндексируется и не засоряет журнал, изменённый —
переиндексируется, работа держится в пределах заданной доли CPU.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestService
from persinder.morphology import NoopStemmer
from persinder.sources import (
    AreaConfig,
    ChangeEvent,
    ChangeKind,
    IndexingPipeline,
    Journal,
    ScanResult,
    WatchConfig,
    default_registry,
    sync_catalog,
)
from persinder.sources.reindex import (
    ReindexStats,
    Throttle,
    format_reindex,
    reindex_files,
    share_from_percent,
)
from persinder.sources.scanner import AreaScanner
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class ReindexTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.area = AreaConfig(
            path=root / "area",
            role=Area.PERSONAL,
            formats=("markdown", "text"),
        )
        self.area.path.mkdir()
        self.root = root
        self.journal = Journal(root / "journal.jsonl")
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.ingest = IngestService(self.store, self.texts, NoopStemmer())
        self.scanner = AreaScanner(WatchConfig(debounce=0))
        self.registry = default_registry()
        self.pipeline = IndexingPipeline(
            self.ingest, self.registry, journal=self.journal, quiet_unchanged=True
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.area.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _index(self, name: str, text: str) -> str | None:
        """Обычный проход конвейера: файл появляется и индексируется."""
        path = self._write(name, text)
        stat = path.stat()
        pipeline = IndexingPipeline(self.ingest, self.registry, journal=self.journal)
        pipeline.process(
            ScanResult(
                area=self.area,
                events=(
                    ChangeEvent(
                        ChangeKind.CREATED,
                        self.area,
                        name,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                    ),
                ),
            )
        )
        return self.store.source_document(Area.PERSONAL.value, name)

    def _journal_text(self) -> str:
        return (self.root / "journal.jsonl").read_text(encoding="utf-8")

    def _reindex(
        self, share: float = 1.0, stop: threading.Event | None = None
    ) -> ReindexStats:
        return reindex_files(
            (self.area,),
            self.scanner,
            self.journal,
            self.pipeline,
            share=share,
            stop=stop,
        )

    def test_unchanged_text_is_left_alone(self):
        """Текст файла совпал с базой — конвейер узнал его по хэшу."""
        self._index("note.md", "кот сидит на окне")

        stats = self._reindex()

        self.assertEqual((stats.files, stats.unchanged, stats.changed), (1, 1, 0))

    def test_changed_text_is_reindexed(self):
        """Текст другой — конвейер индексирует файл заново."""
        old_id = self._index("note.md", "кот сидит на окне")
        self._write("note.md", "пёс бежит по дороге")

        stats = self._reindex()

        self.assertEqual((stats.unchanged, stats.changed), (0, 1))
        new_id = self.store.source_document("personal", "note.md")
        self.assertIsNotNone(new_id)
        self.assertNotEqual(new_id, old_id, "источник переведён на новый документ")
        self.assertIsNone(
            self.store.find_document_by_hash(old_id), "старый текст вытеснен"
        )

    def test_new_file_is_left_to_catalog(self):
        """Файл, которого журнал не знает, разбирает стартовый каталог.

        Переиндексация идёт после каталога и работает по журналу: дважды
        новый файл не индексируется.
        """
        self._write("new.md", "новая заметка")

        stats = self._reindex()

        self.assertEqual((stats.files, stats.changed), (0, 0))
        self.assertIsNone(self.store.source_document("personal", "new.md"))

    def test_file_indexed_by_catalog_is_verified(self):
        """Файл, разобранный каталогом, переиндексация сверяет как обычно."""
        self._write("new.md", "новая заметка")
        catalog = sync_catalog((self.area,), self.scanner, self.journal)
        for result in catalog.results:
            self.pipeline.process(result)

        stats = self._reindex()

        self.assertEqual((stats.files, stats.unchanged), (1, 1))

    def test_unsupported_format_is_not_read(self):
        """Файл, которого нет в индексе, переиндексация не трогает вовсе."""
        (self.area.path / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)

        stats = self._reindex()

        self.assertEqual((stats.files, stats.skipped), (0, 0))

    def test_text_file_with_unknown_suffix_is_verified(self):
        """``.yaml`` индексируется конвейером по содержимому — и сверяется тоже.

        ``registry.supports`` смотрит только расширение и такой файл бы
        пропустил; переиндексация ориентируется на журнал, поэтому правка
        находится.
        """
        self._index("config.yaml", "ключ: значение")
        self._write("config.yaml", "ключ: другое значение")

        stats = self._reindex()

        self.assertEqual((stats.files, stats.changed), (1, 1))

    def test_unchanged_file_does_not_write_journal(self):
        """Неизменённые файлы не добавляют строк в журнал (иначе — спам)."""
        self._index("note.md", "кот сидит на окне")
        before = self._journal_text().splitlines()

        self._reindex()

        after = self._journal_text().splitlines()
        self.assertEqual(before, after)

    def test_changed_file_is_written_to_journal(self):
        """Изменённый файл журналируется как обычно — действие видно."""
        self._index("note.md", "кот сидит на окне")
        self._write("note.md", "пёс бежит по дороге")

        self._reindex()

        tail = self._journal_text()
        self.assertIn('"file": "note.md"', tail)
        self.assertIn('"event": "indexed"', tail)

    def test_stop_aborts_reindex(self):
        """Остановку сервиса переиндексация замечает и выходит."""
        self._index("a.md", "первый")
        self._index("b.md", "второй")
        stop = threading.Event()
        stop.set()

        stats = self._reindex(stop=stop)

        self.assertEqual(stats.files, 0)

    def test_format_line(self):
        text = format_reindex(
            ReindexStats(files=3, changed=1, unchanged=2, skipped=4, seconds=5.0)
        )
        self.assertIn("проверено 3 файлов", text)
        self.assertIn("изменено 1", text)
        self.assertIn("без изменений 2", text)
        self.assertIn("пропущено 4", text)


class ThrottleTest(unittest.TestCase):
    """Доля CPU держится сном, а не приоритетом процесса (он есть не везде)."""

    def test_five_percent_sleeps_nineteen_times_work(self):
        now = [0.0]
        slept: list[float] = []

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        throttle = Throttle(0.05, clock=clock, sleep=sleep)
        now[0] += 1.0  # секунда работы
        throttle.rest()

        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 19.0)
        self.assertAlmostEqual(throttle.slept, 19.0)

    def test_full_share_does_not_sleep(self):
        slept: list[float] = []
        throttle = Throttle(1.0, clock=lambda: 0.0, sleep=slept.append)
        throttle.rest()
        self.assertEqual(slept, [])

    def test_bad_share_is_rejected(self):
        with self.assertRaises(ValueError):
            Throttle(0)

    def test_share_from_percent(self):
        self.assertAlmostEqual(share_from_percent(None), 0.05)
        self.assertAlmostEqual(share_from_percent("5"), 0.05)
        self.assertAlmostEqual(share_from_percent(" 10 "), 0.10)
        self.assertAlmostEqual(share_from_percent("0"), 1.0, "лимит снят явно")
        self.assertAlmostEqual(share_from_percent("500"), 1.0)
        self.assertAlmostEqual(share_from_percent("мусор"), 0.05)


if __name__ == "__main__":
    unittest.main()
