"""Связка watcher → экстрактор → ingest.

``IndexingPipeline`` обрабатывает события сканера: берёт файл области,
прогоняет через реестр экстракторов (с учётом форматов области) и отдаёт
текст в ``IngestService``. Каждый переход пишется в ``Journal`` — он же
служит состоянием watcher'а (D21).

Реестр может вернуть несколько частей (контейнер, например ZIP): тогда каждая
запись становится отдельным документом (``bundle.zip/inner.md``), а в журнал
пишется одна сводная цепочка по самому файлу. Виртуальные части (записей на
диске нет) отдаются без ``source_path``; удаление файла убирает и его части.

В БД конвейер напрямую не ходит: и приём, и удаление — операции
``IngestService`` (единая точка доступа к хранилищам).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from ..ingest import IngestRequest, IngestResult
from ..interfaces import Ingest
from .extract import Extracted, ExtractorRegistry
from .journal import Journal, JournalEvent
from .models import AreaConfig, ChangeEvent, ChangeKind, ScanResult


@dataclass
class PipelineStats:
    """Итоги обработки событий."""

    indexed: int = 0
    duplicates: int = 0
    rejected: int = 0
    skipped: int = 0  # формат не разрешён/не поддержан
    deleted: int = 0
    errors: int = 0
    messages: list[str] = dataclasses.field(default_factory=list[str])


class IndexingPipeline:
    """События сканера → экстракция → ingest → журнал."""

    def __init__(
        self,
        ingest: Ingest,
        registry: ExtractorRegistry,
        journal: Journal | None = None,
    ) -> None:
        self._ingest = ingest
        self._registry = registry
        self._journal = journal

    def _record(
        self,
        area: AreaConfig,
        rel_path: str,
        event: JournalEvent,
        *,
        size: int | None = None,
        mtime_ns: int | None = None,
        detail: str | None = None,
    ) -> None:
        if self._journal is not None:
            self._journal.record(
                area, rel_path, event, size=size, mtime_ns=mtime_ns, detail=detail
            )

    @staticmethod
    def _fail(stats: PipelineStats, rel_path: str, message: str) -> None:
        stats.errors += 1
        stats.messages.append(f"{rel_path}: {message}")

    def _remove_source(self, area: AreaConfig, rel_path: str) -> str:
        """Убрать источник удалённого файла и его части (D13); деталь — для журнала.

        Для файла-контейнера (ZIP) частей несколько (``bundle.zip/inner.md``) —
        ``remove_source_tree`` чистит и сам файл, и все его записи.
        """
        result = self._ingest.remove_source_tree(area.role, rel_path)
        if result.document_id is not None:
            return f"документ {result.document_id} удалён из индекса"
        if result.found:
            return "источник убран, документ сохранён (есть другие источники)"
        return "источника в индексе не было"

    @staticmethod
    def _part_name(rel_path: str, part: Extracted) -> str:
        """Имя документа части контейнера: ``bundle.zip/inner.md`` (или сам файл)."""
        return f"{rel_path}/{part.name}" if part.name else rel_path

    @staticmethod
    def _indexed_detail(indexed: int, chunks: int, doc_ids: list[str]) -> str:
        """Деталь события ``indexed``: один документ — с id, иначе — с числом."""
        if indexed == 1:
            head = f"документ {doc_ids[0]}" if doc_ids else "документ"
        else:
            head = f"{indexed} документов"
        return f"{head}, чанков: {chunks}"

    def _process_event(self, event: ChangeEvent, stats: PipelineStats) -> None:
        """Обработать одно событие: экстракция (возможно, в части), ingest, журнал."""
        area, rel_path = event.area, event.rel_path
        if event.kind is ChangeKind.DELETED:
            detail = self._remove_source(area, rel_path)
            self._record(area, rel_path, JournalEvent.DELETED, detail=detail)
            stats.deleted += 1
            return

        self._record(
            area,
            rel_path,
            JournalEvent.DISCOVERED,
            size=event.size,
            mtime_ns=event.mtime_ns,
        )
        parts = self._registry.extract(area.path / rel_path, area)
        if parts is None:
            self._record(
                area, rel_path, JournalEvent.SKIPPED, detail="формат не обслуживается"
            )
            stats.skipped += 1
            return

        errored = [p for p in parts if p.error]
        usable = [p for p in parts if not p.error and p.text.strip()]
        if not usable:
            if errored:
                detail = "; ".join(p.error or "" for p in errored)
                self._record(area, rel_path, JournalEvent.ERROR, detail=detail)
                self._fail(stats, rel_path, detail)
            else:
                self._record(area, rel_path, JournalEvent.SKIPPED, detail="нет текста")
                stats.skipped += 1
            return

        self._record(area, rel_path, JournalEvent.SENT)
        indexed = duplicates = rejected = 0
        chunks = 0
        doc_ids: list[str] = []
        for part in usable:
            filename = self._part_name(rel_path, part)
            # Части контейнера — виртуальные (на диске только сам архив).
            source_path = str(area.path / rel_path) if not part.name else ""
            outcome = self._ingest.ingest(
                IngestRequest(
                    filename=filename,
                    area=area.role,
                    text=part.text,
                    source_path=source_path,
                )
            )
            if outcome.status == IngestResult.INDEXED:
                indexed += 1
                chunks += outcome.chunks
                if outcome.document_id is not None:
                    doc_ids.append(outcome.document_id)
            elif outcome.status == IngestResult.ALREADY:
                duplicates += 1
            else:
                rejected += 1
                if outcome.message:
                    stats.messages.append(f"{filename}: {outcome.message}")
        for part in errored:
            stats.messages.append(f"{self._part_name(rel_path, part)}: {part.error}")

        stats.indexed += indexed
        stats.duplicates += duplicates
        stats.rejected += rejected

        if indexed:
            self._record(
                area,
                rel_path,
                JournalEvent.INDEXED,
                detail=self._indexed_detail(indexed, chunks, doc_ids),
            )
        elif duplicates:
            self._record(area, rel_path, JournalEvent.DUPLICATE)
        else:
            self._record(
                area,
                rel_path,
                JournalEvent.REJECTED,
                detail=f"отклонено частей: {rejected}",
            )

    def process(self, result: ScanResult) -> PipelineStats:
        """Обработать один результат скана."""
        stats = PipelineStats()
        for event in result.events:
            try:
                self._process_event(event, stats)
            except OSError as exc:
                self._record(
                    event.area, event.rel_path, JournalEvent.ERROR, detail=str(exc)
                )
                self._fail(stats, event.rel_path, str(exc))
        return stats
