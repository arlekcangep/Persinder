"""Модуль `sources`: watcher областей, экстракторы, конфиг, конвейер."""

from .config import ConfigError, load_config
from .extract import (
    Extracted,
    Extractor,
    ExtractorRegistry,
    HtmlExtractor,
    MarkdownExtractor,
    MhtmlExtractor,
    PdfExtractor,
    TextExtractor,
    default_registry,
    extract_html,
)
from .journal import Journal, JournalEvent
from .models import (
    AreaConfig,
    AreaScan,
    ChangeEvent,
    ChangeKind,
    ScanResult,
    WatchConfig,
)
from .pipeline import IndexingPipeline, PipelineStats
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
    "Extractor",
    "Extracted",
    "ExtractorRegistry",
    "IndexingPipeline",
    "HtmlExtractor",
    "Journal",
    "JournalEvent",
    "MarkdownExtractor",
    "MhtmlExtractor",
    "PdfExtractor",
    "PipelineStats",
    "ScanResult",
    "TextExtractor",
    "Watcher",
    "WatchConfig",
    "default_registry",
    "extract_html",
    "load_config",
]
