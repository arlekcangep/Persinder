"""Связка watcher → экстрактор → ingest.

``IndexingPipeline`` обрабатывает события сканера: берёт файл области,
прогоняет через реестр экстракторов (с учётом форматов области) и отдаёт
текст в ``IngestService``. Удаления сейчас только логируются в счётчик —
чистка индекса будет добавлена отдельно (высокий приоритет, D13).
"""

import dataclasses
from dataclasses import dataclass

from ..ingest import IngestRequest, IngestResult
from ..interfaces import Ingest
from .extract import ExtractorRegistry
from .models import AreaConfig, ChangeKind, ScanResult


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
    """События сканера → экстракция → ingest."""

    def __init__(self, ingest: Ingest, registry: ExtractorRegistry) -> None:
        self._ingest = ingest
        self._registry = registry

    def _handle_file(self, area: AreaConfig, rel_path: str) -> IngestResult | None:
        path = area.path / rel_path
        extracted = self._registry.extract(path, area)
        if extracted is None:
            return None
        request = IngestRequest(
            filename=rel_path,
            area=area.role,
            text=extracted.text,
        )
        return self._ingest.ingest(request)

    def process(self, result: ScanResult) -> PipelineStats:
        """Обработать один результат скана."""
        stats = PipelineStats()
        for event in result.events:
            try:
                if event.kind is ChangeKind.DELETED:
                    # TODO(D13): чистка индекса по (area, rel_path).
                    stats.deleted += 1
                    continue
                outcome = self._handle_file(result.area, event.rel_path)
            except OSError as exc:
                stats.errors += 1
                stats.messages.append(f"{event.rel_path}: {exc}")
                continue
            if outcome is None:
                stats.skipped += 1
                continue
            if outcome.status == IngestResult.INDEXED:
                stats.indexed += 1
            elif outcome.status == IngestResult.ALREADY:
                stats.duplicates += 1
            else:
                stats.rejected += 1
                if outcome.message:
                    stats.messages.append(f"{event.rel_path}: {outcome.message}")
        return stats
