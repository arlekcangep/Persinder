"""Тесты OnnxEmbedder (ONNX e5-small INT8) — реальная модель.

Модель-зависимые тесты пропускаются, если каталог пуст (скачать:
``PERSINDER_MODEL_DIR=… python scripts/download_model.py``). Тест на
отсутствие модели — наоборот, модели не требует и бежит всегда.
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path

from persinder.config import Settings
from persinder.embed import OnnxEmbedder


def model_dir() -> Path:
    """Каталог реальной модели e5: ``models_dir / e5-small-int8-v1``."""
    return Settings.default().models_dir / "e5-small-int8-v1"


HAVE_MODEL = OnnxEmbedder.available(model_dir())


@unittest.skipUnless(HAVE_MODEL, "модель e5-small не скачана")
class OnnxEmbedderTest(unittest.TestCase):
    """Поведение с реальной моделью."""

    def setUp(self) -> None:
        self.embedder = OnnxEmbedder(model_dir())

    def test_name_and_dim(self) -> None:
        self.assertEqual(self.embedder.name, "e5-small-int8-v1")
        self.assertEqual(self.embedder.dim, 384)

    def test_unit_norm(self) -> None:
        vectors = self.embedder.embed(["котики любят молоко", "самолёты летают высоко"])
        self.assertEqual(len(vectors), 2)
        for vector in vectors:
            norm = math.sqrt(sum(v * v for v in vector))
            self.assertAlmostEqual(norm, 1.0, places=4)

    def test_deterministic(self) -> None:
        text = "детерминированность эмбеддингов"
        first = self.embedder.embed([text])[0]
        second = self.embedder.embed([text])[0]
        self.assertEqual(first, second)

    def test_empty_list(self) -> None:
        self.assertEqual(self.embedder.embed([]), [])

    def test_semantic_closeness(self) -> None:
        cats = self.embedder.embed(["котики и котята"])[0]
        more_cats = self.embedder.embed(["коты, кошки, котёнок"])[0]
        plane = self.embedder.embed(["самолёты и авиация"])[0]
        sim_cats = sum(a * b for a, b in zip(cats, more_cats, strict=True))
        sim_cross = sum(a * b for a, b in zip(cats, plane, strict=True))
        self.assertGreater(sim_cats, sim_cross)

    def test_long_text_truncated(self) -> None:
        # не падает на тексте длиннее макс. токенов модели (512)
        vectors = self.embedder.embed(["слово " * 5000])
        self.assertEqual(len(vectors), 1)
        norm = math.sqrt(sum(v * v for v in vectors[0]))
        self.assertAlmostEqual(norm, 1.0, places=4)


class OnnxEmbedderNoModelTest(unittest.TestCase):
    """Поведение без модели — каталог не нужен."""

    def test_missing_model_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            OnnxEmbedder(Path("/несуществующий/каталог/модели"))

    def test_available_false_on_empty_dir(self) -> None:
        self.assertFalse(OnnxEmbedder.available(Path("/несуществующий/каталог/модели")))


if __name__ == "__main__":
    unittest.main()
