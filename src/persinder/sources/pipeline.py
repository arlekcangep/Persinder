"""Связка watcher → экстрактор → ingest.

``IndexingPipeline`` обрабатывает события сканера: берёт файл области,
прогоняет через реестр экстракторов (с учётом форматов области) и отдаёт
текст в ``IngestService``. Каждый переход пишется в ``Journal`` — он же
служит состоянием watcher'а (D21).

В БД конвейер напрямую не ходит: и приём, и удаление — операции
``IngestService`` (единая точка доступа к хранилищам).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from ..ingest import IngestRequest, IngestResult
from ..interfaces import Ingest
from .extract import ExtractorRegistry
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
        """Убрать источник удалённого файла (D13); деталь — для журнала."""
        result = self._ingest.remove_source(area.role, rel_path)
        if result.document_id is not None:
            return f"документ {result.document_id} удалён из индекса"
        if result.found:
            return "источник убран, документ сохранён (есть другие источники)"
        return "источника в индексе не было"

    def _process_event(self, event: ChangeEvent, stats: PipelineStats) -> None:
        """Обработать одно событие: экстракция, ingest, записи в журнал."""
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
        extracted = self._registry.extract(area.path / rel_path, area)
        if extracted is None:
            self._record(
                area, rel_path, JournalEvent.SKIPPED, detail="формат не обслуживается"
            )
            stats.skipped += 1
            return
        if extracted.error:
            self._record(area, rel_path, JournalEvent.ERROR, detail=extracted.error)
            self._fail(stats, rel_path, extracted.error)
            return
        if not extracted.text.strip():
            self._record(area, rel_path, JournalEvent.SKIPPED, detail="нет текста")
            stats.skipped += 1
            return

        self._record(area, rel_path, JournalEvent.SENT)
        outcome = self._ingest.ingest(
            IngestRequest(
                filename=rel_path,
                area=area.role,
                text=extracted.text,
                source_path=str(area.path / rel_path),
            )
        )
        if outcome.status == IngestResult.INDEXED:
            self._record(
                area,
                rel_path,
                JournalEvent.INDEXED,
                detail=f"документ {outcome.document_id}, чанков: {outcome.chunks}",
            )
            stats.indexed += 1
        elif outcome.status == IngestResult.ALREADY:
            self._record(area, rel_path, JournalEvent.DUPLICATE)
            stats.duplicates += 1
        else:
            self._record(area, rel_path, JournalEvent.REJECTED, detail=outcome.message)
            stats.rejected += 1
            if outcome.message:
                stats.messages.append(f"{rel_path}: {outcome.message}")

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
