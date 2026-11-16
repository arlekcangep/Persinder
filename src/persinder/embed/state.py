"""Состояние активных моделей на профиль (``home/models.json``).

Два независимых хендла (D24):

- ``embedding`` — какой моделью считаем новые/изменённые файлы;
- ``search`` — какой моделью ищем (query-эмбеддинг + чьи сайдкары читать).

При установке новой модели ``embedding`` переключается сразу (миграция
стартует в фоне), а ``search`` — только по явному ``model use``. Пока они
разные, новые файлы индексируются обеими моделями (см. ``queue_models``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .registry import PLACEHOLDER


@dataclass(frozen=True)
class ModelState:
    """Активные модели профиля."""

    embedding: str = PLACEHOLDER
    search: str = PLACEHOLDER

    def queue_models(self) -> tuple[str, ...]:
        """Модели, под которыми индексируются новые/изменённые файлы.

        Пока поиск и эмбеддинг на разных моделях (миграция), файл считается
        обеими — поиск на старой модели не теряет свежие документы.
        """
        seen: list[str] = []
        for model in (self.embedding, self.search):
            if model not in seen:
                seen.append(model)
        return tuple(seen)


def load(path: str | Path) -> ModelState:
    """Прочитать состояние; нет файла/битый — значения по умолчанию."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return ModelState(
            embedding=str(data.get("embedding", PLACEHOLDER)),
            search=str(data.get("search", PLACEHOLDER)),
        )
    except (OSError, ValueError):
        return ModelState()


def save(state: ModelState, path: str | Path) -> None:
    """Записать состояние атомарно (tmp → rename)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"embedding": state.embedding, "search": state.search},
        ensure_ascii=False,
        indent=2,
    )
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, dest)
