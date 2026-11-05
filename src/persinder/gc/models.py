"""Модель результата сборки сирот.

Отчёт сценария ``gc``: чистые данные, без зависимости от файловой системы и БД.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GcStats:
    """Итоги сборки сирот."""

    scanned: int = 0
    removed: int = 0
    missing: int = 0
