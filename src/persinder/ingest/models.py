"""Внутренние модели сервиса приёма.

Вход и выход сценария ``ingest``: не зависят от HTTP и от БД.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import Area


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
