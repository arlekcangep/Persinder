"""Watch-цикл: периодический polling-скан областей, выдача событий.

Работает в отдельном потоке, запускается по требованию (D1 — ноль ресурсов
в простое). Нативные события (watchdog) добавляются позже как ускоритель:
они лишь триггерят внеплановый скан, источником правды остаётся polling.

Состояние между сканами и запусками берётся из ``Journal`` (D21): в дифф
идут только «устроенные» файлы, поэтому пропущенные debounce'ом или не
дошедшие до ingest файлы обрабатываются при следующем проходе.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .journal import Journal
from .models import AreaConfig, ScanResult, WatchConfig
from .scanner import AreaScanner, scan_all

EventHandler = Callable[[ScanResult], None]


class Watcher:
    """Периодический скан областей; события отдаются обработчику."""

    def __init__(
        self,
        areas: tuple[AreaConfig, ...],
        watch: WatchConfig,
        on_events: EventHandler,
        scanner: AreaScanner | None = None,
        journal: Journal | None = None,
    ) -> None:
        self._areas = areas
        self._watch = watch
        self._on_events = on_events
        self._scanner = scanner or AreaScanner(watch)
        self._journal = journal
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def scan_once(self) -> list[ScanResult]:
        """Один проход по всем областям: события created/modified/deleted."""
        previous = self._journal.snapshot(self._areas) if self._journal else {}
        _snaps, results = scan_all(self._areas, self._scanner, previous)
        return results

    def run_once(self) -> None:
        """Один проход с выдачей непустых результатов обработчику."""
        for result in self.scan_once():
            if result.events:
                self._on_events(result)

    def _loop(self) -> None:
        # Первый проход: все существующие файлы приходят как CREATED —
        # это начальная индексация. Ingest сам отбросит дубликаты при
        # повторных прогонах (тот же content_hash → already_indexed).
        self.run_once()
        while not self._stop.wait(self._watch.poll_interval):
            self.run_once()

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
