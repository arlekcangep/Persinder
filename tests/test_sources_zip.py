"""Тесты архивного контейнера: zip/7z/tar/gz, часть на запись, вложенность 1.

Записи архива классифицируются по содержимому (резолвер ``entry_resolver``),
расширение записи — лишь хинт для неразличимых типов.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from persinder.domain import Area
from persinder.sources import (
    ArchiveExtractor,
    AreaConfig,
    ContainerExtractor,
    Extracted,
    Extractor,
    ExtractorRegistry,
    TextExtractor,
    default_registry,
)
from persinder.sources import extract as extract_module
from persinder.sources.classify import ARCHIVE, ClassMatch
from persinder.sources.extract import (
    BaseExtractor,
)
from persinder.sources.extract import (
    archive_suffix as _archive_suffix,
)

# Классы, разрешённые тестовой области: всё, что даёт текстовые части.
TESTS_CLASSES = ("structure-text", "text", "pub-formats")


def _resolver(root: Path, extractors: tuple[Extractor, ...] = ()):
    """Резолвер записей: реестр из ``extractors`` (или стандартный) + область."""
    reg = default_registry() if not extractors else ExtractorRegistry(extractors)
    area = AreaConfig(path=root, role=Area.PERSONAL, formats=TESTS_CLASSES)
    return reg.entry_resolver(area)


def _inner(root: Path):
    return _resolver(root)


class SuffixTest(unittest.TestCase):
    """Составные суффиксы: tar.gz/tgz — один шаг, до обычного суффикса."""

    def test_compound(self):
        self.assertEqual(_archive_suffix("a.tar.gz"), ".tar.gz")
        self.assertEqual(_archive_suffix("a.tar.bz2"), ".tar.bz2")
        self.assertEqual(_archive_suffix("a.TGZ"), ".tgz")
        self.assertEqual(_archive_suffix("a.txt.gz"), ".gz")
        self.assertEqual(_archive_suffix("a.zip"), ".zip")
        self.assertEqual(_archive_suffix("noext"), "")

    def test_extractor_accepts_tgz_suffixes(self):
        self.assertIn(".tgz", ArchiveExtractor().suffixes())
        self.assertIn(".tar.gz", _archive_suffix("x.tar.gz"))


class ArchiveExtractorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ext = ArchiveExtractor()
        self.resolver = _inner(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _zip(self, name: str, entries: dict[str, str]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, text in entries.items():
                archive.writestr(entry, text.encode("utf-8"))
        return path

    def _tar(self, name: str, mode: str, entries: dict[str, str]) -> Path:
        path = self.root / name
        archive: Any = tarfile.open(path, mode)  # type: ignore[call-overload]  # noqa: SIM115
        # pyright: ignore[reportCallIssue, reportUnknownVariableType, reportUnknownMemberType]
        with archive:
            for entry, text in entries.items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(entry)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))  # pyright: ignore[reportUnknownMemberType]
        return path

    def _names(self, parts: list[Extracted]) -> list[str]:
        return [part.name for part in parts]

    # --- zip ---

    def test_zip_one_part_per_supported_entry(self):
        path = self._zip(
            "a.zip",
            {"notes/one.md": "#python заметка про котиков", "two.txt": "второй файл"},
        )
        parts = self.ext.extract_all(path, self.resolver)
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"notes/one.md", "two.txt"})
        self.assertIn("заметка про котиков", by_name["notes/one.md"].text)
        self.assertEqual(by_name["notes/one.md"].tags, ("python",))
        self.assertEqual(by_name["two.txt"].text, "второй файл")
        self.assertTrue(all(part.error is None for part in parts))

    def test_zip_unsupported_entries_skipped(self):
        # .bin с реально бинарным содержимым (NUL — не текст) пропускается;
        # текстовый .bin, наоборот, по содержимому индексируется как текст
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("data.bin", bytes([0, 1, 2, 3, 255]) * 8)
            archive.writestr("ok.md", "нужный текст")
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["ok.md"])
        self.assertEqual(parts[0].text, "нужный текст")

    # --- tar и составные ---

    def test_tar_entries(self):
        path = self._tar("a.tar", "w", {"dir/n.md": "#tar тэг", "b.txt": "текст тара"})
        parts = self.ext.extract_all(path, self.resolver)
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"dir/n.md", "b.txt"})
        self.assertEqual(by_name["dir/n.md"].tags, ("tar",))
        self.assertEqual(by_name["b.txt"].text, "текст тара")

    def test_tar_gz_one_step(self):
        # tar.gz — один контейнер: записи tar, без промежуточного уровня ".gz"
        path = self._tar("a.tar.gz", "w:gz", {"n.md": "внутри тар-гз"})
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["n.md"])
        self.assertIn("внутри тар-гз", parts[0].text)

    def test_tgz_one_step(self):
        path = self._tar("b.tgz", "w:gz", {"n.md": "внутри тгз"})
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["n.md"])

    def test_tar_bz2(self):
        path = self._tar("c.tar.bz2", "w:bz2", {"n.md": "внутри bz2"})
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["n.md"])

    # --- одиночный gz ---

    def test_single_gz(self):
        path = self.root / "note.txt.gz"
        path.write_bytes(gzip.compress("сжатый текст".encode()))
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["note.txt"])
        self.assertEqual(parts[0].text, "сжатый текст")

    # --- 7z (extra 7z) ---

    def test_7z_entries(self):
        try:
            import py7zr  # noqa: F401
        except ImportError:
            self.skipTest("py7zr не установлен")
        import py7zr

        path = self.root / "a.7z"
        with py7zr.SevenZipFile(path, "w") as archive:
            archive.writestr("#7z содержимое", "dir/n.md")
            archive.writestr("текст 7z", "b.txt")
        parts = self.ext.extract_all(path, self.resolver)
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"dir/n.md", "b.txt"})
        self.assertEqual(by_name["b.txt"].text, "текст 7z")

    def test_7z_without_extra_gives_error_part(self):
        path = self.root / "a.7z"
        path.write_bytes(b"anything")
        with mock.patch.dict("sys.modules", {"py7zr": None}):
            parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(len(parts), 1)
        self.assertIn("persinder[archive7z]", parts[0].error or "")

    # --- rar (extra rar) ---

    def test_rar_without_extra_gives_error_part(self):
        path = self.root / "a.rar"
        path.write_bytes(b"anything")
        with mock.patch.dict("sys.modules", {"rarfile": None}):
            parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(len(parts), 1)
        self.assertIn("persinder[archiverar]", parts[0].error or "")

    # --- вложенность: ровно один уровень ---

    def test_nested_zip_expanded_one_level(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as inner_zip:
            inner_zip.writestr("secret.md", "секрет вложенного архива")
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("inner.zip", nested.getvalue())
            archive.writestr("ok.md", "внешний текст")
        parts = self.ext.extract_all(path, self.resolver)
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"inner.zip/secret.md", "ok.md"})
        self.assertIn("секрет вложенного архива", by_name["inner.zip/secret.md"].text)
        self.assertIn("внешний текст", by_name["ok.md"].text)

    def test_nested_depth_two_not_expanded(self):
        # zip → zip → zip: третий уровень не раскрывается
        level2 = io.BytesIO()
        with zipfile.ZipFile(level2, "w") as z2:
            z2.writestr("deep.txt", "глубокий текст")
        level1 = io.BytesIO()
        with zipfile.ZipFile(level1, "w") as z1:
            z1.writestr("mid.zip", level2.getvalue())
        path = self.root / "top.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mid.zip", level1.getvalue())
        parts = self.ext.extract_all(path, self.resolver)
        # mid.zip раскрыт (уровень 2: deep.zip доступен как запись); deep.zip —
        # это уровень 3: не раскрывается, а как не-текстовая запись — без частей
        self.assertEqual(parts, [])

    def test_nested_tar_inside_zip(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = "текст вложенного тара".encode()
            info = tarfile.TarInfo("n.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("inner.tar", buf.getvalue())
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["inner.tar/n.txt"])
        self.assertEqual(parts[0].text, "текст вложенного тара")

    def test_nested_unsupported_inner_format_skipped(self):
        # blob.bin с бинарным содержимым (NUL) — не текст, класс не распознан
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as inner_zip:
            inner_zip.writestr("blob.bin", bytes([0, 1, 2, 3]) * 16)
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("inner.zip", nested.getvalue())
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(parts, [])

    # --- лимиты и ошибки ---

    def test_directory_entries_skipped(self):
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dir/", "")
            archive.writestr("dir/ok.md", "текст в подпапке")
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["dir/ok.md"])

    def test_corrupt_archive_returns_error_part(self):
        path = self.root / "bad.zip"
        path.write_bytes("это вовсе не zip-архив".encode())
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].text, "")
        self.assertIsNotNone(parts[0].error)
        assert parts[0].error is not None
        self.assertTrue(parts[0].error.startswith("archive:"))

    def test_unknown_container_suffix_error(self):
        path = self.root / "a.tar.xz"
        path.write_bytes(b"not really xz")
        parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(len(parts), 1)
        self.assertIsNotNone(parts[0].error)

    def test_inner_extractor_error_kept_as_error_part(self):
        class Broken(BaseExtractor):
            name = "broken"
            format_class = "text"

            def suffixes(self) -> frozenset[str]:
                return frozenset({".bro"})

            def extract(self, path: Path) -> Extracted:
                return Extracted(text="", error="сломано", extractor=self.name)

        # .bro с бинарным содержимым (NUL) — распознаётся только экстрактором
        # broken по расширению-фолбэку; текстовая b.txt — текстовым экстрактором.
        resolver = _resolver(self.root, (Broken(), TextExtractor()))
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a.bro", bytes([0, 1, 2, 255]) * 8)
            archive.writestr("b.txt", "живой текст")
        parts = self.ext.extract_all(path, resolver)
        by_name = {part.name: part for part in parts}
        self.assertEqual(by_name["a.bro"].error, "сломано")
        self.assertEqual(by_name["b.txt"].text, "живой текст")

    def test_inner_extractor_exception_becomes_error_part(self):
        class Boom(BaseExtractor):
            name = "boom"
            format_class = "text"

            def suffixes(self) -> frozenset[str]:
                return frozenset({".boom"})

            def extract(self, path: Path) -> Extracted:
                raise ValueError("boom")

        # .boom бинарный — только экстрактор boom (фолбэк по расширению)
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a.boom", bytes([0, 1, 2, 255]) * 8)
            archive.writestr("b.txt", "уцелевший текст")
        resolver = _resolver(self.root, (Boom(), TextExtractor()))
        parts = self.ext.extract_all(path, resolver)
        by_name = {part.name: part for part in parts}
        self.assertTrue(str(by_name["a.boom"].error).startswith("archive entry:"))
        self.assertEqual(by_name["b.txt"].text, "уцелевший текст")

    def test_entry_size_limit_skips_big_entries(self):
        path = self._zip("a.zip", {"big.txt": "x" * 100, "small.txt": "ok"})
        with mock.patch.object(extract_module, "ARCHIVE_MAX_ENTRY_BYTES", 10):
            parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(self._names(parts), ["small.txt"])

    def test_total_size_limit_stops(self):
        path = self._zip(
            "a.zip", {"one.txt": "первый", "two.txt": "второй", "three.txt": "третий"}
        )
        with mock.patch.object(extract_module, "ARCHIVE_MAX_TOTAL_BYTES", 1):
            parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(parts, [])

    def test_entries_limit(self):
        entries = {f"f{i}.txt": "t" for i in range(5)}
        path = self._zip("a.zip", entries)
        with mock.patch.object(extract_module, "ARCHIVE_MAX_ENTRIES", 3):
            parts = self.ext.extract_all(path, self.resolver)
        self.assertEqual(len(parts), 3)

    def test_protocol_satisfied(self):
        self.assertIsInstance(ArchiveExtractor(), ContainerExtractor)


class RegistryArchiveTest(unittest.TestCase):
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

    def test_archive_known_but_leaf_responders_empty(self):
        reg = default_registry()
        self.assertIn("archive", reg.known_names())
        self.assertNotIn("zip", reg.known_names())
        # контейнер не попадает в отклики обычных экстракторов
        self.assertEqual(
            reg.leaf_responders(
                ".zip",
                (ClassMatch(ARCHIVE, "zip", "test"),),
                self._area(("archive",)),
            ),
            [],
        )

    def test_extract_zip_through_registry_gives_parts(self):
        path = self._zip("a.zip", {"note.md": "#tag содержимое", "b.txt": "ещё"})
        reg = default_registry()
        parts = reg.extract(path, self._area(("archive", "structure-text", "text")))
        assert parts is not None
        by_name = {part.name: part for part in parts}
        self.assertEqual(set(by_name), {"note.md", "b.txt"})
        self.assertEqual(by_name["note.md"].tags, ("tag",))
        # область без archive не обслуживает архив
        self.assertIsNone(reg.extract(path, self._area(("structure-text",))))

    def test_factory_respects_area_classes(self):
        # archive разрешён, но structure-text — нет: запись .md пропускается
        path = self._zip("a.zip", {"note.md": "разметка", "b.txt": "текст"})
        reg = default_registry()
        parts = reg.extract(path, self._area(("archive", "text")))
        assert parts is not None
        self.assertEqual([part.name for part in parts], ["b.txt"])

    def test_plain_file_extract_is_single_part(self):
        path = self.root / "note.md"
        path.write_text("#tag заметка", encoding="utf-8")
        reg = default_registry()
        parts = reg.extract(path, self._area(("structure-text",)))
        assert parts is not None
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].name, "")
        self.assertIn("заметка", parts[0].text)

    def test_fb2_entry_in_zip(self):
        # запись fb2 внутри zip распознаётся по содержимому (pub-formats)
        path = self._zip(
            "a.zip",
            {
                "y.fb2": "<?xml version='1.0'?><FictionBook xmlns='x'><body>"
                "<p>в архиве fb2</p></body></FictionBook>"
            },
        )
        reg = default_registry()
        parts = reg.extract(path, self._area(("archive", "pub-formats")))
        assert parts is not None
        self.assertEqual([p.extractor for p in parts], ["fb2"])
        self.assertIn("в архиве fb2", parts[0].text)


if __name__ == "__main__":
    unittest.main()
