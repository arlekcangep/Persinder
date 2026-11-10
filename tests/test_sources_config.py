"""Тесты конфига областей/watcher'а (YAML)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from persinder.domain import Area
from persinder.sources import ConfigError, load_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "vault").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, content: str) -> Path:
        path = self.root / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_full_config(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
watch:
  poll_interval: 5
  debounce: 1
  ignore: ["**/drafts/**"]
"""
        )
        areas, watch = load_config(cfg_path)
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0].role, Area.PERSONAL)
        self.assertTrue(areas[0].recursive)
        self.assertEqual(watch.poll_interval, 5.0)
        self.assertEqual(watch.debounce, 1.0)
        self.assertEqual(watch.ignore, ("**/drafts/**",))

    def test_defaults(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: foreign
"""
        )
        areas, watch = load_config(cfg_path)
        self.assertEqual(areas[0].role, Area.FOREIGN)
        self.assertEqual(watch.poll_interval, 30.0)
        self.assertEqual(watch.debounce, 2.0)
        self.assertEqual(watch.ignore, ())

    def test_area_embedding_model(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
    embedding_model: m1
"""
        )
        areas, _ = load_config(
            cfg_path, known_models=frozenset({"m1", "placeholder-v1"})
        )
        self.assertEqual(areas[0].embedding_model, "m1")
        self.assertTrue(areas[0].semantic)

    def test_area_without_embedding_model_uses_global(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
"""
        )
        areas, _ = load_config(cfg_path)
        self.assertIsNone(areas[0].embedding_model)
        self.assertTrue(areas[0].semantic)

    def test_area_semantic_false(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
    semantic: false
"""
        )
        areas, watch = load_config(cfg_path)
        self.assertFalse(areas[0].semantic)
        self.assertTrue(watch.semantic, "глобальная семантика не выключена")

    def test_global_semantic_false(self):
        cfg_path = self._write(
            f"""
semantic: false
areas:
  - path: {self.root / "vault"}
    role: personal
    semantic: true
"""
        )
        areas, watch = load_config(cfg_path)
        self.assertFalse(watch.semantic)
        self.assertTrue(areas[0].semantic, "область свою настройку сохраняет")

    def test_unknown_embedding_model_rejected(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
    embedding_model: nope
"""
        )
        with self.assertRaises(ConfigError):
            load_config(cfg_path, known_models=frozenset({"m1"}))

    def test_bad_semantic_type(self):
        cfg_path = self._write(
            f"""
semantic: maybe
areas:
  - path: {self.root / "vault"}
    role: personal
"""
        )
        with self.assertRaises(ConfigError):
            load_config(cfg_path)

    def test_formats_preserved_not_normalized(self):
        # имена форматов не схлопываются в классы: markdown остаётся markdown,
        # чтобы реестр мог ограничить класс только этим форматом
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
    formats: [markdown, structure-text]
"""
        )
        known = frozenset({"markdown", "structure-text"})
        areas, _ = load_config(cfg_path, known_formats=known)
        self.assertEqual(areas[0].formats, ("markdown", "structure-text"))

    def test_unknown_format_rejected(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: personal
    formats: [nope]
"""
        )
        with self.assertRaises(ConfigError):
            load_config(cfg_path, known_formats=frozenset({"markdown"}))

    def test_bad_role(self):
        cfg_path = self._write(
            f"""
areas:
  - path: {self.root / "vault"}
    role: nope
"""
        )
        with self.assertRaises(ConfigError):
            load_config(cfg_path)

    def test_missing_dir(self):
        cfg_path = self._write(
            """
areas:
  - path: /nonexistent/dir
    role: personal
"""
        )
        with self.assertRaises(ConfigError):
            load_config(cfg_path)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(self.root / "absent.yaml")

    def test_yaml_error(self):
        cfg_path = self._write("areas: [unclosed")
        with self.assertRaises(ConfigError):
            load_config(cfg_path)

    def test_valid_yaml_dict_shape(self):
        # yaml.safe_load действительно возвращает dict для нашего формата.
        data = yaml.safe_load("areas: []\nwatch: {poll_interval: 1}")
        self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main()
