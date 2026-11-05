"""Тесты ANN-индекса (модуль 3): обёртка USearch, ключи, персистентность."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
