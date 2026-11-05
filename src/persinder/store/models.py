"""Модели слоя персистентности.

Строки/записи и результаты запросов SQLite. Наружу за пределы адаптера не
выносятся, кроме как через порты (см. ``interfaces.MetaStore``).
"""

from __future__ import annotations

from typing import TypedDict


class MorphHit(TypedDict):
    """Строка результата морфологического поиска."""

    doc_id: str
    chunk_index: int
    score: float


class EmbeddingQueueItem(TypedDict):
    """Строка очереди на эмбеддинги."""

    id: int
    doc_id: str
    chunk_index: int
    area: str
    source_path: str  # пусто — виртуальный элемент (нет файла на диске)


class StoreCounts(TypedDict):
    """Счётчики содержимого метастора."""

    documents: int
    sources: int
    chunks: int
    pending_embeddings: int
