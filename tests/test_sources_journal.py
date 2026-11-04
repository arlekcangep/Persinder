"""Тесты журнала наблюдения: переходы, состояние, поведение между запусками."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestService
from persinder.morphology import NoopStemmer
from persinder.sources import (
    AreaConfig,
    AreaScan,
    AreaScanner,
    Extracted,
    ExtractorRegistry,
    IndexingPipeline,
    Journal,
    JournalEvent,
    MarkdownExtractor,
    ScanResult,
    TextExtractor,
    WatchConfig,
    Watcher,
)
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class JournalTest(unittest.TestCase):
    """Запись переходов и восстановление состояния из журнала."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.area = AreaConfig(path=self.vault, role=Area.PERSONAL, formats=("text",))
        self.path = self.root / "journal.jsonl"
        self.journal = Journal(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def entries(self) -> list[dict[str, object]]:
        text = self.path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def _settle(self, final: JournalEvent = JournalEvent.INDEXED) -> None:
        self.journal.record(
            self.area, "a.txt", JournalEvent.DISCOVERED, size=3, mtime_ns=7
        )
        self.journal.record(self.area, "a.txt", JournalEvent.SENT)
        self.journal.record(self.area, "a.txt", final)

    def test_record_writes_jsonl_line(self):
        self.journal.record(
            self.area, "a.txt", JournalEvent.DISCOVERED, size=3, mtime_ns=7
        )
        (entry,) = self.entries()
        self.assertEqual(entry["area"], "personal")
        self.assertEqual(entry["root"], str(self.vault))
        self.assertEqual(entry["file"], "a.txt")
        self.assertEqual(entry["event"], "discovered")
        self.assertEqual(entry["size"], 3)
        self.assertEqual(entry["mtime_ns"], 7)
        self.assertIsInstance(entry["ts"], str)

    def test_empty_fields_omitted(self):
        self.journal.record(self.area, "a.txt", JournalEvent.ERROR, detail="boom")
        (entry,) = self.entries()
        self.assertEqual(entry["detail"], "boom")
        self.assertNotIn("size", entry)
        self.assertNotIn("mtime_ns", entry)

    def test_settled_file_in_state(self):
        self._settle()
        snap = Journal(self.path).snapshot((self.area,))
        self.assertEqual(snap[self.vault].files, {"a.txt": (3, 7)})

    def test_pending_file_not_in_state(self):
        # процесс упал между сканом и ingest — файл обработают заново
        self.journal.record(
            self.area, "a.txt", JournalEvent.DISCOVERED, size=3, mtime_ns=7
        )
        self.journal.record(self.area, "a.txt", JournalEvent.SENT)
        self.assertEqual(self.journal.snapshot((self.area,)), {})
        self.assertEqual(Journal(self.path).snapshot((self.area,)), {})

    def test_deleted_file_not_in_state(self):
        self._settle()
        self.journal.record(self.area, "a.txt", JournalEvent.DELETED, detail="убран")
        self.assertEqual(Journal(self.path).snapshot((self.area,)), {})

    def test_reappeared_file_uses_new_stat(self):
        self._settle()
        self.journal.record(self.area, "a.txt", JournalEvent.DELETED)
        self.journal.record(
            self.area, "a.txt", JournalEvent.DISCOVERED, size=9, mtime_ns=11
        )
        self.journal.record(self.area, "a.txt", JournalEvent.INDEXED)
        snap = Journal(self.path).snapshot((self.area,))
        self.assertEqual(snap[self.vault].files, {"a.txt": (9, 11)})

    def test_modified_file_updates_stat(self):
        self._settle()
        self.journal.record(
            self.area, "a.txt", JournalEvent.DISCOVERED, size=4, mtime_ns=8
        )
        self.journal.record(self.area, "a.txt", JournalEvent.INDEXED)
        snap = Journal(self.path).snapshot((self.area,))
        self.assertEqual(snap[self.vault].files, {"a.txt": (4, 8)})

    def test_skipped_and_error_are_settled(self):
        for event in (JournalEvent.SKIPPED, JournalEvent.ERROR):
            path = self.root / f"{event.value}.jsonl"
            journal = Journal(path)
            journal.record(
                self.area, "a.txt", JournalEvent.DISCOVERED, size=1, mtime_ns=2
            )
            journal.record(self.area, "a.txt", event)
            snap = journal.snapshot((self.area,))
            self.assertEqual(snap[self.vault].files, {"a.txt": (1, 2)})

    def test_unknown_area_ignored(self):
        self._settle()
        other = AreaConfig(path=self.root / "other", role=Area.WEB, formats=("text",))
        self.assertEqual(self.journal.snapshot((other,)), {})
        snap = self.journal.snapshot((other, self.area))
        self.assertEqual(set(snap), {self.vault})

    def test_broken_lines_skipped(self):
        self.journal.record(
            self.area, "a.txt", JournalEvent.DISCOVERED, size=3, mtime_ns=7
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{не json\n")
            handle.write('{"root": 5, "file": "b.txt", "event": "indexed"}\n')
            handle.write('{"root": "/x", "file": "c.txt", "event": "нет-такого"}\n')
        # до завершения файла состояние пусто, после — восстанавливается
        self.assertEqual(Journal(self.path).snapshot((self.area,)), {})
        self.journal.record(self.area, "a.txt", JournalEvent.INDEXED)
        snap = Journal(self.path).snapshot((self.area,))
        self.assertEqual(snap[self.vault].files, {"a.txt": (3, 7)})


class _BoomExtractor:
    """Экстрактор, который всегда сообщает об ошибке разбора."""

    name = "boom"

    def suffixes(self) -> frozenset[str]:
        return frozenset({".boom"})

    def extract(self, path: Path) -> Extracted:
        return Extracted(text="", error="сломанный файл")


class JournalPipelineTest(unittest.TestCase):
    """Конвейер наполняет журнал цепочкой переходов по файлу."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.vault = self.root / "vault"
        self.vault.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.area = AreaConfig(
            path=self.vault,
            role=Area.PERSONAL,
            formats=("markdown", "text", "boom"),
        )
        self.store = SqliteStore(self.data / "db.sqlite")
        ingest = IngestService(
            self.store, FsTextStore(self.data / "texts"), NoopStemmer()
        )
        registry = ExtractorRegistry(
            (MarkdownExtractor(), TextExtractor(), _BoomExtractor())
        )
        self.journal = Journal(self.data / "journal.jsonl")
        self.pipeline = IndexingPipeline(
            ingest, registry, metastore=self.store, journal=self.journal
        )
        self.scanner = AreaScanner(WatchConfig(debounce=0.0))
        self._snaps: dict[Path, AreaScan] = {}

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _scan(self, area: AreaConfig | None = None) -> ScanResult:
        area = area or self.area
        snap, events = self.scanner.scan(area, self._snaps.get(area.path))
        self._snaps = {**self._snaps, area.path: snap}
        return ScanResult(area=area, events=events)

    def _events(self, name: str) -> list[str]:
        out: list[str] = []
        text = (self.data / "journal.jsonl").read_text(encoding="utf-8")
        for line in text.splitlines():
            entry = json.loads(line)
            if entry["file"] == name:
                out.append(str(entry["event"]))
        return out

    def test_new_file_chain(self):
        (self.vault / "note.md").write_text("заметка про котиков", encoding="utf-8")
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.indexed, 1)
        self.assertEqual(self._events("note.md"), ["discovered", "sent", "indexed"])

    def test_duplicate_chain(self):
        (self.vault / "a.md").write_text("общий текст", encoding="utf-8")
        self.pipeline.process(self._scan())
        (self.vault / "b.md").write_text("общий текст", encoding="utf-8")
        self.pipeline.process(self._scan())
        self.assertEqual(self._events("b.md"), ["discovered", "sent", "duplicate"])

    def test_skipped_format_chain(self):
        area = AreaConfig(path=self.vault, role=Area.PERSONAL, formats=("text",))
        (self.vault / "note.md").write_text("текст", encoding="utf-8")
        self.pipeline.process(self._scan(area))
        self.assertEqual(self._events("note.md"), ["discovered", "skipped"])

    def test_error_chain(self):
        (self.vault / "broken.boom").write_text("x", encoding="utf-8")
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.errors, 1)
        self.assertEqual(self._events("broken.boom"), ["discovered", "error"])

    def test_delete_chain(self):
        (self.vault / "note.md").write_text("уникальный текст", encoding="utf-8")
        self.pipeline.process(self._scan())
        (self.vault / "note.md").unlink()
        self.pipeline.process(self._scan())
        self.assertEqual(self._events("note.md")[-1], "deleted")
        last = json.loads(
            (self.data / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertIn("удалён", str(last["detail"]))
        self.assertEqual(self.store.counts()["documents"], 0)


class JournalWatcherTest(unittest.TestCase):
    """Состояние между запусками берётся из журнала, а не из снапшота."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.vault = self.root / "vault"
        self.data.mkdir()
        self.vault.mkdir()
        self.area = AreaConfig(
            path=self.vault, role=Area.PERSONAL, formats=("markdown", "text")
        )
        self.journal_path = self.data / "journal.jsonl"
        self.store = SqliteStore(self.data / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _run(self, watch: WatchConfig | None = None) -> list[str]:
        """Один «запуск» watcher'а: новые объекты, состояние — из журнала."""
        ingest = IngestService(
            self.store, FsTextStore(self.data / "texts"), NoopStemmer()
        )
        registry = ExtractorRegistry((MarkdownExtractor(), TextExtractor()))
        journal = Journal(self.journal_path)
        pipeline = IndexingPipeline(
            ingest, registry, metastore=self.store, journal=journal
        )
        seen: list[str] = []

        def on_events(result: ScanResult) -> None:
            for event in result.events:
                seen.append(f"{event.rel_path}:{event.kind.value}")
            pipeline.process(result)

        watcher = Watcher(
            (self.area,), watch or WatchConfig(debounce=0.0), on_events, journal=journal
        )
        watcher.run_once()
        return seen

    def test_settled_file_not_reprocessed_deletion_seen(self):
        (self.vault / "a.md").write_text("уникальный текст", encoding="utf-8")
        self.assertEqual(self._run(), ["a.md:created"])
        self.assertEqual(self.store.counts()["documents"], 1)
        self.assertEqual(self._run(), [])  # уже «устроен» — событий нет
        (self.vault / "a.md").unlink()
        self.assertEqual(self._run(), ["a.md:deleted"])
        self.assertEqual(self.store.counts()["documents"], 0)

    def test_fresh_file_not_swallowed_by_debounce(self):
        watch = WatchConfig(poll_interval=0.05, debounce=0.2)
        (self.vault / "fresh.md").write_text("свежий", encoding="utf-8")
        self.assertEqual(self._run(watch), [])  # файл ещё «пишется»
        time.sleep(0.3)
        self.assertEqual(self._run(watch), ["fresh.md:created"])


if __name__ == "__main__":
    unittest.main()
