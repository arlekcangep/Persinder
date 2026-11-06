"""Экстракторы: файл области → текст и метаданные для `ingest`.

Архитектура:

- **Порт** ``Extractor`` (Protocol): умеет объявить поддерживаемые суффиксы
  и извлечь из файла текст + тэги.
- **Реестр** ``ExtractorRegistry``: сопоставляет суффикс → экстрактор с учётом
  белого списка форматов области (``AreaConfig.formats``).

Минимальный набор первого прототипа: Markdown/текстовые заметки Obsidian
с извлечением тэгов (D5: тэги остаются внутри заметок, извлекаются, не
дублируются), HTML/MHTML, PDF, а также ZIP (``ZipExtractor`` — контейнер:
распаковывает архив, каждая запись идёт отдельной частью через фабрику
экстракторов области). DOCX/EPUB/OCR — добавляются как новые реализации порта.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
import zipfile
from collections.abc import Iterable
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
    """Результат извлечения одной единицы (файла или части контейнера).

    ``name`` — путь части внутри исходного файла (для контейнеров, например
    записи ZIP: ``"dir/inner.md"``); пусто — сам исходный файл.
    ``error`` — непустой при проблеме разбора; текст в этом случае пуст.
    """

    text: str
    tags: tuple[str, ...] = ()
    pages: int = 0
    error: str | None = None
    name: str = ""


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


@runtime_checkable
class ExtractorFactory(Protocol):
    """Источник экстракторов для контейнеров.

    Контейнер (ZIP) не держит готовую карту, а спрашивает фабрику — она
    учитывает область: для разных каталогов набор разрешённых экстракторов
    может отличаться.
    """

    def for_suffix(self, suffix: str) -> Extractor | None:
        """Экстрактор для суффикса (нижний регистр, с точкой) или ``None``."""
        ...


@runtime_checkable
class ContainerExtractor(Protocol):
    """Экстрактор-контейнер: раскрывает файл в несколько частей.

    В отличие от ``Extractor`` (один файл → один результат), контейнер отдаёт
    список ``Extracted`` — по одному на вложенную единицу. Экстракторы для
    частей он берёт из фабрики (``ExtractorFactory``).
    """

    name: str

    def suffixes(self) -> frozenset[str]:
        """Расширения файлов-контейнеров (в нижнем регистре, с точкой)."""
        ...

    def extract_all(self, path: Path, factory: ExtractorFactory) -> list[Extracted]:
        """Раскрыть контейнер в список частей."""
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


class HtmlExtractor:
    """HTML → текст, стандартная библиотека (``html.parser``).

    Вырезаются ``script``/``style``/``noscript``; блоки (p, div, заголовки,
    li, br, tr) дают переносы строк, чтобы слова не склеивались. Заголовок
    ``<title>`` идёт в результат как псевдо-тэг для отображения.
    """

    name = "html"

    def suffixes(self) -> frozenset[str]:
        return frozenset({".html", ".htm"})

    def extract(self, path: Path) -> Extracted:
        raw = path.read_text(encoding="utf-8", errors="replace")
        return extract_html(raw)


class MhtmlExtractor:
    """MHTML (сохранённые веб-страницы, ``.mht``) — стандартная библиотека.

    MHTML — это MIME-посылка: берём первую текстовую HTML-часть, локальные
    ссылки на ресурсы (cid:, images) вырезаются вместе с разметкой.
    """

    name = "mhtml"

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
            return Extracted(text="", error=f"mhtml: {exc}")
        if html is None:
            # Не multipart: иногда это просто HTML с MIME-заголовками.
            raw = path.read_text(encoding="utf-8", errors="replace")
            html = raw
        return extract_html(html)


def extract_html(raw: str) -> Extracted:
    """HTML-строка → текст и тэги (общий движок для html и mhtml)."""
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


# Пределы распаковки ZIP — защита от архивов-бомб (много записей / огромные).
ZIP_MAX_ENTRIES = 1000
ZIP_MAX_ENTRY_BYTES = 32 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class ZipExtractor:
    """ZIP-архив — контейнер: распаковывает и делегирует по суффиксу.

    Каждая поддерживаемая запись архива становится отдельной частью
    (``Extracted.name`` — путь внутри архива): штатный экстрактор берётся из
    фабрики (``ExtractorFactory``, с учётом области), вложенный архив не
    раскрывается (в фабрике нет ``zip``). Неподдерживаемые форматы и слишком
    большие записи пропускаются; лимиты по числу/сумме — от бомб. Битая запись
    не валит архив (часть с ``error``), битый архив — одна часть с ошибкой.
    """

    name = "zip"

    def suffixes(self) -> frozenset[str]:
        return frozenset({".zip"})

    def extract_all(self, path: Path, factory: ExtractorFactory) -> list[Extracted]:
        parts: list[Extracted] = []
        total = 0
        try:
            with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as tmp:
                for index, info in enumerate(archive.infolist()):
                    if index >= ZIP_MAX_ENTRIES:
                        break
                    if info.is_dir():
                        continue
                    name = info.filename.replace("\\", "/")
                    suffix = Path(name).suffix.lower()
                    extractor = factory.for_suffix(suffix)
                    if extractor is None:
                        continue  # формат не поддержан (в т.ч. вложенный zip)
                    if info.file_size > ZIP_MAX_ENTRY_BYTES:
                        continue
                    if total + info.file_size > ZIP_MAX_TOTAL_BYTES:
                        break
                    try:
                        data = archive.read(info)
                    except Exception:  # noqa: BLE001 — шифрование/битая запись
                        continue
                    total += len(data)
                    parts.append(
                        self._extract_entry(
                            extractor, Path(tmp) / f"{index}{suffix}", name, data
                        )
                    )
        except (zipfile.BadZipFile, OSError) as exc:
            return [Extracted(text="", error=f"zip: {exc}")]
        return parts

    @staticmethod
    def _extract_entry(
        extractor: Extractor, entry: Path, name: str, data: bytes
    ) -> Extracted:
        """Развернуть одну запись во временный файл и отдать экстрактору."""
        entry.write_bytes(data)
        try:
            result = extractor.extract(entry)
        except Exception as exc:  # noqa: BLE001 — битая запись не валит архив
            return Extracted(text="", name=name, error=f"zip entry: {exc}")
        return Extracted(
            text=result.text,
            tags=result.tags,
            pages=result.pages,
            error=result.error,
            name=name,
        )


class _AreaExtractors:
    """Фабрика экстракторов одной области: суффикс → экстрактор."""

    def __init__(self, mapping: dict[str, Extractor]) -> None:
        self._mapping = mapping

    def for_suffix(self, suffix: str) -> Extractor | None:
        return self._mapping.get(suffix.lower())


class ExtractorRegistry:
    """Сопоставление суффиксов экстракторам с учётом форматов области.

    Экстракторы (``Extractor``) и контейнеры (``ContainerExtractor``)
    регистрируются явно. Для области берутся только те, чьи имена входят в её
    белый список ``formats`` (см. ``AreaConfig``). Пустой список — область не
    индексируется.
    """

    def __init__(
        self,
        extractors: tuple[Extractor, ...],
        containers: tuple[ContainerExtractor, ...] = (),
    ) -> None:
        if not extractors and not containers:
            raise ValueError("реестр экстракторов пуст")
        self._extractors: dict[str, Extractor] = {}
        self._containers: dict[str, ContainerExtractor] = {}
        self._names: set[str] = set()
        for extractor in extractors:
            if extractor.name in self._names:
                raise ValueError(f"имя экстрактора {extractor.name} занято")
            for suffix in extractor.suffixes():
                if suffix in self._extractors or suffix in self._containers:
                    raise ValueError(
                        f"суффикс {suffix} обслуживают несколько экстракторов"
                    )
                self._extractors[suffix] = extractor
            self._names.add(extractor.name)
        for container in containers:
            if container.name in self._names:
                raise ValueError(f"имя контейнера {container.name} занято")
            for suffix in container.suffixes():
                if suffix in self._extractors or suffix in self._containers:
                    raise ValueError(
                        f"суффикс {suffix} обслуживают несколько экстракторов"
                    )
                self._containers[suffix] = container
            self._names.add(container.name)

    def known_names(self) -> frozenset[str]:
        """Доступные имена экстракторов (для проверки конфига)."""
        return frozenset(self._names)

    def for_area(self, area: AreaConfig) -> dict[str, Extractor]:
        """Обычные экстракторы области: имя в её ``formats``, суффикс → экстрактор."""
        enabled = set(area.formats) & self._names
        return {
            suffix: extractor
            for suffix, extractor in self._extractors.items()
            if extractor.name in enabled
        }

    def _containers_for_area(self, area: AreaConfig) -> dict[str, ContainerExtractor]:
        """Контейнеры области: имя в её ``formats``, суффикс → контейнер."""
        enabled = set(area.formats) & self._names
        return {
            suffix: container
            for suffix, container in self._containers.items()
            if container.name in enabled
        }

    def extract(self, path: Path, area: AreaConfig) -> list[Extracted] | None:
        """Извлечь файл с учётом форматов области.

        Обычный файл — список из одной части; контейнер — по части на запись;
        ``None`` — формат не обслуживается.
        """
        suffix = path.suffix.lower()
        container = self._containers_for_area(area).get(suffix)
        if container is not None:
            return container.extract_all(path, _AreaExtractors(self.for_area(area)))
        extractor = self.for_area(area).get(suffix)
        return [extractor.extract(path)] if extractor else None


def default_registry() -> ExtractorRegistry:
    """Стандартный набор: markdown + text + html + mhtml + zip; pdf — если extra.

    ZIP — контейнер: он получает фабрику экстракторов области и делегирует ей
    записи архива по суффиксу. Вложенные zip не раскрываются: самой фабрике
    контейнеры не видны.
    """
    extractors: list[Extractor] = [
        MarkdownExtractor(),
        TextExtractor(),
        HtmlExtractor(),
        MhtmlExtractor(),
    ]
    with contextlib.suppress(RuntimeError):
        extractors.append(PdfExtractor())
    containers: tuple[ContainerExtractor, ...] = (ZipExtractor(),)
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
