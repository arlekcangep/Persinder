"""Тесты HtmlExtractor и MhtmlExtractor (стандартная библиотека)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.sources import HtmlExtractor, MhtmlExtractor, extract_html

HTML_PAGE = """
<html><head><title>Тестовая страница</title>
<style>body { color: red } .x { #notatag }</style>
<script>var a = "#injs";</script>
</head>
<body>
<h1>Заголовок</h1>
<p>Первый   абзац&nbsp;с текстом</p>
<div>Второй блок</div>
<ul><li>пункт один</li><li>пункт два</li></ul>
<table><tr><td>ячейка</td></tr></table>
line<br>break
<span>#тег в тексте</span>
</body></html>
"""


def mhtml_bytes(html: str) -> bytes:
    """Собрать MHTML-посылку с HTML-частью."""
    boundary = "----persinder-boundary"
    head = (
        "From: <test@example.com>\n"
        "Subject: saved page\n"
        f"MIME-Version: 1.0\n"
        f'Content-Type: multipart/related; boundary="{boundary}"\n\n'
    )
    part1 = (
        f"--{boundary}\n"
        "Content-Type: text/html; charset=utf-8\n"
        "Content-Transfer-Encoding: 8bit\n\n"
        f"{html}\n"
    )
    part2 = (
        f"--{boundary}\n"
        "Content-Type: image/png\n"
        "Content-Transfer-Encoding: base64\n"
        "Content-ID: <img1@example.com>\n\n"
        "iVBORw0KGgo=\n"
        f"--{boundary}--\n"
    )
    return (head + part1 + part2).encode("utf-8")


class HtmlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.extractor = HtmlExtractor()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_basic_text(self):
        result = extract_html(HTML_PAGE)
        self.assertIn("Заголовок", result.text)
        self.assertIn("Первый абзац с текстом", result.text)  # nbsp свёрнут
        self.assertIn("пункт один", result.text)
        self.assertIn("ячейка", result.text)
        self.assertIn("line", result.text)
        self.assertIn("break", result.text)

    def test_script_style_removed(self):
        result = extract_html(HTML_PAGE)
        self.assertNotIn("color: red", result.text)
        self.assertNotIn("#injs", result.text)

    def test_title_as_tag(self):
        result = extract_html(HTML_PAGE)
        self.assertIn("тестовая страница", {t.lower() for t in result.tags})

    def test_words_not_glued(self):
        # Блоки дают переносы: слова из разных блоков не слипаются.
        result = extract_html("<div>foo</div><div>bar</div>")
        self.assertNotIn("foobar", result.text)
        self.assertIn("foo", result.text)
        self.assertIn("bar", result.text)

    def test_inline_tag_extracted(self):
        result = extract_html("<p>текст #otchet конец</p>")
        self.assertIn("otchet", result.tags)

    def test_file_html(self):
        result = self.extractor.extract(self._write("p.html", HTML_PAGE))
        self.assertIn("Заголовок", result.text)

    def test_file_htm_suffix(self):
        self.assertEqual(self.extractor.suffixes(), frozenset({".html", ".htm"}))

    def test_broken_markup_no_crash(self):
        result = extract_html("<div><p>незакрыто всё")
        self.assertIn("незакрыто", result.text)


class MhtmlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.extractor = MhtmlExtractor()

    def tearDown(self):
        self._tmp.cleanup()

    def test_mhtml_page(self):
        path = self.root / "page.mht"
        path.write_bytes(mhtml_bytes(HTML_PAGE))
        result = self.extractor.extract(path)
        self.assertIsNone(result.error)
        self.assertIn("Заголовок", result.text)
        self.assertNotIn("color: red", result.text)

    def test_mhtml_suffixes(self):
        self.assertEqual(self.extractor.suffixes(), frozenset({".mht", ".mhtml"}))

    def test_plain_html_in_mht(self):
        # Некоторые сохранялки кладут HTML без multipart-обёртки.
        path = self.root / "flat.mht"
        path.write_text(HTML_PAGE, encoding="utf-8")
        result = self.extractor.extract(path)
        self.assertIn("Заголовок", result.text)

    def test_broken_mhtml(self):
        # Двоичный мусор: не должен уронить экстрактор; результат — что-то.
        path = self.root / "bad.mht"
        path.write_bytes(b"\x00\xff binary garbage \x00\xff")
        result = self.extractor.extract(path)
        self.assertTrue(result.text or result.error)


if __name__ == "__main__":
    unittest.main()
