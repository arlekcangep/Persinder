"""Watch-цикл: периодический polling-скан областей, выдача событий.

Работает в отдельном потоке, запускается по требованию (D1 — ноль ресурсов
в простое). Нативные события (watchdog) добавляются позже как ускоритель:
они лишь триггерят внеплановый скан, источником правды остаётся polling.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from .models import AreaConfig, AreaScan, ScanResult, WatchConfig
from .scanner import AreaScanner, scan_all
from .snapshots import SnapshotStore

EventHandler = Callable[[ScanResult], None]


class Watcher:
    """Периодический скан областей; события отдаются обработчику."""

    def __init__(
        self,
        areas: tuple[AreaConfig, ...],
        watch: WatchConfig,
        on_events: EventHandler,
        scanner: AreaScanner | None = None,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self._areas = areas
        self._watch = watch
        self._on_events = on_events
        self._scanner = scanner or AreaScanner(watch)
        self._snapshots_store = snapshots
        self._snapshots: dict[Path, AreaScan] = (
            snapshots.load(areas) if snapshots else {}
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def scan_once(self) -> list[ScanResult]:
        """Один проход по всем областям: события created/modified/deleted."""
        snaps, results = scan_all(self._areas, self._scanner, self._snapshots)
        self._snapshots = snaps
        if self._snapshots_store is not None:
            self._snapshots_store.save(snaps)
        return results

    def _loop(self) -> None:
        # Первый проход: все существующие файлы приходят как CREATED —
        # это начальная индексация. Ingest сам отбросит дубликаты при
        # повторных прогонах (тот же content_hash → already_indexed).
        for result in self.scan_once():
            if result.events:
                self._on_events(result)
        while not self._stop.wait(self._watch.poll_interval):
            for result in self.scan_once():
                if result.events:
                    self._on_events(result)

    def start(self) -> None:
        """Запустить фоновый цикл скана."""
        if self._thread is not None:
            raise RuntimeError("watcher уже запущен")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="watch")
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Остановить цикл и дождаться завершения потока."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
