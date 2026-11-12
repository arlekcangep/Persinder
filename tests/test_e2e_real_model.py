"""Сквозной тест на реальной модели e5 — пользовательский сценарий.

Здесь проверяется то, чего не видят синтетические тесты: настоящий эмбеддер,
настоящие сайдкары, настоящий ANN и поиск по смыслу. Инвариант простой: если
документ проиндексирован и у него посчитаны векторы, семантический поиск
обязан его находить.

Тест пропускается, если модель не установлена (``persinder model install``):
в CI без весов он не нужен, а на рабочей машине — обязателен.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.config import Settings
from persinder.domain import Area
from persinder.embed import EmbedWorker
from persinder.embed.onnx import OnnxEmbedder
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import default_stemmer
from persinder.search import SearchQuery, SearchService
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore
from persinder.vectorindex import UsannIndex, VectorSearch

MODEL = "e5-small-int8-v1"


def _model_dir() -> Path:
    return Settings.default().models_dir / MODEL


@unittest.skipUnless(
    (_model_dir() / "model.onnx").is_file(), f"модель {MODEL} не установлена"
)
class RealModelSearchTest(unittest.TestCase):
    """Поиск живой моделью: «медвежонок» должен находить «жил был медведь»."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        home = Path(self._tmp.name)
        self.settings = Settings(home=home)
        self.settings.ensure()
        self.store = SqliteStore(self.settings.db_path)
        self.embedder = OnnxEmbedder(_model_dir())
        self.ingest = IngestService(
            self.store,
            FsTextStore(self.settings.text_dir),
            default_stemmer(),
            virtual_dir=self.settings.virtual_dir,
            embedding_models=(MODEL,),
        )
        self.search = SearchService(
            self.store,
            default_stemmer(),
            vector_search=VectorSearch(
                self.store,
                lambda _key: self.embedder,
                search_model=MODEL,
                ann_dir=self.settings.ann_dir,
            ),
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _ingest(self, filename: str, text: str) -> str:
        result = self.ingest.ingest(
            IngestRequest(
                filename=filename, area=Area.PERSONAL, text=text, source_path=""
            )
        )
        self.assertIsNotNone(result.document_id, "документ должен проиндексироваться")
        doc_id = result.document_id
        assert doc_id is not None
        return doc_id

    def _embed(self) -> None:
        EmbedWorker(
            self.store,
            FsTextStore(self.settings.text_dir),
            lambda _key: self.embedder,
            virtual_dir=self.settings.virtual_dir,
            ann_dir=self.settings.ann_dir,
        ).run_once()

    def test_semantics_finds_derived_word(self):
        """«медвежонок» → «жил был медведь»: смысл, а не лексема.

        Это тот самый случай, который не работал вживую: морфология дериваты
        не связывает, значит документ обязан находить семантический слой.
        """
        doc_id = self._ingest("bear.txt", "жил был медведь")
        self._embed()

        result = self.search.search(SearchQuery(text="медвежонок", semantic_limit=5))

        self.assertTrue(result.semantic_hits, "семантика обязана найти документ")
        top = result.semantic_hits[0]
        self.assertEqual(top.document_id, doc_id)
        self.assertGreater(top.score, 0.85)

    def test_indexed_document_is_never_semantically_invisible(self):
        """Инвариант: у документа есть векторы и сайдкар ⇒ он находится.

        Раньше документ мог быть в индексе (морфология его видит), но не
        находиться семантикой: векторов нет в таблице, или ключа нет в ANN.
        Проверяем обе ситуации — после потери таблицы и после порчи индекса.
        """
        doc_id = self._ingest("bear.txt", "жил был медведь")
        self._embed()
        query = SearchQuery(text="медвежонок", semantic_limit=5)
        self.assertEqual(self.search.search(query).semantic_hits[0].document_id, doc_id)

        # 1. Таблица векторов потеряна (сайдкар остался): поиск не падает,
        #    а сервер при старте вернёт документ в очередь (см. enqueue_missing).
        self.store.drop_embeddings_model(MODEL)
        self.assertFalse(self.search.search(query).semantic_hits)

        # 2. Векторы вернули, но ANN-файл протух (ключей меньше, чем строк).
        self.store.put_embeddings(
            MODEL,
            [(doc_id, 0, self._vector_blob("жил был медведь"))],
        )
        stale = self.settings.ann_dir / f"{MODEL}.usearch"
        stale.unlink(missing_ok=True)
        stale.write_bytes(b"")
        hits = self.search.search(query).semantic_hits
        self.assertEqual(hits[0].document_id, doc_id, "точный скан спасает")

    def test_live_edit_moves_semantics_to_new_text(self):
        """Файл поправили при работающем сервисе: семантика едет за новым текстом.

        Так выглядит пользовательский сценарий: заметку дописали в редакторе,
        watcher переиндексировал файл. Новое содержимое — новый ``doc_id``
        (хэш текста), и вместе с ним должны переехать чанки, векторы, ключи
        ANN и сайдкар. Если хоть одно звено осталось от старой версии, поиск
        по смыслу будет искать по устаревшему тексту.
        """
        v1 = self._ingest("note.txt", "заметка про котиков и собак")
        self._embed()
        first = self.search.search(SearchQuery(text="котики", semantic_limit=5))
        self.assertEqual(first.semantic_hits[0].document_id, v1)

        v2 = self._ingest("note.txt", "жил был медведь, он приходил к пасеке за мёдом")
        self._embed()

        self.assertNotEqual(v2, v1, "новая версия — новый doc_id")
        self.assertEqual(self.store.counts()["documents"], 1)
        self.assertEqual(
            self.search.search(SearchQuery(text="котики", morph_limit=5)).hits, ()
        )
        hits = self.search.search(SearchQuery(text="медвежонок", semantic_limit=5))
        self.assertEqual(hits.semantic_hits[0].document_id, v2)
        self.assertGreater(hits.semantic_hits[0].score, 0.8)
        # Производные согласованы: чанки = векторы = ключи ANN.
        chunks = self.store.counts()["chunks"]
        ann = UsannIndex(self.settings.ann_dir / f"{MODEL}.usearch", self.embedder.dim)
        self.assertEqual(self.store.count_embeddings(MODEL), chunks)
        self.assertEqual(ann.size, chunks, "в ANN остались ключи старой версии")

    def _vector_blob(self, text: str) -> bytes:
        from persinder.embed import pack_f16_vectors

        return pack_f16_vectors([self.embedder.embed([text])[0]], self.embedder.dim)


if __name__ == "__main__":
    unittest.main()
