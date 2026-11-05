"""Тесты SQLite-метастора."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Chunk, Document, EmbeddingJob
from persinder.store import SqliteStore


class SqliteStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SqliteStore(Path(self._tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _save(self, doc_id: str = "d1", stemmed: str = "привет мир") -> None:
        doc = Document(
            id=doc_id,
            content_hash=doc_id,
            title=None,
            char_count=10,
            text_key=doc_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
        chunks = [Chunk(index=0, char_start=0, char_end=10, stemmed=stemmed)]
        jobs = [
            EmbeddingJob(
                doc_id=doc_id,
                chunk_index=0,
                area="personal",
                created_at="2026-01-01T00:00:00+00:00",
            )
        ]
        self.store.save_document(doc, "personal", "a.txt", chunks, jobs)

    def test_save_and_find_by_hash(self):
        self._save()
        self.assertEqual(self.store.find_document_by_hash("d1"), "d1")
        self.assertIsNone(self.store.find_document_by_hash("nope"))

    def test_search_morph(self):
        self._save(stemmed="привет мир нейросети")
        found = self.store.search_morph("нейросети")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["doc_id"], "d1")

    def test_pending_embeddings(self):
        self._save()
        pending = self.store.pending_embeddings()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["doc_id"], "d1")
        self.assertEqual(pending[0]["model"], "placeholder-v1")

    def test_enqueue_document_for_models(self):
        self._save()
        self.store.enqueue_document("d1", "personal", "b.txt", models=["m1", "m2"])
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(len(pending), 3)  # placeholder-v1 + m1 + m2
        models = sorted(item["model"] for item in pending)
        self.assertEqual(models, ["m1", "m2", "placeholder-v1"])

    def test_drop_queue_model(self):
        self._save()
        self.store.enqueue_document("d1", "personal", "b.txt", models=["m1"])
        self.store.drop_queue_model("m1")
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual([item["model"] for item in pending], ["placeholder-v1"])

    def test_enqueue_all_for_models(self):
        self._save()
        self.store.enqueue_all(["m1", "m2"])
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(len(pending), 3)  # placeholder-v1 + m1 + m2

    def test_all_sources(self):
        self._save()
        self.store.add_source("d1", "personal", "b.txt", source_path="/x/b.txt")
        sources = self.store.all_sources()
        self.assertTrue(any(row["source_path"] == "/x/b.txt" for row in sources))
        self.assertTrue(any(row["source_path"] == "" for row in sources))

    def test_counts(self):
        self._save()
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["chunks"], 1)
        self.assertEqual(counts["pending_embeddings"], 1)


if __name__ == "__main__":
    unittest.main()
