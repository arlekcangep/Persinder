"""Тесты удаления: remove_source, каскад документа, общие источники."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Chunk, Document, EmbeddingJob
from persinder.store import SqliteStore


class RemoveSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SqliteStore(Path(self._tmp.name) / "db.sqlite")

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

    def test_remove_source_removes_lone_document(self):
        self._save("d1", "текст", "personal", "a.md")
        removed = self.store.remove_source("personal", "a.md")
        self.assertEqual(removed, "d1")
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 0)
        self.assertEqual(counts["sources"], 0)
        self.assertEqual(counts["chunks"], 0)
        self.assertEqual(counts["pending_embeddings"], 0)

    def test_remove_one_of_two_sources_keeps_document(self):
        self._save("d1", "текст", "personal", "a.md")
        self.store.add_source("d1", "personal", "b.md")
        self.assertIsNone(self.store.remove_source("personal", "a.md"))
        self.assertEqual(self.store.counts()["documents"], 1)
        removed = self.store.remove_source("personal", "b.md")
        self.assertEqual(removed, "d1")
        self.assertEqual(self.store.counts()["documents"], 0)

    def test_remove_unknown_source_is_noop(self):
        self.assertIsNone(self.store.remove_source("personal", "ghost.md"))

    def test_has_source(self):
        self._save("d1", "текст", "personal", "a.md")
        self.assertTrue(self.store.has_source("personal", "a.md"))
        self.assertFalse(self.store.has_source("personal", "b.md"))
        self.assertFalse(self.store.has_source("web", "a.md"))
        self.store.remove_source("personal", "a.md")
        self.assertFalse(self.store.has_source("personal", "a.md"))

    def test_fts_cleared_after_delete(self):
        self._save("d1", "квантовый флакс", "personal", "a.md")
        self.assertEqual(len(self.store.search_morph("флакс", limit=5)), 1)
        self.store.remove_source("personal", "a.md")
        self.assertEqual(self.store.search_morph("флакс", limit=5), [])

    def test_delete_document_direct(self):
        self._save("d1", "текст", "web", "p.html")
        self.store.delete_document("d1")
        self.assertEqual(self.store.counts()["documents"], 0)

    def test_same_filename_in_different_areas_independent(self):
        self._save("d1", "текст один", "personal", "note.md")
        self._save("d2", "текст два", "foreign", "note.md")
        self.assertEqual(self.store.remove_source("personal", "note.md"), "d1")
        self.assertEqual(self.store.counts()["documents"], 1)
        self.assertEqual(self.store.remove_source("foreign", "note.md"), "d2")
        self.assertEqual(self.store.counts()["documents"], 0)


if __name__ == "__main__":
    unittest.main()
