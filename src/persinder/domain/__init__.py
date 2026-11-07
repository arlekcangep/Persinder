"""Ядро Persinder: доменные модели и значения."""

from .chunking import chunk_text
from .clock import utcnow
from .models import Area, Chunk, Document, EmbeddingJob

__all__ = [
    "Area",
    "Chunk",
    "Document",
    "EmbeddingJob",
    "chunk_text",
    "utcnow",
]
