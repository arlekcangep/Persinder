"""Тесты конвейера: события сканера → экстракция → ingest."""

from __future__ import annotations

import time
import unittest
import zipfile
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
    Journal,
    MarkdownExtractor,
    ScanResult,
    TextExtractor,
    WatchConfig,
    ZipExtractor,
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
        self.texts = self.data / "texts"
        ingest = IngestService(self.store, FsTextStore(self.texts), NoopStemmer())
        registry = ExtractorRegistry((MarkdownExtractor(), TextExtractor()))
        self.journal = Journal(self.data / "journal.jsonl")
        self.pipeline = IndexingPipeline(ingest, registry, journal=self.journal)
        self.scanner = AreaScanner(WatchConfig(debounce=0.0))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.area.path / name
        path.write_text(text, encoding="utf-8")
        return path

    def _texts(self) -> list[str]:
        return sorted(p.name for p in self.texts.glob("*.txt"))

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

    def test_deleted_removes_document(self):
        self._write("note.md", "уникальный текст для удаления")
        self.pipeline.process(self._scan())
        self.assertEqual(self.store.counts()["documents"], 1)
        self.assertEqual(len(self._texts()), 1)
        (self.area.path / "note.md").unlink()
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.deleted, 1)
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 0)
        self.assertEqual(counts["chunks"], 0)
        self.assertEqual(counts["pending_embeddings"], 0)
        self.assertEqual(self._texts(), [])  # сырой текст убран вместе с документом

    def test_modified_removes_old_text(self):
        self._write("note.md", "версия номер один")
        self.pipeline.process(self._scan())
        self.assertEqual(len(self._texts()), 1)
        time.sleep(0.01)
        self._write("note.md", "версия номер два")
        self.pipeline.process(self._scan())
        self.assertEqual(self.store.counts()["documents"], 1)
        self.assertEqual(len(self._texts()), 1)  # старый текст вытеснен

    def test_deleted_source_keeps_shared_document(self):
        # Один и тот же текст в двух файлах: один источник убрали —
        # документ жив, пока жив второй.
        text = "общий текст двух источников"
        self._write("a.md", text)
        self._write("b.md", text)
        self.pipeline.process(self._scan())
        self.assertEqual(self.store.counts()["documents"], 1)
        (self.area.path / "a.md").unlink()
        stats = self.pipeline.process(self._scan())
        self.assertEqual(stats.deleted, 1)
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)  # b.md держит документ
        (self.area.path / "b.md").unlink()
        self.pipeline.process(self._scan())
        self.assertEqual(self.store.counts()["documents"], 0)

    def test_deleted_unknown_file_is_noop(self):
        self._write("note.md", "текст")
        self.pipeline.process(self._scan())
        (self.area.path / "note.md").unlink()
        self.pipeline.process(self._scan())
        # Повторное удаление того же пути — без ошибок.
        from persinder.sources import ChangeEvent, ChangeKind, ScanResult

        phantom = ScanResult(
            area=self.area,
            events=(ChangeEvent(ChangeKind.DELETED, self.area, "note.md"),),
        )
        stats = self.pipeline.process(phantom)
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(stats.errors, 0)

    def test_indexed_text_is_searchable(self):
        self._write("note.md", "уникальный квантовый флакс")
        self.pipeline.process(self._scan())
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["chunks"], 1)

    def test_zip_archive_indexed_through_pipeline(self):
        # Область допускает zip; внутреннее содержимое — markdown/text.
        area = AreaConfig(
            path=self.area.path,
            role=Area.PERSONAL,
            formats=("zip", "markdown", "text"),
        )
        md = MarkdownExtractor()
        txt = TextExtractor()
        registry = ExtractorRegistry(
            (md, txt, ZipExtractor({".md": md, ".markdown": md, ".txt": txt}))
        )
        ingest = IngestService(self.store, FsTextStore(self.texts), NoopStemmer())
        pipeline = IndexingPipeline(ingest, registry, journal=self.journal)

        archive_path = self.area.path / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("inside.md", "#tag содержимое архива")

        stats = pipeline.process(self._scan(area))
        self.assertEqual(stats.indexed, 1)
        self.assertEqual(self.store.counts()["documents"], 1)
        # в хранилище лёг склеенный текст всех записей архива
        stored = "".join(
            p.read_text(encoding="utf-8") for p in self.texts.glob("*.txt")
        )
        self.assertIn("содержимое архива", stored)


if __name__ == "__main__":
    unittest.main()
