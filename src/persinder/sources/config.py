"""Загрузка конфига областей и watcher'а из YAML.

PyYAML — опциональная зависимость (extra ``config``). Без него модуль падает
с понятной ошибкой при попытке загрузить YAML.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import yaml

from ..domain import Area
from .models import AreaConfig, WatchConfig


class ConfigError(ValueError):
    """Некорректный конфиг областей/watcher'а."""


def _expand(path_str: str) -> Path:
    """Путь из конфига: ``~`` раскрывается, относительный — от CWD конфига.

    Относительные пути в конфиге удобны для переносимых профилей
    (git bash на Windows, флешка): они считаются от каталога, где лежит
    конфиг, — watcher резолвит их сам (см. ``load_config``).
    """
    return Path(path_str).expanduser()


def load_config(
    path: Path,
    known_formats: frozenset[str] | None = None,
    known_models: frozenset[str] | None = None,
) -> tuple[tuple[AreaConfig, ...], WatchConfig]:
    """Прочитать YAML-конфиг: области + настройки watcher'а.

    Формат:

        semantic: true            # false — полностью выключить семантику
        areas:
          - path: ~/Obsidian/Personal
            role: personal
            formats: [text, structure-text]   # классы / конкретные форматы
            embedding_model: e5-small-int8-v1 # модель именно для папки
            semantic: true                    # false — папка без эмбеддингов
        watch:
          poll_interval: 30
          debounce: 2
          ignore: ["**/drafts/**"]

    ``formats`` — имена **классов** (``text``, ``structure-text``,
    ``pub-formats``, ``archive``…) либо **конкретных форматов** (``markdown``,
    ``html``, ``pdf``…). Имя класса включает весь класс, имя формата — только
    его (``[structure-text]`` индексирует md+html+mhtml, ``[markdown]`` —
    только markdown).

    Семантика: верхний ``semantic: false`` выключает эмбеддинги целиком;
    ``semantic: false`` у области — только для неё. ``embedding_model``
    выбирает модель из установленных для конкретной папки (иначе — глобальная
    из ``models.json``).

    ``known_formats`` — доступные имена форматов/классов; ``known_models`` —
    установленные модели (ключи + заглушка). Если заданы, неизвестное имя —
    ошибка конфига.

    Относительные пути ``path`` считаются от каталога, где лежит конфиг, —
    профиль переносим между машинами.
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
    semantic_raw = top.get("semantic", True)
    if not isinstance(semantic_raw, bool):
        raise ConfigError("semantic: ожидается bool")
    semantic_enabled = semantic_raw
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
        formats = tuple(f.strip() for f in cast("list[str]", formats_raw))
        if known_formats is not None:
            unknown = set(formats) - set(known_formats)
            if unknown:
                raise ConfigError(
                    f"areas[{i}].formats: неизвестные форматы {sorted(unknown)} "
                    f"(доступно: {sorted(known_formats)})"
                )
        # Семантика области и модель именно для неё (None — глобальная).
        area_semantic_raw = entry.get("semantic")
        if area_semantic_raw is not None and not isinstance(area_semantic_raw, bool):
            raise ConfigError(f"areas[{i}].semantic: ожидается bool")
        area_semantic = True if area_semantic_raw is None else area_semantic_raw
        model_raw = entry.get("embedding_model")
        if model_raw is not None and (
            not isinstance(model_raw, str) or not model_raw.strip()
        ):
            raise ConfigError(f"areas[{i}].embedding_model: непустая строка или null")
        embedding_model = model_raw.strip() if isinstance(model_raw, str) else None
        if (
            embedding_model
            and known_models is not None
            and semantic_enabled
            and embedding_model not in known_models
        ):
            raise ConfigError(
                f"areas[{i}].embedding_model: модель не установлена "
                f"{embedding_model!r} (доступно: {sorted(known_models)})"
            )
        # Имена не нормализуются: класс включает весь класс, конкретный формат —
        # только себя (разбор — в ExtractorRegistry._is_enabled).
        area = AreaConfig(
            path=_expand(path_raw),
            role=role,
            recursive=recursive,
            formats=formats,
            embedding_model=embedding_model,
            semantic=area_semantic,
        )
        if not area.path.is_absolute():
            # относительный путь — от каталога конфига, не от CWD процесса
            area = AreaConfig(
                path=(path.parent / area.path).resolve(),
                role=role,
                recursive=recursive,
                formats=area.formats,
                embedding_model=area.embedding_model,
                semantic=area.semantic,
            )
        if not area.path.is_dir():
            raise ConfigError(f"areas[{i}]: каталог не существует: {area.path}")
        areas.append(area)

    watch = replace(WatchConfig.from_dict(top.get("watch")), semantic=semantic_enabled)
    if not areas:
        raise ConfigError("areas: пустой список — нечего индексировать")
    return tuple(areas), watch
