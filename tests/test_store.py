"""Тесты SQLite-метастора."""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Chunk, Document, EmbeddingJob
from persinder.embed import pack_f16_vectors
from persinder.store import SqliteStore

DIM = 4


def _blob(vector: list[float]) -> bytes:
    return pack_f16_vectors([vector], DIM)


class SqliteStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SqliteStore(Path(self._tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _save(
        self, doc_id: str = "d1", stemmed: str = "привет мир", filename: str = "a.txt"
    ) -> None:
        doc = Document(
            id=doc_id,
            content_hash=doc_id,
            title=None,
            char_count=10,
            text_key=doc_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
        chunks = [Chunk(index=0, char_start=0, char_end=10, stemmed=stemmed)]
        jobs = [
            EmbeddingJob(
                doc_id=doc_id,
                chunk_index=0,
                area="personal",
                created_at="2026-01-01T00:00:00+00:00",
            )
        ]
        self.store.save_document(doc, "personal", filename, chunks, jobs)

    def test_save_and_find_by_hash(self):
        self._save()
        self.assertEqual(self.store.find_document_by_hash("d1"), "d1")
        self.assertIsNone(self.store.find_document_by_hash("nope"))

    def test_search_morph(self):
        self._save(stemmed="привет мир нейросети")
        found = self.store.search_morph("нейросети")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["doc_id"], "d1")

    def test_chunk_span(self):
        self._save()
        self.assertEqual(self.store.chunk_span("d1", 0), (0, 10))
        self.assertIsNone(self.store.chunk_span("d1", 99))
        self.assertIsNone(self.store.chunk_span("nope", 0))

    def test_pending_embeddings(self):
        self._save()
        pending = self.store.pending_embeddings()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["doc_id"], "d1")
        self.assertEqual(pending[0]["model"], "placeholder-v1")

    def test_enqueue_document_for_models(self):
        self._save()
        self.store.enqueue_document("d1", "personal", "b.txt", models=["m1", "m2"])
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(len(pending), 3)  # placeholder-v1 + m1 + m2
        models = sorted(item["model"] for item in pending)
        self.assertEqual(models, ["m1", "m2", "placeholder-v1"])

    def test_drop_queue_model(self):
        self._save()
        self.store.enqueue_document("d1", "personal", "b.txt", models=["m1"])
        self.store.drop_queue_model("m1")
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual([item["model"] for item in pending], ["placeholder-v1"])

    def test_clear_queue(self):
        """Полная переиндексация начинает с чистой очереди (D25)."""
        self._save()
        self.store.enqueue_document("d1", "personal", "b.txt", models=["m1", "m2"])
        self.assertEqual(self.store.counts()["pending_embeddings"], 3)

        cleared = self.store.clear_queue()

        self.assertEqual(cleared, 3)
        self.assertEqual(self.store.counts()["pending_embeddings"], 0)
        self.assertEqual(self.store.counts()["pending_documents"], 0)

    def test_enqueue_all_for_models(self):
        self._save()
        self.store.enqueue_all(["m1", "m2"])
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(len(pending), 3)  # placeholder-v1 + m1 + m2

    def test_enqueue_missing_uses_resolver(self):
        self._save("d1")
        self.store.drop_queue_model("placeholder-v1")
        self.store.add_source("d1", "personal", "b.txt", source_path="/vault/a/b.md")
        self.store.add_source("d1", "personal", "c.txt", source_path="/vault/b/c.md")

        def models_for(_area: str, source_path: str) -> tuple[str, ...]:
            return ("m-virtual",) if not source_path else ("m-a",)

        self.store.enqueue_missing(
            ["global"], lambda _model, _doc, _path: False, models_for=models_for
        )
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(
            sorted(item["model"] for item in pending),
            ["m-a", "m-a", "m-virtual"],
            "резолвер выбирает модель по источнику, а не общий список",
        )

    def test_enqueue_missing_resolver_empty_skips(self):
        self._save()
        self.store.drop_queue_model("placeholder-v1")
        self.store.enqueue_missing(
            ["global"],
            lambda _model, _doc, _path: False,
            models_for=lambda _area, _path: (),
        )
        self.assertEqual(self.store.pending_embeddings(limit=100), [])

    def test_enqueue_missing_repairs_lost_vectors(self):
        """Сайдкар есть, а векторов в таблице нет — документ должен вернуться
        в очередь: иначе после восстановления БД он невидим для семантики."""
        self._save("d1")
        self.store.drop_queue_model("placeholder-v1")
        self.store.put_embeddings("m1", [("d1", 0, _blob([1.0, 0.0, 0.0, 0.0]))])

        def has_sidecar(_model: str, _doc: str, _path: str) -> bool:
            return True

        self.store.enqueue_missing(["m1"], has_sidecar)
        self.assertEqual(
            self.store.pending_embeddings(limit=100),
            [],
            "сайдкар и векторы на месте — задача не нужна",
        )

        self.store.drop_embeddings_model("m1")
        self.store.enqueue_missing(["m1"], has_sidecar)
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual([item["model"] for item in pending], ["m1"])

    def test_enqueue_missing_skips_docs_without_text(self):
        """Нет сырого текста — считать нечего: задачи не ставим.

        Иначе каждый старт сервиса заново кладёт тысячи задач документа,
        чей текст удалён, а воркер тут же снимает их как сиротские.
        """
        self._save("d1")
        self._save("d2", filename="d2.txt")
        self.store.drop_queue_model("placeholder-v1")

        self.store.enqueue_missing(
            ["placeholder-v1"],
            lambda _model, _doc, _path: False,
            has_text=lambda doc_id: doc_id != "d1",
        )
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(
            {item["doc_id"] for item in pending},
            {"d2"},
            "документ без текста в очередь не попадает",
        )

    def test_doc_ids_in_paths(self):
        self._save("d1")  # виртуальный источник (source_path = "")
        self.store.add_source("d1", "personal", "b.txt", source_path="/vault/a/b.md")
        self._save("d2", filename="d2.txt")
        self.store.add_source("d2", "personal", "d.txt", source_path="/vault/b/d.md")
        self.assertEqual(self.store.doc_ids_in_paths(["/vault/a/"]), {"d1"})
        self.assertEqual(self.store.doc_ids_in_paths(["/vault/"]), {"d1", "d2"})
        self.assertEqual(self.store.doc_ids_in_paths(["/other/"]), set())
        # виртуальные источники — только по явному разрешению
        self.assertEqual(
            self.store.doc_ids_in_paths([], include_virtual=True), {"d1", "d2"}
        )
        self.assertEqual(self.store.doc_ids_in_paths([]), set())

    def test_pending_embeddings_small_docs_first(self):
        """Очередь отдаёт малые документы раньше гигантских: большой PDF не
        замораживает появление сайдкаров для мелких файлов (порция 100 задач
        не захватывается целиком одним огромным документом)."""

        def save_doc(doc_id: str, n: int) -> None:
            doc = Document(
                id=doc_id,
                content_hash=doc_id,
                title=None,
                char_count=n * 10,
                text_key=doc_id,
                created_at="2026-01-01T00:00:00+00:00",
            )
            chunks = [
                Chunk(index=i, char_start=i * 10, char_end=i * 10 + 10, stemmed="слово")
                for i in range(n)
            ]
            jobs = [
                EmbeddingJob(
                    doc_id=doc_id,
                    chunk_index=i,
                    area="personal",
                    created_at="2026-01-01T00:00:00+00:00",
                )
                for i in range(n)
            ]
            self.store.save_document(doc, "personal", f"{doc_id}.txt", chunks, jobs)

        save_doc("big", 50)  # гигантский — в БД первый, задач больше всех
        save_doc("small", 1)  # мелкий — после
        pending = self.store.pending_embeddings(limit=100)
        self.assertEqual(pending[0]["doc_id"], "small", "мелкий документ первым")
        self.assertEqual(len(pending), 51)

    def test_all_sources(self):
        self._save()
        self.store.add_source("d1", "personal", "b.txt", source_path="/x/b.txt")
        sources = self.store.all_sources()
        self.assertTrue(any(row["source_path"] == "/x/b.txt" for row in sources))
        self.assertTrue(any(row["source_path"] == "" for row in sources))

    def test_sources_for_docs_prefers_real_file(self):
        self._save()
        self.store.add_source("d1", "web", "web-note", source_path="")
        self.store.add_source("d1", "personal", "a.txt", source_path="/notes/a.txt")

        rows = self.store.sources_for_docs(["d1", "nope"])

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["area"], "personal")
        self.assertEqual(rows[0]["filename"], "a.txt")
        self.assertEqual(rows[0]["source_path"], "/notes/a.txt")
        self.assertIn("", [row["source_path"] for row in rows])
        self.assertEqual(self.store.sources_for_docs([]), [])

    def test_document_source_keeps_container_refs(self):
        """Разбор пути части архива хранится явно: контейнер + цепочка записей."""
        self._save()
        self.store.add_source(
            "d1",
            "personal",
            "bundle.zip/inner.zip/note.md",
            source_path="/vault/bundle.zip/inner.zip/note.md",
            container_path="/vault/bundle.zip",
            container_entries=("inner.zip", "note.md"),
        )
        source = self.store.document_source("d1")
        assert source is not None
        self.assertEqual(source["container_path"], "/vault/bundle.zip")
        self.assertEqual(source["container_entries"], ["inner.zip", "note.md"])
        self.assertEqual(source["source_path"], "/vault/bundle.zip/inner.zip/note.md")

    def test_document_source_defaults_for_plain_file(self):
        self._save()
        source = self.store.document_source("d1")
        assert source is not None
        self.assertEqual(source["container_path"], "")
        self.assertEqual(source["container_entries"], [])

    def test_migrate_adds_container_refs_to_old_db(self):
        """Старая БД без рефов: колонки добавляются, у старых строк пустые рефы."""
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite"
            conn = sqlite3.connect(str(db))
            conn.executescript(
                """
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    title TEXT,
                    char_count INTEGER NOT NULL,
                    text_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    extractor TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE document_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    area TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(doc_id, area, filename)
                );
                INSERT INTO documents VALUES
                    ('d1', 'hash', 't', 3, 'hash', '2026-01-01', 'markdown');
                INSERT INTO document_sources
                    (doc_id, area, filename, source_path, created_at)
                    VALUES ('d1', 'personal', 'a.txt', '/vault/a.txt', '2026-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = SqliteStore(db)
            try:
                source = store.document_source("d1")
            finally:
                store.close()
        assert source is not None
        self.assertEqual(source["source_path"], "/vault/a.txt")
        self.assertEqual(source["container_path"], "")
        self.assertEqual(source["container_entries"], [])

    def test_migrate_renames_entries_column(self):
        """Промежуточное имя ``entries`` переименовывается, данные сохраняются."""
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite"
            conn = sqlite3.connect(str(db))
            conn.executescript(
                """
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    title TEXT,
                    char_count INTEGER NOT NULL,
                    text_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    extractor TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE document_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    area TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT '',
                    container_path TEXT NOT NULL DEFAULT '',
                    entries TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(doc_id, area, filename)
                );
                INSERT INTO documents VALUES
                    ('d1', 'hash', 't', 3, 'hash', '2026-01-01', 'markdown');
                INSERT INTO document_sources
                    (doc_id, area, filename, source_path, container_path,
                     entries, created_at)
                    VALUES ('d1', 'personal', 'a.zip/x.md', '/vault/a.zip/x.md',
                            '/vault/a.zip', '["x.md"]', '2026-01-01');
                """
            )
            conn.commit()
            conn.close()

            store = SqliteStore(db)
            try:
                source = store.document_source("d1")
            finally:
                store.close()
        assert source is not None
        self.assertEqual(source["container_path"], "/vault/a.zip")
        self.assertEqual(source["container_entries"], ["x.md"])

    def test_embeddings_roundtrip(self):
        self._save()
        blob = b"\x00\x3c" * 2  # 384 f16 = 768 байт: заполняем нулями
        self.store.put_embeddings("m1", [("d1", 0, blob), ("d1", 1, blob)])
        rows = self.store.embeddings_for_model("m1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["doc_id"], "d1")
        self.assertEqual(rows[0]["chunk_index"], 0)
        self.assertEqual(rows[0]["vector"], blob)
        # другие модели не смешиваются
        self.assertEqual(self.store.embeddings_for_model("m2"), [])

    def test_embeddings_replace_on_same_key(self):
        self._save()
        self.store.put_embeddings("m1", [("d1", 0, b"old")])
        self.store.put_embeddings("m1", [("d1", 0, b"new")])
        rows = self.store.embeddings_for_model("m1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vector"], b"new")

    def test_embeddings_ids_stable_across_upsert(self):
        """``id`` строки не меняется при перезаписи вектора — ключ для ANN (§8)."""
        self._save()
        ids = self.store.put_embeddings("m1", [("d1", 0, b"old"), ("d1", 1, b"x")])
        self.assertEqual(len(ids), 2)
        same = self.store.put_embeddings("m1", [("d1", 0, b"new")])
        self.assertEqual(same, [ids[0]], "перезапись вектора сохраняет id строки")
        self.assertEqual(
            {row["id"] for row in self.store.embeddings_ids_for_model("m1")},
            set(ids),
        )
        self.assertEqual(
            [row["doc_id"] for row in self.store.embeddings_for_ids([ids[0]])],
            ["d1"],
        )
        self.assertEqual(self.store.embedding_models(), ["m1"])
        self.assertEqual(self.store.embeddings_for_ids([]), [])

    def test_embeddings_cascade_on_doc_delete(self):
        """Удаление документа уносит его векторы (FK ON DELETE CASCADE, D24)."""
        self._save(doc_id="d1")
        self.store.put_embeddings("m1", [("d1", 0, b"v")])
        self.store.delete_document("d1")
        self.assertEqual(self.store.embeddings_for_model("m1"), [])

    def test_drop_embeddings_model(self):
        self._save()
        self.store.put_embeddings("m1", [("d1", 0, b"v")])
        self.store.put_embeddings("m2", [("d1", 0, b"v")])
        self.store.drop_embeddings_model("m1")
        self.assertEqual(self.store.embeddings_for_model("m1"), [])
        self.assertEqual(len(self.store.embeddings_for_model("m2")), 1)

    def test_counts(self):
        self._save()
        counts = self.store.counts()
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["chunks"], 1)
        self.assertEqual(counts["pending_embeddings"], 1)


if __name__ == "__main__":
    unittest.main()
