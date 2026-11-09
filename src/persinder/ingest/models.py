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
    source_path: str | None = None
    """Абсолютный путь источника на диске.

    Есть — файл реальный, сайдкар пишется рядом с ним (``.persinder/``).
    Нет — данных на диске нет (web, вставленный текст): их эмбеддинги и сам
    текст живут в отдельной папке (``Settings.virtual_dir``).
    """
    extractor: str | None = None
    """Имя экстрактора, который извлёк этот текст (для атрибуции в БД)."""


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
class RemoveResult:
    """Итог удаления источника: был ли он и вычищен ли документ целиком."""

    found: bool
    document_id: str | None = None


@dataclass(frozen=True)
class RecoveryStats:
    """Итог восстановления по журналу: сколько брошенных текстов убрано."""

    checked: int = 0
    removed: int = 0
