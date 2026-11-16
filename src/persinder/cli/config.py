"""Где искать конфиг профиля и какие модели считаются известными."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..embed import PLACEHOLDER, installed_keys


def known_models(settings: Settings) -> frozenset[str]:
    """Установленные модели профиля + встроенная заглушка (для валидации)."""
    return frozenset(installed_keys(settings.models_dir)) | {PLACEHOLDER}


def config_candidates(settings: Settings) -> tuple[Path, ...]:
    """Где искать конфиг, если ``--config`` не задан: папка запуска, затем home."""
    names = ("persinder.yaml", "persinder.yml")
    return tuple(Path.cwd() / name for name in names) + tuple(
        settings.home / name for name in names
    )


def candidate_config(settings: Settings) -> Path | None:
    """Первый существующий конфиг из стандартных мест (``--config`` не задан)."""
    for candidate in config_candidates(settings):
        if candidate.is_file():
            return candidate
    return None
