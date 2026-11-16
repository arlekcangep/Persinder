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


DEFAULT_NO_CACHE: tuple[str, ...] = ("text", "structure-text", "fb2")
"""Форматы, чей текст по умолчанию **не** хранится в кэше (T5b, D31).

Имена — классы (``text``, ``structure-text``) или конкретные форматы
(``fb2``). Выбраны дешёвые детерминированные способы извлечения: голый
текст, markdown/html/mhtml и fb2 — это разбор разметки, его не жалко
повторить при ``/context`` или подсчёте эмбеддингов. Дорогие форматы
(``pdf``, ``epub``, ``chm``, ``rtf``) остаются в кэше, а части архивов не
кэшировать нельзя: на диске их нет.
"""


@dataclass(frozen=True)
class AreaConfig:
    """Одна область из конфига: папка, роль, форматы и семантика."""

    path: Path
    role: Area
    recursive: bool = True
    formats: tuple[
        str, ...
    ] = ()  # класс (весь класс) или конкретный формат; пусто — не индексировать
    embedding_model: str | None = None
    """Модель эмбеддингов именно для этой папки; ``None`` — глобальная."""
    semantic: bool = True
    """``False`` — не индексировать папку семантически (только морфология)."""
    no_cache: tuple[str, ...] | None = None
    """Имена форматов/классов, чей текст **не** хранится в кэше (T5b, D31).

    ``None`` — в конфиге не задано: наследовать верхний уровень (при прямом
    создании области — кэшировать всё). ``()`` — кэшировать всё явно. Имя
    класса исключает весь класс, имя формата — только его.
    """


def queue_models_for(
    area: AreaConfig, *, semantic_enabled: bool = True
) -> tuple[str, ...] | None:
    """Под какими моделями ставить эмбеддинги документов области.

    ``()`` — семантика выключена (глобально или для папки), очередь не
    пополняется. Имя модели — только эта модель. ``None`` — глобальные модели
    профиля (``ModelState.queue_models``): вызывающий подставит их сам.
    """
    if not semantic_enabled or not area.semantic:
        return ()
    if area.embedding_model:
        return (area.embedding_model,)
    return None


@dataclass(frozen=True)
class WatchConfig:
    """Настройки watcher'а и глобальные переключатели конфига."""

    poll_interval: float = 30.0
    debounce: float = 2.0
    ignore: tuple[str, ...] = ()
    semantic: bool = True
    """``False`` (верхний уровень конфига) — полностью выключить семантику."""
    min_score: float | None = None
    """Порог косинуса семантического поиска по умолчанию (блок ``search``)."""
    no_cache: tuple[str, ...] = DEFAULT_NO_CACHE
    """Верхний уровень: форматы, чей текст не хранится в кэше (T5b, D31).

    Список области **заменяет** этот, а не дополняет: ``no_cache: []``
    возвращает кэш всей области, ``no_cache: [markdown]`` сужает запрет.
    """

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
    """Событие изменения файла внутри области.

    ``size``/``mtime_ns`` — слепок файла на момент скана (None у ``DELETED``:
    файла уже нет).
    """

    kind: ChangeKind
    area: AreaConfig
    rel_path: str
    size: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True)
class ScanResult:
    """Дифф скана области."""

    area: AreaConfig
    events: tuple[ChangeEvent, ...]
