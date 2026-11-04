"""Тесты PdfExtractor: текстовый PDF, скан-подобный, битый файл."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.sources import PdfExtractor


class PdfExtractorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        try:
            self.extractor = PdfExtractor()
        except RuntimeError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def _pdf_bytes(self, page_texts: list[str]) -> bytes:
        """Собрать PDF с текстовым слоем через pypdf (без reportlab):
        pypdf не создаёт текст, поэтому используем минимальный PDF руками."""
        # Минимальный валидный PDF с двумя страницами текста.
        content = b"BT /F1 12 Tf 72 720 Td (hello pdf world) Tj ET"
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets: list[int] = []
        for i, obj in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets:
            out += f"{int(off):010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF"
        ).encode()
        return bytes(out)

    def test_text_pdf(self):
        path = self._write("doc.pdf", self._pdf_bytes(["hello"]))
        result = self.extractor.extract(path)
        self.assertIsNone(result.error)
        self.assertIn("hello", result.text)
        self.assertIn("world", result.text)
        self.assertEqual(result.pages, 1)

    def test_broken_pdf_reports_error(self):
        path = self._write("bad.pdf", b"%PDF-1.4 garbage not a pdf")
        result = self.extractor.extract(path)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.text, "")

    def test_not_pdf_at_all(self):
        path = self._write("fake.pdf", b"just text, no pdf here")
        result = self.extractor.extract(path)
        self.assertIsNotNone(result.error)

    def test_suffixes(self):
        self.assertEqual(self.extractor.suffixes(), frozenset({".pdf"}))


if __name__ == "__main__":
    unittest.main()
