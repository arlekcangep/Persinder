"""Тесты реестра моделей: каталог, установка/удаление, фабрика (D24)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.embed import (
    PLACEHOLDER,
    build_embedder,
    install,
    installed_keys,
    is_installed,
    models_root,
    remove_weights,
    validate_key,
)
from persinder.embed.registry import CATALOG
from persinder.embed.service import PlaceholderEmbedder


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_catalog_has_default_model(self):
        self.assertIn("e5-small-int8-v1", CATALOG)
        spec = CATALOG["e5-small-int8-v1"]
        self.assertEqual(spec.dim, 384)
        names = {name for _, name in spec.files}
        self.assertIn("model.onnx", names)
        self.assertIn("tokenizer.json", names)

    def test_validate_key(self):
        validate_key("e5-small-int8-v1")
        validate_key("placeholder-v1")
        for bad in ("", "плохое имя", "a/b", "..", ".", "a b"):
            with self.assertRaises(ValueError):
                validate_key(bad)

    def test_not_installed_in_empty_root(self):
        self.assertEqual(installed_keys(self.root), [])
        self.assertFalse(is_installed(self.root, "e5-small-int8-v1"))

    def test_installed_keys_scans_subdirs(self):
        (self.root / "e5-small-int8-v1").mkdir(parents=True)
        (self.root / "e5-small-int8-v1" / "model.onnx").write_bytes(b"x")
        (self.root / "e5-small-int8-v1" / "tokenizer.json").write_bytes(b"y")
        (self.root / "broken").mkdir()
        (self.root / "broken" / "model.onnx").write_bytes(b"x")
        # неполная пара не считается установленной
        self.assertEqual(installed_keys(self.root), ["e5-small-int8-v1"])
        self.assertTrue(is_installed(self.root, "e5-small-int8-v1"))
        self.assertFalse(is_installed(self.root, "broken"))

    def test_install_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            install(self.root, "unknown-model")

    def test_remove_weights_refuses_placeholder(self):
        with self.assertRaises(ValueError):
            remove_weights(self.root, PLACEHOLDER)

    def test_remove_weights_deletes_dir(self):
        target = self.root / "e5-small-int8-v1"
        target.mkdir()
        (target / "model.onnx").write_bytes(b"x")
        remove_weights(self.root, "e5-small-int8-v1")
        self.assertFalse(target.exists())
        # повторное удаление не падает
        remove_weights(self.root, "e5-small-int8-v1")

    def test_build_embedder_placeholder(self):
        embedder = build_embedder(self.root, PLACEHOLDER)
        self.assertIsInstance(embedder, PlaceholderEmbedder)
        self.assertEqual(embedder.name, PLACEHOLDER)

    def test_build_embedder_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            build_embedder(self.root, "e5-small-int8-v1")

    def test_build_embedder_installed(self):
        target = self.root / "e5-small-int8-v1"
        target.mkdir(parents=True)
        (target / "model.onnx").write_bytes(b"x")
        (target / "tokenizer.json").write_bytes(b"y")
        from unittest import mock

        fake = mock.Mock()
        with mock.patch("persinder.embed.registry.OnnxEmbedder", return_value=fake):
            embedder = build_embedder(self.root, "e5-small-int8-v1")
        self.assertIs(embedder, fake)


class ModelsRootTest(unittest.TestCase):
    def test_models_live_under_home(self):
        self.assertEqual(models_root(Path("/x/y")), Path("/x/y") / "models")
        self.assertEqual(
            models_root(Path.home() / ".persinder"),
            Path.home() / ".persinder" / "models",
        )


if __name__ == "__main__":
    unittest.main()
