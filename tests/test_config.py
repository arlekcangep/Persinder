"""Тесты ``Settings``: пути по умолчанию и ``<home>/settings.json``."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from persinder.config import Settings, SettingsError, default_home


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"

    def _write(self, data: object) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    # --- по умолчанию ---

    def test_default_paths_are_inside_home(self):
        settings = Settings(home=self.home)
        self.assertEqual(settings.text_dir, self.home / "text_cache")
        self.assertEqual(settings.virtual_dir, self.home / "virtual")

    def test_load_without_settings_file(self):
        settings = Settings.load(self.home)
        self.assertEqual(settings.home, self.home)
        self.assertEqual(settings.text_dir, self.home / "text_cache")
        self.assertEqual(settings.virtual_dir, self.home / "virtual")

    def test_default_home_is_dot_persinder(self):
        self.assertEqual(default_home(), Path.home() / ".persinder")

    def test_default_and_load_agree(self):
        self.assertEqual(Settings.default().text_dir, Settings.load().text_dir)

    # --- пути из settings.json ---

    def test_absolute_paths_from_settings_file(self):
        other = Path(self._tmp.name) / "big-disk"
        self._write(
            {"text_dir": str(other / "texts"), "virtual_dir": str(other / "virt")}
        )
        settings = Settings.load(self.home)
        self.assertEqual(settings.text_dir, other / "texts")
        self.assertEqual(settings.virtual_dir, other / "virt")

    def test_relative_paths_are_relative_to_home(self):
        self._write({"text_dir": "cache/texts", "virtual_dir": "virt"})
        settings = Settings.load(self.home)
        self.assertEqual(settings.text_dir, self.home / "cache" / "texts")
        self.assertEqual(settings.virtual_dir, self.home / "virt")

    def test_relative_paths_follow_explicit_home(self):
        # --home переопределяет каталог приложения: относительные пути —
        # от нового home, а не от ~/.persinder.
        self._write({"text_dir": "texts"})
        settings = Settings.load(self.home)
        self.assertEqual(settings.text_dir, self.home / "texts")

    def test_tilde_expands(self):
        fake_home = Path(self._tmp.name) / "user"
        fake_home.mkdir()
        self._write({"text_dir": "~/texts"})
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            settings = Settings.load(self.home)
        self.assertEqual(settings.text_dir, fake_home / "texts")

    def test_partial_keys(self):
        self._write({"text_dir": "texts"})
        settings = Settings.load(self.home)
        self.assertEqual(settings.text_dir, self.home / "texts")
        self.assertEqual(settings.virtual_dir, self.home / "virtual")

    # --- ошибки конфига ---

    def test_unknown_key_is_error(self):
        self._write({"txt_dir": "texts"})  # опечатка не должна молча игнорироваться
        with self.assertRaises(SettingsError) as ctx:
            Settings.load(self.home)
        self.assertIn("txt_dir", str(ctx.exception))

    def test_non_string_value_is_error(self):
        self._write({"text_dir": 42})
        with self.assertRaises(SettingsError):
            Settings.load(self.home)

    def test_empty_value_is_error(self):
        self._write({"virtual_dir": "   "})
        with self.assertRaises(SettingsError):
            Settings.load(self.home)

    def test_broken_json_is_error(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "settings.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(SettingsError) as ctx:
            Settings.load(self.home)
        self.assertIn("JSON", str(ctx.exception))

    def test_non_object_json_is_error(self):
        self._write(["text_dir"])
        with self.assertRaises(SettingsError):
            Settings.load(self.home)

    # --- ensure ---

    def test_ensure_creates_configured_dirs(self):
        self._write({"text_dir": "texts", "virtual_dir": "virt"})
        settings = Settings.load(self.home).ensure()
        self.assertTrue(settings.text_dir.is_dir())
        self.assertTrue(settings.virtual_dir.is_dir())
        self.assertTrue(settings.ann_dir.is_dir())

    def test_ensure_creates_dirs_outside_home(self):
        other = Path(self._tmp.name) / "another-disk" / "texts"
        self._write({"text_dir": str(other)})
        Settings.load(self.home).ensure()
        self.assertTrue(other.is_dir())

    def test_settings_path_property(self):
        self.assertEqual(
            Settings(home=self.home).settings_path, self.home / "settings.json"
        )
