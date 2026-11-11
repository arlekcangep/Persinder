"""Стартовый обход каталогов областей: файлы — источник правды (D25).

Журнал наблюдения (D21) — не истина, а **память**: чем закончилась прошлая
индексация. На старте сервис сначала читает полные каталоги всех областей
(имена файлов; хэши на этом этапе не считаются — это долго) и сверяет их с
журналом по именам:

* файл на диске, которого журнал не знает (или знает как недоведённый) —
  **новый**, его индексируем первым делом;
* журнал знает файл, а на диске его нет — **удалённый**, его источник убираем.

Только после этого в работу вступает обычный скан по ``(size, mtime)`` и, если
запущен ``--reindex``, фоновая переиндексация (``reindex``): все файлы идут
через обычный конвейер, а неизменённые он узнаёт по уже имеющемуся хэшу
содержимого. Так файл, появившийся пока сервис был выключен, индексируется
сразу — без сброса журнала и без полного перечитывания библиотеки.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .journal import SETTLED, Journal, JournalEvent
from .models import AreaConfig, ChangeKind, ScanResult
from .scanner import AreaScanner


@dataclass
class CatalogStats:
    """Итог стартового обхода: что увидели и что из этого делать."""

    areas: int = 0
    files: int = 0
    added: int = 0
    removed: int = 0
    results: list[ScanResult] = field(default_factory=list[ScanResult])

    @property
    def empty(self) -> bool:
        return not self.results

    def line(self) -> str:
        """Строка для консоли: каталог прочитан, состав сверен."""
        head = f"Каталог: {self.files} файлов в {self.areas} областях"
        if not self.results:
            return f"{head}; новых и удалённых нет"
        return f"{head}; новых: {self.added}, удалённых: {self.removed}"


def sync_catalog(
    areas: tuple[AreaConfig, ...],
    scanner: AreaScanner,
    journal: Journal,
) -> CatalogStats:
    """Прочитать каталоги и вернуть события «новое/удалённое» по именам."""
    records: dict[str, list[tuple[str, JournalEvent]]] = {}
    for root, rel, event in journal.records():
        records.setdefault(root, []).append((rel, event))

    stats = CatalogStats(areas=len(areas))
    for area in areas:
        catalog = scanner.snapshot(area)
        stats.files += len(catalog.files)
        entries = records.get(str(area.path), [])
        settled = {rel for rel, state in entries if state in SETTLED}
        known = {rel for rel, state in entries if state is not JournalEvent.DELETED}
        events = scanner.name_diff(catalog, settled, known)
        if not events:
            continue
        for change in events:
            if change.kind is ChangeKind.CREATED:
                stats.added += 1
            else:
                stats.removed += 1
        stats.results.append(ScanResult(area=area, events=events))
    return stats
