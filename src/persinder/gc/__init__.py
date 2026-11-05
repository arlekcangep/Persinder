"""Сервис обслуживания: сборка сирот сырого текста."""

from .models import GcStats
from .service import MIN_ORPHAN_AGE_SECONDS, collect_orphans

__all__ = ["collect_orphans", "GcStats", "MIN_ORPHAN_AGE_SECONDS"]
