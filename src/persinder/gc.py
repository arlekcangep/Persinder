"""Сборка сирот: сырой текст, оставшийся без документа.

Текст кладётся в ``textstore`` при приёме и нужен воркеру эмбеддингов. При
правке файла или удалении документа запись в БД пропадает, а файл сырого
текста остаётся — это и есть сирота. ``collect_orphans`` сверяет каталог с
ключами, на которые ссылается метастор, и удаляет лишнее.

Свежий текст не трогаем: ``IngestService`` пишет файл **до** появления
документа в сторе, поэтому совсем недавний файл может быть «в полёте»
(например, параллельно работает ``serve``). Возрастной порог это закрывает.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .interfaces import MetaStore, TextStore

#: Сколько секунд не трогать свежий текст (окно гонки ingest).
MIN_ORPHAN_AGE_SECONDS = 3600


@dataclass(frozen=True)
class GcStats:
    """Итоги сборки сирот."""

    scanned: int = 0
    removed: int = 0
    missing: int = 0

    def __str__(self) -> str:
        return (
            f"GC: проверено текстов {self.scanned}, "
            f"удалено сирот {self.removed}, "
            f"документов без текста {self.missing}"
        )


def collect_orphans(
    store: MetaStore,
    textstore: TextStore,
    *,
    min_age_seconds: int = MIN_ORPHAN_AGE_SECONDS,
    now: float | None = None,
) -> GcStats:
    """Удалить сырой текст, на который не ссылается ни один документ.

    Живым считается текст с ключом из ``store.text_keys()``. Файлы моложе
    ``min_age_seconds`` пропускаются (окно гонки ingest). Документ без своего
    файла только считается: текст понадобится воркеру эмбеддингов, но
    переиндексация вернёт его; удалять тут нечего.
    """
    keys = textstore.keys()
    live = set(store.text_keys())
    moment = time.time() if now is None else now
    removed = 0
    for key in keys:
        if key in live:
            continue
        mtime = textstore.mtime(key)
        if mtime is None or moment - mtime < min_age_seconds:
            continue
        textstore.delete(key)
        removed += 1
    missing = len(live - set(keys))
    return GcStats(scanned=len(keys), removed=removed, missing=missing)
