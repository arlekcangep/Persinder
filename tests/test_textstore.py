"""Тесты файлового хранилища текста."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.textstore import FsTextStore


class FsTextStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._root = Path(self._tmp.name) / "texts"
        self.store = FsTextStore(self._root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_put_read_exists_delete(self):
        key = "abc"
        self.assertFalse(self.store.exists(key))
        path = self.store.put(key, "привет мир")
        self.assertTrue(self.store.exists(key))
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.store.read(key), "привет мир")
        self.store.delete(key)
        self.assertFalse(self.store.exists(key))

    def test_overwrite_is_atomic(self):
        self.store.put("k", "первый")
        self.store.put("k", "второй")
        self.assertEqual(self.store.read("k"), "второй")
        # временный файл не остаётся
        leftovers = list(self._root.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_same_text_is_not_rewritten(self):
        """Переиндексация приносит тот же текст — файл не перезаписывается."""
        import os

        path = Path(self.store.put("k", "тот же текст"))
        stamp = 1_000_000_000  # заведомо старое время: любая запись его сменит
        os.utime(path, ns=(stamp, stamp))

        self.store.put("k", "тот же текст")

        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_rejects_bad_key(self):
        with self.assertRaises(ValueError):
            self.store.put("../escape", "x")


if __name__ == "__main__":
    unittest.main()
