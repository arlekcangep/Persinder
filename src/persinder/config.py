"""Настройки и каталоги Persinder.

Пути к большим папкам (кэш текста, виртуальные документы) можно вынести на
другой диск через ``<home>/settings.json``: это **машино-локальные** настройки,
в отличие от переносимого YAML-профиля областей (``persinder.yaml``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

#: Файл машино-локальных настроек внутри ``home``.
SETTINGS_FILE = "settings.json"

#: Известные ключи ``settings.json`` (неизвестный — ошибка: опечатка иначе
#: молча оставит путь по умолчанию и забьёт диск не тем каталогом).
_KNOWN_KEYS = frozenset({"text_dir", "virtual_dir"})

#: Каталог кэша сырого текста внутри ``home`` (D26). Историческое имя
#: ``pending_texts`` больше не используется — это не очередь, а кэш.
TEXT_DIR_NAME = "text_cache"

#: Каталог данных, которых нет на диске (web, вставленный текст).
VIRTUAL_DIR_NAME = "virtual"


class SettingsError(ValueError):
    """Некорректный ``settings.json``."""


def default_home() -> Path:
    """Каталог приложения по умолчанию: ``~/.persinder``."""
    return Path.home() / ".persinder"


@dataclass(frozen=True)
class Settings:
    """Настройки приложения.

    ``text_root``/``virtual_root`` — переопределения путей из
    ``settings.json`` (``None`` — подкаталог ``home``). В файле пути могут
    быть абсолютными, с ``~`` или относительными — последние считаются от
    ``home`` (см. ``load``).
    """

    home: Path
    chunk_size: int = 1200
    chunk_overlap: int = 200
    host: str = "127.0.0.1"
    port: int = 8756
    text_root: Path | None = None
    virtual_root: Path | None = None

    @property
    def settings_path(self) -> Path:
        """Файл машино-локальных настроек (``<home>/settings.json``)."""
        return self.home / SETTINGS_FILE

    @property
    def db_path(self) -> Path:
        return self.home / "persinder.db"

    @property
    def text_dir(self) -> Path:
        """Кэш сырого текста (``<home>/text_cache`` по умолчанию, D26)."""
        return (
            self.text_root if self.text_root is not None else self.home / TEXT_DIR_NAME
        )

    @property
    def virtual_dir(self) -> Path:
        """Папка данных, которых нет на диске (web, вставленный текст).

        Сюда ingest кладёт их сайдкары (``<doc_id>.pdr``); сам текст живёт в
        ``text_dir``.
        """
        return (
            self.virtual_root
            if self.virtual_root is not None
            else self.home / VIRTUAL_DIR_NAME
        )

    @property
    def models_dir(self) -> Path:
        """Каталог моделей эмбеддингов: ``<home>/models`` (внутри APP-каталога).

        Каждая модель — своя подпапка ``<models_dir>/<ключ>/`` (веса +
        токенизатор). Модели всегда лежат в подкаталоге ``home`` — отдельного
        глобального каталога/переменной нет.
        """
        return self.home / "models"

    @property
    def models_state_path(self) -> Path:
        """Файл активных моделей профиля (``embedding``/``search``, D24)."""
        return self.home / "models.json"

    @property
    def ann_dir(self) -> Path:
        """Каталог ANN-индексов USearch: ``<home>/ann/<модель>.usearch``.

        Индекс — локальное производное (пересобирается из таблицы ``embeddings``,
        ``persinder ann rebuild``), живёт вне синка, на mmap — без резидента в RAM.
        """
        return self.home / "ann"

    def ensure(self) -> Settings:
        """Создать каталоги, если их нет."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)
        self.virtual_dir.mkdir(parents=True, exist_ok=True)
        self.ann_dir.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> Settings:
        return cls.load()

    @classmethod
    def load(cls, home: Path | None = None) -> Settings:
        """Настройки из ``<home>/settings.json``; файла может не быть.

        Пути в файле: ``~`` раскрывается, относительные считаются от ``home``
        (не от CWD). Разрешение делается один раз здесь, чтобы все команды
        видели одинаковые каталоги.
        """
        root = Path(home).expanduser() if home is not None else default_home()
        data = _read_settings(root / SETTINGS_FILE)
        return cls(
            home=root,
            text_root=_path_setting(data, "text_dir", root),
            virtual_root=_path_setting(data, "virtual_dir", root),
        )


def _read_settings(path: Path) -> dict[str, object]:
    """Прочитать ``settings.json`` (``{}``, если файла нет)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsError(f"не читается {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"ошибка JSON в {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: ожидается объект JSON")
    mapping = cast("dict[str, object]", data)
    unknown = set(mapping) - _KNOWN_KEYS
    if unknown:
        raise SettingsError(
            f"{path}: неизвестные ключи {sorted(unknown)} "
            f"(поддерживаются: {sorted(_KNOWN_KEYS)})"
        )
    return mapping


def _path_setting(data: Mapping[str, object], key: str, home: Path) -> Path | None:
    """Путь из ``settings.json``: ``~`` раскрыт, относительный — от ``home``."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{key}: ожидается непустая строка с путём")
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else home / path
