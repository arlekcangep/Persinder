"""Фоновая сверка хэшей файлов при ``--reindex`` (D25).

Reindex больше не сбрасывает журнал и не перечитывает библиотеку разом
(именно это давало всплеск нагрузки на старте). Вместо этого:

* очередь эмбеддингов очищается (задачи прошлого прогона не нужны);
* в фоне перебираются файлы всех областей; текст каждого файла извлекается
  и сравнивается с базой **по уже имеющемуся хэшу содержимого**
  (``documents.content_hash`` — он же ``doc_id``, отдельного хранилища
  хэшей не нужно);
* совпало — идём дальше; не совпало (другой текст, источника нет, файл
  новый) — файл отдаётся конвейеру и индексируется заново.

Сверка держится в пределах ``share`` процессорного времени (по умолчанию 5%):
между файлами ``Throttle`` спит. Работает в отдельном потоке, поэтому сервис
отвечает на запросы и индексирует обычные изменения параллельно.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..interfaces import MetaStore
from .extract import ExtractorRegistry
from .journal import SETTLED, Journal, JournalEvent
from .models import AreaConfig, ChangeEvent, ChangeKind, ScanResult
from .scanner import AreaScanner

#: Журнал считает файл разобранным, но документа нет: не перечитывать его
#: на каждой сверке (причина уже названа — её печатает сверка при старте).
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
class SweepStats:
    """Итог фоновой сверки хэшей."""

    files: int = 0  # файлов прочитано и сверено
    same: int = 0  # текст совпал с базой
    changed: int = 0  # изменённых/новых — отдано конвейеру
    untouched: int = 0  # пропущено: формат не обслуживается или уже разобран
    errors: int = 0
    seconds: float = 0.0
    messages: list[str] = field(default_factory=list[str])


def _text_hashes(rel: str, parts: list[tuple[str, str]]) -> dict[str, str]:
    """``имя источника → sha256 текста`` для частей файла (как их пишет конвейер).

    ``parts`` — пары ``(имя части, текст)``; имя пустое — сам файл.
    """
    out: dict[str, str] = {}
    for name, text in parts:
        source = f"{rel}/{name}" if name else rel
        out[source] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out


def sweep_hashes(
    areas: tuple[AreaConfig, ...],
    scanner: AreaScanner,
    registry: ExtractorRegistry,
    store: MetaStore,
    journal: Journal,
    on_changed: Callable[[ScanResult], None],
    *,
    share: float = 0.05,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SweepStats:
    """Сверить текст всех файлов с базой; изменённые — отдать ``on_changed``.

    ``share`` — доля процессорного времени на сверку (0.05 = 5%). ``stop``
    обрывает сверку при остановке сервиса; ``clock`` подменяем в тестах, чтобы
    не ждать. Изменённый файл конвейер прочитает ещё раз: повторное чтение
    только у изменённых, а не у всей библиотеки.
    """
    started = clock()
    throttle = Throttle(share, clock=clock)
    records = {
        (root, rel): event for root, rel, event in journal.records() if event in SETTLED
    }
    stats = SweepStats()

    for area in areas:
        catalog = scanner.snapshot(area)
        for rel, (size, mtime_ns) in sorted(catalog.files.items()):
            if stop is not None and stop.is_set():
                stats.seconds = clock() - started
                return stats
            path = area.path / rel
            if not registry.supports(path, area):
                stats.untouched += 1
                continue
            if records.get((str(area.path), rel)) in _NOT_INDEXED:
                stats.untouched += 1
                continue
            try:
                extracted = registry.extract(path, area) or []
            except Exception as exc:  # noqa: BLE001 — один файл не валит сверку
                stats.errors += 1
                stats.messages.append(f"{rel}: {exc}")
                throttle.rest()
                continue
            usable = [
                (part.name, part.text)
                for part in extracted
                if not part.error and part.text.strip()
            ]
            if not usable:
                stats.untouched += 1
                throttle.rest()
                continue
            expected = _text_hashes(rel, usable)
            current = {
                name: store.source_document(area.role.value, name) for name in expected
            }
            stats.files += 1
            if current == expected:
                stats.same += 1
            else:
                stats.changed += 1
                on_changed(
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
            throttle.rest()
    stats.seconds = clock() - started
    return stats


def format_sweep(stats: SweepStats) -> str:
    """Строка отчёта о сверке хэшей для консоли и журнала."""
    return (
        f"Сверка хэшей: файлов {stats.files}, без изменений {stats.same}, "
        f"изменено {stats.changed}, пропущено {stats.untouched}, "
        f"ошибок {stats.errors}; {stats.seconds:.0f} с"
    )
