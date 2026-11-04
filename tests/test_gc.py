"""Тесты сборки сирот: текст без документа удаляется, живой — остаётся."""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area, Chunk, Document, EmbeddingJob
from persinder.gc import collect_orphans
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class CollectOrphansTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.text_root = root / "texts"
        self.store = SqliteStore(root / "db.sqlite")
        self.textstore = FsTextStore(self.text_root)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _save(self, doc_id: str, text: str, area: str, filename: str) -> None:
        doc = Document(
            id=doc_id,
            content_hash=doc_id,
            title=None,
            char_count=len(text),
            text_key=doc_id,
            created_at="2024-01-01T00:00:00+00:00",
        )
        chunk = Chunk(index=0, char_start=0, char_end=len(text), stemmed=text)
        job = EmbeddingJob(
            doc_id=doc_id,
            chunk_index=0,
            area=area,
            created_at="2024-01-01T00:00:00+00:00",
        )
        self.store.save_document(doc, area, filename, [chunk], [job])

    def _put(self, key: str, text: str, *, age: float, now: float) -> None:
        """Положить текст и выставить возраст файла (секунды до ``now``)."""
        self.textstore.put(key, text)
        stamp = now - age
        os.utime(self.text_root / f"{key}.txt", (stamp, stamp))

    def test_orphan_removed(self):
        now = time.time()
        self._put("orphan", "никому не нужный текст", age=7200, now=now)
        stats = collect_orphans(self.store, self.textstore, now=now)
        self.assertEqual((stats.scanned, stats.removed, stats.missing), (1, 1, 0))
        self.assertFalse(self.textstore.exists("orphan"))

    def test_live_text_kept(self):
        now = time.time()
        self._save("d1", "нужный текст", "personal", "a.md")
        self._put("d1", "нужный текст", age=7200, now=now)
        stats = collect_orphans(self.store, self.textstore, now=now)
        self.assertEqual((stats.scanned, stats.removed, stats.missing), (1, 0, 0))
        self.assertTrue(self.textstore.exists("d1"))

    def test_fresh_orphan_kept(self):
        # ingest пишет текст раньше документа — совсем свежий файл не трогаем.
        now = time.time()
        self._put("inflight", "ещё не в сторе", age=10, now=now)
        stats = collect_orphans(self.store, self.textstore, now=now)
        self.assertEqual(stats.removed, 0)
        self.assertTrue(self.textstore.exists("inflight"))
        # с нулевым порогом тот же файл уже считается сиротой
        stats = collect_orphans(self.store, self.textstore, min_age_seconds=0, now=now)
        self.assertEqual(stats.removed, 1)

    def test_missing_text_reported(self):
        self._save("d1", "текст", "personal", "a.md")
        stats = collect_orphans(self.store, self.textstore)
        self.assertEqual((stats.scanned, stats.removed, stats.missing), (0, 0, 1))

    def test_tmp_files_untouched(self):
        now = time.time()
        self._put("orphan", "текст", age=7200, now=now)
        tmp = self.text_root / "half.txt.tmp"
        tmp.write_text("недописанное", encoding="utf-8")
        stats = collect_orphans(self.store, self.textstore, now=now)
        self.assertEqual(stats.scanned, 1)  # .tmp в каталог не входит
        self.assertEqual(stats.removed, 1)
        self.assertTrue(tmp.exists())

    def test_empty_store_and_dir(self):
        stats = collect_orphans(self.store, self.textstore)
        self.assertEqual((stats.scanned, stats.removed, stats.missing), (0, 0, 0))


class OrphanAfterRebindTest(unittest.TestCase):
    """Правка файла оставляет старый текст — его и убирает GC."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.text_root = root / "texts"
        self.store = SqliteStore(root / "db.sqlite")
        self.textstore = FsTextStore(self.text_root)
        self.ingest = IngestService(self.store, self.textstore, NoopStemmer())

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _write(self, text: str) -> None:
        self.ingest.ingest(
            IngestRequest(filename="a.md", area=Area.PERSONAL, text=text)
        )

    def test_rebind_leaves_one_orphan(self):
        self._write("версия номер один")
        self._write("версия номер два")
        self.assertEqual(len(self.textstore.keys()), 2)
        stats = collect_orphans(self.store, self.textstore, min_age_seconds=0)
        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.textstore.keys(), self.store.text_keys())

    def test_deleted_source_leaves_orphan(self):
        self._write("текст, который потом удалят")
        self.store.remove_source("personal", "a.md")
        self.assertEqual(self.store.text_keys(), [])
        stats = collect_orphans(self.store, self.textstore, min_age_seconds=0)
        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.textstore.keys(), [])


if __name__ == "__main__":
    unittest.main()
