"""Тесты ZIP-экстрактора: распаковка и делегирование штатным экстракторам."""

from __future__ import annotations

import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    Extracted,
    Extractor,
    MarkdownExtractor,
    TextExtractor,
    ZipExtractor,
    default_registry,
)
from persinder.sources import extract as extract_module


def _inner() -> dict[str, Extractor]:
    md = MarkdownExtractor()
    txt = TextExtractor()
    return {".md": md, ".markdown": md, ".txt": txt}


class ZipExtractorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _zip(self, name: str, entries: dict[str, str]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, text in entries.items():
                archive.writestr(entry, text.encode("utf-8"))
        return path

    def test_combines_supported_entries(self):
        path = self._zip(
            "a.zip",
            {"notes/one.md": "#python заметка про котиков", "two.txt": "второй файл"},
        )
        result = ZipExtractor(_inner()).extract(path)
        self.assertIn("заметка про котиков", result.text)
        self.assertIn("второй файл", result.text)
        # имя каждого файла — заголовком, чтобы был виден источник
        self.assertIn("notes/one.md", result.text)
        self.assertIn("two.txt", result.text)
        self.assertEqual(result.tags, ("python",))
        self.assertIsNone(result.error)

    def test_unsupported_entries_skipped(self):
        path = self._zip("a.zip", {"data.bin": "мусор", "ok.md": "нужный текст"})
        result = ZipExtractor(_inner()).extract(path)
        self.assertIn("нужный текст", result.text)
        self.assertNotIn("мусор", result.text)

    def test_nested_zip_not_expanded(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as inner_zip:
            inner_zip.writestr("secret.md", "секрет внутри вложенного архива")
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("inner.zip", nested.getvalue())
            archive.writestr("ok.md", "внешний текст")
        result = ZipExtractor(_inner()).extract(path)
        self.assertIn("внешний текст", result.text)
        self.assertNotIn("секрет", result.text)

    def test_directory_entries_skipped(self):
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dir/", "")
            archive.writestr("dir/ok.md", "текст в подпапке")
        result = ZipExtractor(_inner()).extract(path)
        self.assertIn("текст в подпапке", result.text)
        self.assertIsNone(result.error)

    def test_only_unsupported_entries_gives_empty_text(self):
        path = self._zip("a.zip", {"a.bin": "x", "b.dat": "y"})
        result = ZipExtractor(_inner()).extract(path)
        self.assertEqual(result.text, "")
        self.assertIsNone(result.error)

    def test_corrupt_zip_returns_error(self):
        path = self.root / "bad.zip"
        path.write_bytes("это вовсе не zip-архив".encode())
        result = ZipExtractor(_inner()).extract(path)
        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertTrue(result.error.startswith("zip:"))

    def test_inner_extractor_error_is_skipped(self):
        class Broken:
            name = "broken"

            def suffixes(self) -> frozenset[str]:
                return frozenset({".bro"})

            def extract(self, path: Path) -> Extracted:
                return Extracted(text="", error="сломано")

        path = self._zip("a.zip", {"a.bro": "неважно", "b.txt": "живой текст"})
        result = ZipExtractor({".bro": Broken(), ".txt": TextExtractor()}).extract(path)
        self.assertIn("живой текст", result.text)
        self.assertIsNone(result.error)

    def test_inner_extractor_exception_is_skipped(self):
        class Boom:
            name = "boom"

            def suffixes(self) -> frozenset[str]:
                return frozenset({".boom"})

            def extract(self, path: Path) -> Extracted:
                raise ValueError("boom")

        path = self._zip("a.zip", {"a.boom": "x", "b.txt": "уцелевший текст"})
        result = ZipExtractor({".boom": Boom(), ".txt": TextExtractor()}).extract(path)
        self.assertIn("уцелевший текст", result.text)

    def test_entry_size_limit_skips_big_entries(self):
        path = self._zip("a.zip", {"big.txt": "x" * 100, "small.txt": "ok"})
        with mock.patch.object(extract_module, "ZIP_MAX_ENTRY_BYTES", 10):
            result = ZipExtractor(_inner()).extract(path)
        self.assertNotIn("xxxx", result.text)
        self.assertIn("ok", result.text)

    def test_total_size_limit_stops(self):
        path = self._zip(
            "a.zip", {"one.txt": "первый", "two.txt": "второй", "three.txt": "третий"}
        )
        with mock.patch.object(extract_module, "ZIP_MAX_TOTAL_BYTES", 1):
            result = ZipExtractor(_inner()).extract(path)
        # ни одна запись не проходит по суммарному лимиту — текста нет
        self.assertEqual(result.text, "")

    def test_protocol_satisfied(self):
        self.assertIsInstance(ZipExtractor(_inner()), Extractor)


class RegistryZipTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _area(self, formats: tuple[str, ...]) -> AreaConfig:
        return AreaConfig(path=self.root, role=Area.PERSONAL, formats=formats)

    def test_zip_known_and_gated_by_formats(self):
        reg = default_registry()
        self.assertIn("zip", reg.known_names())
        self.assertEqual(set(reg.for_area(self._area(("zip",)))), {".zip"})
        self.assertNotIn(".zip", reg.for_area(self._area(("markdown", "text"))))

    def test_extract_zip_through_registry(self):
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("note.md", "#tag содержимое")
        reg = default_registry()
        result = reg.extract(path, self._area(("zip",)))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("содержимое", result.text)
        self.assertEqual(result.tags, ("tag",))
        # область без zip не обслуживает архив
        self.assertIsNone(reg.extract(path, self._area(("markdown",))))


if __name__ == "__main__":
    unittest.main()
