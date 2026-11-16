"""Тесты RTF-экстрактора: текст без управляющих последовательностей.

RTF — структурированный текст (класс ``pub-formats``); на данном этапе нужен
только текст, управляющие последовательности отбрасываются (striprtf, extra
``rtf``). ``striprtf`` — чистый Python, без компиляции.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    RtfExtractor,
    default_registry,
)
from persinder.sources.classify import PUB_FORMATS, ClassMatch

# Классы, разрешённые тестовой областью: всё, что даёт текстовые части.
TESTS_CLASSES = ("structure-text", "text", "pub-formats")

# Минимальный RTF с текстом и управляющими последовательностями.
SAMPLE = (
    r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Arial;}} \f0\fs24 "
    r"Привет, RTF! \par Вторая строка. {\b жирный} конец.}"
)


def _area(root: Path) -> AreaConfig:
    return AreaConfig(path=root, role=Area.PERSONAL, formats=TESTS_CLASSES)


class RtfExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_extract_plain_text(self):
        """Управляющие последовательности убраны, виден только текст."""
        try:
            import striprtf  # noqa: F401
        except ImportError:
            self.skipTest("striprtf не установлен (extra rtf)")
        path = self._write("note.rtf", SAMPLE.encode("cp1251"))
        result = RtfExtractor().extract(path)
        self.assertIsNone(result.error)
        self.assertIn("Привет, RTF!", result.text)
        self.assertIn("Вторая строка.", result.text)
        self.assertIn("жирный", result.text)
        # управляющие последовательности не должны попасть в текст
        self.assertNotIn("\\rtf", result.text)
        self.assertNotIn("\\par", result.text)
        self.assertNotIn("\\b ", result.text)

    def test_utf8_rtf(self):
        try:
            import striprtf  # noqa: F401
        except ImportError:
            self.skipTest("striprtf не установлен (extra rtf)")
        path = self._write("note.rtf", SAMPLE.encode("utf-8"))
        result = RtfExtractor().extract(path)
        self.assertIn("Привет, RTF!", result.text)

    def test_broken_rtf_reports_error(self):
        try:
            import striprtf  # noqa: F401
        except ImportError:
            self.skipTest("striprtf не установлен (extra rtf)")
        path = self._write("bad.rtf", b"{\\rtf1\\ansi\\deff0 broken")
        result = RtfExtractor().extract(path)
        self.assertIsNone(result.error)  # striprtf устойчив к усечённому RTF
        self.assertIn("broken", result.text)

    def test_registry_recognizes_rtf(self):
        """RTF попадает в pub-formats и извлекается через реестр."""
        try:
            import striprtf  # noqa: F401
        except ImportError:
            self.skipTest("striprtf не установлен (extra rtf)")
        reg = default_registry()
        path = self._write("book.rtf", SAMPLE.encode("cp1251"))
        parts = reg.extract(path, _area(self.root))
        self.assertIsNotNone(parts)
        assert parts is not None
        self.assertEqual(parts[0].extractor, "rtf")
        self.assertIn("Привет, RTF!", parts[0].text)

    def test_classifier_magic(self):
        """Классификатор опознаёт RTF по сигнатуре «{\\rtf» (не расширению)."""
        from persinder.sources.classify import default_classifiers

        matcher = default_classifiers()
        matches = matcher.classify(SAMPLE.encode("cp1251"), extension="")
        self.assertIn(
            ClassMatch(PUB_FORMATS, "rtf", "magic"),
            matches,
        )


if __name__ == "__main__":
    unittest.main()
