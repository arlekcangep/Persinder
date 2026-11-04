"""Внутренние модели сервиса поиска."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchQuery:
    """Запрос поиска."""

    text: str
    limit: int = 20


@dataclass(frozen=True)
class SearchHit:
    """Одно попадание поиска."""

    document_id: str
    chunk_index: int
    score: float


@dataclass(frozen=True)
class SearchResult:
    """Результат поиска."""

    query: str
    hits: tuple[SearchHit, ...] = ()
