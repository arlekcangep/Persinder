"""Текстовые отчёты CLI: сводки сканов, эмбеддингов и восстановления."""

from __future__ import annotations

import sys
import time
from contextlib import suppress
from pathlib import Path

from ..api.index_log import KIND_INDEX
from ..embed import EmbedStats
from ..ingest import RecoveryStats
from ..sources import PipelineStats


def print_stats(stats: PipelineStats) -> None:
    """Однострочная сводка прохода скана (пофайловые строки пишет IndexLog)."""
    parts: list[str] = []
    for count, label in (
        (stats.indexed, "проиндексировано"),
        (stats.duplicates, "дубликатов"),
        (stats.skipped, "пропущено"),
        (stats.rejected, "отклонено"),
        (stats.deleted, "удалено"),
        (stats.errors, "ошибок"),
    ):
        if count:
            parts.append(f"{label}: {count}")
    if not parts:
        return
    print(f"Скан: {', '.join(parts)}", flush=True)


def format_recovery(stats: RecoveryStats) -> str:
    """Человекочитаемый отчёт восстановления (оформление — на слое CLI)."""
    return (
        f"Ingest: проверено незавершённых вызовов {stats.checked}, "
        f"убрано брошенных текстов {stats.removed}"
    )


def format_embed(stats: EmbedStats) -> str:
    """Человекочитаемый отчёт прохода воркера эмбеддингов."""
    if stats.docs == 0 and stats.skipped == 0 and stats.errors == 0:
        return "Embed: очередь пуста"
    return (
        f"Embed: документов {stats.docs}, чанков {stats.chunks}, "
        f"сайдкаров {stats.sidecars}, пропущено {stats.skipped}, "
        f"ошибок {stats.errors}; очередь {stats.queue_before} → {stats.pending}"
    )


def say(text: str) -> None:
    """Печать, которая не роняет фоновый поток из-за кодировки консоли.

    На Windows вывод в перенаправленный файл идёт в локальной кодировке
    (cp1251/cp866), и символ вне её (``→``, ``≥``, эмодзи) даёт
    ``UnicodeEncodeError``. В фоновом потоке это исключение убивает поток
    целиком: сервис продолжает отвечать по HTTP, а очередь эмбеддингов стоит
    навсегда и молча. Поэтому печать здесь с запасным вариантом.
    """
    try:
        print(text, flush=True)
    except UnicodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        safe = text.encode(encoding, "replace").decode(encoding, "replace")
        with suppress(Exception):
            print(safe, flush=True)


def append_embed_error(log_path: Path | None, message: str) -> None:
    """Дописать строку ошибки в ``<home>/embed_errors.log`` (best-effort).

    Лог нужен, чтобы сбой был виден после факта: пользователь не сидит у
    консоли, а отсутствие папки/сайдкара замечает позже.
    """
    if log_path is None:
        return
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except (OSError, UnicodeError):
        pass  # лог не должен ронять сервис


__all__ = [
    "KIND_INDEX",
    "append_embed_error",
    "format_embed",
    "format_recovery",
    "print_stats",
    "say",
]
