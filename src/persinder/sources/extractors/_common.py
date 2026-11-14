"""Общие помощники экстракторов: HTML→текст, тэги, суффиксы архивов.

Функции и константы, которые используют несколько экстракторов:
разбор HTML, тэги (inline + frontmatter), суффиксы архивов с учётом
составных (``.tar.gz``) и пределы анти-бомб для контейнеров.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ._base import Extracted

TAG_RE = re.compile(r"(?<![\w`])#([\w-]+)", re.UNICODE)

# Frontmatter --- ... --- в начале файла.
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Строки тэгов в frontmatter: tags: [a, b] / tags:\n  - a
FM_TAGS_INLINE_RE = re.compile(r"^tags:\s*\[(.*?)\]\s*$", re.MULTILINE | re.IGNORECASE)
FM_TAGS_LIST_RE = re.compile(
    r"^tags:\s*$\n((?:\s*-\s*.+\n?)+)", re.MULTILINE | re.IGNORECASE
)


def extract_html(raw: str) -> Extracted:
    """HTML-строка → текст и тэги (общий движок для html/mhtml/fb2-частей epub)."""
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        """Накопитель текста с пропусканием скриптов и блоковыми переносами."""

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.chunks: list[str] = []
            self._skip_depth = 0
            self.title: str = ""

        SKIP = {"script", "style", "noscript", "template"}
        BLOCK = {
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "pre",
            "table",
        }

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in self.SKIP:
                self._skip_depth += 1
            elif tag in self.BLOCK or tag == "br":
                self.chunks.append("\n")
            elif tag == "title":
                self._in_title = True

        def handle_endtag(self, tag: str) -> None:
            if tag in self.SKIP:
                self._skip_depth = max(0, self._skip_depth - 1)
            elif tag in self.BLOCK:
                self.chunks.append("\n")
            elif tag == "title":
                self._in_title = False

        def handle_data(self, data: str) -> None:
            if self._skip_depth:
                return
            if getattr(self, "_in_title", False):
                self.title += data
            else:
                self.chunks.append(data)

    parser = _Text()
    try:
        parser.feed(raw)
    except Exception as exc:  # noqa: BLE001 — повреждённая разметка
        return Extracted(text="", error=f"html: {exc}")

    # Схлопываем пробельные «простыни» между блоками.
    lines = [
        re.sub(r"[ \t\xa0]+", " ", ln).strip()
        for ln in "".join(parser.chunks).splitlines()
    ]
    text = "\n".join(ln for ln in lines if ln)
    tags = extract_tags(strip_code_inline(text), None)
    title = parser.title.strip()
    if title and title.lower() not in {t.lower() for t in tags}:
        tags = (title[:80], *tags)  # заголовок страницы — первым тэгом
    return Extracted(text=text, tags=tags)


ARCHIVE_MAX_ENTRIES = 1000
ARCHIVE_MAX_ENTRY_BYTES = 32 * 1024 * 1024
ARCHIVE_MAX_TOTAL_BYTES = 64 * 1024 * 1024
# Максимальная вложенность архивов (сколько раз рекурсивно раскрываем).
# ``folder/first.zip/folder/second.zip/file.pdf`` — глубина 2; 4 с запасом.
ARCHIVE_MAX_DEPTH = 4


COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz")
TGZ_SUFFIXES = (".tgz", ".tbz2", ".txz")


def archive_suffix(name: str) -> str:
    """Суффикс записи с учётом составных: ``a.tar.gz`` → ``.tar.gz``."""
    low = name.lower()
    for compound in (*COMPOUND_SUFFIXES, *TGZ_SUFFIXES):
        if low.endswith(compound):
            return compound
    return Path(low).suffix


def dedup_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Убрать дубликаты тэгов без учёта регистра, сохранив порядок появления."""
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        low = tag.lower()
        if low and low not in seen:
            seen.add(low)
            unique.append(tag)
    return tuple(unique)


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
    return dedup_tags(tags)


def strip_code_inline(body: str) -> str:
    """Убрать inline-код, чтобы `#` внутри бэктиков не считался тэгом."""
    return re.sub(r"`[^`]*`", "", body)
