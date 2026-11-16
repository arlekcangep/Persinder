"""Polling-сканер областей: снапшот файловой системы и дифф.

Источник правды индексации (ADR D20): периодический обход, сравнение
``(size, mtime_ns)``. Работает на любой ФС, включая сетевые шары и vboxsf,
где нативные события (inotify / ReadDirectoryChangesW) ненадёжны.
"""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path, PurePosixPath

from .models import (
    AreaConfig,
    AreaScan,
    ChangeEvent,
    ChangeKind,
    ScanResult,
    WatchConfig,
)

# Служебное и производное — не индексируется (см. §6.4 архитектуры).
BUILTIN_IGNORES: tuple[str, ...] = (
    ".obsidian",
    ".persinder",
    ".stfolder",
    "*.sync-conflict-*",
    ".syncthing.*.tmp",
)


def _is_ignored(rel_parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    """Совпадает ли любой компонент пути (или весь путь) с паттерном."""
    name = rel_parts[-1]
    for pattern in patterns:
        if any(fnmatch.fnmatch(part, pattern) for part in rel_parts):
            return True
        if fnmatch.fnmatch("/".join(rel_parts), pattern):
            return True
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


class AreaScanner:
    """Обход одной области с учётом ignore-паттернов и debounce."""

    def __init__(self, config: WatchConfig) -> None:
        self._config = config
        self._patterns = BUILTIN_IGNORES + config.ignore

    def snapshot(self, area: AreaConfig) -> AreaScan:
        """Обойти область и вернуть снапшот ``rel_path -> (size, mtime_ns)``."""
        files: dict[str, tuple[int, int]] = {}
        root = area.path
        dirs: list[Path] = [root]
        while dirs:
            current = dirs.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                rel_parts = (Path(current, entry.name).relative_to(root)).parts
                if _is_ignored(rel_parts, self._patterns):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if area.recursive:
                        dirs.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                files[PurePosixPath(*rel_parts).as_posix()] = (
                    stat.st_size,
                    stat.st_mtime_ns,
                )
        return AreaScan(area=area, files=files)

    def diff(self, before: AreaScan | None, after: AreaScan) -> tuple[ChangeEvent, ...]:
        """Сравнить снапшоты, выдав события created/modified/deleted.

        Файлы, «молодые» debounce-интервала, считаются ещё изменяющимися:
        их состояние не попадает в события (будет поймано следующим сканом).
        """
        now_ns = time.time_ns()
        quiet_ns = int(self._config.debounce * 1_000_000_000)
        old = before.files if before else {}
        events: list[ChangeEvent] = []

        for rel, stat in after.files.items():
            if now_ns - stat[1] < quiet_ns:
                continue  # ещё пишется — подождём следующего скана
            prev = old.get(rel)
            if prev is None:
                events.append(
                    ChangeEvent(
                        ChangeKind.CREATED,
                        after.area,
                        rel,
                        size=stat[0],
                        mtime_ns=stat[1],
                    )
                )
            elif prev != stat:
                events.append(
                    ChangeEvent(
                        ChangeKind.MODIFIED,
                        after.area,
                        rel,
                        size=stat[0],
                        mtime_ns=stat[1],
                    )
                )

        for rel in old.keys() - after.files.keys():
            events.append(ChangeEvent(ChangeKind.DELETED, after.area, rel))

        return tuple(events)

    def name_diff(
        self,
        catalog: AreaScan,
        settled: set[str],
        known: set[str] | None = None,
    ) -> tuple[ChangeEvent, ...]:
        """Сравнить каталог области с журналом **по именам файлов**.

        Стартовый проход идёт от файлов, а не от журнала (D25): на диске
        видно, что появилось и что исчезло, — без чтения файлов и без
        хэшей. ``settled`` — имена, которые журнал считает устроенными
        (новые из них — «появившиеся»); ``known`` — все имена, о которых
        журнал вообще знает (исчезли те, которых нет на диске; по умолчанию
        совпадает с ``settled``).

        Файлы, тронутые только что (моложе debounce), пропускаются: их ещё
        пишут, следующий проход сканера их подхватит.
        """
        now_ns = time.time_ns()
        quiet_ns = int(self._config.debounce * 1_000_000_000)
        seen = settled if known is None else known
        events: list[ChangeEvent] = []
        for rel, (size, mtime_ns) in catalog.files.items():
            if rel in settled:
                continue
            if now_ns - mtime_ns < quiet_ns:
                continue  # ещё пишется — поймает обычный скан
            events.append(
                ChangeEvent(
                    ChangeKind.CREATED,
                    catalog.area,
                    rel,
                    size=size,
                    mtime_ns=mtime_ns,
                )
            )
        for rel in sorted(seen - catalog.files.keys()):
            events.append(ChangeEvent(ChangeKind.DELETED, catalog.area, rel))
        return tuple(events)

    def scan(
        self, area: AreaConfig, previous: AreaScan | None = None
    ) -> tuple[AreaScan, tuple[ChangeEvent, ...]]:
        """Скан области: новый снапшот + дифф с предыдущим."""
        after = self.snapshot(area)
        return after, self.diff(previous, after)


def scan_all(
    areas: tuple[AreaConfig, ...],
    scanner: AreaScanner,
    previous: dict[Path, AreaScan] | None = None,
) -> tuple[dict[Path, AreaScan], list[ScanResult]]:
    """Скан всех областей; вернуть новые снапшоты и результаты с событиями.

    Ключ снапшота — путь области: папок с одной ролью может быть несколько.
    """
    prev_map = previous or {}
    snaps: dict[Path, AreaScan] = {}
    results: list[ScanResult] = []
    for area in areas:
        snap, events = scanner.scan(area, prev_map.get(area.path))
        snaps[area.path] = snap
        results.append(ScanResult(area=area, events=events))
    return snaps, results
