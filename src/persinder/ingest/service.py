"""Модуль 1: сервис приёма текста и метаданных.

Быстрый морфологический слой индексации + откладывание эмбеддингов в очередь.
Единственная точка доступа к метастору и хранилищу текста: остальные модули
(в том числе watcher) ходят в БД только через этот сервис.

Владелец инварианта «документ + его сырой текст + его сайдкары» (D22, D24):
при вытеснении и удалении документа тут же убираются и текст, и сайдкары.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

from ..domain import Area, Chunk, Document, EmbeddingJob, chunk_text, utcnow
from ..embed.sidecar import file_sidecar_path, virtual_sidecar_path
from ..interfaces import MetaStore, Stemmer, TextStore
from ..morphology import analyze_to_string
from .journal import IngestEntry, IngestEvent, IngestJournal
from .models import IngestRequest, IngestResult, RecoveryStats, RemoveResult


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
        virtual_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._stemmer = stemmer
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._journal = journal
        # Папка для данных, которых нет на диске: их сайдкары и сам текст.
        self._virtual_dir = virtual_dir

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

    @staticmethod
    def _delete_sidecar(path: Path) -> None:
        """Удалить сайдкар; отсутствие файла — не ошибка."""
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _drop_sidecars(
        self,
        evicted: list[str],
        previous: str | None,
        source_path: str,
        doc_id: str,
    ) -> None:
        """Убрать сайдкары, ставшие неактуальными (D24).

        Источник ``(area, filename)`` перепривязан к другому документу — его
        прежний сайдкар больше не соответствует содержимому: удаляем. Плюс
        виртуальные сайдкары вытесненных документов (их данных на диске нет).
        """
        if previous is not None and previous != doc_id and source_path:
            self._delete_sidecar(file_sidecar_path(source_path))
        if self._virtual_dir is not None:
            for old_id in evicted:
                self._delete_sidecar(virtual_sidecar_path(self._virtual_dir, old_id))

    # --- приём ---

    def ingest(self, request: IngestRequest) -> IngestResult:
        text = request.text or ""
        area = request.area.value
        source_path = request.source_path or ""
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
            evicted = self._store.add_source(
                existing, area, request.filename, source_path=source_path
            )
            self._drop_texts(evicted)
            self._drop_sidecars(evicted, previous, source_path, existing)
            # У нового источника свой сайдкар — ставим его задачи в очередь.
            self._store.enqueue_document(existing, area, source_path)
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
                source_path=source_path,
            )
            for chunk in chunks
        ]

        evicted = self._store.save_document(
            document,
            area,
            request.filename,
            chunks,
            jobs,
            source_path=source_path,
        )
        self._drop_texts(evicted)
        self._drop_sidecars(evicted, previous, source_path, doc_id)
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
        """Убрать источник, его сырой текст и сайдкар (если документ вычищен)."""
        area_name = area.value
        doc_id = self._store.source_document(area_name, filename)
        if doc_id is None:
            return RemoveResult(found=False)
        source_path = self._store.source_path(area_name, filename)
        self._log(IngestEvent.REMOVE, doc_id=doc_id, area=area_name, filename=filename)
        purged = self._store.remove_source(area_name, filename)
        if purged is not None:
            self._textstore.delete(purged)
        if source_path:
            self._delete_sidecar(file_sidecar_path(source_path))
        elif purged is not None and self._virtual_dir is not None:
            self._delete_sidecar(virtual_sidecar_path(self._virtual_dir, purged))
        self._log(IngestEvent.REMOVED, doc_id=doc_id, area=area_name, filename=filename)
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
