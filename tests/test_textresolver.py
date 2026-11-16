"""Тесты ``TextResolver``: кэш, файл, часть контейнера, причины отказа.

Резолвер — единственное место, где решается «откуда взять текст документа»
(его же использует воркер эмбеддингов). Здесь проверяется источник текста;
срез по символам и коды API — в ``test_context_api.py``.
"""

from __future__ import annotations

import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Document
from persinder.sources import default_registry
from persinder.sources.textresolver import SOURCE_MISSING, TextResolver
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore

TEXT = "текст оригинала"


class DocumentTextTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = SqliteStore(self.root / "meta.db")
        self.texts = FsTextStore(self.root / "texts")
        self.registry = default_registry()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _resolver(self, registry=None) -> TextResolver:
        return TextResolver(self.store, self.texts, registry)

    def _save(
        self,
        doc_id: str,
        source_path: str = "",
        extractor: str = "",
        container_path: str = "",
        container_entries: tuple[str, ...] = (),
    ) -> None:
        self.store.save_document(
            Document(
                id=doc_id,
                content_hash=doc_id,
                title="t",
                char_count=1,
                text_key=doc_id,
                created_at="2026-01-01T00:00:00Z",
                extractor=extractor,
            ),
            "personal",
            f"{doc_id}.txt",
            [],
            [],
            source_path=source_path,
            container_path=container_path,
            container_entries=container_entries,
        )

    def test_cache_first(self):
        """Текст в кэше — оригинал не читаем (файла может вообще не быть)."""
        self.texts.put("doc1", TEXT)
        self._save("doc1", source_path=str(self.root / "gone.md"), extractor="markdown")

        document = self._resolver(self.registry).document_text("doc1")

        self.assertEqual(document.text, TEXT)
        self.assertTrue(document.from_cache)
        self.assertIsNone(document.code)

    def test_reads_file(self):
        path = self.root / "doc.md"
        path.write_text(TEXT, encoding="utf-8")
        self._save("doc1", source_path=str(path), extractor="markdown")

        document = self._resolver(self.registry).document_text("doc1")

        self.assertEqual(document.text, TEXT)
        self.assertFalse(document.from_cache)
        self.assertIsNone(document.code)
        self.assertEqual(document.content_hash, "doc1")

    def test_reads_container_part_addressably(self):
        zip_path = self.root / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("inner.md", TEXT)
        self._save(
            "doc1",
            source_path=str(zip_path / "inner.md"),
            extractor="markdown",
            container_path=str(zip_path),
            container_entries=("inner.md",),
        )

        document = self._resolver(self.registry).document_text("doc1")

        self.assertEqual(document.text, TEXT)
        self.assertIsNone(document.code)

    def test_missing_container_entry(self):
        zip_path = self.root / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("other.md", "не то")
        self._save(
            "doc1",
            source_path=str(zip_path / "inner.md"),
            extractor="markdown",
            container_path=str(zip_path),
            container_entries=("inner.md",),
        )

        document = self._resolver(self.registry).document_text("doc1")

        self.assertIsNone(document.text)
        self.assertEqual(document.code, "entry_missing")
        # снаружи это ``error`` с пояснением, а не «источник отсутствует»
        _, status = self._resolver(self.registry).resolve("doc1", 0, 5)
        self.assertTrue(status is not None and status.startswith("error"))

    def test_unknown_document_is_missing(self):
        document = self._resolver(self.registry).document_text("no-such-doc")
        self.assertIsNone(document.text)
        self.assertEqual(document.code, "no_document")
        _, status = self._resolver(self.registry).resolve("no-such-doc", 0, 5)
        self.assertEqual(status, SOURCE_MISSING)

    def test_virtual_document_is_missing(self):
        self._save("doc1", extractor="")
        document = self._resolver(self.registry).document_text("doc1")
        self.assertIsNone(document.text)
        self.assertEqual(document.code, "virtual")

    def test_without_registry(self):
        path = self.root / "doc.md"
        path.write_text(TEXT, encoding="utf-8")
        self._save("doc1", source_path=str(path), extractor="markdown")

        document = self._resolver(None).document_text("doc1")

        self.assertIsNone(document.text)
        self.assertEqual(document.code, "no_registry")

    def test_unknown_extractor(self):
        path = self.root / "doc.md"
        path.write_text(TEXT, encoding="utf-8")
        self._save("doc1", source_path=str(path), extractor="nope")

        document = self._resolver(self.registry).document_text("doc1")

        self.assertIsNone(document.text)
        self.assertEqual(document.code, "unknown_extractor")

    def test_missing_file(self):
        self._save("doc1", source_path=str(self.root / "gone.md"), extractor="markdown")
        document = self._resolver(self.registry).document_text("doc1")
        self.assertIsNone(document.text)
        self.assertEqual(document.code, "missing_file")


if __name__ == "__main__":
    unittest.main()
