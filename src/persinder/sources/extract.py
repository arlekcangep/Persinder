"""Экстракторы: файл области → текст и метаданные для `ingest`.

Минимальный набор первого прототипа: Markdown/текстовые заметки Obsidian
с извлечением тэгов (D5: тэги остаются внутри заметок, извлекаются, не
дублируются). PDF/DOCX/EPUB — следующие шаги.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Тэг: #слово (буквы/цифры/подчёркивание/дефис), не внутри строки кода `` ` ``
# и не заголовок с `# ` (пробел после решётки).
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
    """Результат извлечения из одного файла."""

    text: str
    tags: tuple[str, ...]


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


def extract_markdown(path: Path) -> Extracted:
    """Извлечь текст и тэги из Markdown-заметки Obsidian."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm_match = FRONTMATTER_RE.match(raw)
    frontmatter: str | None = None
    body = raw
    if fm_match:
        frontmatter = fm_match.group(1)
        body = raw[fm_match.end() :]
    return Extracted(text=body, tags=extract_tags(strip_code_inline(body), frontmatter))


def extract_text(path: Path) -> Extracted:
    """Извлечь текст из простого текстового файла (без frontmatter-логики)."""
    return Extracted(text=path.read_text(encoding="utf-8", errors="replace"), tags=())


def extract(path: Path) -> Extracted | None:
    """Выбрать экстрактор по расширению; None — тип не поддержан."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return extract_markdown(path)
    if suffix in {".txt"}:
        return extract_text(path)
    return None


SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".txt"})
