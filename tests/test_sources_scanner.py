"""Тесты polling-сканера: снапшот, дифф, ignore-паттерны."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    BUILTIN_IGNORES,
    AreaConfig,
    AreaScanner,
    ChangeKind,
    WatchConfig,
)


def make_area(root: Path, role: Area = Area.PERSONAL) -> AreaConfig:
    return AreaConfig(path=root, role=role)


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.txt").write_text("alpha", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_text("beta", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _scanner(self) -> AreaScanner:
        return AreaScanner(WatchConfig(debounce=0.0))

    def test_snapshot(self):
        snap = self._scanner().snapshot(make_area(self.root))
        self.assertEqual(set(snap.files), {"a.txt", "sub/b.txt"})
        size, mtime = snap.files["a.txt"]
        self.assertEqual(size, 5)
        self.assertGreater(mtime, 0)

    def test_created_modified_deleted(self):
        scanner = self._scanner()
        area = make_area(self.root)
        first, _ = scanner.scan(area)

        time.sleep(0.01)
        (self.root / "a.txt").write_text("alpha2", encoding="utf-8")
        (self.root / "new.txt").write_text("new", encoding="utf-8")
        (self.root / "sub" / "b.txt").unlink()

        _, events = scanner.scan(area, first)
        kinds = {(e.rel_path, e.kind) for e in events}
        self.assertIn(("new.txt", ChangeKind.CREATED), kinds)
        self.assertIn(("a.txt", ChangeKind.MODIFIED), kinds)
        self.assertIn(("sub/b.txt", ChangeKind.DELETED), kinds)

    def test_unchanged_no_events(self):
        scanner = self._scanner()
        area = make_area(self.root)
        first, _ = scanner.scan(area)
        _, events = scanner.scan(area, first)
        self.assertEqual(events, ())

    def test_builtin_ignores(self):
        (self.root / ".obsidian").mkdir()
        (self.root / ".obsidian" / "config").write_text("x", encoding="utf-8")
        (self.root / ".stfolder").mkdir()
        (self.root / ".stfolder" / "mark").write_text("x", encoding="utf-8")
        (self.root / "doc.sync-conflict-20240101.md").write_text("x", encoding="utf-8")
        (self.root / ".syncthing.abc.tmp").write_text("x", encoding="utf-8")
        (self.root / ".persinder").mkdir()
        (self.root / ".persinder" / "sidecar.json").write_text("x", encoding="utf-8")

        snap = self._scanner().snapshot(make_area(self.root))
        self.assertEqual(set(snap.files), {"a.txt", "sub/b.txt"})

    def test_custom_ignore(self):
        (self.root / "sub" / "draft.txt").write_text("x", encoding="utf-8")
        snap = self._scanner().snapshot(make_area(self.root))
        self.assertIn("sub/draft.txt", snap.files)
        scanner = AreaScanner(WatchConfig(debounce=0.0, ignore=("*.txt",)))
        snap2 = scanner.snapshot(make_area(self.root))
        self.assertEqual(snap2.files, {})

    def test_debounce_holds_fresh_files(self):
        scanner = AreaScanner(WatchConfig(debounce=3600.0))
        area = make_area(self.root)
        first, _ = scanner.scan(area)
        time.sleep(0.01)
        (self.root / "fresh.txt").write_text("x", encoding="utf-8")
        _, events = scanner.scan(area, first)
        self.assertEqual(events, ())  # свежий файл ещё «пишется»

    def test_non_recursive(self):
        area = AreaConfig(path=self.root, role=Area.PERSONAL, recursive=False)
        snap = self._scanner().snapshot(area)
        self.assertEqual(set(snap.files), {"a.txt"})

    def test_builtin_patterns_content(self):
        self.assertIn(".obsidian", BUILTIN_IGNORES)
        self.assertIn("*.sync-conflict-*", BUILTIN_IGNORES)


if __name__ == "__main__":
    unittest.main()
