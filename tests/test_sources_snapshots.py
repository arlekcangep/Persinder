"""Тесты персистентности снапшотов между запусками watcher'а."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    AreaScanner,
    SnapshotStore,
    WatchConfig,
)


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.area = AreaConfig(path=self.vault, role=Area.PERSONAL, formats=("text",))
        self.path = self.root / "snapshots.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        (self.vault / "a.txt").write_text("x", encoding="utf-8")
        scanner = AreaScanner(WatchConfig(debounce=0.0))
        snap, _ = scanner.scan(self.area)
        store = SnapshotStore(self.path)
        store.save({self.area.path: snap})

        loaded = SnapshotStore(self.path).load((self.area,))
        self.assertIn(self.area.path, loaded)
        self.assertEqual(loaded[self.area.path].files, snap.files)

    def test_load_without_file(self):
        self.assertEqual(SnapshotStore(self.path).load((self.area,)), {})

    def test_load_broken_file(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(SnapshotStore(self.path).load((self.area,)), {})

    def test_area_removed_from_config_skipped(self):
        (self.vault / "a.txt").write_text("x", encoding="utf-8")
        scanner = AreaScanner(WatchConfig(debounce=0.0))
        snap, _ = scanner.scan(self.area)
        SnapshotStore(self.path).save({self.area.path: snap})
        other = AreaConfig(path=self.root / "elsewhere", role=Area.WEB, formats=())
        self.assertEqual(SnapshotStore(self.path).load((other,)), {})

    def test_watcher_persists_between_runs(self):
        """Главный сценарий: два запуска — второй видит удаление."""
        from persinder.sources import Watcher

        (self.vault / "a.txt").write_text("x", encoding="utf-8")

        def collect(run: int) -> list[str]:
            from persinder.sources import ScanResult

            seen: list[str] = []

            def on_events(result: ScanResult) -> None:
                for e in result.events:
                    seen.append(f"{e.rel_path}:{e.kind.value}")

            store = SnapshotStore(self.path)
            watcher = Watcher(
                (self.area,),
                WatchConfig(poll_interval=0.05, debounce=0.0),
                on_events,
                scanner=AreaScanner(WatchConfig(debounce=0.0)),
                snapshots=store,
            )
            watcher.start()
            import time

            time.sleep(0.3)
            watcher.stop()
            return seen

        first = collect(1)
        self.assertIn("a.txt:created", first)
        (self.vault / "a.txt").unlink()
        second = collect(2)
        self.assertIn("a.txt:deleted", second)


if __name__ == "__main__":
    unittest.main()
