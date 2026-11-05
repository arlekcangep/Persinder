"""Модуль 1: сервис приёма текста и метаданных.

Быстрый морфологический слой индексации + откладывание эмбеддингов в очередь.
Единственная точка доступа к метастору и хранилищу текста: остальные модули
(в том числе watcher) ходят в БД только через этот сервис.
"""

from __future__ import annotations

import hashlib

from ..domain import Area, Chunk, Document, EmbeddingJob, utcnow
from ..interfaces import MetaStore, Stemmer, TextStore
from ..morphology import analyze_to_string
from .journal import IngestEntry, IngestEvent, IngestJournal
from .models import IngestRequest, IngestResult, RecoveryStats, RemoveResult


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
        store: MetaStore,
        textstore: TextStore,
        stemmer: Stemmer,
        chunk_size: int = 1200,
        chunk_overlap: int = 200,
        journal: IngestJournal | None = None,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._stemmer = stemmer
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._journal = journal

    # --- служебное ---

    def _log(
        self,
        op: IngestEvent,
        *,
        doc_id: str | None = None,
        previous: str | None = None,
        area: str | None = None,
        filename: str | None = None,
        detail: str | None = None,
    ) -> None:
        if self._journal is not None:
            self._journal.record(
                op,
                doc_id=doc_id,
                previous=previous,
                area=area,
                filename=filename,
                detail=detail,
            )

    def _drop_texts(self, doc_ids: list[str]) -> None:
        """Удалить сырой текст вытесненных документов."""
        for doc_id in doc_ids:
            self._textstore.delete(doc_id)

    # --- приём ---

    def ingest(self, request: IngestRequest) -> IngestResult:
        text = request.text or ""
        area = request.area.value
        if not text.strip():
            self._log(
                IngestEvent.REJECTED,
                area=area,
                filename=request.filename,
                detail="пустой текст",
            )
            return IngestResult(IngestResult.REJECTED, message="пустой текст")
        if not request.filename.strip():
            self._log(IngestEvent.REJECTED, area=area, detail="пустое имя файла")
            return IngestResult(IngestResult.REJECTED, message="пустое имя файла")

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Прежний документ файла — кандидат на вытеснение (и чистку текста).
        previous = self._store.source_document(area, request.filename)

        existing = self._store.find_document_by_hash(content_hash)
        if existing:
            self._log(
                IngestEvent.DUPLICATE,
                doc_id=existing,
                previous=previous,
                area=area,
                filename=request.filename,
            )
            evicted = self._store.add_source(existing, area, request.filename)
            self._drop_texts(evicted)
            self._log(
                IngestEvent.DUPLICATED,
                doc_id=existing,
                area=area,
                filename=request.filename,
            )
            return IngestResult(
                IngestResult.ALREADY,
                existing,
                message="документ уже проиндексирован (дубликат по хэшу)",
            )

        doc_id = content_hash
        self._log(
            IngestEvent.INDEX,
            doc_id=doc_id,
            previous=previous,
            area=area,
            filename=request.filename,
        )
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
                area=area,
                created_at=now,
            )
            for chunk in chunks
        ]

        evicted = self._store.save_document(
            document, area, request.filename, chunks, jobs
        )
        self._drop_texts(evicted)
        self._log(
            IngestEvent.INDEXED,
            doc_id=doc_id,
            area=area,
            filename=request.filename,
            detail=f"чанков: {len(chunks)}",
        )
        return IngestResult(IngestResult.INDEXED, doc_id, len(chunks))

    # --- удаление ---

    def remove_source(self, area: Area, filename: str) -> RemoveResult:
        """Убрать источник и его сырой текст (если документ вычищен целиком)."""
        doc_id = self._store.source_document(area.value, filename)
        if doc_id is None:
            return RemoveResult(found=False)
        self._log(IngestEvent.REMOVE, doc_id=doc_id, area=area.value, filename=filename)
        purged = self._store.remove_source(area.value, filename)
        if purged is not None:
            self._textstore.delete(purged)
        self._log(
            IngestEvent.REMOVED, doc_id=doc_id, area=area.value, filename=filename
        )
        return RemoveResult(found=True, document_id=purged)

    # --- восстановление ---

    def recover(self) -> RecoveryStats:
        """Подчистить брошенное по журналу: незавершённые вызовы прошлого запуска.

        Для каждой незавершённой записи смотрим на ``doc_id`` и ``previous``:
        если документа в базе нет, а текст на диске остался — это сирота,
        удаляем. DB-строки всегда целостны (запись и удаление — по одной
        транзакции), поэтому чистить нужно только диск.
        """
        if self._journal is None:
            return RecoveryStats()
        stats = RecoveryStats()
        for entry in self._journal.unfinished():
            stats = self._recover_entry(entry, stats)
        self._journal.compact()
        return stats

    def _recover_entry(self, entry: IngestEntry, stats: RecoveryStats) -> RecoveryStats:
        removed = stats.removed
        for key in (entry.doc_id, entry.previous):
            if key is None:
                continue
            alive = self._store.find_document_by_hash(key) is not None
            if not alive and self._textstore.exists(key):
                self._textstore.delete(key)
                removed += 1
        return RecoveryStats(checked=stats.checked + 1, removed=removed)
