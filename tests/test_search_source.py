"""Источник в результатах поиска: area, путь и имя файла."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.search import SearchQuery, SearchService
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore


class SearchSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.ingest = IngestService(
            self.store, self.texts, NoopStemmer(), chunk_size=1000, chunk_overlap=0
        )
        self.search = SearchService(self.store, NoopStemmer(), textstore=self.texts)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _ingest(
        self,
        filename: str,
        text: str,
        *,
        area: Area = Area.PERSONAL,
        source_path: str | None = None,
    ) -> None:
        result = self.ingest.ingest(
            IngestRequest(
                filename=filename,
                area=area,
                text=text,
                source_path=source_path,
            )
        )
        self.assertIsNotNone(result.document_id)

    def test_file_hit_has_area_path_and_filename(self):
        self._ingest("note.md", "кот сидит на коврике", source_path="/notes/note.md")

        result = self.search.search(SearchQuery(text="кот"))

        self.assertEqual(len(result.hits), 1)
        hit = result.hits[0]
        self.assertEqual(hit.area, "personal")
        self.assertEqual(hit.source_path, "/notes/note.md")
        self.assertEqual(hit.filename, "note.md")

    def test_virtual_hit_has_area_and_filename_without_path(self):
        self._ingest("web-note", "кот сидит на коврике", area=Area.WEB)

        result = self.search.search(SearchQuery(text="кот"))

        self.assertEqual(len(result.hits), 1)
        hit = result.hits[0]
        self.assertEqual(hit.area, "web")
        self.assertEqual(hit.source_path, "")
        self.assertEqual(hit.filename, "web-note")


if __name__ == "__main__":
    unittest.main()
