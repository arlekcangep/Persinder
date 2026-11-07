"""Тесты состояния активных моделей профиля (home/models.json, D24)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.embed import ModelState, load, save


class ModelStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "models.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_to_placeholder(self):
        state = load(self.path)
        self.assertEqual(state.embedding, "placeholder-v1")
        self.assertEqual(state.search, "placeholder-v1")

    def test_missing_or_broken_file_is_default(self):
        self.path.write_text("{не-json", encoding="utf-8")
        self.assertEqual(load(self.path), ModelState())

    def test_roundtrip(self):
        state = ModelState(embedding="e5-small-int8-v1", search="placeholder-v1")
        save(state, self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(load(self.path), state)

    def test_queue_models_single_when_same(self):
        state = ModelState(embedding="e5-small-int8-v1", search="e5-small-int8-v1")
        self.assertEqual(state.queue_models(), ("e5-small-int8-v1",))

    def test_queue_models_union_during_migration(self):
        """embedding ≠ search (миграция) — новые файлы считаются обеими моделями."""
        state = ModelState(embedding="e5-small-int8-v1", search="placeholder-v1")
        self.assertEqual(state.queue_models(), ("e5-small-int8-v1", "placeholder-v1"))

    def test_queue_models_deduplicates(self):
        state = ModelState(embedding="placeholder-v1", search="placeholder-v1")
        self.assertEqual(state.queue_models(), ("placeholder-v1",))


if __name__ == "__main__":
    unittest.main()
