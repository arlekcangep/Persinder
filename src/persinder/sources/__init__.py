"""Модуль `sources`: watcher областей, экстракторы, конфиг."""

from .config import ConfigError, load_config
from .extract import extract, extract_markdown, extract_tags
from .models import (
    AreaConfig,
    AreaScan,
    ChangeEvent,
    ChangeKind,
    ScanResult,
    WatchConfig,
)
from .scanner import BUILTIN_IGNORES, AreaScanner
from .watcher import Watcher

__all__ = [
    "AreaConfig",
    "AreaScan",
    "AreaScanner",
    "BUILTIN_IGNORES",
    "ChangeEvent",
    "ChangeKind",
    "ConfigError",
    "ScanResult",
    "Watcher",
    "WatchConfig",
    "extract",
    "extract_markdown",
    "extract_tags",
    "load_config",
]
