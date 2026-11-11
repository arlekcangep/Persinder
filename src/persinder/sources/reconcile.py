"""Сверка журнала наблюдения с фактическим состоянием индекса.

Журнал — «что уже сделано» (D21), и скан пропускает файлы, у которых не
изменился слепок ``(size, mtime)``. Это правильно для скорости, но журнал
может разойтись с реальностью:

* файл был **пропущен**, когда формат ещё не обслуживался (``fb2`` до
  появления экстрактора, ``rtf`` без extra, ``pdf`` без extra) — теперь
  формат поддержан, а слепок тот же, значит события не будет никогда;
* документ помечен «индексирован», а **сырой текст потерян** (удалён руками,
  сбой, чужой профиль) — эмбеддинг считать нечем, сайдкара нет, в очередь
  документ не попадает (``enqueue_missing`` пропускает такие);
* документа **нет в базе**, хотя журнал говорит «индексирован» (потерянная
  БД, восстановление из сайдкаров без источников).

Во всех трёх случаях файл лежит на диске, очередь пуста, и со стороны это
выглядит как «сервис забыл файл» — без единой строчки в логе.

``reconcile`` при старте сверяет журнал с базой и текстами:

* забывает записи, которые больше не соответствуют реальности, — следующий
  скан прочитает файл заново и он сам попадёт в очередь;
* отдельно сообщает о файлах, которые так и не индексируются, с причиной и
  подсказкой (формат не разрешён в ``formats`` области / не установлен пакет).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path

from ..interfaces import MetaStore, TextStore
from .extract import ExtractorRegistry
from .journal import Journal, JournalEvent
from .models import AreaConfig


@dataclass
class ReconcileStats:
    """Итоги сверки журнала с базой."""

    #: (путь файла, причина) — записи забыты, файл будет прочитан заново.
    retried: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: Причина → файлы, которые так и не индексируются (нужно действие).
    unsupported: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: Сколько файлов журнала проверено (для строки «всё согласовано»).
    checked: int = 0

    @property
    def empty(self) -> bool:
        return not self.retried and not self.unsupported


def _posix(value: str) -> str:
    return value.replace("\\", "/").rstrip("/")


def reconcile(
    areas: tuple[AreaConfig, ...],
    registry: ExtractorRegistry,
    journal: Journal,
    store: MetaStore,
    textstore: TextStore,
) -> ReconcileStats:
    """Сверить журнал с базой; расхождения пометить к перечитыванию."""
    by_path: dict[str, list[str]] = {}
    for row in store.all_sources():
        source = _posix(str(row["source_path"]))
        if source:
            by_path.setdefault(source, []).append(str(row["doc_id"]))
    known_paths = sorted(by_path)

    def docs_for(path: Path) -> list[str]:
        """Документы файла: сам файл и его части (архив/запись)."""
        key = _posix(str(path))
        found = list(by_path.get(key, []))
        prefix = key + "/"
        index = bisect.bisect_left(known_paths, prefix)
        while index < len(known_paths) and known_paths[index].startswith(prefix):
            found.extend(by_path[known_paths[index]])
            index += 1
        return found

    area_by_root = {str(area.path): area for area in areas}
    stats = ReconcileStats()
    forget: list[tuple[str, str]] = []
    for root, rel, event in journal.records():
        area = area_by_root.get(root)
        if area is None:
            continue
        path = area.path / rel
        if not path.is_file():
            continue
        stats.checked += 1
        if event is JournalEvent.SKIPPED:
            if registry.supports(path, area):
                forget.append((root, rel))
                stats.retried.append((rel, "формат теперь поддерживается"))
            else:
                reason = registry.explain_miss(path, area)
                stats.unsupported.setdefault(reason, []).append(rel)
            continue
        if event not in (JournalEvent.INDEXED, JournalEvent.DUPLICATE):
            continue
        docs = docs_for(path)
        if not docs:
            forget.append((root, rel))
            stats.retried.append((rel, "документа нет в базе"))
        elif not any(textstore.exists(doc_id) for doc_id in docs):
            forget.append((root, rel))
            stats.retried.append((rel, "сырой текст потерян"))
    if forget:
        journal.forget(forget)
    return stats
