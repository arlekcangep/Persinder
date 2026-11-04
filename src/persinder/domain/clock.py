"""Единый источник времени (ядро)."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> str:
    """Текущее время в UTC в формате ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
