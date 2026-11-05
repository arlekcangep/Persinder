"""Сервис приёма: морфоиндекс + очередь эмбеддингов + журнал вызовов."""

from .journal import INGEST_RETENTION_DAYS, IngestEntry, IngestEvent, IngestJournal
from .models import (
    IngestRequest,
    IngestResult,
    RecoveryStats,
    RemoveResult,
)
from .service import IngestService, chunk_text

__all__ = [
    "IngestService",
    "chunk_text",
    "IngestRequest",
    "IngestResult",
    "RemoveResult",
    "RecoveryStats",
    "IngestJournal",
    "IngestEvent",
    "IngestEntry",
    "INGEST_RETENTION_DAYS",
]
