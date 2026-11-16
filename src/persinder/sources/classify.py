"""Классификаторы: тип файла по содержимому (magic-байты/снифф) → класс.

Слой между файлом и экстракторами (см. ``extract.ExtractorRegistry``):

- **Класс** — «семья» форматов из конфига области: ``text``, ``structure-text``,
  ``pub-formats``, ``archive``, ``ocr``, ``code``, ``science``. Конфиг
  перечисляет классы, а не расширения.
- **Подтип** — конкретный формат внутри класса (``markdown``, ``html``, ``pdf``,
  ``epub``...): по нему реестр выбирает конкретный экстрактор, когда в одном
  классе их несколько.
- **Классификаторов может быть несколько** (порт ``Classifier``): свой
  magic-таблицы, сниффер текста, в будущем — обёртка над готовой библиотекой
  (Windows-friendly). ``ClassifierSet`` прогоняет их по очереди и сливает
  совпадения. Работает и для записей внутри архивов (вход — ``bytes``, не путь).

``classify`` берёт только **начало** файла (первые байты) с диска — для работы
с архивными записями есть ``classify_bytes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# --- классы: имена, которые можно писать в конфиге области (`formats`) ---
TEXT = "text"
STRUCTURE_TEXT = "structure-text"
PUB_FORMATS = "pub-formats"
ARCHIVE = "archive"
OCR = "ocr"
CODE = "code"
SCIENCE = "science"

# Классы, зарезервированные под будущие экстракторы: имя валидно в конфиге,
# файлы таких классов сейчас просто не индексируются (см. default_registry).
RESERVED: frozenset[str] = frozenset({OCR, CODE, SCIENCE})

# Сколько байт начала файла читаем для классификации. Для распаковываемых
# типов больше и не нужно: все magic-сигнатуры в первых 512 байтах.
SNIFF_BYTES = 8192


@dataclass(frozen=True)
class ClassMatch:
    """Совпадение классификатора: класс + конкретный подтип.

    ``subtype`` — точный формат внутри класса (``markdown``, ``pdf``, ``epub``);
    пустая строка — класс назван, но подтип не определён (тогда реестр
    рассудит по расширению). ``source`` — имя классификатора (для отладки).
    """

    format_class: str
    subtype: str = ""
    source: str = ""


@runtime_checkable
class Classifier(Protocol):
    """Классификатор: байты (+ расширение-хинт) → совпадения классов."""

    name: str

    def classify(self, data: bytes, extension: str) -> tuple[ClassMatch, ...]:
        """Доли содержимого → совпадения (пусто — тип не распознан)."""
        ...


class MagicTableClassifier:
    """Собственная таблица magic-байтов (чистый Python, без зависимостей).

    Покрывает всё, что нужно текущим классам: контейнеры (zip/7z/tar/gz/rar),
    PDF, ePub (zip с ``mimetype``), OLE2 (старые .pub/.doc), картинки и djvu/tiff
    (класс ``ocr``). ePub проверяется до zip: это тоже PK-архив, но с особым
    ``mimetype`` — он относится к ``pub-formats``, а не ``archive``.
    """

    name = "magic"

    def classify(self, data: bytes, extension: str) -> tuple[ClassMatch, ...]:
        head = data[:512]
        found: list[ClassMatch] = []

        # --- pub-formats ---
        if head.startswith(b"%PDF-"):
            found.append(ClassMatch(PUB_FORMATS, "pdf", self.name))
            return tuple(found)
        if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            if _is_epub(data):
                return (ClassMatch(PUB_FORMATS, "epub", self.name),)
            return (ClassMatch(ARCHIVE, "zip", self.name),)
        if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            # OLE2/CFB: MS Publisher (.pub), .doc, .ppt — текст извлекать пока
            # не умеем (нет экстрактора), но класс обязателен для конфига.
            return (ClassMatch(PUB_FORMATS, "office-cfb", self.name),)

        # --- archive ---
        if head.startswith(b"7z\xbc\xaf\x27\x1c"):
            return (ClassMatch(ARCHIVE, "7z", self.name),)
        if head.startswith(b"\x1f\x8b"):
            return (ClassMatch(ARCHIVE, "gzip", self.name),)
        if head.startswith((b"Rar!\x1a\x07",)):
            return (ClassMatch(ARCHIVE, "rar", self.name),)
        if head[257:262] == b"ustar":
            return (ClassMatch(ARCHIVE, "tar", self.name),)

        # --- ocr (резерв: экстрактора пока нет) ---
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return (ClassMatch(OCR, "png", self.name),)
        if head.startswith(b"\xff\xd8\xff"):
            return (ClassMatch(OCR, "jpeg", self.name),)
        if head.startswith(b"GIF8"):
            return (ClassMatch(OCR, "gif", self.name),)
        if head.startswith(b"BM"):
            return (ClassMatch(OCR, "bmp", self.name),)
        if head[:12] == b"RIFF" and head[8:12] == b"WEBP":
            return (ClassMatch(OCR, "webp", self.name),)
        if head.startswith((b"II*\x00", b"MM\x00*")):
            return (ClassMatch(OCR, "tiff", self.name),)
        if head[:8] == b"AT&TFORM" or (head[:4] == b"FORM" and head[4:8] == b"DJVU"):
            return (ClassMatch(OCR, "djvu", self.name),)

        return tuple(found)


def _is_epub(data: bytes) -> bool:
    """ePub — это zip, у которого в корне лежит ``mimetype`` c epub-типом.

    Имя записи идёт сразу после 30-байтового локального заголовка PK-архива;
    смотреть его в сыром виде хрупко, но для классического epub достаточно:
    в данных после сигнатуры встречается строка ``mimetypeapplication/epub+zip``.
    """
    return b"mimetypeapplication/epub+zip" in data[:SNIFF_BYTES]


class TextSniffClassifier:
    """Сниффер текста: html / markdown / fb2 / ipynb / plain text.

    У текстовых типов magic-байтов нет — тип определяется по первым KB.
    Би-нари (NUL-байты, высокая доля непечатаемых) — не текст, возвращаем пусто.
    """

    name = "text"

    def classify(self, data: bytes, extension: str) -> tuple[ClassMatch, ...]:
        if not data:
            return ()
        sample = data[:SNIFF_BYTES]
        if b"\x00" in sample[:2048]:
            return ()  # NUL-байты — явный маркер бинарного (текст так не начинается)
        try:
            text = sample.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            # Не-UTF8: возможно cp1251/koi8 (текст) или бинарный мусор.
            # Успешный строгий UTF-8-декод выше уже значит «это текст»;
            # здесь решающая эвристика по непечатаемым.
            if _looks_binary(sample):
                return ()
            text = sample.decode("utf-8", errors="replace")
        head = text.lstrip("\ufeff \t\r\n")[:2048]

        # --- fb2: XML-документ с корнем FictionBook ---
        if "<FictionBook" in head or head.lstrip("<?xml").lstrip().startswith(
            "<FictionBook"
        ):
            return (ClassMatch(PUB_FORMATS, "fb2", self.name),)
        # --- ipynb (science, резерв) ---
        if head.lstrip().startswith("{") and '"nbformat"' in head:
            return (ClassMatch(SCIENCE, "ipynb", self.name),)

        # --- structure-text: html / markdown ---
        low = head.lower()
        if (
            low.startswith("<!doctype html")
            or "<html" in low[:512]
            or "<head" in low[:512]
        ):
            return (ClassMatch(STRUCTURE_TEXT, "html", self.name),)
        if _looks_markdown(head):
            return (ClassMatch(STRUCTURE_TEXT, "markdown", self.name),)
        if extension in (".md", ".markdown"):
            # Расширение-хинт: заметка без явных маркеров разметки
            return (ClassMatch(STRUCTURE_TEXT, "markdown", self.name),)
        return (ClassMatch(TEXT, "text", self.name),)


def _looks_binary(data: bytes) -> bool:
    """Эвристика «это не текст» (здесь — только не-UTF8 кандидаты).

    NUL-байты почти всегда бинарное; иначе — доля «непечатаемых» байтов.
    Для не-UTF8-текстов (cp1251/koi8) печатаемыми считаем и продолжения
    многобайтовых кодировок (0x80–0xFF).
    """
    if b"\x00" in data[:2048]:
        return True
    sample = data[:4096]
    if not sample:
        return False
    printable = (
        bytearray(range(9, 14))
        + bytearray(range(32, 128))
        + bytearray(range(0x80, 0x100))
    )
    bad = sum(1 for b in sample if b not in printable)
    return bad / len(sample) > 0.1


def _looks_markdown(text: str) -> bool:
    """Консервативная эвристика markdown: frontmatter или заголовки и маркеры."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("---\n"):
        return True  # frontmatter: YAML-шапка заметки Obsidian
    lines = [ln.strip() for ln in stripped.splitlines()[:8] if ln.strip()]
    headings = sum(1 for ln in lines if ln.startswith("#"))
    markers = [
        ln for ln in lines if ln[:2] in ("- ", "* ", "+ ") or ln[:3].startswith("1.")
    ]
    links = "[x](x)" in text or "[[" in text
    return bool(headings >= 1 and (markers or links or len(lines) > 2))


@dataclass(frozen=True)
class ClassifierSet:
    """Несколько классификаторов подряд; совпадения сливаются в порядке вызова.

    Первый по приоритету (порядок в конструкторе) имеет решающий голос по
    своему типу: если magic определил контейнер или pdf — текст-сниффер уже
    не нужен. Если классификатор вернул пусто — пробует следующий.
    """

    classifiers: tuple[Classifier, ...] = (
        MagicTableClassifier(),
        TextSniffClassifier(),
    )

    def classify(self, data: bytes, extension: str = "") -> tuple[ClassMatch, ...]:
        for classifier in self.classifiers:
            found = classifier.classify(data, extension)
            if found:
                return found
        return ()

    def classify_path(self, path: Path) -> tuple[ClassMatch, ...]:
        """Прочитать начало файла с диска и классифицировать."""
        return self.classify(_read_head(path), path.suffix.lower())


def _read_head(path: Path, size: int = SNIFF_BYTES) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def default_classifiers() -> ClassifierSet:
    """Стандартный набор: magic-таблицы + сниффер текста."""
    return ClassifierSet((MagicTableClassifier(), TextSniffClassifier()))
