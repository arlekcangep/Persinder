"""Экстракторы: файл области → текст и метаданные для `ingest`.

Архитектура (двухслойная: классификаторы + экстракторы):

- **Классификаторы** (``classify``) определяют класс файла **по содержимому**
  (magic-байты / снифф первых KB); для записей архивов — по байтам.
- **Экстрактор** (``Extractor``) объявляет свой класс-семью (``format_class``),
  подтипы (``subtypes``, например ``markdown``/``html`` внутри
  ``structure-text``) и умеет отвечать — ``responds(extension, matches)``.
- **Реестр** ``ExtractorRegistry`` при обработке файла **опрашивает все
  экстракторы**, передавая им расширение и класс контента; откликнувшиеся
  запускаются **один за другим**. Конфиг области (``AreaConfig.formats``) —
  белый список **классов**.
- Контейнер (``ArchiveExtractor``) раскрывает записи через ту же
  классификацию по байтам (``_AreaEntryResolver``).

Минимальный набор: Markdown/текстовые заметки Obsidian (тэги не дублируются),
HTML/MHTML, PDF (extra ``pdf``), FB2 и EPUB (stdlib: xml/zipfile), а также
архивы (zip/7z/tar/gz/rar) — контейнер, каждая запись — отдельная часть,
вложенные архивы раскрываются на один уровень. OCR/код — будущие классы.
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import re
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .classify import (
    ARCHIVE,
    PUB_FORMATS,
    RESERVED,
    STRUCTURE_TEXT,
    TEXT,
    ClassifierSet,
    ClassMatch,
    default_classifiers,
)
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
    """Результат извлечения одной единицы (файла или части контейнера).

    ``name`` — путь части внутри исходного файла (например записи ZIP:
    ``"dir/inner.md"``); пусто — сам исходный файл.
    ``extractor`` — имя экстрактора, который дал этот результат (пишется в БД).
    ``error`` — непустой при проблеме разбора; текст в этом случае пуст.
    """

    text: str
    tags: tuple[str, ...] = ()
    pages: int = 0
    error: str | None = None
    name: str = ""
    extractor: str = ""


@runtime_checkable
class Extractor(Protocol):
    """Порт экстрактора: класс + подтипы + ответ на опрос реестра + извлечение."""

    name: str
    format_class: str

    def suffixes(self) -> frozenset[str]:
        """Расширения (нижний регистр, с точкой), например ``.md`` — фолбэк."""
        ...

    def responds(self, extension: str, matches: tuple[ClassMatch, ...]) -> bool:
        """Отклик на опрос реестра: обработает ли этот файл."""
        ...

    def extract(self, path: Path) -> Extracted:
        """Извлечь текст и метаданные из файла."""
        ...


@runtime_checkable
class ContainerExtractor(Protocol):
    """Экстрактор-контейнер: раскрывает файл в несколько частей.

    В отличие от ``Extractor`` (один файл → один результат), контейнер отдаёт
    список ``Extracted``. Экстракторы для частей он берёт из резолвера
    (``_AreaEntryResolver``), который классифицирует записи по байтам.
    """

    name: str
    format_class: str

    def suffixes(self) -> frozenset[str]:
        """Расширения файлов-контейнеров (в нижнем регистре, с точкой)."""
        ...

    def responds(self, extension: str, matches: tuple[ClassMatch, ...]) -> bool:
        """Отклик на опрос реестра: этот ли контейнер."""
        ...

    def extract_all(self, path: Path, resolver: _AreaEntryResolver) -> list[Extracted]:
        """Раскрыть контейнер в список частей."""
        ...


class BaseExtractor:
    """Общая логика отклика для обычных экстракторов.

    Отклик, если: (1) назван наш класс **и** подтип совпадает/не назван —
    точное попадание; (2) классификатор ничего не назвал **или** назвал лишь
    общий ``text`` (контент неразличим) — тогда расширение выбирает подтип
    внутри класса (например ``.md`` → markdown, ``.mht`` → mhtml). Уверенный
    чужой класс расширением не перекрывается (``.txt`` с телом %PDF- — это
    pdf, а не текст).
    """

    format_class: str = TEXT
    subtypes: frozenset[str] = frozenset()

    def suffixes(self) -> frozenset[str]:
        """Расширения (нижний регистр, с точкой) — фолбэк при неразличимом контенте."""
        raise NotImplementedError

    def responds(self, extension: str, matches: tuple[ClassMatch, ...]) -> bool:
        for match in matches:
            if match.format_class != self.format_class:
                continue
            if not match.subtype or match.subtype in self.subtypes:
                return True
        if not matches or _generic_text_only(matches):
            return extension in self.suffixes()
        return False


def _generic_text_only(matches: tuple[ClassMatch, ...]) -> bool:
    """Все совпадения — лишь общий «просто текст» (без подтипа)."""
    return bool(matches) and all(
        match.format_class == TEXT and match.subtype == "text" for match in matches
    )


class MarkdownExtractor(BaseExtractor):
    """Заметки Obsidian: Markdown + frontmatter + inline-тэги (класс structure-text)."""

    name = "markdown"
    format_class = STRUCTURE_TEXT
    subtypes = frozenset({"markdown"})

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
            extractor=self.name,
        )


class TextExtractor(BaseExtractor):
    """Простой текст (без frontmatter-логики), класс ``text``."""

    name = "text"
    format_class = TEXT
    subtypes = frozenset({"text"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".txt"})

    def extract(self, path: Path) -> Extracted:
        return Extracted(
            text=path.read_text(encoding="utf-8", errors="replace"),
            extractor=self.name,
        )


class HtmlExtractor(BaseExtractor):
    """HTML → текст (стандартная библиотека), класс ``structure-text``."""

    name = "html"
    format_class = STRUCTURE_TEXT
    subtypes = frozenset({"html"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".html", ".htm"})

    def extract(self, path: Path) -> Extracted:
        raw = path.read_text(encoding="utf-8", errors="replace")
        return dataclasses.replace(extract_html(raw), extractor=self.name)


class MhtmlExtractor(BaseExtractor):
    """MHTML (``.mht``) — MIME-посылка, стандартная библиотека."""

    name = "mhtml"
    format_class = STRUCTURE_TEXT
    subtypes = frozenset({"mhtml"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".mht", ".mhtml"})

    def extract(self, path: Path) -> Extracted:
        import email
        from email import policy

        try:
            msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
            html: str | None = None
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_content()
                    html = payload if isinstance(payload, str) else None
                    break
        except Exception as exc:  # noqa: BLE001 — повреждённая MIME-посылка
            return Extracted(text="", error=f"mhtml: {exc}", extractor=self.name)
        if html is None:
            # Не multipart: иногда это просто HTML с MIME-заголовками.
            raw = path.read_text(encoding="utf-8", errors="replace")
            html = raw
        return dataclasses.replace(extract_html(html), extractor=self.name)


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


class PdfExtractor(BaseExtractor):
    """PDF через pypdf (extra ``pdf``), класс ``pub-formats``."""

    name = "pdf"
    format_class = PUB_FORMATS
    subtypes = frozenset({"pdf"})

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
            return Extracted(text="", tags=(), error=f"pdf: {exc}", extractor=self.name)
        result = Extracted(
            text="\n\n".join(p for p in pages if p),
            pages=len(pages),
            extractor=self.name,
        )
        return result


class Fb2Extractor(BaseExtractor):
    """FB2 (FictionBook XML) — стандартная библиотека, класс ``pub-formats``.

    Текст берётся из тела документа (``body``), границы абзацев — переносы;
    тэги извлекаются из ``<tag>`` внутри текста.
    """

    name = "fb2"
    format_class = PUB_FORMATS
    subtypes = frozenset({"fb2"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".fb2"})

    def extract(self, path: Path) -> Extracted:
        return self._extract_xml(path.read_bytes())

    def _extract_xml(self, data: bytes) -> Extracted:
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(data)
        except Exception as exc:  # noqa: BLE001 — повреждённый XML
            return Extracted(text="", error=f"fb2: {exc}", extractor=self.name)
        paragraphs: list[str] = []
        for body_tag in root.iter():
            if not body_tag.tag.endswith("}body"):
                continue
            for para in body_tag.iter():
                if para.tag.endswith(("}p", "}poem", "}subtitle", "}title")):
                    piece: list[str] = []
                    _collect_text(para, piece)
                    joined = "".join(piece).strip()
                    if joined:
                        paragraphs.append(joined)
        tags = tuple(dict.fromkeys(t.strip() for t in text_tag_iter(root)))
        text = "\n\n".join(paragraphs)
        if not text:
            return Extracted(
                text="", error="fb2: не найдено тело документа", extractor=self.name
            )
        return Extracted(text=text, tags=tags, extractor=self.name)


def _collect_text(elem: Any, out: list[str]) -> None:
    """Рекурсивно собрать текст элемента и его потомков (с хвостами)."""
    if elem.text:
        out.append(elem.text)
    for child in elem:
        _collect_text(child, out)
        if child.tail:
            out.append(child.tail)


def text_tag_iter(elem: Any) -> Iterable[str]:
    """Все тексты внутренних ``<tag>`` (не итератор-тегов ``tag``)."""
    for node in elem.iter():
        if node.tag.endswith("}tag") and node.text:
            yield node.text


class EpubExtractor(BaseExtractor):
    """EPUB — zip с OPF-навигацией (stdlib), класс ``pub-formats``.

    Через ``container.xml`` находим манифест (OPF), порядок чтения — spine;
    HTML-части прогоняются через общий ``extract_html`` и склеиваются.
    """

    name = "epub"
    format_class = PUB_FORMATS
    subtypes = frozenset({"epub"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".epub"})

    def extract(self, path: Path) -> Extracted:
        try:
            with zipfile.ZipFile(path) as archive:
                return self._extract_zip(archive)
        except Exception as exc:  # noqa: BLE001 — битый epub/zip
            return Extracted(text="", error=f"epub: {exc}", extractor=self.name)

    def _extract_zip(self, archive: Any) -> Extracted:
        names = set(archive.namelist())
        opf_path = _epub_opf_path(archive)
        spine = (
            _epub_spine_hrefs(archive, opf_path)
            if opf_path and opf_path in names
            else []
        )
        if not spine:
            spine = [
                n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))
            ]
        parts: list[str] = []
        for href in spine:
            target = _resolve_opf_path(opf_path, href) if opf_path else href
            if target not in names:
                continue
            raw_html = archive.read(target)
            text = raw_html.decode("utf-8", errors="replace")
            result = extract_html(text)
            if result.text:
                parts.append(result.text)
        if not parts:
            return Extracted(
                text="", error="epub: нет текстовых частей", extractor=self.name
            )
        return Extracted(text="\n\n".join(parts), extractor=self.name)


def _epub_opf_path(archive: Any) -> str | None:
    import xml.etree.ElementTree as ET

    try:
        names = set(archive.namelist())
        container_name = next(n for n in names if n.lower() == "meta-inf/container.xml")
        container = ET.fromstring(archive.read(container_name))
    except (StopIteration, KeyError, ET.ParseError):
        return None
    for rootfile in container.iter():
        if rootfile.tag.endswith("}rootfile"):
            return rootfile.attrib.get("full-path") or None
    return None


def _epub_spine_hrefs(archive: Any, opf_path: str) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        opf = ET.fromstring(archive.read(opf_path))
    except (KeyError, ET.ParseError):
        return []
    manifest: dict[str, str] = {}
    for item in opf.iter():
        if item.tag.endswith("}item") and "id" in item.attrib and "href" in item.attrib:
            manifest[item.attrib["id"]] = item.attrib["href"]
    spine: list[str] = []
    for ref in opf.iter():
        if ref.tag.endswith("}itemref") and "idref" in ref.attrib:
            href = manifest.get(ref.attrib["idref"])
            if href:
                spine.append(href)
    return spine


def _resolve_opf_path(opf_path: str, href: str) -> str:
    from urllib.parse import unquote, urlparse

    href = unquote(urlparse(href).path)
    base = Path(opf_path).parent
    if base.as_posix() in ("", "."):
        return href
    return (base / href).as_posix()


# Пределы распаковки архивов — защита от архивов-бомб (много записей / огромные).
ARCHIVE_MAX_ENTRIES = 1000
ARCHIVE_MAX_ENTRY_BYTES = 32 * 1024 * 1024
ARCHIVE_MAX_TOTAL_BYTES = 64 * 1024 * 1024

# Составные суффиксы проверяются до обычных: tar.gz/tgz — один tar-контейнер.
COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz")
TGZ_SUFFIXES = (".tgz", ".tbz2", ".txz")


def archive_suffix(name: str) -> str:
    """Суффикс записи с учётом составных: ``a.tar.gz`` → ``.tar.gz``."""
    low = name.lower()
    for compound in (*COMPOUND_SUFFIXES, *TGZ_SUFFIXES):
        if low.endswith(compound):
            return compound
    return Path(low).suffix


class ArchiveExtractor(BaseExtractor):
    """Архивы (zip/7z/tar/gz/rar) — контейнер, класс ``archive``.

    Каждая запись — отдельная часть; экстрактор для записи выбирает резолвер
    (``_AreaEntryResolver``) **по содержимому записи** (магия/снифф байтов),
    расширение — лишь хинт. Записи-архивы раскрываются **на один уровень**
    (глубже — защита от бомб и рекурсии). ``tar.gz``/``tgz`` — один tar-уровень,
    одиночный ``.gz`` — один «файл» без суффикса. Пределы анти-бомб: число
    записей, размер записи, суммарный размер. Битая запись не валит архив
    (часть с ``error``), битый архив — одна часть с ошибкой. ``7z`` требует
    extra ``7z`` (py7zr), ``rar`` — extra ``rar`` (rarfile + внешний распаковщик).
    """

    name = "archive"
    format_class = ARCHIVE
    subtypes = frozenset({"zip", "7z", "tar", "gzip", "rar"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".zip", ".7z", ".tar", ".gz", ".tgz", ".rar"})

    # --- читатели форматов: путь → [(имя записи, байты)] ---

    @staticmethod
    def _read_zip(path: Path) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                out.append(
                    (
                        info.filename.replace("\\", "/"),
                        archive.read(info),
                    )
                )
        return out

    @staticmethod
    def _read_7z(path: Path) -> list[tuple[str, bytes]]:
        try:
            import py7zr  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
        except ImportError as exc:  # extra '7z' не установлен
            raise RuntimeError(
                "формат 7z требует extra '7z': pip install 'persinder[archive7z]'"
            ) from exc
        out: list[tuple[str, bytes]] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            py7zr.SevenZipFile(path, "r") as archive,
        ):
            archive.extractall(path=tmp)
            root = Path(tmp)
            for name in archive.getnames():
                file = root / name
                if file.is_file():
                    out.append((name.replace("\\", "/"), file.read_bytes()))
        return out

    @staticmethod
    def _read_tar(path: Path) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                out.append((member.name.replace("\\", "/"), handle.read()))
        return out

    @staticmethod
    def _read_gz(path: Path) -> list[tuple[str, bytes]]:
        data = gzip.decompress(path.read_bytes())
        # имя без последнего суффикса: notes.txt.gz → notes.txt
        stem = (
            path.name[: -len(".gz")] if path.name.lower().endswith(".gz") else path.name
        )
        return [(stem, data)]

    def _read_rar(self, path: Path) -> list[tuple[str, bytes]]:
        try:
            import rarfile  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
        except ImportError as exc:  # extra 'rar' не установлен
            raise RuntimeError(
                "формат rar требует extra 'rar': pip install 'persinder[archiverar]'"
            ) from exc
        out: list[tuple[str, bytes]] = []
        archive: Any = rarfile.RarFile(path)  # pyright: ignore[reportUnknownMemberType]
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = str(info.filename).replace("\\", "/")
                out.append((name, bytes(archive.read(info))))
        return out

    def _reader(self, suffix: str) -> Callable[[Path], list[tuple[str, bytes]]] | None:
        readers: dict[str, Callable[[Path], list[tuple[str, bytes]]]] = {
            ".zip": self._read_zip,
            ".tar": self._read_tar,
            ".tar.gz": self._read_tar,
            ".tar.bz2": self._read_tar,
            ".tar.xz": self._read_tar,
            ".tgz": self._read_tar,
            ".tbz2": self._read_tar,
            ".txz": self._read_tar,
            ".gz": self._read_gz,
            ".7z": self._read_7z,
            ".rar": self._read_rar,
        }
        return readers.get(suffix)

    # --- главный вход контейнера ---

    def extract_all(self, path: Path, resolver: _AreaEntryResolver) -> list[Extracted]:
        try:
            suffix = archive_suffix(path.name)
            reader = self._reader(suffix)
            if reader is None:
                return [
                    Extracted(text="", error=f"archive: формат {suffix} не читается")
                ]
            entries = reader(path)
        except RuntimeError as exc:
            return [Extracted(text="", error=str(exc))]
        except Exception as exc:  # noqa: BLE001 — битый/нечитаемый архив
            return [Extracted(text="", error=f"archive: {exc}")]
        return self._expand_entries(entries, resolver)

    def _expand_entries(
        self,
        entries: list[tuple[str, bytes]],
        resolver: _AreaEntryResolver,
    ) -> list[Extracted]:
        """Записи архива → части (классификация по байтам); вложенные на 1 уровень."""
        parts: list[Extracted] = []
        total = 0
        for index, (name, data) in enumerate(entries):
            if index >= ARCHIVE_MAX_ENTRIES:
                break
            suffix = archive_suffix(name)
            if total + len(data) > ARCHIVE_MAX_TOTAL_BYTES:
                break
            if (
                self._reader(suffix) is not None
                and len(data) <= ARCHIVE_MAX_ENTRY_BYTES
            ):
                # Вложенный архив по имени — раскрыть его записи как части (глубина 1).
                parts.extend(self._nested_parts(name, data, resolver))
                total += len(data)
                continue
            if len(data) > ARCHIVE_MAX_ENTRY_BYTES:
                continue
            total += len(data)
            extractors = resolver.extractors_for(name, data)
            if not extractors:
                continue  # формат не поддержан/класс не в конфиге области
            multi = len(extractors) > 1
            for extractor in extractors:
                part = self._part_from_bytes(extractor, name, data)
                if multi:
                    # Несколько экстракторов на одну запись: имена частей
                    # различаем по экстрактору, чтобы источники не конфликтовали.
                    part = dataclasses.replace(part, name=f"{name}/{extractor.name}")
                parts.append(part)
        return parts

    def _nested_parts(
        self, name: str, data: bytes, resolver: _AreaEntryResolver
    ) -> list[Extracted]:
        """Раскрыть вложенный архив (ровно один уровень глубже)."""
        with tempfile.TemporaryDirectory() as tmp:
            inner_path = Path(tmp) / Path(name).name
            inner_path.write_bytes(data)
            try:
                reader = self._reader(archive_suffix(name))
                assert reader is not None
                nested_entries = reader(inner_path)
            except Exception as exc:  # noqa: BLE001 — битый вложенный архив
                return [Extracted(text="", name=name, error=f"archive: {exc}")]
        parts: list[Extracted] = []
        total = 0
        for index, (entry_name, entry_data) in enumerate(nested_entries):
            if index >= ARCHIVE_MAX_ENTRIES:
                break
            if total + len(entry_data) > ARCHIVE_MAX_TOTAL_BYTES:
                break
            if len(entry_data) > ARCHIVE_MAX_ENTRY_BYTES:
                continue
            total += len(entry_data)
            extractors = resolver.extractors_for(entry_name, entry_data)
            if not extractors:
                continue  # вложенные архивы дальше не раскрываются
            full = f"{name}/{entry_name}"
            multi = len(extractors) > 1
            for extractor in extractors:
                part = self._part_from_bytes(extractor, full, entry_data)
                if multi:
                    part = dataclasses.replace(part, name=f"{full}/{extractor.name}")
                parts.append(part)
        return parts

    @staticmethod
    def _part_from_bytes(extractor: Extractor, name: str, data: bytes) -> Extracted:
        """Запись архива → временный файл → Extracted с именем части."""
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / ("entry" + archive_suffix(name))
            entry.write_bytes(data)
            try:
                result = extractor.extract(entry)
            except Exception as exc:  # noqa: BLE001 — битая запись не валит архив
                return Extracted(
                    text="",
                    name=name,
                    error=f"archive entry: {exc}",
                    extractor=extractor.name,
                )
        return Extracted(
            text=result.text,
            tags=result.tags,
            pages=result.pages,
            error=result.error,
            name=name,
            extractor=extractor.name,
        )


class _AreaEntryResolver:
    """Отбор экстракторов для записей контейнера: классификация по байтам.

    Не «карта суффикс → экстрактор», а опрос через ту же схему, что и у
    реестра: содержимое записи (+ имя как хинт) → класс/подтип → отклики.
    """

    def __init__(
        self,
        registry: ExtractorRegistry,
        area: AreaConfig,
        classifiers: ClassifierSet,
    ) -> None:
        self._registry = registry
        self._area = area
        self._classifiers = classifiers

    def extractors_for(self, name: str, data: bytes) -> list[Extractor]:
        extension = archive_suffix(name)
        matches = self._classifiers.classify(data, extension)
        return self._registry.leaf_responders(extension, matches, self._area)


class ExtractorRegistry:
    """Опрос всех экстракторов с учётом классов конфига области.

    Экстракторы (``Extractor``) и контейнеры (``ContainerExtractor``)
    регистрируются явно. При обработке файла реестр (1) классифицирует
    содержимое (``classify``), (2) **опрашивает все экстракторы**, передавая
    расширение и совпадения классов, (3) запускает откликнувшихся по очереди.
    Список классов области (``AreaConfig.formats``) — белый список: экстрактор
    работает, только если его ``format_class`` в нём.
    """

    def __init__(
        self,
        extractors: tuple[Extractor, ...],
        containers: tuple[ContainerExtractor, ...] = (),
        classifiers: ClassifierSet | None = None,
    ) -> None:
        if not extractors and not containers:
            raise ValueError("реестр экстракторов пуст")
        self._extractors: tuple[Extractor, ...] = extractors
        self._containers: tuple[ContainerExtractor, ...] = containers
        self._names: set[str] = set()
        self._classes: set[str] = set()
        all_extractors: tuple[Extractor | ContainerExtractor, ...] = (
            *extractors,
            *containers,
        )
        for extractor in all_extractors:
            if extractor.name in self._names:
                raise ValueError(f"имя экстрактора {extractor.name} занято")
            self._names.add(extractor.name)
            self._classes.add(extractor.format_class)
        self._classifiers = classifiers or default_classifiers()

    def known_names(self) -> frozenset[str]:
        """Доступные в конфиге имена: классы + легаси-имена экстракторов.

        Возвращается общее множество, чтобы старые конфиги (``formats:
        [markdown, …]``) продолжали валидироваться; ``load_config`` нормализует
        легаси-имена в классы (см. ``CLASS_ALIASES``).
        """
        return frozenset(self._classes | RESERVED | set(CLASS_ALIASES))

    def known_classes(self) -> frozenset[str]:
        """Классы этого реестра (без легаси-алиасов)."""
        return frozenset(self._classes | RESERVED)

    # --- опрос ---

    def leaf_responders(
        self,
        extension: str,
        matches: tuple[ClassMatch, ...],
        area: AreaConfig,
    ) -> list[Extractor]:
        """Обычные экстракторы области, откликнувшиеся на (расширение + классы)."""
        enabled = self._enabled_classes(area)
        return [
            extractor
            for extractor in self._extractors
            if extractor.format_class in enabled
            and extractor.responds(extension, matches)
        ]

    def container_responder(
        self,
        extension: str,
        matches: tuple[ClassMatch, ...],
        area: AreaConfig,
    ) -> ContainerExtractor | None:
        """Контейнер области, откликнувшийся на (расширение + классы)."""
        enabled = self._enabled_classes(area)
        for container in self._containers:
            if container.format_class in enabled and container.responds(
                extension, matches
            ):
                return container
        return None

    def entry_resolver(self, area: AreaConfig) -> _AreaEntryResolver:
        """Резолвер экстракторов для записей контейнера (по содержимому записи)."""
        return _AreaEntryResolver(self, area, self._classifiers)

    @staticmethod
    def _enabled_classes(area: AreaConfig) -> frozenset[str]:
        """Классы области с нормализацией легаси-имён экстракторов.

        ``AreaConfig.formats`` создаётся из конфига уже классами (``load_config``
        применяет ``CLASS_ALIASES``); но область могут построить напрямую со
        старыми именами (``markdown``, ``html``, ``pdf``…) — здесь они
        приводятся к классам, чтобы конфиги до перехода продолжали работать.
        """
        return frozenset(CLASS_ALIASES.get(f, f) for f in area.formats)

    # --- main вход: файл области → части ---

    def extract(self, path: Path, area: AreaConfig) -> list[Extracted] | None:
        """Извлечь файл: классификация по содержимому → опрос → запуск.

        Результат — части (обычный файл — одна часть, контейнер — по части на
        запись); ``None`` — ничего не откликнулось (класс не в конфиге либо
        формат не поддержан).
        """
        extension = path.suffix.lower()
        matches = self._classifiers.classify_path(path)
        container = self.container_responder(extension, matches, area)
        extractors = self.leaf_responders(extension, matches, area)

        all_responders = len(extractors) + (1 if container else 0)
        parts: list[Extracted] = []
        if container is not None:
            resolver = _AreaEntryResolver(self, area, self._classifiers)
            parts.extend(container.extract_all(path, resolver))
        for extractor in extractors:
            result = extractor.extract(path)
            if all_responders > 1:
                # Несколько экстракторов на один файл: имя части — экстрактор,
                # чтобы части не конфликтовали за один источник (см. _part_name).
                result = dataclasses.replace(result, name=result.name or extractor.name)
            parts.append(result)
        if not parts:
            return None
        return parts


# Легаси-алиасы: старые имена экстракторов в конфиге → классы.
CLASS_ALIASES: dict[str, str] = {
    "markdown": STRUCTURE_TEXT,
    "html": STRUCTURE_TEXT,
    "mhtml": STRUCTURE_TEXT,
    "text": TEXT,
    "pdf": PUB_FORMATS,
    "archive": ARCHIVE,
    "fb2": PUB_FORMATS,
    "epub": PUB_FORMATS,
}


def default_registry() -> ExtractorRegistry:
    """Стандартный набор: text + structure-text + pub-formats + archive.

    PDF добавляется, только если установлен extra ``pdf`` (pypdf). FB2/EPUB —
    стандартная библиотека. Классы ``ocr``/``code``/``science`` зарезервированы
    в ``known_names``, но экстракторов у них нет — файлы таких классов просто
    не индексируются, пока не будут реализованы.
    """
    extractors: list[Extractor] = [
        MarkdownExtractor(),
        TextExtractor(),
        HtmlExtractor(),
        MhtmlExtractor(),
        Fb2Extractor(),
        EpubExtractor(),
    ]
    with contextlib.suppress(RuntimeError):
        extractors.append(PdfExtractor())
    containers: tuple[ContainerExtractor, ...] = (ArchiveExtractor(),)
    return ExtractorRegistry(tuple(extractors), containers)


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
