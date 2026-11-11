"""``persinder watch``: следить за областями и индексировать найденное."""

from __future__ import annotations

import signal
import time
from dataclasses import replace
from pathlib import Path

from ..api import IndexLog
from ..config import Settings
from ..sources import (
    JOURNAL_RETENTION_DAYS,
    IndexingPipeline,
    Journal,
    Watcher,
    default_registry,
    load_config,
)
from .config import known_models
from .report import format_recovery, print_stats
from .service import build_service


class WatchRunner:
    """Watcher областей: скан, индексация, журнал."""

    def __init__(self, config_path: Path, home: Path | None) -> None:
        self._config_path = config_path
        self._settings = Settings.default()
        if home:
            self._settings = replace(self._settings, home=home)

    def run(self) -> int:
        settings = self._settings
        registry = default_registry()
        areas, watch = load_config(
            self._config_path,
            known_formats=registry.known_names(),
            known_models=known_models(settings),
        )
        ingest, _search, store = build_service(
            settings, areas=areas, semantic=watch.semantic
        )
        journal = Journal(settings.home / "journal.jsonl")
        dropped = journal.compact()
        if dropped:
            print(
                f"Журнал: обрезано старых записей: {dropped} "
                f"(старше {JOURNAL_RETENTION_DAYS} дней)"
            )
        recovery = ingest.recover()
        if recovery.removed:
            print(format_recovery(recovery))
        log = IndexLog()
        pipeline = IndexingPipeline(
            ingest, registry, journal=journal, semantic=watch.semantic, log=log
        )

        print(f"Областей: {len(areas)}, интервал скана: {watch.poll_interval} с")
        watcher = Watcher(
            areas,
            watch,
            lambda result: print_stats(pipeline.process(result)),
            journal=journal,
        )
        watcher.start()
        try:
            signal.pause() if hasattr(signal, "pause") else self._wait_forever()
        except KeyboardInterrupt:
            pass
        finally:
            watcher.stop()
            store.close()
        return 0

    @staticmethod
    def _wait_forever() -> None:
        while True:
            time.sleep(3600)


def run_watch(config_path: Path, home: Path | None) -> int:
    """Запустить watcher: скан областей и индексация найденного."""
    return WatchRunner(config_path, home).run()
