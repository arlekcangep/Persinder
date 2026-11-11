"""Тесты экстракторов: реестр, Markdown/текст, извлечение тэгов."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    ExtractorRegistry,
    Fb2Extractor,
    HtmlExtractor,
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
        # легаси-имена конкретных форматов тоже принимаются
        self.assertIn("markdown", names)
        self.assertIn("fb2", names)

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

    def test_class_name_enables_whole_class(self):
        # имя класса structure-text включает md и html
        reg = default_registry()
        md_path = self._write("a.md", "# Заголовок")
        html_path = self._write("a.html", "<html><body>привет</body></html>")
        whole = area(self.root, ("structure-text",))
        self.assertIsNotNone(reg.extract(md_path, whole))
        self.assertIsNotNone(reg.extract(html_path, whole))

    def test_format_name_restricts_to_single_format(self):
        # имя формата markdown/HTML включает только его из всего класса
        reg = default_registry()
        md_path = self._write("a.md", "# Заголовок")
        html_path = self._write("a.html", "<html><body>привет</body></html>")

        only_md = area(self.root, ("markdown",))
        self.assertIsNotNone(reg.extract(md_path, only_md))
        self.assertIsNone(reg.extract(html_path, only_md))

        only_html = area(self.root, ("html",))
        self.assertIsNone(reg.extract(md_path, only_html))
        self.assertIsNotNone(reg.extract(html_path, only_html))

    def test_format_name_restricts_pub_formats(self):
        # fb2 — конкретный формат класса pub-formats: только он, без text
        reg = default_registry()
        fb2_path = self._write(
            "a.fb2",
            "<?xml version='1.0'?><FictionBook xmlns='x'>"
            "<body><p>fb2 текст</p></body></FictionBook>",
        )
        txt_path = self._write("a.txt", "обычный текст")
        only_fb2 = area(self.root, ("fb2",))
        self.assertIsNotNone(reg.extract(fb2_path, only_fb2))
        self.assertIsNone(reg.extract(txt_path, only_fb2))
        # класс pub-formats по-прежнему включает fb2
        self.assertIsNotNone(reg.extract(fb2_path, area(self.root, ("pub-formats",))))

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


class EncodingTest(unittest.TestCase):
    """Детектор кодировок: BOM → UTF-8 → cp1251 → cp1252 → latin-1."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_bytes(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_utf8_bytes_kept(self):
        path = self._write_bytes("u.txt", "Привет, мир".encode())
        self.assertEqual(TextExtractor().extract(path).text, "Привет, мир")

    def test_utf8_bom_stripped(self):
        path = self._write_bytes("u.txt", b"\xef\xbb\xbf" + "Привет".encode())
        self.assertEqual(TextExtractor().extract(path).text, "Привет")

    def test_cp1251_text(self):
        path = self._write_bytes("c.txt", "Привет, мир!".encode("cp1251"))
        self.assertEqual(TextExtractor().extract(path).text, "Привет, мир!")

    def test_markdown_cp1251(self):
        path = self._write_bytes("c.md", "# Заголовок\n#тэг тело".encode("cp1251"))
        result = MarkdownExtractor().extract(path)
        self.assertEqual(result.text, "# Заголовок\n#тэг тело")
        self.assertEqual(result.tags, ("тэг",))

    def test_html_cp1251(self):
        html = "<html><head><title>Титул</title></head><body>тело</body></html>"
        path = self._write_bytes("c.html", html.encode("cp1251"))
        result = HtmlExtractor().extract(path)
        self.assertEqual(result.text, "тело")
        self.assertIn("Титул", result.tags)

    def test_cp1252_text(self):
        # À/é — типичные байты западной кодовой таблицы (0xC0/0xE9)
        path = self._write_bytes("w.txt", b"caf\xe9 \xc0 propos")
        self.assertEqual(TextExtractor().extract(path).text, "café À propos")

    def test_fb2_cp1251(self):
        """fb2 с декларацией windows-1251 читается (ElementTree её не знает)."""
        xml = (
            "<?xml version='1.0' encoding='windows-1251'?>"
            "<FictionBook xmlns='http://example.com/fb2'>"
            "<body><p>Книга в кодировке cp1251</p><p>вторая строка</p></body>"
            "</FictionBook>"
        )
        path = self._write_bytes("book.fb2", xml.encode("cp1251"))
        result = Fb2Extractor().extract(path)
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "Книга в кодировке cp1251\n\nвторая строка")

    def test_fb2_utf8_no_declaration(self):
        xml = (
            "<FictionBook xmlns='http://example.com/fb2'>"
            "<body><p>Просто текст</p></body></FictionBook>"
        )
        path = self._write_bytes("book2.fb2", xml.encode())
        result = Fb2Extractor().extract(path)
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "Просто текст")


if __name__ == "__main__":
    unittest.main()
