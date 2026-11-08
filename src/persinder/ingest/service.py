"""Модуль 1: сервис приёма текста и метаданных.

Быстрый морфологический слой индексации + откладывание эмбеддингов в очередь.
Единственная точка доступа к метастору и хранилищу текста: остальные модули
(в том числе watcher) ходят в БД только через этот сервис.

Владелец инварианта «документ + его сырой текст + его сайдкары» (D22, D24):
при вытеснении и удалении документа тут же убираются и текст, и сайдкары.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..domain import Area, Chunk, Document, EmbeddingJob, chunk_text, utcnow
from ..embed.sidecar import (
    delete_file_sidecars,
    delete_virtual_sidecars,
    file_sidecar_path,
    virtual_sidecar_path,
)
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
        embedding_models: tuple[str, ...] = ("placeholder-v1",),
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._stemmer = stemmer
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._journal = journal
        # Папка для данных, которых нет на диске: их сайдкары и сам текст.
        self._virtual_dir = virtual_dir
        # Модели, под которыми считаем эмбеды новых/изменённых файлов (D24).
        self._embedding_models = embedding_models

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

    def _drop_sidecars(
        self,
        evicted: list[str],
        previous: str | None,
        source_path: str,
        doc_id: str,
    ) -> None:
        """Убрать сайдкары, ставшие неактуальными (D24).

        Источник ``(area, filename)`` перепривязан к другому документу — его
        прежние сайдкары (по всем моделям) больше не соответствуют
        содержимому: удаляем. Плюс виртуальные сайдкары вытесненных
        документов (их данных на диске нет).
        """
        if previous is not None and previous != doc_id and source_path:
            delete_file_sidecars(source_path)
        if self._virtual_dir is not None:
            for old_id in evicted:
                delete_virtual_sidecars(self._virtual_dir, old_id)

    def _models_without_sidecar(self, doc_id: str, source_path: str) -> list[str]:
        """Модели, под которыми у источника ещё нет сайдкара.

        Сайдкар — признак того, что документ уже отработан под моделью для
        этого источника: воркер пишет его только после записи векторов в
        таблицу. Уже посчитанное заново в очередь не ставим — иначе каждый
        запуск с ``--reindex`` (сброс журнала) пересчитывал бы эмбеддинги всех
        документов, постоянно нагружая процессор.
        """
        missing: list[str] = []
        for model in self._embedding_models:
            if source_path:
                path = file_sidecar_path(source_path, model)
            elif self._virtual_dir is not None:
                path = virtual_sidecar_path(self._virtual_dir, doc_id, model)
            else:
                # Некуда положить сайдкар — считаем, что его нет.
                missing.append(model)
                continue
            if not path.exists():
                missing.append(model)
        return missing

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
            # У нового источника свои сайдкары — ставим его задачи под каждой
            # активной моделью (D24: пока embedding ≠ search, файл считается обеими).
            # Но только если сайдкара ещё нет: повторный скан (в т.ч. после
            # --reindex) не должен заново считать уже посчитанные эмбеддинги.
            pending_models = self._models_without_sidecar(existing, source_path)
            if pending_models:
                self._store.enqueue_document(
                    existing, area, source_path, models=pending_models
                )
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
                model=model,
            )
            for chunk in chunks
            for model in self._embedding_models
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
        """Убрать источник, его сырой текст и сайдкары (если документ вычищен)."""
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
            delete_file_sidecars(source_path)
        elif purged is not None and self._virtual_dir is not None:
            delete_virtual_sidecars(self._virtual_dir, purged)
        self._log(IngestEvent.REMOVED, doc_id=doc_id, area=area_name, filename=filename)
        return RemoveResult(found=True, document_id=purged)

    def remove_source_tree(self, area: Area, filename: str) -> RemoveResult:
        """Убрать источник и все его части (файлы контейнера: ``filename/...``).

        Файл-контейнер (ZIP) даёт по источнику на каждую запись
        (``bundle.zip/inner.md``). При исчезновении самого файла нужно убрать
        и его, и части. Для обычного файла частей нет — поведение как у
        ``remove_source``.
        """
        area_name = area.value
        names = [filename]
        names.extend(self._store.source_filenames_under(area_name, filename + "/"))
        found = False
        purged_doc: str | None = None
        for name in names:
            result = self.remove_source(area, name)
            if result.found:
                found = True
                if purged_doc is None and result.document_id is not None:
                    purged_doc = result.document_id
        return RemoveResult(found=found, document_id=purged_doc)

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
