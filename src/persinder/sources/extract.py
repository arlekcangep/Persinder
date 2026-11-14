"""Экстракторы: файл области → текст и метаданные для `ingest`.

Реализация вынесена в пакет ``extractors`` (интерфейсы — ``interfaces.py``,
база — ``extractors/_base.py``, реестр — ``extractors/registry.py``, каждый
экстрактор — отдельный модуль). Этот файл — публичная точка входа:
реэкспортирует API пакета, чтобы существующие импорты
(``from persinder.sources.extract import ...``) продолжали работать.
"""

from .extractors import (
    ARCHIVE_MAX_DEPTH,
    ARCHIVE_MAX_ENTRIES,
    ARCHIVE_MAX_ENTRY_BYTES,
    ARCHIVE_MAX_TOTAL_BYTES,
    CLASS_ALIASES,
    COMPOUND_SUFFIXES,
    TGZ_SUFFIXES,
    ArchiveExtractor,
    BaseExtractor,
    ChmExtractor,
    EpubExtractor,
    Extracted,
    ExtractorRegistry,
    Fb2Extractor,
    HtmlExtractor,
    MarkdownExtractor,
    MhtmlExtractor,
    PdfExtractor,
    RtfExtractor,
    TextExtractor,
    archive_suffix,
    dedup_tags,
    default_registry,
    extract_html,
    extract_tags,
    strip_code_inline,
)
from .interfaces import ContainerExtractor, Extractor

__all__ = [
    "ARCHIVE_MAX_DEPTH",
    "ARCHIVE_MAX_ENTRIES",
    "ARCHIVE_MAX_ENTRY_BYTES",
    "ARCHIVE_MAX_TOTAL_BYTES",
    "CLASS_ALIASES",
    "COMPOUND_SUFFIXES",
    "TGZ_SUFFIXES",
    "ArchiveExtractor",
    "BaseExtractor",
    "ChmExtractor",
    "ContainerExtractor",
    "EpubExtractor",
    "Extracted",
    "Extractor",
    "ExtractorRegistry",
    "Fb2Extractor",
    "HtmlExtractor",
    "MarkdownExtractor",
    "MhtmlExtractor",
    "PdfExtractor",
    "RtfExtractor",
    "TextExtractor",
    "archive_suffix",
    "dedup_tags",
    "default_registry",
    "extract_html",
    "extract_tags",
    "strip_code_inline",
]
