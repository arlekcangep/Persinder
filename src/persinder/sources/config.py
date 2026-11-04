"""Загрузка конфига областей и watcher'а из YAML.

PyYAML — опциональная зависимость (extra ``config``). Без него модуль падает
с понятной ошибкой при попытке загрузить YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from ..domain import Area
from .models import AreaConfig, WatchConfig


class ConfigError(ValueError):
    """Некорректный конфиг областей/watcher'а."""


def _expand(path_str: str) -> Path:
    return Path(path_str).expanduser()


def load_config(
    path: Path, known_formats: frozenset[str] | None = None
) -> tuple[tuple[AreaConfig, ...], WatchConfig]:
    """Прочитать YAML-конфиг: области + настройки watcher'а.

    Формат:

        areas:
          - path: ~/Obsidian/Personal
            role: personal
            formats: [markdown, text]   # какие экстракторы применять
        watch:
          poll_interval: 30
          debounce: 2
          ignore: ["**/drafts/**"]

    ``known_formats`` — имена доступных экстракторов; если задан, неизвестное
    имя в ``formats`` — ошибка конфига.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"конфиг не найден: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"ошибка YAML в {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("конфиг: ожидается таблица (mapping) верхнего уровня")

    top = cast("dict[str, object]", raw)
    areas_raw = top.get("areas", [])
    if not isinstance(areas_raw, list):
        raise ConfigError("areas: ожидается список")
    areas: list[AreaConfig] = []
    for i, item in enumerate(cast("list[object]", areas_raw)):
        if not isinstance(item, dict):
            raise ConfigError(f"areas[{i}]: ожидается таблица")
        entry = cast("dict[str, object]", item)
        path_raw = entry.get("path")
        role_raw = entry.get("role")
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise ConfigError(f"areas[{i}].path: непустая строка обязательна")
        try:
            role = Area.parse(role_raw)
        except ValueError as exc:
            raise ConfigError(f"areas[{i}].role: {exc}") from exc
        recursive = entry.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ConfigError(f"areas[{i}].recursive: ожидается bool")
        formats_raw = entry.get("formats", [])
        if not isinstance(formats_raw, list) or not all(
            isinstance(f, str) and f.strip() for f in cast("list[object]", formats_raw)
        ):
            raise ConfigError(f"areas[{i}].formats: список непустых строк")
        formats = tuple(cast("list[str]", formats_raw))
        if known_formats is not None:
            unknown = set(formats) - set(known_formats)
            if unknown:
                raise ConfigError(
                    f"areas[{i}].formats: неизвестные форматы {sorted(unknown)} "
                    f"(доступно: {sorted(known_formats)})"
                )
        area = AreaConfig(
            path=_expand(path_raw),
            role=role,
            recursive=recursive,
            formats=tuple(f.strip() for f in formats),
        )
        if not area.path.is_dir():
            raise ConfigError(f"areas[{i}]: каталог не существует: {area.path}")
        areas.append(area)

    watch = WatchConfig.from_dict(top.get("watch"))
    if not areas:
        raise ConfigError("areas: пустой список — нечего индексировать")
    return tuple(areas), watch
