"""Тесты конвейера: события сканера → экстракция → ingest."""

from __future__ import annotations

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
    ExtractorRegistry,
    IndexingPipeline,
    MarkdownExtractor,
    ScanResult,
    TextExtractor,
    WatchConfig,
)
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.area = AreaConfig(
            path=self.root / "vault",
            role=Area.PERSONAL,
            formats=("markdown", "text"),
        )
        self.area.path.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.store = SqliteStore(self.data / "db.sqlite")
        ingest = IngestService(
            self.store, FsTextStore(self.data / "texts"), NoopStemmer()
        )
        registry = ExtractorRegistry((MarkdownExtractor(), TextExtractor()))
        self.pipeline = IndexingPipeline(ingest, registry)
        self.scanner = AreaScanner(WatchConfig(debounce=0.0))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.area.path / name
        path.write_text(text, encoding="utf-8")
        return path

    def _scan(self, area: AreaConfig | None = None) -> ScanResult:
        area = area or self.area
        snaps: dict[Path, AreaScan] = getattr(self, "_snaps", {})
        prev = snaps.get(area.path)
        snap, events = self.scanner.scan(area, prev)
        self._snaps = {**snaps, area.path: snap}
        return ScanResult(area=area, events=events)

    def test_created_event_indexes_file(self):
        self._write("note.md", "заметка про котиков")
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.indexed, 1)
        self.assertEqual(self.store.counts()["documents"], 1)

    def test_modified_same_text_is_duplicate(self):
        self._write("note.md", "первая версия")
        self.pipeline.process(self._scan())
        time.sleep(0.01)  # обеспечить иной mtime_ns при перезаписи
        self._write("note.md", "первая версия")
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.duplicates, 1)

    def test_skipped_when_format_not_allowed(self):
        area = AreaConfig(path=self.area.path, role=Area.PERSONAL, formats=("text",))
        self._write("note.md", "#tag")
        stats = self.pipeline.process(self._scan(area))
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(self.store.counts()["documents"], 0)

    def test_txt_still_allowed_for_text_only_area(self):
        area = AreaConfig(path=self.area.path, role=Area.PERSONAL, formats=("text",))
        self._write("note.txt", "просто текст")
        stats = self.pipeline.process(self._scan(area))
        self.assertEqual(stats.indexed, 1)

    def test_deleted_counted(self):
        self._write("note.md", "текст")
        self.pipeline.process(self._scan())
        (self.area.path / "note.md").unlink()
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.indexed, 0)

    def test_indexed_text_is_searchable(self):
        self._write("note.md", "уникальный квантовый флакс")
        self.pipeline.process(self._scan())
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["chunks"], 1)


if __name__ == "__main__":
    unittest.main()
