"""Источник в результатах поиска: area, путь и имя файла."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from persinder.domain import Area
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.search import SearchHit, SearchQuery, SearchService
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore
from persinder.vectorindex import VectorSearch


class _RecordingVectorSearch:
    """Заглушка векторного слоя: запоминает переданный порог."""

    def __init__(self) -> None:
        self.min_score: float | None = None

    def search(
        self, text: str, limit: int = 20, min_score: float | None = None
    ) -> list[SearchHit]:
        self.min_score = min_score
        return [SearchHit("semantic-doc", 0, 0.9)]


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

    def test_default_min_score_and_query_override(self):
        vector = _RecordingVectorSearch()
        service = SearchService(
            self.store,
            NoopStemmer(),
            vector_search=cast("VectorSearch", vector),
            default_min_score=0.8,
        )

        service.search(SearchQuery(text="кот"))
        self.assertEqual(vector.min_score, 0.8, "порог из конфига по умолчанию")

        service.search(SearchQuery(text="кот", min_score=0.95))
        self.assertEqual(vector.min_score, 0.95, "явный порог запроса перекрывает")


if __name__ == "__main__":
    unittest.main()
