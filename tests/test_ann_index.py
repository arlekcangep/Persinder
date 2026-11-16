"""Тесты ANN-индекса (модуль 3): обёртка USearch, ключи, персистентность."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from persinder.vectorindex import UsannIndex


class UsannIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "m1.usearch"
        self.index = UsannIndex(self.path, dim=4)

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_orders_by_cosine(self):
        self.index.add(
            [10, 11, 12],
            [[1.0, 0.0, 0.0, 0.0], [0.5, 0.5, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
        )
        hits = self.index.search([1.0, 0.0, 0.0, 0.0], 3)
        self.assertEqual([key for key, _ in hits], [10, 11, 12])
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)
        self.assertAlmostEqual(hits[1][1], 0.7071, places=3)

    def test_duplicate_key_is_skipped(self):
        self.index.add([10], [[1.0, 0.0, 0.0, 0.0]])
        self.index.add([10], [[0.0, 0.0, 1.0, 0.0]])  # тот же ключ — пропуск
        self.assertEqual(self.index.size, 1)
        hits = self.index.search([1.0, 0.0, 0.0, 0.0], 1)
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)  # вектор не изменился

    def test_remove(self):
        self.index.add([1, 2], [[1.0, 0, 0, 0], [0, 1.0, 0, 0]])
        self.index.remove([1])
        self.assertEqual(self.index.keys(), {2})

    def test_sync_drops_stale_keys(self):
        self.index.add([1, 2, 3], [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]])
        self.index.sync([2, 3])  # 1 протух (документ удалён)
        self.assertEqual(self.index.keys(), {2, 3})

    def test_persist_and_reopen(self):
        self.index.add([7], [[1.0, 0.0, 0.0, 0.0]])
        self.assertTrue(self.path.exists())
        reopened = UsannIndex(self.path, dim=4)
        self.assertEqual(reopened.keys(), {7})
        self.assertAlmostEqual(reopened.search([1.0, 0, 0, 0], 1)[0][1], 1.0, places=5)

    def test_drop_removes_file(self):
        self.index.add([7], [[1.0, 0.0, 0.0, 0.0]])
        self.index.drop()
        self.assertFalse(self.path.exists())

    def test_corrupt_file_recovers_fresh(self):
        """Битый файл (обрыв сохранения упавшим прогоном) не валит индекс:
        он удаляется и индекс строится с нуля — документы не застревают."""
        self.path.write_bytes(b"not a usearch file" * 16)
        index = UsannIndex(self.path, dim=4)  # открытие падает — не разваливается
        self.assertFalse(self.path.exists(), "битый файл удалён")
        index.add([1], [[1.0, 0.0, 0.0, 0.0]])
        self.assertTrue(self.path.exists())
        self.assertEqual(index.size, 1)
        self.assertAlmostEqual(index.search([1.0, 0, 0, 0], 1)[0][1], 1.0, places=5)

    def test_search_matches_exact_scan_on_dense_cone(self):
        """Выдача совпадает с точным сканом на «конусе», как у e5.

        Векторы пассажей e5 лежат плотным конусом (попарный косинус ~0.8), а
        запрос приходит из другой области пространства. Граф HNSW на такой
        геометрии не доходит до краевых векторов и теряет истинных соседей:
        на реальном корпусе запрос «волчок» не отдавал единственный нужный
        чанк (косинус 0.849 — лучший), хотя при точном переборе он первый.
        Поэтому ответ индекса обязан совпадать с точным сканом: до
        ``EXACT_SEARCH_MAX`` он и есть точный перебор.
        """
        path = Path(self._tmp.name) / "cone.usearch"
        dim, count = 384, 4000
        rng = np.random.default_rng(0)
        axis = np.zeros(dim, dtype=np.float32)
        axis[0] = 1.0
        vectors = axis + 0.038 * rng.standard_normal((count, dim)).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        index = UsannIndex(path, dim=dim)
        index.add(list(range(1, count + 1)), vectors.tolist())

        # На этом наборе чистый ANN (граф, даже с запасом обхода 128) теряет
        # соседей у 4 запросов из 50 — тест падал бы на приблизительном поиске.
        for query in vectors[:50]:
            expected = [
                int(position) + 1
                for position in np.argsort(-(vectors @ query), kind="stable")[:5]
            ]
            got = [key for key, _ in index.search(query.tolist(), 5)]
            self.assertEqual(got, expected, "ANN потерял соседей, которые видит скан")


if __name__ == "__main__":
    unittest.main()
