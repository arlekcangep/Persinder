"""Тесты CHM: контейнер ITSF/ITSP, LZX-поток и экстрактор страниц.

CHM не требует внешних библиотек и C-сборки: оглавление (ITSF/ITSP/PMGL)
и LZX-поток разбираются собственным кодом на стандартной библиотеке. Фикстура
собирается прямо в тесте — это настоящий контейнер, но с LZX-блоком типа
«uncompressed»: он проходит весь путь (ControlData → ResetTable → Content →
LZXState), не требуя Хаффмана.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    ChmExtractor,
    default_registry,
)
from persinder.sources.chm import ChmArchive
from persinder.sources.classify import PUB_FORMATS, ClassMatch

TESTS_CLASSES = ("structure-text", "text", "pub-formats")

_PAGES = (
    b"<html><head><title>Hello</title></head><body>Hello, world</body></html>",
    b"<html><head><title>Two</title></head><body>Second page</body></html>",
)

_GUID = "{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}"
_CONTROL = "::DataSpace/Storage/MSCompressed/ControlData"
_CONTENT = "::DataSpace/Storage/MSCompressed/Content"
_RESET = (
    f"::DataSpace/Storage/MSCompressed/Transform/{_GUID}/InstanceData/ResetTable"
)

_HDR = 0x60
_DIR_OFF = 0x60
_ITSP_LEN = 0x54
_BLOCK = 0x1000


def _cword(value: int) -> bytes:
    """«Сжатый dword» оглавления CHM: base-128 varint старшими группами вперёд."""
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


class _Bits:
    """Писатель битов LZX: 16-битные LE-слова, внутри слова — старший бит вперёд."""

    def __init__(self) -> None:
        self._bits: list[int] = []

    def write(self, value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    def to_bytes(self) -> bytes:
        while len(self._bits) % 16:
            self._bits.append(0)
        out = bytearray()
        for index in range(0, len(self._bits), 16):
            word = 0
            for bit in self._bits[index : index + 16]:
                word = (word << 1) | bit
            out += bytes((word & 0xFF, word >> 8))
        return bytes(out)


def _lzx_uncompressed(payload: bytes) -> bytes:
    """LZX-поток из одного блока «uncompressed» (тип 3) — без Хаффмана."""
    bits = _Bits()
    bits.write(0, 1)  # нет intel-заголовка
    bits.write(3, 3)  # тип блока: uncompressed
    bits.write(len(payload) >> 8, 16)
    bits.write(len(payload) & 0xFF, 8)
    # После заголовка декодер «выравнивает» позицию к слову и читает R0/R1/R2.
    return bits.to_bytes() + struct.pack("<III", 1, 1, 1) + payload


def build_chm(pages: tuple[bytes, ...] = _PAGES) -> bytes:
    """Собрать минимальный корректный CHM с LZX-потоком и страницами."""
    payload = b"".join(pages)
    if len(payload) % 2:
        payload += b" "
    stream = _lzx_uncompressed(payload)
    control = struct.pack("<I4sIIIII", 6, b"LZXC", 2, 2, 2, 1, 0)
    reset = (
        struct.pack("<IIII", 2, 1, 8, 0x28)
        + struct.pack("<QQQ", len(payload), len(stream), len(payload))
        + struct.pack("<Q", 0)
    )
    raw_entries = ((_CONTROL, control), (_CONTENT, stream), (_RESET, reset))

    blob = bytearray()
    entries: list[tuple[str, int, int, int]] = []
    start = 0
    for name, data in raw_entries:
        entries.append((name, 0, start, len(data)))
        blob += data
        start += len(data)
    offset = 0
    for index, page in enumerate(pages):
        entries.append((f"/page {index + 1}.html", 1, offset, len(page)))
        offset += len(page)
    entries.sort()

    page_block = bytearray(_BLOCK)
    page_block[:4] = b"PMGL"
    struct.pack_into("<i", page_block, 0x08, 0)
    struct.pack_into("<i", page_block, 0x0C, -1)  # предыдущая страница
    struct.pack_into("<i", page_block, 0x10, -1)  # следующая страница
    pos = 0x14
    for name, space, entry_start, length in entries:
        encoded = name.encode("utf-8")
        for chunk in (
            _cword(len(encoded)),
            encoded,
            _cword(space),
            _cword(entry_start),
            _cword(length),
        ):
            page_block[pos : pos + len(chunk)] = chunk
            pos += len(chunk)
    struct.pack_into("<I", page_block, 4, _BLOCK - pos)  # свободное место

    itsp = bytearray(_ITSP_LEN)
    itsp[:4] = b"ITSP"
    struct.pack_into("<I", itsp, 4, 1)
    struct.pack_into("<I", itsp, 8, _ITSP_LEN)
    struct.pack_into("<I", itsp, 0x10, _BLOCK)
    struct.pack_into("<i", itsp, 0x1C, -1)  # корень не задан...
    struct.pack_into("<i", itsp, 0x20, 0)  # ...голова индекса — страница 0

    data_offset = _DIR_OFF + _ITSP_LEN + _BLOCK
    itsf = bytearray(_HDR)
    itsf[:4] = b"ITSF"
    struct.pack_into("<I", itsf, 4, 3)
    struct.pack_into("<I", itsf, 8, _HDR)
    struct.pack_into("<Q", itsf, 0x48, _DIR_OFF)
    struct.pack_into("<Q", itsf, 0x50, _ITSP_LEN + _BLOCK)
    struct.pack_into("<Q", itsf, 0x58, data_offset)
    return bytes(itsf) + bytes(itsp) + bytes(page_block) + bytes(blob)


def _area(root: Path) -> AreaConfig:
    return AreaConfig(path=root, role=Area.PERSONAL, formats=TESTS_CLASSES)


class ChmArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_directory_and_pages(self):
        """Оглавление PMGL читается, LZX-поток отдаёт ровно эти страницы."""
        archive = ChmArchive(self._write("help.chm", build_chm()))
        names = archive.names()
        self.assertIn("/page 1.html", names)
        self.assertIn("/page 2.html", names)
        self.assertEqual(archive.read("/page 1.html"), _PAGES[0])
        self.assertEqual(archive.read("/page 2.html"), _PAGES[1])

    def test_not_a_chm(self):
        path = self._write("bad.chm", b"not a chm at all")
        with self.assertRaises(ValueError):
            ChmArchive(path)


class ChmExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_extract_pages(self):
        """Текст всех страниц склеен, заголовки страниц попадают в тэги."""
        result = ChmExtractor().extract(self._write("help.chm", build_chm()))
        self.assertIsNone(result.error)
        self.assertEqual(result.extractor, "chm")
        self.assertEqual(result.pages, 2)
        self.assertIn("Hello, world", result.text)
        self.assertIn("Second page", result.text)
        self.assertNotIn("<html", result.text)
        self.assertEqual(result.tags, ("Hello", "Two"))

    def test_broken_chm_reports_error(self):
        result = ChmExtractor().extract(self._write("bad.chm", b"garbage"))
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertTrue(result.error.startswith("chm: "))
        self.assertEqual(result.text, "")

    def test_registry_recognizes_chm(self):
        """CHM попадает в pub-formats и извлекается через реестр."""
        reg = default_registry()
        path = self._write("book.chm", build_chm())
        parts = reg.extract(path, _area(self.root))
        self.assertIsNotNone(parts)
        assert parts is not None
        self.assertEqual(parts[0].extractor, "chm")
        self.assertIn("Hello, world", parts[0].text)

    def test_classifier_magic(self):
        """Классификатор опознаёт CHM по сигнатуре ITSF (не расширению)."""
        from persinder.sources.classify import default_classifiers

        matches = default_classifiers().classify(build_chm(), extension="")
        self.assertIn(ClassMatch(PUB_FORMATS, "chm", "magic"), matches)


if __name__ == "__main__":
    unittest.main()
