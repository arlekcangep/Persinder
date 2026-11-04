"""Внутренние модели модуля `sources` (watcher областей)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from ..domain import Area


class ChangeKind(str, Enum):
    """Тип изменения файла в области."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class AreaConfig:
    """Одна область из конфига: папка и её роль."""

    path: Path
    role: Area
    recursive: bool = True


@dataclass(frozen=True)
class WatchConfig:
    """Настройки watcher'а."""

    poll_interval: float = 30.0
    debounce: float = 2.0
    ignore: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: object) -> WatchConfig:
        """Разобрать блок ``watch`` из YAML; при ошибке типов — ValueError."""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("watch: ожидается таблица (mapping)")
        data = cast("dict[str, object]", raw)
        interval = data.get("poll_interval", cls.poll_interval)
        debounce = data.get("debounce", cls.debounce)
        ignore = data.get("ignore", [])
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("watch.poll_interval: положительное число секунд")
        if not isinstance(debounce, (int, float)) or debounce < 0:
            raise ValueError("watch.debounce: неотрицательное число секунд")
        if not isinstance(ignore, list) or not all(
            isinstance(i, str) for i in cast("list[object]", ignore)
        ):
            raise ValueError("watch.ignore: список строк")
        return cls(
            poll_interval=float(interval),
            debounce=float(debounce),
            ignore=tuple(cast("list[str]", ignore)),
        )


@dataclass(frozen=True)
class AreaScan:
    """Снапшот области: относительный путь → (size, mtime_ns)."""

    area: AreaConfig
    files: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class ChangeEvent:
    """Событие изменения файла внутри области."""

    kind: ChangeKind
    area: AreaConfig
    rel_path: str


@dataclass(frozen=True)
class ScanResult:
    """Дифф скана области."""

    area: AreaConfig
    events: tuple[ChangeEvent, ...]
