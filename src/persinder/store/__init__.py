"""Адаптер персистентности на SQLite."""

from .models import EmbeddingQueueItem, MorphHit, StoreCounts
from .sqlite import SqliteStore

__all__ = ["SqliteStore", "MorphHit", "EmbeddingQueueItem", "StoreCounts"]
