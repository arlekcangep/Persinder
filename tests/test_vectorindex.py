"""Тесты модуля 3: VectorSearch — косинус по SQLite-таблице embeddings."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Chunk, Document, EmbeddingJob
from persinder.embed import pack_f16_vectors
from persinder.interfaces import Embedder
from persinder.store import SqliteStore
from persinder.vectorindex import VectorSearch, rebuild_index

DIM = 4


class FixedEmbedder:
    """Эмбеддер с фиксированным вектором запроса (тест не про нейросеть)."""

    name = "m1"
    dim = DIM

    def __init__(self, query_vector: list[float]) -> None:
        self._query_vector = query_vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._query_vector for _ in texts]

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return [self._query_vector for _ in texts]


def _blob(vector: list[float]) -> bytes:
    return pack_f16_vectors([vector], DIM)


class VectorSearchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SqliteStore(Path(self._tmp.name) / "db.sqlite")
        self.query_vector = [1.0, 0.0, 0.0, 0.0]
        self.search = VectorSearch(
            self.store,
            lambda key: FixedEmbedder(self.query_vector),
            search_model="m1",
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _save(self, doc_id: str) -> None:
        """Создать документ (FK таблицы embeddings ссылается на documents)."""
        doc = Document(
            id=doc_id,
            content_hash=doc_id,
            title=None,
            char_count=10,
            text_key=doc_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
        chunks = [Chunk(index=0, char_start=0, char_end=10, stemmed="текст")]
        jobs = [
            EmbeddingJob(
                doc_id=doc_id,
                chunk_index=0,
                area="personal",
                created_at="2026-01-01T00:00:00+00:00",
            )
        ]
        self.store.save_document(doc, "personal", f"{doc_id}.txt", chunks, jobs)

    def _seed(self) -> None:
        for doc_id in ("d1", "d2"):
            self._save(doc_id)
        # векторы нормированы к единичной длине; косинус = скалярное произведение
        self.store.put_embeddings(
            "m1",
            [
                ("d1", 0, _blob([1.0, 0.0, 0.0, 0.0])),  # 1.0
                ("d1", 1, _blob([0.5, 0.5, 0.0, 0.0])),  # 0.5
                ("d2", 0, _blob([0.0, 1.0, 0.0, 0.0])),  # 0.0
                ("d2", 1, _blob([0.8, 0.2, 0.0, 0.0])),  # 0.8
            ],
        )

    def test_ranked_by_cosine_desc(self):
        self._seed()
        hits = self.search.search("запрос про оси X", limit=10)
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in hits],
            [("d1", 0), ("d2", 1), ("d1", 1), ("d2", 0)],
        )
        self.assertAlmostEqual(hits[0].score, 1.0, places=3)
        self.assertAlmostEqual(hits[1].score, 0.8, places=3)

    def test_top_k_limit(self):
        self._seed()
        hits = self.search.search("запрос", limit=2)
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in hits],
            [("d1", 0), ("d2", 1)],
        )

    def test_empty_table_returns_empty(self):
        self.assertEqual(self.search.search("ничего нет"), [])

    def test_only_search_model_rows(self):
        # векторы другой модели в таблице не участвуют (фильтр по модели)
        self._save("d3")
        self.store.put_embeddings("m2", [("d3", 0, _blob([1.0, 0.0, 0.0, 0.0]))])
        self.assertEqual(self.search.search("запрос"), [])

    def test_blank_text_and_zero_limit(self):
        self._seed()
        self.assertEqual(self.search.search("   "), [])
        self.assertEqual(self.search.search("запрос", limit=0), [])
        self.assertEqual(self.search.search("запрос", limit=-1), [])

    def test_uses_search_model_embedder(self):
        # поиск идёт активной поисковой моделью, а не «первым попавшимся»
        captured: list[str] = []

        def factory(key: str) -> Embedder:
            captured.append(key)
            return FixedEmbedder(self.query_vector)

        search = VectorSearch(self.store, factory, search_model="e5-small-int8-v1")
        self.assertEqual(search.search("запрос"), [])
        self.assertEqual(captured, ["e5-small-int8-v1"])


class VectorSearchAnnTest(unittest.TestCase):
    """ANN-путь (USearch): индекс поверх таблицы + фолбэк на точный скан."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SqliteStore(Path(self._tmp.name) / "db.sqlite")
        self.ann_dir = Path(self._tmp.name) / "ann"
        self.ann_dir.mkdir()
        self.query_vector = [1.0, 0.0, 0.0, 0.0]

        def factory(key: str) -> Embedder:
            return FixedEmbedder(self.query_vector)

        self.factory = factory
        self.scan = VectorSearch(self.store, factory, search_model="m1")
        self.ann = VectorSearch(
            self.store, factory, search_model="m1", ann_dir=self.ann_dir
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _save(self, doc_id: str) -> None:
        doc = Document(
            id=doc_id,
            content_hash=doc_id,
            title=None,
            char_count=10,
            text_key=doc_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
        chunks = [Chunk(index=0, char_start=0, char_end=10, stemmed="текст")]
        jobs = [
            EmbeddingJob(
                doc_id=doc_id,
                chunk_index=0,
                area="personal",
                created_at="2026-01-01T00:00:00+00:00",
            )
        ]
        self.store.save_document(doc, "personal", f"{doc_id}.txt", chunks, jobs)

    def _seed(self) -> None:
        for doc_id in ("d1", "d2"):
            self._save(doc_id)
        self.store.put_embeddings(
            "m1",
            [
                ("d1", 0, _blob([1.0, 0.0, 0.0, 0.0])),
                ("d1", 1, _blob([0.5, 0.5, 0.0, 0.0])),
                ("d2", 0, _blob([0.0, 1.0, 0.0, 0.0])),
                ("d2", 1, _blob([0.8, 0.2, 0.0, 0.0])),
            ],
        )

    def test_falls_back_to_scan_without_index_file(self):
        self._seed()
        # файла индекса нет → путь с ann_dir работает через точный скан
        self.assertFalse((self.ann_dir / "m1.usearch").exists())
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in self.ann.search("запрос", 10)],
            [("d1", 0), ("d2", 1), ("d1", 1), ("d2", 0)],
        )

    def test_rebuild_index_returns_count_and_creates_file(self):
        self._seed()
        count = rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        self.assertEqual(count, 4)
        self.assertTrue((self.ann_dir / "m1.usearch").exists())

    def test_ann_matches_exact_scan(self):
        self._seed()
        rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        ann_hits = [
            (h.document_id, h.chunk_index) for h in self.ann.search("запрос", 10)
        ]
        scan_hits = [
            (h.document_id, h.chunk_index) for h in self.scan.search("запрос", 10)
        ]
        self.assertEqual(ann_hits, scan_hits)

    def test_stale_key_filtered_after_document_delete(self):
        self._seed()
        rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        # документ удалён каскадом; в ANN-файле его ключи пока остались
        self.store.delete_document("d1")
        hits = self.ann.search("запрос", 10)
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in hits], [("d2", 1), ("d2", 0)]
        )

    def test_rebuild_after_cascade_drops_stale_keys(self):
        self._seed()
        rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        self.store.delete_document("d1")
        count = rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        self.assertEqual(count, 2)
        hits = self.ann.search("запрос", 10)
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in hits], [("d2", 1), ("d2", 0)]
        )

    def test_rebuild_empty_model_returns_zero(self):
        self.assertEqual(rebuild_index(self.store, self.factory, self.ann_dir, "m1"), 0)


if __name__ == "__main__":
    unittest.main()
