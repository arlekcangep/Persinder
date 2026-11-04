"""Журнал наблюдения: статусы файлов по ходу конвейера.

Журнал — единственный источник состояния watcher'а (ADR D21): последняя
запись по файлу определяет, что с ним уже сделано, а дифф следующего скана
строится по «устроенным» файлам из журнала. Формат — JSONL: append-only,
одна строка — один переход; файл лежит рядом с БД и читается глазами/grep'ом.

Жизненный цикл файла::

    discovered → sent → indexed | duplicate | rejected | error
                     ↘ skipped (формат не обслуживается / нет текста)
    deleted

``discovered``/``sent`` — незавершённые состояния: если процесс упал между
сканом и ingest, файл при следующем запуске обрабатывается заново (в дифф
такие файлы не попадают). ``deleted`` означает, что файла больше нет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from ..domain import utcnow
from .models import AreaConfig, AreaScan


class JournalEvent(str, Enum):
    """Переход в жизненном цикле файла."""

    DISCOVERED = "discovered"  # скан нашёл файл (created/modified)
    SENT = "sent"  # текст передан в ingest
    INDEXED = "indexed"  # ingest создал документ
    DUPLICATE = "duplicate"  # такой документ уже есть (содержимое совпало)
    REJECTED = "rejected"  # ingest отклонил документ
    SKIPPED = "skipped"  # формат не обслуживается / нет текста
    ERROR = "error"  # ошибка экстракции или ingest
    DELETED = "deleted"  # файл исчез, источник убран из индекса


# Обработанные файлы: участвуют в диффе сканера («на диске и сделано»).
SETTLED: frozenset[JournalEvent] = frozenset(
    {
        JournalEvent.INDEXED,
        JournalEvent.DUPLICATE,
        JournalEvent.REJECTED,
        JournalEvent.SKIPPED,
        JournalEvent.ERROR,
    }
)


@dataclass(frozen=True)
class _FileRecord:
    """Последнее известное состояние файла: переход + слепок (size, mtime)."""

    event: JournalEvent
    size: int | None = None
    mtime_ns: int | None = None


def _as_int(value: object) -> int | None:
    """Привести значение из JSON к int (bool — не число)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _read(path: Path) -> dict[tuple[str, str], _FileRecord]:
    """Прочитать журнал; битые строки (обрыв при аварии) пропускаются."""
    records: dict[tuple[str, str], _FileRecord] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, object]", raw)
        root = entry.get("root")
        rel = entry.get("file")
        event_raw = entry.get("event")
        if not isinstance(root, str) or not isinstance(rel, str):
            continue
        if not isinstance(event_raw, str):
            continue
        try:
            event = JournalEvent(event_raw)
        except ValueError:
            continue
        key = (root, rel)
        prev = records.get(key)
        size = _as_int(entry.get("size"))
        mtime_ns = _as_int(entry.get("mtime_ns"))
        records[key] = _FileRecord(
            event=event,
            size=size if size is not None else (prev.size if prev else None),
            mtime_ns=(
                mtime_ns if mtime_ns is not None else (prev.mtime_ns if prev else None)
            ),
        )
    return records


class Journal:
    """Append-only JSONL-журнал + индекс состояний файлов в памяти."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[tuple[str, str], _FileRecord] = _read(path)

    def record(
        self,
        area: AreaConfig,
        rel_path: str,
        event: JournalEvent,
        *,
        size: int | None = None,
        mtime_ns: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Дописать переход в журнал и обновить состояние в памяти."""
        entry: dict[str, object] = {
            "ts": utcnow(),
            "area": area.role.value,
            "root": str(area.path),
            "file": rel_path,
            "event": event.value,
        }
        if size is not None:
            entry["size"] = size
        if mtime_ns is not None:
            entry["mtime_ns"] = mtime_ns
        if detail:
            entry["detail"] = detail
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        key = (str(area.path), rel_path)
        prev = self._records.get(key)
        self._records[key] = _FileRecord(
            event=event,
            size=size if size is not None else (prev.size if prev else None),
            mtime_ns=(
                mtime_ns if mtime_ns is not None else (prev.mtime_ns if prev else None)
            ),
        )

    def snapshot(self, areas: tuple[AreaConfig, ...]) -> dict[Path, AreaScan]:
        """Состояние «устроенных» файлов областей — предыдущее для диффа."""
        by_root: dict[str, dict[str, tuple[int, int]]] = {}
        for (root, rel), rec in self._records.items():
            if rec.event not in SETTLED:
                continue
            if rec.size is None or rec.mtime_ns is None:
                continue
            by_root.setdefault(root, {})[rel] = (rec.size, rec.mtime_ns)
        result: dict[Path, AreaScan] = {}
        for area in areas:
            files = by_root.get(str(area.path))
            if files:
                result[area.path] = AreaScan(area=area, files=dict(files))
        return result
