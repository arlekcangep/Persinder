"""Фоновая переиндексация при ``--reindex`` (D25).

Своей сверки хэшей здесь нет: хэш содержимого уже считает
``IngestService.ingest`` (``sha256(text)`` против ``documents.content_hash``),
и конвейер на нём же останавливает неизменённые файлы (``ALREADY``).
Переиндексация поэтому — это просто прогон файлов через обычный конвейер:
совпал текст с базой — документ не трогается, изменился — файл индексируется
заново. Отдельного «второго» механизма проверки нет.

Прогоняются файлы, которые журнал уже довёл до ``indexed``/``duplicate``:
новые для журнала файлы разбирает стартовый обход каталога (``catalog``) до
переиндексации, а разобранные с отказом (``skipped``/``error``/``rejected``)
не перечитываются на каждом старте. Расширения при этом не фильтруются:
конвейер классифицирует по содержимому, поэтому ``.yaml``/``.json`` тоже
должны сверяться, хотя их суффикса нет в ``formats``.

Работу держим в пределах ``share`` процессорного времени (по умолчанию 5%):
между файлами ``Throttle`` спит. Поток фоновый, поэтому сервис отвечает на
запросы и индексирует обычные изменения параллельно.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .journal import SETTLED, Journal, JournalEvent
from .models import AreaConfig, ChangeEvent, ChangeKind, ScanResult
from .pipeline import IndexingPipeline
from .scanner import AreaScanner

#: Журнал считает файл разобранным с отказом — перечитывать его каждый старт
#: незачем (причина уже названа и видна в журнале).
_NOT_INDEXED: frozenset[JournalEvent] = frozenset(
    {JournalEvent.SKIPPED, JournalEvent.ERROR, JournalEvent.REJECTED}
)


class Throttle:
    """Держит долю времени работы не выше ``share`` — сон между порциями.

    После порции работы длительностью ``t`` спим ``t * (1 - share) / share``,
    поэтому средняя загрузка процессора ≈ ``share`` (5% по умолчанию).
    Часы и сон подменяемы: тесты не спят по-настоящему.
    """

    def __init__(
        self,
        share: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < share <= 1:
            raise ValueError("share должен быть в интервале (0, 1]")
        self._share = share
        self._clock = clock
        self._sleep = sleep
        self._started = clock()
        self.slept = 0.0

    def rest(self) -> None:
        """Поспать так, чтобы средняя доля работы не превысила ``share``."""
        if self._share >= 1:
            return
        elapsed = self._clock() - self._started
        pause = elapsed * (1 - self._share) / self._share
        if pause > 0:
            self.slept += pause
            self._sleep(pause)
        self._started = self._clock()


def share_from_percent(value: str | None, default: float = 5.0) -> float:
    """Проценты из окружения (``PERSINDER_REINDEX_CPU_LIMIT``) → доля."""
    if value is None or not value.strip():
        return default / 100.0
    try:
        percent = float(value)
    except ValueError:
        return default / 100.0
    if percent <= 0:
        return 1.0  # 0 — лимит снят явно
    return min(percent, 100.0) / 100.0


@dataclass
class ReindexStats:
    """Итог фоновой переиндексации.

    ``files`` — файлов прогнано через конвейер; ``changed`` — из них
    переиндексировано (текст не совпал с базой); ``unchanged`` — конвейер
    узнал текст по хэшу и ничего не делал; ``skipped`` — формат перестал
    обслуживаться (или текста нет).
    """

    files: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: int = 0
    seconds: float = 0.0
    messages: list[str] = field(default_factory=list[str])


def reindex_files(
    areas: tuple[AreaConfig, ...],
    scanner: AreaScanner,
    journal: Journal,
    pipeline: IndexingPipeline,
    *,
    share: float = 0.05,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReindexStats:
    """Прогнать проиндексированные файлы через конвейер; посчитать изменения.

    Решение «файл не менялся» принимает ``IngestService`` по хэшу текста —
    здесь его нет. ``pipeline`` создаётся с ``quiet_unchanged=True``: по
    неизменённым файлам не пишется ни журнал, ни консоль (иначе каждый старт
    добавлял бы по три строки на каждый файл), результат виден в итоговой
    строке. ``stop`` обрывает проход при остановке сервиса; ``clock``
    подменяем в тестах, чтобы не ждать.
    """
    started = clock()
    throttle = Throttle(share, clock=clock)
    records = {
        (root, rel): event
        for root, rel, event in journal.records()
        if event in SETTLED and event not in _NOT_INDEXED
    }
    stats = ReindexStats()

    for area in areas:
        catalog = scanner.snapshot(area)
        for rel, (size, mtime_ns) in sorted(catalog.files.items()):
            if stop is not None and stop.is_set():
                stats.seconds = clock() - started
                return stats
            if (str(area.path), rel) not in records:
                continue  # новый файл разобрал каталог, отказ — не перечитываем
            outcome = pipeline.process(
                ScanResult(
                    area=area,
                    events=(
                        ChangeEvent(
                            ChangeKind.MODIFIED,
                            area,
                            rel,
                            size=size,
                            mtime_ns=mtime_ns,
                        ),
                    ),
                )
            )
            stats.files += 1
            stats.changed += outcome.indexed
            stats.unchanged += outcome.duplicates
            stats.skipped += outcome.skipped
            stats.errors += outcome.errors
            stats.messages.extend(outcome.messages)
            throttle.rest()
    stats.seconds = clock() - started
    return stats


def format_reindex(stats: ReindexStats) -> str:
    """Строка отчёта о переиндексации для консоли и журнала."""
    return (
        f"Переиндексация: проверено {stats.files} файлов, "
        f"изменено {stats.changed}, без изменений {stats.unchanged}, "
        f"пропущено {stats.skipped}, ошибок {stats.errors}; "
        f"{stats.seconds:.0f} с"
    )
