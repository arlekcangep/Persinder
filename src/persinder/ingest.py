"""Модуль 1: сервис приёма текста и метаданных.

Быстрый морфологический слой индексации + откладывание эмбеддингов в очередь.
"""

from __future__ import annotations

import hashlib

from .morphology import analyze_to_string
from .models import (
    Chunk,
    Document,
    EmbeddingJob,
    IngestRequest,
    IngestResult,
    utcnow,
)


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Нарезать текст на диапазоны ``(start, end)`` с перекрытием."""
    if size <= 0:
        raise ValueError("размер чанка должен быть положительным")
    if overlap < 0 or overlap >= size:
        raise ValueError("перекрытие должно быть в диапазоне [0, размер чанка)")
    length = len(text)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < length:
        end = min(start + size, length)
        spans.append((start, end))
        if end >= length:
            break
        start = end - overlap
    return spans


class IngestService:
    """Реализация сервиса приёма (см. ``interfaces.Ingest``)."""

    def __init__(
        self,
        store: object,
        textstore: object,
        stemmer: object,
        chunk_size: int = 1200,
        chunk_overlap: int = 200,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._stemmer = stemmer
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    def ingest(self, request: IngestRequest) -> IngestResult:
        text = request.text or ""
        if not text.strip():
            return IngestResult(IngestResult.REJECTED, message="пустой текст")
        if not request.filename.strip():
            return IngestResult(IngestResult.REJECTED, message="пустое имя файла")

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        existing = self._store.find_document_by_hash(content_hash)
        if existing:
            self._store.add_source(existing, request.area.value, request.filename)
            return IngestResult(
                IngestResult.ALREADY,
                existing,
                message="документ уже проиндексирован (дубликат по хэшу)",
            )

        doc_id = content_hash
        self._textstore.put(doc_id, text)

        spans = chunk_text(text, self._chunk_size, self._chunk_overlap)
        chunks = [
            Chunk(
                index=i,
                char_start=start,
                char_end=end,
                stemmed=analyze_to_string(text[start:end], self._stemmer),
            )
            for i, (start, end) in enumerate(spans)
        ]

        now = utcnow()
        document = Document(
            id=doc_id,
            content_hash=content_hash,
            title=request.title,
            char_count=len(text),
            text_key=doc_id,
            created_at=now,
        )
        jobs = [
            EmbeddingJob(
                doc_id=doc_id,
                chunk_index=chunk.index,
                area=request.area.value,
                created_at=now,
            )
            for chunk in chunks
        ]

        self._store.save_document(
            document, request.area.value, request.filename, chunks, jobs
        )
        return IngestResult(IngestResult.INDEXED, doc_id, len(chunks))
