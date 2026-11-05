"""Доменные модели (ядро).

Чистые типы без зависимостей от транспорта и хранилищ: сущности и значения,
которые описывают предметную область Persinder.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Area(str, Enum):
    """Тип папки-источника (роль области)."""

    PERSONAL = "personal"
    FOREIGN = "foreign"
    WEB = "web"

    @classmethod
    def parse(cls, value: object) -> Area:
        """Разобрать строку в Area, иначе ValueError."""
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(a.value for a in cls)
            raise ValueError(
                f"неизвестная область: {value!r} (допустимо: {allowed})"
            ) from exc


@dataclass(frozen=True)
class Document:
    """Документ как доменная сущность."""

    id: str
    content_hash: str
    title: str | None
    char_count: int
    text_key: str
    created_at: str


@dataclass(frozen=True)
class Chunk:
    """Чанк документа с уже стеммированным текстом."""

    index: int
    char_start: int
    char_end: int
    stemmed: str


@dataclass(frozen=True)
class EmbeddingJob:
    """Задача на подсчёт эмбеддингов для чанка."""

    doc_id: str
    chunk_index: int
    area: str
    created_at: str
    source_path: str = ""

    @property
    def key(self) -> str:
        """Ключ задачи в очереди: документ + источник."""
        return f"{self.doc_id}\x00{self.source_path}"
