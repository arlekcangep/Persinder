"""Журнал вызовов ingest-апи: намерения, итоги, восстановление.

Append-only JSONL рядом с БД. Каждый вызов оставляет след: намерение *до*
опасного шага (запись/удаление сырого текста) и итог после. Если процесс упал
между ними, ``IngestService.recover`` по журналу убирает брошенный текст.

Повторная индексация не нужна: watcher при следующем скане сам пришлёт файл —
в его собственном журнале по этому файлу нет ответа. Ingest-журналу достаточно
подчистить то, что не доехало.

Жизненный цикл::

    index     → indexed
    duplicate → duplicated
    remove    → removed
    rejected  — терминально (ничего не пишется на диск)

``index``/``duplicate``/``remove`` — незавершённые состояния: восстановление
смотрит на них. История обрезается (``compact``), последняя запись по объекту
хранится всегда.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import cast

from ..domain import utcnow

#: Сколько дней истории вызовов хранить (состояние объекта — всегда).
INGEST_RETENTION_DAYS = 60


class IngestEvent(str, Enum):
    """Тип записи в журнале вызовов ingest."""

    INDEX = "index"  # намерение: пишем сырой текст
    INDEXED = "indexed"  # документ записан
    DUPLICATE = "duplicate"  # намерение: содержимое уже есть, привязываем источник
    DUPLICATED = "duplicated"  # источник привязан
    REJECTED = "rejected"  # отклонено (пустой текст и т.п.)
    REMOVE = "remove"  # намерение: убираем источник
    REMOVED = "removed"  # источник убран, текст удалён


#: Незавершённые переходы: по ним работает восстановление.
PENDING: frozenset[IngestEvent] = frozenset(
    {IngestEvent.INDEX, IngestEvent.DUPLICATE, IngestEvent.REMOVE}
)


@dataclass(frozen=True)
class IngestEntry:
    """Разобранная строка журнала вызовов."""

    op: IngestEvent
    ts: datetime | None = None
    doc_id: str | None = None
    previous: str | None = None
    area: str | None = None
    filename: str | None = None
    detail: str | None = None

    @property
    def key(self) -> str:
        """Ключ объекта: документ (``doc_id``) или источник."""
        if self.doc_id is not None:
            return f"doc:{self.doc_id}"
        return f"src:{self.area}\x00{self.filename}"


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse(line: str) -> IngestEntry | None:
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
    op_raw = entry.get("op")
    if not isinstance(op_raw, str):
        return None
    try:
        op = IngestEvent(op_raw)
    except ValueError:
        return None
    ts: datetime | None = None
    ts_raw = entry.get("ts")
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = None
    return IngestEntry(
        op=op,
        ts=ts,
        doc_id=_as_str(entry.get("doc_id")),
        previous=_as_str(entry.get("previous")),
        area=_as_str(entry.get("area")),
        filename=_as_str(entry.get("file")),
        detail=_as_str(entry.get("detail")),
    )


class IngestJournal:
    """Append-only журнал вызовов ingest + разбор незавершённых."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(
        self,
        op: IngestEvent,
        *,
        doc_id: str | None = None,
        previous: str | None = None,
        area: str | None = None,
        filename: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Дописать вызов в журнал."""
        entry: dict[str, object] = {"ts": utcnow(), "op": op.value}
        if doc_id is not None:
            entry["doc_id"] = doc_id
        if previous is not None:
            entry["previous"] = previous
        if area is not None:
            entry["area"] = area
        if filename is not None:
            entry["file"] = filename
        if detail:
            entry["detail"] = detail
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[IngestEntry]:
        """Прочитать журнал; битые строки пропускаются."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        return [e for e in (_parse(line) for line in lines) if e is not None]

    def unfinished(self) -> list[IngestEntry]:
        """Последняя запись по каждому объекту, если она «незавершённая»."""
        last: dict[str, IngestEntry] = {}
        for entry in self.entries():
            last[entry.key] = entry
        return [entry for entry in last.values() if entry.op in PENDING]

    def compact(self, retention_days: int = INGEST_RETENTION_DAYS) -> int:
        """Обрезать журнал: история за окно + последняя запись по объекту."""
        try:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        parsed: list[tuple[str, IngestEntry]] = []
        for line in raw_lines:
            entry = _parse(line)
            if entry is not None:
                parsed.append((line.strip(), entry))
        last: dict[str, int] = {}
        for index, (_, entry) in enumerate(parsed):
            last[entry.key] = index
        keep: list[str] = []
        for index, (line, entry) in enumerate(parsed):
            fresh = entry.ts is not None and entry.ts >= cutoff
            if fresh or last.get(entry.key) == index:
                keep.append(line)
        removed = len(parsed) - len(keep)
        if removed or len(parsed) != len(raw_lines):
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
            os.replace(tmp, self._path)
        return removed
