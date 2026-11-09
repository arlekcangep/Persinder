"""Тесты экстракторов: реестр, Markdown/текст, извлечение тэгов."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    ExtractorRegistry,
    MarkdownExtractor,
    TextExtractor,
    default_registry,
)
from persinder.sources.extract import extract_tags


def area(
    root: Path, formats: tuple[str, ...] = ("structure-text", "text")
) -> AreaConfig:
    return AreaConfig(path=root, role=Area.PERSONAL, formats=formats)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_recognized_classes(self):
        reg = default_registry()
        names = reg.known_names()
        self.assertIn("structure-text", names)
        self.assertIn("pub-formats", names)
        self.assertIn("archive", names)
        # зарезервированные под будущее классы валидны в конфиге
        self.assertIn("ocr", names)
        self.assertIn("science", names)
        # легаси-имена экстракторов тоже принимаются (нормализуются в классы)
        self.assertIn("markdown", names)

    def test_md_resolved_to_markdown_structure_text(self):
        reg = default_registry()
        path = self._write("n.md", "# Заголовок\n#tag заметка")
        parts = reg.extract(path, area(self.root))
        assert parts is not None
        self.assertEqual(parts[0].extractor, "markdown")
        self.assertEqual(parts[0].tags, ("tag",))

    def test_formats_whitelist_classes(self):
        reg = default_registry()
        # только structure-text: md индексируется, txt — нет
        md_path = self._write("n.md", "# Заголовок")
        txt_path = self._write("n.txt", "обычный текст")
        only_struct = area(self.root, ("structure-text",))
        self.assertIsNotNone(reg.extract(md_path, only_struct))
        self.assertIsNone(reg.extract(txt_path, only_struct))

    def test_empty_formats_disables_area(self):
        reg = default_registry()
        path = self._write("n.md", "# Заголовок")
        self.assertIsNone(reg.extract(path, area(self.root, ())))

    def test_unknown_class_not_found(self):
        reg = ExtractorRegistry((MarkdownExtractor(), TextExtractor()))
        path = self._write("n.md", "# Заголовок")
        self.assertIsNone(reg.extract(path, area(self.root, ("science",))))

    def test_extract_respects_area_classes(self):
        reg = default_registry()
        path = self._write("n.md", "#tag заметка")
        self.assertIsNotNone(reg.extract(path, area(self.root)))
        self.assertIsNone(reg.extract(path, area(self.root, ("text",))))

    def test_content_based_pdf_class_not_in_area(self):
        # PDF по содержимому (без расширения .pdf) — класс pub-formats не в
        # конфиге области → не извлечён
        reg = ExtractorRegistry((TextExtractor(),))
        path = self._write("n.txt", "%PDF-1.4\nтекст не pdf совсем")
        self.assertIsNone(reg.extract(path, area(self.root, ("text",))))

    def test_duplicate_name_rejected(self):
        class Second(MarkdownExtractor):
            name = "markdown"

        with self.assertRaises(ValueError):
            ExtractorRegistry((MarkdownExtractor(), Second()))

    def test_protocol_satisfied(self):
        from persinder.sources import Extractor

        self.assertIsInstance(MarkdownExtractor(), Extractor)
        self.assertIsInstance(TextExtractor(), Extractor)


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_inline_tags(self):
        tags = extract_tags("Заметка про #python и #téta-2, но не # внутри кода", None)
        self.assertIn("python", tags)

    def test_tag_not_in_code(self):
        tags = extract_tags("код `dict = {# not a tag}` конец", None)
        self.assertEqual(tags, ())

    def test_tag_not_heading(self):
        tags = extract_tags("# Заголовок не тэг\nа это #тэг", None)
        self.assertEqual(tags, ("тэг",))

    def test_frontmatter_inline_tags(self):
        text = "---\ntitle: T\ntags: [a, B, c]\n---\nтело #d\n"
        result = MarkdownExtractor().extract(self._write("n.md", text))
        lowered = {t.lower() for t in result.tags}
        self.assertEqual(lowered, {"a", "b", "c", "d"})
        self.assertNotIn("---", result.text)

    def test_frontmatter_list_tags(self):
        text = "---\ntags:\n  - alpha\n  - beta\n---\nтело\n"
        result = MarkdownExtractor().extract(self._write("n.md", text))
        self.assertEqual({t.lower() for t in result.tags}, {"alpha", "beta"})

    def test_case_dedup(self):
        tags = extract_tags("#Foo и #foo и #FOO", None)
        self.assertEqual(len(tags), 1)

    def test_plain_text(self):
        result = TextExtractor().extract(self._write("n.txt", "просто текст"))
        self.assertEqual(result.text, "просто текст")
        self.assertEqual(result.tags, ())

    def test_markdown_no_frontmatter(self):
        result = MarkdownExtractor().extract(self._write("n.md", "# Заголовок\n#tag"))
        self.assertEqual(result.tags, ("tag",))
        self.assertTrue(result.text.startswith("# Заголовок"))


if __name__ == "__main__":
    unittest.main()
