"""Последние события индексации (лог конвейера) — для консоли и web UI.

Принцип: одно место формирует и вывод, и передачу. ``IndexLog`` — это
единственный выход событий: печатает в консоль (flush, чтобы видеть сразу)
и хранит колесо обозрения событий для чтения по API.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from ..domain import utcnow


@dataclass
class LogEvent:
    """Одно событие в логе."""

    id: int
    ts: str = field(default_factory=lambda: utcnow())
    level: str = "info"
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level,
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

    def _log_event(self, level: str, message: str) -> LogEvent:
        with self._lock:
            self._next_id += 1
            ev = LogEvent(id=self._next_id, level=level, message=message)
            self._events.append(ev)
            if len(self._events) > self._max:
                self._events.popleft()
            if self._console:
                self._print(self._format(ev), flush=True)
            return ev

    def add(self, level: str = "info", message: str = "") -> None:
        """Дописать событие уровня ``info``."""
        self._log_event(level, message)

    def info(self, message: str) -> None:
        self._log_event("info", message)

    def warning(self, message: str) -> None:
        self._log_event("warning", message)

    def error(self, message: str) -> None:
        self._log_event("error", message)

    @staticmethod
    def _format(ev: LogEvent) -> str:
        """Оформление одной строки лога."""
        time = ev.ts[11:19] if len(ev.ts) >= 19 else ev.ts
        return f"[{ev.level.upper()}] {time} {ev.message}"

    # --- чтение ---

    def entries(self, limit: int = 200, after_id: int | None = None) -> list[LogEvent]:
        """Последние события; ``after_id`` — только новее указанного (для
        incremental-просмотра)."""
        with self._lock:
            events = list(self._events)
        if after_id is not None:
            events = [ev for ev in events if ev.id > after_id]
        return events[-limit:]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id

    # --- тестам ---

    def enable_console(self, enable: bool = True) -> None:
        self._console = enable
