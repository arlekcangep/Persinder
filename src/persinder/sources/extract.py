"""Экстракторы: файл области → текст и метаданные для `ingest`.

Архитектура:

- **Порт** ``Extractor`` (Protocol): умеет объявить поддерживаемые суффиксы
  и извлечь из файла текст + тэги.
- **Реестр** ``ExtractorRegistry``: сопоставляет суффикс → экстрактор с учётом
  белого списка форматов области (``AreaConfig.formats``).

Минимальный набор первого прототипа: Markdown/текстовые заметки Obsidian
с извлечением тэгов (D5: тэги остаются внутри заметок, извлекаются, не
дублируются). PDF/DOCX/EPUB/OCR — добавляются как новые реализации порта.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import AreaConfig

# Тэг: #слово (буквы/цифры/подчёркивание/дефис), не внутри inline-кода и
# не заголовок (пробел после решётки).
TAG_RE = re.compile(r"(?<![\w`])#([\w-]+)", re.UNICODE)

# Frontmatter --- ... --- в начале файла.
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Строки тэгов в frontmatter: tags: [a, b] / tags:\n  - a
FM_TAGS_INLINE_RE = re.compile(r"^tags:\s*\[(.*?)\]\s*$", re.MULTILINE | re.IGNORECASE)
FM_TAGS_LIST_RE = re.compile(
    r"^tags:\s*$\n((?:\s*-\s*.+\n?)+)", re.MULTILINE | re.IGNORECASE
)


@dataclass(frozen=True)
class Extracted:
    """Результат извлечения из одного файла.

    ``error`` — непустой при проблеме разбора; текст в этом случае пуст.
    """

    text: str
    tags: tuple[str, ...] = ()
    pages: int = 0
    error: str | None = None


@runtime_checkable
class Extractor(Protocol):
    """Порт экстрактора: имя + поддерживаемые суффиксы + извлечение."""

    name: str

    def suffixes(self) -> frozenset[str]:
        """Расширения файлов (в нижнем регистре, с точкой), например ``.md``."""
        ...

    def extract(self, path: Path) -> Extracted:
        """Извлечь текст и метаданные из файла."""
        ...


class MarkdownExtractor:
    """Заметки Obsidian: Markdown + frontmatter + inline-тэги."""

    name = "markdown"

    def suffixes(self) -> frozenset[str]:
        return frozenset({".md", ".markdown"})

    def extract(self, path: Path) -> Extracted:
        raw = path.read_text(encoding="utf-8", errors="replace")
        fm_match = FRONTMATTER_RE.match(raw)
        frontmatter: str | None = None
        body = raw
        if fm_match:
            frontmatter = fm_match.group(1)
            body = raw[fm_match.end() :]
        return Extracted(
            text=body,
            tags=extract_tags(strip_code_inline(body), frontmatter),
        )


class TextExtractor:
    """Простой текстовый файл (без frontmatter-логики)."""

    name = "text"

    def suffixes(self) -> frozenset[str]:
        return frozenset({".txt"})

    def extract(self, path: Path) -> Extracted:
        return Extracted(text=path.read_text(encoding="utf-8", errors="replace"))


class PdfExtractor:
    """PDF через pypdf (extra ``pdf``).

    Текст собирается постранично. Страницы без текстового слоя (сканы)
    дают пустой фрагмент — ошибки не возникает. OCR таких файлов — задача
    отдельного экстрактора. При ошибке разбора файла возвращается пустой
    результат с сообщением, чтобы одно битое тело не валило скан области.
    """

    name = "pdf"

    def __init__(self) -> None:
        try:
            import pypdf

            del pypdf  # доступность extra 'pdf' проверена
        except ImportError as exc:
            raise RuntimeError(
                "PdfExtractor требует extra 'pdf': pip install 'persinder[pdf]'"
            ) from exc

    def suffixes(self) -> frozenset[str]:
        return frozenset({".pdf"})

    def extract(self, path: Path) -> Extracted:
        try:
            from pypdf import PdfReader

            reader: Any = PdfReader(str(path))  # pyright: ignore[reportUnknownVariableType, reportMissingTypeStubs]
            raw_pages: list[Any] = list(reader.pages)
            pages: list[str] = []
            for page in raw_pages:
                text: object = page.extract_text()
                pages.append((str(text) if text else "").strip())
        except Exception as exc:  # noqa: BLE001 — любое повреждение PDF
            return Extracted(text="", tags=(), error=f"pdf: {exc}")
        return Extracted(text="\n\n".join(p for p in pages if p), pages=len(pages))


class ExtractorRegistry:
    """Сопоставление суффиксов экстракторам с учётом форматов области.

    Экстракторы регистрируются явно. Для области берутся только те, чьи имена
    входят в её белый список ``formats`` (см. ``AreaConfig``). Пустой список —
    область не индексируется.
    """

    def __init__(self, extractors: tuple[Extractor, ...]) -> None:
        if not extractors:
            raise ValueError("реестр экстракторов пуст")
        self._extractors: dict[str, Extractor] = {}
        for extractor in extractors:
            for suffix in extractor.suffixes():
                if suffix in self._extractors:
                    raise ValueError(
                        f"суффикс {suffix} обслуживают несколько экстракторов"
                    )
                self._extractors[suffix] = extractor
        self._names = {e.name for e in extractors}

    def known_names(self) -> frozenset[str]:
        """Доступные имена экстракторов (для проверки конфига)."""
        return frozenset(self._names)

    def for_area(self, area: AreaConfig) -> dict[str, Extractor]:
        """Экстракторы области: имя в её ``formats``, суффикс → экстрактор."""
        enabled = set(area.formats) & self._names
        return {
            suffix: extractor
            for suffix, extractor in self._extractors.items()
            if extractor.name in enabled
        }

    def extract(self, path: Path, area: AreaConfig) -> Extracted | None:
        """Извлечь файл с учётом форматов области; None — не поддержан."""
        suffix = path.suffix.lower()
        extractor = self.for_area(area).get(suffix)
        return extractor.extract(path) if extractor else None


def default_registry() -> ExtractorRegistry:
    """Стандартный набор: markdown + text; pdf — если установлен extra."""
    extractors: list[Extractor] = [MarkdownExtractor(), TextExtractor()]
    with contextlib.suppress(RuntimeError):
        extractors.append(PdfExtractor())
    return ExtractorRegistry(tuple(extractors))


def extract_tags(body: str, frontmatter: str | None) -> tuple[str, ...]:
    """Собрать тэги: inline ``#tag`` из тела + тэги из frontmatter."""
    tags: list[str] = [m.group(1) for m in TAG_RE.finditer(body)]
    if frontmatter:
        inline = FM_TAGS_INLINE_RE.search(frontmatter)
        if inline:
            tags.extend(t.strip().lstrip("#") for t in inline.group(1).split(","))
        else:
            listed = FM_TAGS_LIST_RE.search(frontmatter)
            if listed:
                for line in listed.group(1).splitlines():
                    item = line.strip().lstrip("-").strip()
                    if item:
                        tags.append(item.lstrip("#"))
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        low = tag.lower()
        if low and low not in seen:
            seen.add(low)
            unique.append(tag)
    return tuple(unique)


def strip_code_inline(body: str) -> str:
    """Убрать inline-код, чтобы `#` внутри бэктиков не считался тэгом."""
    return re.sub(r"`[^`]*`", "", body)
