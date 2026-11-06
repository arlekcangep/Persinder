"""Тесты ZIP-экстрактора: контейнер распаковывает архив в части по записи."""

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
    ContainerExtractor,
    Extracted,
    Extractor,
    MarkdownExtractor,
    TextExtractor,
    ZipExtractor,
    default_registry,
)
from persinder.sources import extract as extract_module


class _Factory:
    """Тестовая фабрика: суффикс → экстрактор (как у области)."""

    def __init__(self, mapping: dict[str, Extractor]) -> None:
        self._mapping = mapping

    def for_suffix(self, suffix: str) -> Extractor | None:
        return self._mapping.get(suffix.lower())


def _inner() -> _Factory:
    md = MarkdownExtractor()
    txt = TextExtractor()
    return _Factory({".md": md, ".markdown": md, ".txt": txt})


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

    def test_one_part_per_supported_entry(self):
        path = self._zip(
            "a.zip",
            {"notes/one.md": "#python заметка про котиков", "two.txt": "второй файл"},
        )
        parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual(len(parts), 2)
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"notes/one.md", "two.txt"})
        self.assertIn("заметка про котиков", by_name["notes/one.md"].text)
        self.assertEqual(by_name["notes/one.md"].tags, ("python",))
        self.assertEqual(by_name["two.txt"].text, "второй файл")
        self.assertTrue(all(part.error is None for part in parts))

    def test_unsupported_entries_skipped(self):
        path = self._zip("a.zip", {"data.bin": "мусор", "ok.md": "нужный текст"})
        parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual([part.name for part in parts], ["ok.md"])
        self.assertEqual(parts[0].text, "нужный текст")

    def test_nested_zip_not_expanded(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as inner_zip:
            inner_zip.writestr("secret.md", "секрет внутри вложенного архива")
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("inner.zip", nested.getvalue())
            archive.writestr("ok.md", "внешний текст")
        parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual([part.name for part in parts], ["ok.md"])
        self.assertNotIn("секрет", parts[0].text)

    def test_directory_entries_skipped(self):
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dir/", "")
            archive.writestr("dir/ok.md", "текст в подпапке")
        parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual([part.name for part in parts], ["dir/ok.md"])

    def test_backslashes_normalized(self):
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dir\\win.md", "windows-путь")
        parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual([part.name for part in parts], ["dir/win.md"])

    def test_only_unsupported_entries_gives_no_parts(self):
        path = self._zip("a.zip", {"a.bin": "x", "b.dat": "y"})
        self.assertEqual(ZipExtractor().extract_all(path, _inner()), [])

    def test_corrupt_zip_returns_error_part(self):
        path = self.root / "bad.zip"
        path.write_bytes("это вовсе не zip-архив".encode())
        parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].text, "")
        self.assertEqual(parts[0].name, "")
        self.assertIsNotNone(parts[0].error)
        assert parts[0].error is not None
        self.assertTrue(parts[0].error.startswith("zip:"))

    def test_inner_extractor_error_kept_as_error_part(self):
        class Broken:
            name = "broken"

            def suffixes(self) -> frozenset[str]:
                return frozenset({".bro"})

            def extract(self, path: Path) -> Extracted:
                return Extracted(text="", error="сломано")

        path = self._zip("a.zip", {"a.bro": "неважно", "b.txt": "живой текст"})
        parts = ZipExtractor().extract_all(
            path, _Factory({".bro": Broken(), ".txt": TextExtractor()})
        )
        by_name = {part.name: part for part in parts}
        self.assertEqual(by_name["a.bro"].error, "сломано")
        self.assertEqual(by_name["b.txt"].text, "живой текст")

    def test_inner_extractor_exception_becomes_error_part(self):
        class Boom:
            name = "boom"

            def suffixes(self) -> frozenset[str]:
                return frozenset({".boom"})

            def extract(self, path: Path) -> Extracted:
                raise ValueError("boom")

        path = self._zip("a.zip", {"a.boom": "x", "b.txt": "уцелевший текст"})
        parts = ZipExtractor().extract_all(
            path, _Factory({".boom": Boom(), ".txt": TextExtractor()})
        )
        by_name = {part.name: part for part in parts}
        self.assertTrue(str(by_name["a.boom"].error).startswith("zip entry:"))
        self.assertEqual(by_name["b.txt"].text, "уцелевший текст")

    def test_entry_size_limit_skips_big_entries(self):
        path = self._zip("a.zip", {"big.txt": "x" * 100, "small.txt": "ok"})
        with mock.patch.object(extract_module, "ZIP_MAX_ENTRY_BYTES", 10):
            parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual([part.name for part in parts], ["small.txt"])

    def test_total_size_limit_stops(self):
        path = self._zip(
            "a.zip", {"one.txt": "первый", "two.txt": "второй", "three.txt": "третий"}
        )
        with mock.patch.object(extract_module, "ZIP_MAX_TOTAL_BYTES", 1):
            parts = ZipExtractor().extract_all(path, _inner())
        self.assertEqual(parts, [])

    def test_protocol_satisfied(self):
        self.assertIsInstance(ZipExtractor(), ContainerExtractor)


class RegistryZipTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _area(self, formats: tuple[str, ...]) -> AreaConfig:
        return AreaConfig(path=self.root, role=Area.PERSONAL, formats=formats)

    def _zip(self, name: str, entries: dict[str, str]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, text in entries.items():
                archive.writestr(entry, text.encode("utf-8"))
        return path

    def test_zip_known_but_leaf_map_excludes_it(self):
        reg = default_registry()
        self.assertIn("zip", reg.known_names())
        # контейнер не попадает в карту «суффикс → обычный экстрактор»
        self.assertEqual(reg.for_area(self._area(("zip",))), {})
        self.assertNotIn(".zip", reg.for_area(self._area(("markdown", "text"))))

    def test_extract_zip_through_registry_gives_parts(self):
        path = self._zip("a.zip", {"note.md": "#tag содержимое", "b.txt": "ещё"})
        reg = default_registry()
        parts = reg.extract(path, self._area(("zip", "markdown", "text")))
        self.assertIsNotNone(parts)
        assert parts is not None
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"note.md", "b.txt"})
        self.assertEqual(by_name["note.md"].tags, ("tag",))
        # область без zip не обслуживает архив
        self.assertIsNone(reg.extract(path, self._area(("markdown",))))

    def test_factory_respects_area_formats(self):
        # zip разрешён, но markdown — нет: запись .md пропускается, .txt — нет
        path = self._zip("a.zip", {"note.md": "разметка", "b.txt": "текст"})
        reg = default_registry()
        parts = reg.extract(path, self._area(("zip", "text")))
        assert parts is not None
        self.assertEqual([part.name for part in parts], ["b.txt"])

    def test_plain_file_extract_is_single_part(self):
        path = self.root / "note.md"
        path.write_text("#tag заметка", encoding="utf-8")
        reg = default_registry()
        parts = reg.extract(path, self._area(("markdown",)))
        self.assertIsNotNone(parts)
        assert parts is not None
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].name, "")
        self.assertIn("заметка", parts[0].text)


if __name__ == "__main__":
    unittest.main()
