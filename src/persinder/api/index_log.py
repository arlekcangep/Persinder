"""Последние события индексации (лог конвейера) — для консоли и web UI.

Принцип: одно место формирует и вывод, и передачу. ``IndexLog`` — это
единственный выход событий: печатает в консоль (flush, чтобы видеть сразу)
и хранит колесо обозрения событий для чтения по API.

У события две независимые характеристики: **уровень** (``info``/``warning``/
``error`` — важность) и **вид** (``kind``: индексация файлов, эмбеддинги,
служебные сообщения — откуда событие). Вид нужен фильтру в web-журнале:
например, посмотреть только создание сайдкаров или только ошибки файлов.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from ..domain import utcnow

#: Вид события: файлы области (скан, экстракция, ingest).
KIND_INDEX = "index"
#: Вид события: эмбеддинги и сайдкары (фоновый воркер).
KIND_EMBED = "embed"
#: Вид события: служебные сообщения (по умолчанию).
KIND_SYSTEM = "system"

#: Все допустимые виды событий (для валидации фильтра).
LOG_KINDS: tuple[str, ...] = (KIND_INDEX, KIND_EMBED, KIND_SYSTEM)


@dataclass
class LogEvent:
    """Одно событие в логе."""

    id: int
    ts: str = field(default_factory=lambda: utcnow())
    level: str = "info"
    kind: str = KIND_SYSTEM
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level,
            "kind": self.kind,
            "message": self.message,
        }


class IndexLog:
    """Колесо обозрения событий индексации.

    Потокобезопасно: события записываются под локалом, читается без блока.
    При переполнении старые (младшие ``id``) отбрасываются.

    Пример вывода в консоль:

        [INFO] 12:45:10 ✓ заметка.md (2 документа, 5 чанков)
        [ERROR] 12:45:11 ✗ фото.jpg (неизвестный формат)
    """

    def __init__(self, max_events: int = 500) -> None:
        self._max = max_events
        self._events: deque[LogEvent] = deque()
        self._lock = threading.Lock()
        self._next_id = 0
        self._print = print  # чтобы в тестах отключать консоль
        self._console = True

    # --- запись ---

    def _log_event(self, level: str, message: str, kind: str) -> LogEvent:
        with self._lock:
            self._next_id += 1
            ev = LogEvent(id=self._next_id, level=level, kind=kind, message=message)
            self._events.append(ev)
            if len(self._events) > self._max:
                self._events.popleft()
            if self._console:
                self._print(self._format(ev), flush=True)
            return ev

    def add(
        self, level: str = "info", message: str = "", kind: str = KIND_SYSTEM
    ) -> None:
        """Дописать событие с явным уровнем и видом."""
        self._log_event(level, message, kind)

    def info(self, message: str, kind: str = KIND_SYSTEM) -> None:
        self._log_event("info", message, kind)

    def warning(self, message: str, kind: str = KIND_SYSTEM) -> None:
        self._log_event("warning", message, kind)

    def error(self, message: str, kind: str = KIND_SYSTEM) -> None:
        self._log_event("error", message, kind)

    @staticmethod
    def _format(ev: LogEvent) -> str:
        """Оформление одной строки лога."""
        time = ev.ts[11:19] if len(ev.ts) >= 19 else ev.ts
        return f"[{ev.level.upper()}] {time} {ev.message}"

    # --- чтение ---

    def entries(
        self,
        limit: int = 200,
        after_id: int | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[LogEvent]:
        """Последние события с фильтрами.

        ``after_id`` — только новее указанного (для incremental-просмотра);
        ``kind`` — только этот вид (``index``/``embed``/``system``);
        ``since`` — не раньше границы (включительно); ``until`` — раньше
        границы (исключительно). Границы — aware ``datetime`` в UTC.
        """
        with self._lock:
            events = list(self._events)
        if after_id is not None:
            events = [ev for ev in events if ev.id > after_id]
        if kind:
            events = [ev for ev in events if ev.kind == kind]
        if since is not None:
            events = [ev for ev in events if _event_time(ev) >= since]
        if until is not None:
            events = [ev for ev in events if _event_time(ev) < until]
        return events[-limit:]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id

    # --- тестам ---

    def enable_console(self, enable: bool = True) -> None:
        self._console = enable


def parse_time_bound(raw: str, *, end_of_day: bool = False) -> datetime | None:
    """Граница фильтра по времени из строки запроса.

    Принимает дату (``2026-11-10``) или ISO-дата-время (``2026-11-10T12:30``,
    ``...Z``, со смещением). Дата без времени — начало дня UTC, а при
    ``end_of_day`` — начало *следующего* дня (чтобы ``to=2026-11-10`` включал
    весь день). Пустая строка — границы нет (``None``). Некорректная — ``ValueError``.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"некорректная дата: {raw!r}") from exc
    date_only = "T" not in text and " " not in text
    if date_only:
        parsed = datetime.combine(parsed.date(), time.min, tzinfo=timezone.utc)
        if end_of_day:
            parsed += timedelta(days=1)
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _event_time(ev: LogEvent) -> datetime:
    """Метка события как aware ``datetime`` (битая метка — «очень давно»)."""
    try:
        parsed = datetime.fromisoformat(ev.ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
