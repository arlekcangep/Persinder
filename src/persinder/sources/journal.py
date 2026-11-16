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

Журнал периодически обрезается (``compact``): история переходов за
``JOURNAL_RETENTION_DAYS``, а последнее состояние каждого файла хранится
всегда — иначе давно не менявшийся файл потерял бы состояние.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import cast

from ..domain import utcnow
from .models import AreaConfig, AreaScan

#: Сколько дней истории переходов хранить (состояние файлов — всегда).
JOURNAL_RETENTION_DAYS = 60


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


@dataclass(frozen=True)
class _Entry:
    """Разобранная строка журнала."""

    root: str
    rel: str
    event: JournalEvent
    ts: datetime | None
    size: int | None
    mtime_ns: int | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.root, self.rel)

    @property
    def has_stat(self) -> bool:
        return self.size is not None and self.mtime_ns is not None


def _as_int(value: object) -> int | None:
    """Привести значение из JSON к int (bool — не число)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _parse(line: str) -> _Entry | None:
    """Разобрать строку журнала; None — битая или чужая запись."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    entry = cast("dict[str, object]", raw)
    root = entry.get("root")
    rel = entry.get("file")
    event_raw = entry.get("event")
    if not isinstance(root, str) or not isinstance(rel, str):
        return None
    if not isinstance(event_raw, str):
        return None
    try:
        event = JournalEvent(event_raw)
    except ValueError:
        return None
    ts: datetime | None = None
    ts_raw = entry.get("ts")
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = None
    return _Entry(
        root=root,
        rel=rel,
        event=event,
        ts=ts,
        size=_as_int(entry.get("size")),
        mtime_ns=_as_int(entry.get("mtime_ns")),
    )


def _read(path: Path) -> dict[tuple[str, str], _FileRecord]:
    """Прочитать журнал; битые строки (обрыв при аварии) пропускаются."""
    records: dict[tuple[str, str], _FileRecord] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return records
    for line in lines:
        entry = _parse(line)
        if entry is None:
            continue
        prev = records.get(entry.key)
        records[entry.key] = _FileRecord(
            event=entry.event,
            size=entry.size
            if entry.size is not None
            else (prev.size if prev else None),
            mtime_ns=(
                entry.mtime_ns
                if entry.mtime_ns is not None
                else (prev.mtime_ns if prev else None)
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
        prev = self._records.get((str(area.path), rel_path))
        self._records[(str(area.path), rel_path)] = _FileRecord(
            event=event,
            size=size if size is not None else (prev.size if prev else None),
            mtime_ns=(
                mtime_ns if mtime_ns is not None else (prev.mtime_ns if prev else None)
            ),
        )

    def records(self) -> list[tuple[str, str, JournalEvent]]:
        """Последние состояния файлов: ``(root, rel, event)`` по всем областям."""
        return [(root, rel, rec.event) for (root, rel), rec in self._records.items()]

    def forget(self, keys: Iterable[tuple[str, str]]) -> int:
        """Убрать записи о файлах: следующий скан снова сочтёт их новыми.

        Нужно сверке при старте: журнал говорит «устроено», а в базе нет
        документа/текста (или формат теперь поддержан) — без забывания файл
        никогда не будет прочитан заново, потому что слепок ``(size, mtime)``
        не менялся. Возвращает число забытых записей.
        """
        drop = set(keys)
        if not drop:
            return 0
        try:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            raw_lines = []
        keep: list[str] = []
        removed = 0
        for line in raw_lines:
            entry = _parse(line)
            if entry is not None and entry.key in drop:
                removed += 1
                continue
            keep.append(line)
        for key in drop:
            self._records.pop(key, None)
        if removed:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
            os.replace(tmp, self._path)
        return removed

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

    def compact(self, retention_days: int = JOURNAL_RETENTION_DAYS) -> int:
        """Обрезать журнал: история переходов + последнее состояние файлов.

        Сохраняются все записи за ``retention_days`` и — независимо от
        возраста — последняя запись по каждому файлу и последняя запись со
        слепком ``(size, mtime_ns)``: без них давно не менявшийся файл
        потерял бы состояние и переиндексировался. Битые строки отбрасываются.
        Возвращает число удалённых записей.
        """
        try:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        parsed: list[tuple[str, _Entry]] = []
        for line in raw_lines:
            entry = _parse(line)
            if entry is not None:
                parsed.append((line.strip(), entry))
        last: dict[tuple[str, str], int] = {}
        last_stat: dict[tuple[str, str], int] = {}
        for index, (_, entry) in enumerate(parsed):
            last[entry.key] = index
            if entry.has_stat:
                last_stat[entry.key] = index
        keep_indexes: set[int] = set(last.values())
        for key, index in last_stat.items():
            # Удалённому файлу слепок не нужен: состояние — «нет файла».
            if parsed[last[key]][1].event is not JournalEvent.DELETED:
                keep_indexes.add(index)
        keep: list[str] = []
        for index, (line, entry) in enumerate(parsed):
            fresh = entry.ts is not None and entry.ts >= cutoff
            if fresh or index in keep_indexes:
                keep.append(line)
        removed = len(parsed) - len(keep)
        if removed or len(parsed) != len(raw_lines):
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
            os.replace(tmp, self._path)
        return removed
