"""Тесты фоновой сверки хэшей (``sources.hashsweep``, D25).

Reindex не перечитывает всё подряд: текст каждого файла сравнивается с базой
по уже существующему хэшу содержимого (``documents.content_hash``). Совпало —
файл не трогается; нет — уходит в конвейер. Работает в фоне и не забирает
больше заданной доли CPU.
"""

from __future__ import annotations

import hashlib
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.sources import AreaConfig, Journal, WatchConfig, default_registry
from persinder.sources.hashsweep import (
    SweepStats,
    Throttle,
    format_sweep,
    share_from_percent,
    sweep_hashes,
)
from persinder.sources.scanner import AreaScanner
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class SweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.area = AreaConfig(
            path=root / "area",
            role=Area.PERSONAL,
            formats=("markdown", "text"),
        )
        self.area.path.mkdir()
        self.journal = Journal(root / "journal.jsonl")
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.ingest = IngestService(self.store, self.texts, NoopStemmer())
        self.scanner = AreaScanner(WatchConfig(debounce=0))
        self.registry = default_registry()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.area.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _index(self, name: str, text: str) -> str:
        self._write(name, text)
        outcome = self.ingest.ingest(
            IngestRequest(
                filename=name,
                area=Area.PERSONAL,
                text=text,
                source_path=str(self.area.path / name),
            )
        )
        assert outcome.document_id is not None
        return outcome.document_id

    def _sweep(self, share: float = 1.0, stop: threading.Event | None = None):
        changed: list[str] = []

        def on_changed(result) -> None:
            changed.extend(event.rel_path for event in result.events)

        stats = sweep_hashes(
            (self.area,),
            self.scanner,
            self.registry,
            self.store,
            self.journal,
            on_changed,
            share=share,
            stop=stop,
        )
        return stats, changed

    def test_unchanged_text_is_left_alone(self):
        """Текст файла совпал с базой — файл не переиндексируется."""
        self._index("note.md", "кот сидит на окне")

        stats, changed = self._sweep()

        self.assertEqual((stats.files, stats.same, stats.changed), (1, 1, 0))
        self.assertEqual(changed, [])

    def test_changed_text_is_reported(self):
        """Текст другой — файл отдаётся конвейеру."""
        self._index("note.md", "кот сидит на окне")
        self._write("note.md", "пёс бежит по дороге")

        stats, changed = self._sweep()

        self.assertEqual((stats.same, stats.changed), (0, 1))
        self.assertEqual(changed, ["note.md"])

    def test_new_file_without_document_is_reported(self):
        """Файла нет в базе вовсе (появился во время работы) — индексируем."""
        self._write("new.md", "новая заметка")

        stats, changed = self._sweep()

        self.assertEqual((stats.files, stats.changed), (1, 1))
        self.assertEqual(changed, ["new.md"])

    def test_lost_source_is_reported(self):
        """Текст в базе есть, но привязан к другому файлу — источник потерян."""
        self._index("a.md", "общий текст")
        self._write("b.md", "общий текст")

        stats, changed = self._sweep()

        self.assertEqual(changed, ["b.md"])

    def test_unsupported_format_is_not_read(self):
        """Файл, который область не обслуживает, не читается и не хэшируется."""
        (self.area.path / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)

        stats, changed = self._sweep()

        self.assertEqual((stats.files, stats.untouched), (0, 1))
        self.assertEqual(changed, [])

    def test_already_skipped_file_is_not_retried(self):
        """Разобранный с отказом файл не перечитывается каждой сверкой."""
        path = self._write("page.html", "<html><body>текст</body></html>")
        from persinder.sources.journal import JournalEvent

        self.journal.record(
            self.area,
            "page.html",
            JournalEvent.SKIPPED,
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
        )

        stats, changed = self._sweep()

        self.assertEqual((stats.files, stats.untouched), (0, 1))
        self.assertEqual(changed, [])

    def test_stop_aborts_sweep(self):
        """Остановку сервиса сверка замечает и выходит."""
        self._index("a.md", "первый")
        self._index("b.md", "второй")
        self._index("c.md", "третий")
        stop = threading.Event()
        stop.set()

        stats, changed = self._sweep(stop=stop)

        self.assertEqual(stats.files, 0)
        self.assertEqual(changed, [])

    def test_format_line(self):
        text = format_sweep(
            SweepStats(files=3, same=2, changed=1, untouched=4, errors=0, seconds=5.0)
        )
        self.assertIn("файлов 3", text)
        self.assertIn("изменено 1", text)
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


class TextHashTest(unittest.TestCase):
    def test_sweep_uses_content_hash_of_text(self):
        """Сверка опирается на ``content_hash`` (он же doc_id), а не на файл."""
        from persinder.sources.hashsweep import _text_hashes

        hashes = _text_hashes("note.md", [("", "текст")])
        expected = hashlib.sha256("текст".encode()).hexdigest()
        self.assertEqual(hashes, {"note.md": expected})

        parts = _text_hashes("bundle.zip", [("inner.md", "внутри")])
        self.assertEqual(list(parts), ["bundle.zip/inner.md"])
