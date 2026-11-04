"""Доменные типы Persinder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TypedDict


def utcnow() -> str:
    """Текущее время в UTC в формате ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
class IngestRequest:
    """Вход сервиса приёма: текст и метаданные."""

    filename: str
    area: Area
    text: str
    title: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """Результат приёма документа."""

    INDEXED = "indexed"
    ALREADY = "already_indexed"
    REJECTED = "rejected"

    status: str
    document_id: str | None = None
    chunks: int = 0
    message: str | None = None


@dataclass(frozen=True)
class Document:
    """Запись документа в метасторе."""

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


class StoreCounts(TypedDict):
    """Счётчики содержимого метастора."""

    documents: int
    sources: int
    chunks: int
    pending_embeddings: int
