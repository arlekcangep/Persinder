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

    def test_chunk_span(self):
        self._save()
        self.assertEqual(self.store.chunk_span("d1", 0), (0, 10))
        self.assertIsNone(self.store.chunk_span("d1", 99))
        self.assertIsNone(self.store.chunk_span("nope", 0))

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

    def test_embeddings_roundtrip(self):
        self._save()
        blob = b"\x00\x3c" * 2  # 384 f16 = 768 байт: заполняем нулями
        self.store.put_embeddings("m1", [("d1", 0, blob), ("d1", 1, blob)])
        rows = self.store.embeddings_for_model("m1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["doc_id"], "d1")
        self.assertEqual(rows[0]["chunk_index"], 0)
        self.assertEqual(rows[0]["vector"], blob)
        # другие модели не смешиваются
        self.assertEqual(self.store.embeddings_for_model("m2"), [])

    def test_embeddings_replace_on_same_key(self):
        self._save()
        self.store.put_embeddings("m1", [("d1", 0, b"old")])
        self.store.put_embeddings("m1", [("d1", 0, b"new")])
        rows = self.store.embeddings_for_model("m1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vector"], b"new")

    def test_embeddings_ids_stable_across_upsert(self):
        """``id`` строки не меняется при перезаписи вектора — ключ для ANN (§8)."""
        self._save()
        ids = self.store.put_embeddings("m1", [("d1", 0, b"old"), ("d1", 1, b"x")])
        self.assertEqual(len(ids), 2)
        same = self.store.put_embeddings("m1", [("d1", 0, b"new")])
        self.assertEqual(same, [ids[0]], "перезапись вектора сохраняет id строки")
        self.assertEqual(
            {row["id"] for row in self.store.embeddings_ids_for_model("m1")},
            set(ids),
        )
        self.assertEqual(
            [row["doc_id"] for row in self.store.embeddings_for_ids([ids[0]])],
            ["d1"],
        )
        self.assertEqual(self.store.embedding_models(), ["m1"])
        self.assertEqual(self.store.embeddings_for_ids([]), [])

    def test_embeddings_cascade_on_doc_delete(self):
        """Удаление документа уносит его векторы (FK ON DELETE CASCADE, D24)."""
        self._save(doc_id="d1")
        self.store.put_embeddings("m1", [("d1", 0, b"v")])
        self.store.delete_document("d1")
        self.assertEqual(self.store.embeddings_for_model("m1"), [])

    def test_drop_embeddings_model(self):
        self._save()
        self.store.put_embeddings("m1", [("d1", 0, b"v")])
        self.store.put_embeddings("m2", [("d1", 0, b"v")])
        self.store.drop_embeddings_model("m1")
        self.assertEqual(self.store.embeddings_for_model("m1"), [])
        self.assertEqual(len(self.store.embeddings_for_model("m2")), 1)

    def test_counts(self):
        self._save()
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["chunks"], 1)
        self.assertEqual(counts["pending_embeddings"], 1)


if __name__ == "__main__":
    unittest.main()
