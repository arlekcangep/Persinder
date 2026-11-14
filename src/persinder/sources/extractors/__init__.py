"""Экстракторы: файл области → текст и метаданные для `ingest`.

Пакет разбивает бывший ``extract.py`` на слои:

- ``interfaces`` — порты экстрактора и контейнера;
- ``_base`` — результат извлечения (``Extracted``) и общая логика отклика;
- ``_common`` — общие помощники (HTML→текст, тэги, суффиксы архивов);
- по одному модулю на экстрактор (``markdown``, ``html``, ``pdf``, ...);
- ``registry`` — реестр экстракторов и фабрика ``default_registry``.

Публичный API проекта не меняется: ``src/persinder/sources/__init__.py``
импортирует всё из ``extract.py`` — тонкой обёртки над этим пакетом.
"""

from ._base import BaseExtractor, Extracted
from ._common import (
    ARCHIVE_MAX_DEPTH,
    ARCHIVE_MAX_ENTRIES,
    ARCHIVE_MAX_ENTRY_BYTES,
    ARCHIVE_MAX_TOTAL_BYTES,
    COMPOUND_SUFFIXES,
    TGZ_SUFFIXES,
    archive_suffix,
    dedup_tags,
    extract_html,
    extract_tags,
    strip_code_inline,
)
from .archive import ArchiveExtractor
from .chm import ChmExtractor
from .epub import EpubExtractor
from .fb2 import Fb2Extractor
from .html import HtmlExtractor
from .markdown import MarkdownExtractor
from .mhtml import MhtmlExtractor
from .pdf import PdfExtractor
from .registry import CLASS_ALIASES, ExtractorRegistry, default_registry
from .rtf import RtfExtractor
from .text import TextExtractor

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
    "EpubExtractor",
    "Extracted",
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
