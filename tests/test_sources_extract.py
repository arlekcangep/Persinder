"""Тесты экстракторов Markdown/текста и извлечения тэгов."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.sources import extract, extract_markdown, extract_tags


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
        result = extract_markdown(self._write("n.md", text))
        lowered = {t.lower() for t in result.tags}
        self.assertEqual(lowered, {"a", "b", "c", "d"})
        self.assertNotIn("---", result.text)

    def test_frontmatter_list_tags(self):
        text = "---\ntags:\n  - alpha\n  - beta\n---\nтело\n"
        result = extract_markdown(self._write("n.md", text))
        self.assertEqual({t.lower() for t in result.tags}, {"alpha", "beta"})

    def test_case_dedup(self):
        tags = extract_tags("#Foo и #foo и #FOO", None)
        self.assertEqual(len(tags), 1)

    def test_plain_text(self):
        result = extract(self._write("n.txt", "просто текст"))
        assert result is not None
        self.assertEqual(result.text, "просто текст")
        self.assertEqual(result.tags, ())

    def test_unsupported(self):
        result = extract(self._write("n.pdf", "%PDF"))
        self.assertIsNone(result)
        result_md = extract_markdown(self._write("n.md", "# Заголовок\n#tag"))
        self.assertEqual(result_md.text, "# Заголовок\n#tag")

    def test_markdown_no_frontmatter(self):
        result = extract_markdown(self._write("n.md", "# Заголовок\n#tag"))
        self.assertEqual(result.tags, ("tag",))
        self.assertTrue(result.text.startswith("# Заголовок"))


if __name__ == "__main__":
    unittest.main()
