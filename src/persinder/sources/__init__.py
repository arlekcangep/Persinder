"""Модуль `sources`: watcher областей, экстракторы, конфиг, конвейер."""

from .config import ConfigError, load_config
from .extract import (
    ContainerExtractor,
    Extracted,
    Extractor,
    ExtractorFactory,
    ExtractorRegistry,
    HtmlExtractor,
    MarkdownExtractor,
    MhtmlExtractor,
    PdfExtractor,
    TextExtractor,
    ZipExtractor,
    default_registry,
    extract_html,
)
from .journal import JOURNAL_RETENTION_DAYS, Journal, JournalEvent
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
    "ContainerExtractor",
    "Extractor",
    "Extracted",
    "ExtractorFactory",
    "ExtractorRegistry",
    "IndexingPipeline",
    "HtmlExtractor",
    "JOURNAL_RETENTION_DAYS",
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
    "ZipExtractor",
    "default_registry",
    "extract_html",
    "load_config",
]
