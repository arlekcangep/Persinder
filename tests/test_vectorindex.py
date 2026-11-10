"""Тесты модуля 3: VectorSearch — косинус по SQLite-таблице embeddings."""

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Chunk, Document, EmbeddingJob
from persinder.embed import pack_f16_vectors
from persinder.interfaces import Embedder
from persinder.store import SqliteStore
from persinder.vectorindex import ModelScope, VectorSearch, rebuild_index

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

    def test_min_score_filters_low_similarity(self):
        self._seed()
        hits = self.search.search("запрос", limit=10, min_score=0.6)
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in hits],
            [("d1", 0), ("d2", 1)],
            "остаются только хиты с косинусом >= 0.6",
        )

    def test_min_score_above_all_returns_empty(self):
        self._seed()
        self.assertEqual(self.search.search("запрос", limit=10, min_score=2.0), [])

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

    def test_ann_min_score_filters(self):
        self._seed()
        rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        hits = self.ann.search("запрос", 10, min_score=0.75)
        self.assertEqual(
            [(h.document_id, h.chunk_index) for h in hits],
            [("d1", 0), ("d2", 1)],
        )

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


class VectorSearchModelScopeTest(unittest.TestCase):
    """Несколько активных моделей и скоуп по папкам (без мусора из чужих)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = SqliteStore(self.root / "db.sqlite")
        self.ann_dir = self.root / "ann"
        self.ann_dir.mkdir()
        self.query_vector = [1.0, 0.0, 0.0, 0.0]

        def factory(_key: str) -> Embedder:
            return FixedEmbedder(self.query_vector)

        self.factory = factory

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _save_at(self, doc_id: str, source_path: str) -> None:
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
        self.store.save_document(
            doc, "personal", f"{doc_id}.txt", chunks, jobs, source_path=source_path
        )

    def _search(
        self,
        models: str | Sequence[str],
        scopes: Mapping[str, ModelScope] | None,
    ) -> VectorSearch:
        return VectorSearch(
            self.store,
            self.factory,
            search_model=models,
            ann_dir=self.ann_dir,
            scopes=scopes,
        )

    def test_models_merge_by_max_score(self):
        self._save_at("d1", "/vault/d1.md")
        self._save_at("d2", "/vault/d2.md")
        self.store.put_embeddings("m1", [("d1", 0, _blob([0.90, 0.0, 0.0, 0.0]))])
        self.store.put_embeddings(
            "m2",
            [
                ("d1", 0, _blob([0.95, 0.0, 0.0, 0.0])),
                ("d2", 0, _blob([0.50, 0.0, 0.0, 0.0])),
            ],
        )
        search = self._search(("m1", "m2"), None)
        hits = search.search("запрос", limit=10)
        self.assertEqual(
            [(hit.document_id, round(hit.score, 2)) for hit in hits],
            [("d1", 0.95), ("d2", 0.5)],
            "один чанк из двух моделей — максимум счёта, без дублей",
        )

    def test_scope_excludes_foreign_folders(self):
        self._save_at("global", "/global/a.md")
        self._save_at("own", "/own/x.md")
        self._save_at("virt", "")
        # у глобальной модели остался старый вектор документа из чужой папки
        self.store.put_embeddings(
            "m-global",
            [
                ("global", 0, _blob([1.0, 0.0, 0.0, 0.0])),
                ("own", 0, _blob([0.9, 0.0, 0.0, 0.0])),
                ("virt", 0, _blob([0.8, 0.0, 0.0, 0.0])),
            ],
        )
        # модель папки индексировала и чужой документ (мусор) — он не нужен
        self.store.put_embeddings(
            "m-own",
            [
                ("own", 0, _blob([1.0, 0.0, 0.0, 0.0])),
                ("global", 0, _blob([0.95, 0.0, 0.0, 0.0])),
            ],
        )
        scopes = {
            "m-global": ModelScope(paths=("/global/",), allow_virtual=True),
            "m-own": ModelScope(paths=("/own/",)),
        }
        hits = self._search(("m-global", "m-own"), scopes).search("запрос", 10)
        self.assertEqual(
            [hit.document_id for hit in hits],
            ["global", "own", "virt"],
            "глобальная не ищет в папке со своей моделью, модель папки — только там",
        )

    def test_semantic_false_area_excluded_from_all(self):
        self._save_at("a", "/off/a.md")
        self._save_at("b", "/on/b.md")
        self.store.put_embeddings(
            "m1",
            [
                ("a", 0, _blob([1.0, 0.0, 0.0, 0.0])),
                ("b", 0, _blob([0.9, 0.0, 0.0, 0.0])),
            ],
        )
        scopes = {"m1": ModelScope(paths=("/on/",), allow_virtual=True)}
        hits = self._search(("m1",), scopes).search("запрос", 10)
        self.assertEqual([hit.document_id for hit in hits], ["b"])

    def test_ann_overfetch_finds_scoped_hit(self):
        # Чужие папки с высоким сходством не должны вытеснить свой документ.
        for i in range(10):
            self._save_at(f"out{i}", f"/other/{i}.md")
            self.store.put_embeddings(
                "m1", [(f"out{i}", 0, _blob([1.0, 0.0, 0.0, 0.0]))]
            )
        self._save_at("mine", "/vault/x.md")
        self.store.put_embeddings("m1", [("mine", 0, _blob([0.1, 0.9, 0.0, 0.0]))])
        rebuild_index(self.store, self.factory, self.ann_dir, "m1")
        search = self._search(
            ("m1",), {"m1": ModelScope(paths=("/vault/",), allow_virtual=False)}
        )
        hits = search.search("запрос", limit=1)
        self.assertEqual([hit.document_id for hit in hits], ["mine"])


if __name__ == "__main__":
    unittest.main()
