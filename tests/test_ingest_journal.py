"""Тесты журнала вызовов ingest и восстановления по нему."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area, Chunk, Document, EmbeddingJob
from persinder.ingest import (
    IngestEvent,
    IngestJournal,
    IngestRequest,
    IngestService,
)
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class IngestJournalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ingest.jsonl"
        self.journal = IngestJournal(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_and_parse(self):
        self.journal.record(
            IngestEvent.INDEX,
            doc_id="d1",
            previous="d0",
            area="personal",
            filename="a.md",
            detail="чанков: 2",
        )
        (entry,) = self.journal.entries()
        self.assertEqual(entry.op, IngestEvent.INDEX)
        self.assertEqual(entry.doc_id, "d1")
        self.assertEqual(entry.previous, "d0")
        self.assertEqual(entry.filename, "a.md")
        self.assertEqual(entry.detail, "чанков: 2")
        self.assertIsNotNone(entry.ts)

    def test_unfinished_is_last_per_object(self):
        self.journal.record(IngestEvent.INDEX, doc_id="d1")
        self.journal.record(IngestEvent.INDEXED, doc_id="d1")  # d1 завершён
        self.journal.record(IngestEvent.INDEX, doc_id="d2")  # d2 — нет
        self.journal.record(IngestEvent.REJECTED, area="personal", filename="bad")
        unfinished = self.journal.unfinished()
        self.assertEqual([e.doc_id for e in unfinished], ["d2"])

    def test_broken_lines_skipped(self):
        self.journal.record(IngestEvent.INDEX, doc_id="d1")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{не json\n")
            handle.write('{"op": "нет-такого"}\n')
        self.assertEqual(len(self.journal.entries()), 1)

    def test_compact_keeps_fresh_and_last(self):
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(
            timespec="seconds"
        )
        lines = [
            {"ts": old, "op": "index", "doc_id": "d1"},
            {"ts": old, "op": "indexed", "doc_id": "d1"},
            {"ts": old, "op": "index", "doc_id": "d1"},  # последняя по d1
            {"op": "index", "doc_id": "d2"},  # свежая (без ts)
        ]
        self.path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        removed = self.journal.compact()
        self.assertEqual(removed, 2)  # две старые не-последние записи
        self.assertEqual(len(self.journal.entries()), 2)

    def test_missing_file_is_noop(self):
        self.assertEqual(self.journal.unfinished(), [])
        self.assertEqual(self.journal.compact(), 0)


class RecoveryTest(unittest.TestCase):
    """Ingest восстанавливается по своему журналу на запуске."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.texts = root / "texts"
        self.path = root / "ingest.jsonl"
        self.store = SqliteStore(root / "db.sqlite")
        self.textstore = FsTextStore(self.texts)
        self.journal = IngestJournal(self.path)
        self.ingest = IngestService(
            self.store, self.textstore, NoopStemmer(), journal=self.journal
        )

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

    def test_pending_index_without_document_is_cleaned(self):
        self.journal.record(
            IngestEvent.INDEX, doc_id="d1", area="personal", filename="a.md"
        )
        self.textstore.put("d1", "брошенный текст")
        stats = self.ingest.recover()
        self.assertEqual(stats.removed, 1)
        self.assertFalse(self.textstore.exists("d1"))

    def test_pending_index_with_document_keeps_text(self):
        # Краш после коммита, до записи итога: документ есть — текст не трогаем.
        self._save("d1", "текст", "personal", "a.md")
        self.textstore.put("d1", "текст")
        self.journal.record(
            IngestEvent.INDEX, doc_id="d1", area="personal", filename="a.md"
        )
        stats = self.ingest.recover()
        self.assertEqual(stats.removed, 0)
        self.assertTrue(self.textstore.exists("d1"))

    def test_evicted_previous_is_cleaned(self):
        self._save("d2", "новая версия", "personal", "a.md")
        self.textstore.put("d1", "старая версия")
        self.textstore.put("d2", "новая версия")
        # d1 остался неотмеченным как незавершённый; d2 — завершён
        self.journal.record(
            IngestEvent.INDEX, doc_id="d1", area="personal", filename="a.md"
        )
        self.journal.record(
            IngestEvent.INDEX,
            doc_id="d2",
            previous="d1",
            area="personal",
            filename="a.md",
        )
        self.journal.record(
            IngestEvent.INDEXED, doc_id="d2", area="personal", filename="a.md"
        )
        self.ingest.recover()
        self.assertFalse(self.textstore.exists("d1"))
        self.assertTrue(self.textstore.exists("d2"))

    def test_pending_remove_is_cleaned(self):
        self.textstore.put("d1", "текст удаляемого")
        self.journal.record(
            IngestEvent.REMOVE, doc_id="d1", area="personal", filename="a.md"
        )
        stats = self.ingest.recover()
        self.assertEqual(stats.removed, 1)
        self.assertFalse(self.textstore.exists("d1"))


class InlineCleanupTest(unittest.TestCase):
    """Штатное удаление и правка чистят текст сразу, не дожидаясь восстановления."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.texts = root / "texts"
        self.store = SqliteStore(root / "db.sqlite")
        self.journal = IngestJournal(root / "ingest.jsonl")
        self.ingest = IngestService(
            self.store, FsTextStore(self.texts), NoopStemmer(), journal=self.journal
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _files(self) -> list[str]:
        return sorted(p.name for p in self.texts.glob("*.txt"))

    def test_modify_replaces_text_and_logs(self):
        self.ingest.ingest(
            IngestRequest(filename="a.md", area=Area.PERSONAL, text="версия один")
        )
        self.ingest.ingest(
            IngestRequest(filename="a.md", area=Area.PERSONAL, text="версия два")
        )
        self.assertEqual(len(self._files()), 1)  # старый текст вытеснен
        ops = [e.op for e in self.journal.entries()]
        self.assertEqual(
            ops,
            [
                IngestEvent.INDEX,
                IngestEvent.INDEXED,
                IngestEvent.INDEX,
                IngestEvent.INDEXED,
            ],
        )

    def test_remove_deletes_text_and_logs(self):
        self.ingest.ingest(
            IngestRequest(filename="a.md", area=Area.PERSONAL, text="текст")
        )
        result = self.ingest.remove_source(Area.PERSONAL, "a.md")
        self.assertIsNotNone(result.document_id)
        self.assertEqual(self._files(), [])
        ops = [e.op for e in self.journal.entries()]
        self.assertEqual(ops[-2:], [IngestEvent.REMOVE, IngestEvent.REMOVED])

    def test_duplicate_logs_and_keeps_shared_text(self):
        self.ingest.ingest(
            IngestRequest(filename="a.md", area=Area.PERSONAL, text="общий текст")
        )
        outcome = self.ingest.ingest(
            IngestRequest(filename="b.md", area=Area.PERSONAL, text="общий текст")
        )
        self.assertEqual(outcome.status, "already_indexed")
        self.assertEqual(len(self._files()), 1)  # текст один на двоих
        ops = [e.op for e in self.journal.entries()]
        self.assertEqual(ops[-2:], [IngestEvent.DUPLICATE, IngestEvent.DUPLICATED])

    def test_rejected_logged(self):
        self.ingest.ingest(
            IngestRequest(filename="a.md", area=Area.PERSONAL, text="   ")
        )
        ops = [e.op for e in self.journal.entries()]
        self.assertEqual(ops, [IngestEvent.REJECTED])


if __name__ == "__main__":
    unittest.main()
