"""SQLite-метастор: документы, источники, чанки, FTS5, очередь эмбеддингов.

Потокобезопасен: HTTP-сервер обрабатывает запросы в отдельных потоках,
поэтому соединение открыто с ``check_same_thread=False`` и защищено блокировкой.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from ..domain import Chunk, Document, EmbeddingJob, utcnow
from .models import EmbeddingQueueItem, EmbeddingRow, MorphHit, SourceRow, StoreCounts

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    title        TEXT,
    char_count   INTEGER NOT NULL,
    text_key     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    extractor    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS document_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    area        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(doc_id, area, filename)
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    UNIQUE(doc_id, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    stemmed,
    doc_id UNINDEXED,
    chunk_index UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS embedding_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    area        TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT 'placeholder-v1',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    UNIQUE(doc_id, chunk_index, source_path, model)
);

CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    tag    TEXT NOT NULL,
    UNIQUE(doc_id, tag)
);

-- Векторы чанков под моделью: локальное производное для векторного поиска
-- (модуль 3). Сайдкар синхронизируется через Syncthing, эта таблица
-- пересобирается из него/воркером на каждом устройстве (§8).
-- ``id`` — стабильный ключ строки (ANN-индекс ссылается на чанк именно им);
-- ``vector`` — f16, размерность из модели.
CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model       TEXT NOT NULL,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    UNIQUE(model, doc_id, chunk_index)
);
"""


class SqliteStore:
    """Реализация MetaStore на SQLite (см. ``interfaces.MetaStore``)."""

    def __init__(self, db_path: Path | str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Добавить новые колонки в существующие базы (прототип, без версий)."""
        for table, column in (
            ("document_sources", "source_path"),
            ("embedding_queue", "source_path"),
            ("documents", "extractor"),
        ):
            cols = [
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            ]
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        self._migrate_queue_model()
        self._migrate_embeddings_id()

    def _migrate_embeddings_id(self) -> None:
        """Старая таблица ``embeddings`` (PK model+doc_id+chunk_index) → с id.

        ANN-индексу (USearch) нужен стабильный целочисленный ключ строки;
        старым строкам id раздаются по порядку.
        """
        cols = [
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(embeddings)")
        ]
        if "id" in cols:
            return
        self._conn.executescript(
            """
            ALTER TABLE embeddings RENAME TO embeddings_old;
            CREATE TABLE embeddings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT NOT NULL,
                doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                vector      BLOB NOT NULL,
                UNIQUE(model, doc_id, chunk_index)
            );
            INSERT INTO embeddings (model, doc_id, chunk_index, vector)
            SELECT model, doc_id, chunk_index, vector FROM embeddings_old;
            DROP TABLE embeddings_old;
            """
        )

    def _migrate_queue_model(self) -> None:
        """Очередь теперь модельная (колонка ``model`` + новый UNIQUE).

        Старая UNIQUE была ``(doc_id, chunk_index, source_path)`` — без модели
        сайдкары разных моделей конфликтовали бы. Таблица пересоздаётся,
        существующие задачи считаются моделью ``placeholder-v1``.
        """
        cols = [
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(embedding_queue)")
        ]
        if "model" in cols:
            return
        self._conn.executescript(
            """
            ALTER TABLE embedding_queue RENAME TO embedding_queue_old;
            CREATE TABLE embedding_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id      TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                area        TEXT NOT NULL,
                source_path TEXT NOT NULL DEFAULT '',
                model       TEXT NOT NULL DEFAULT 'placeholder-v1',
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                UNIQUE(doc_id, chunk_index, source_path, model)
            );
            INSERT INTO embedding_queue
                (id, doc_id, chunk_index, area, source_path, model, status, created_at)
            SELECT id, doc_id, chunk_index, area, source_path, 'placeholder-v1',
                   status, created_at
              FROM embedding_queue_old;
            DROP TABLE embedding_queue_old;
            """
        )

    # --- чтение ---

    def find_document_by_hash(self, content_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM documents WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return row["id"] if row else None

    def search_morph(self, stemmed_query: str, limit: int = 20) -> list[MorphHit]:
        """Морфологический поиск по FTS5 (запрос уже стеммирован)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, chunk_index, bm25(chunks_fts) AS score "
                "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                (stemmed_query, limit),
            ).fetchall()
        return [
            MorphHit(
                doc_id=str(row["doc_id"]),
                chunk_index=int(row["chunk_index"]),
                score=float(row["score"]),
            )
            for row in rows
        ]

    def chunk_span(self, doc_id: str, chunk_index: int) -> tuple[int, int] | None:
        """Границы чанка в сыром тексте: ``(char_start, char_end)``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT char_start, char_end FROM chunks "
                "WHERE doc_id = ? AND chunk_index = ?",
                (doc_id, chunk_index),
            ).fetchone()
        if row is None:
            return None
        return (int(row["char_start"]), int(row["char_end"]))

    def pending_embeddings(self, limit: int = 100) -> list[EmbeddingQueueItem]:
        """Задачи на эмбеддинги, малые документы первыми.

        ``ORDER BY`` — по числу невыполненных задач документа, затем по id:
        один гигантский документ (большой PDF — десятки тысяч чанков) не
        замораживает очередь, пока считаются мелкие — сайдкары продолжают
        появляться, а крупный молотится между ними.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, chunk_index, area, source_path, model "
                "FROM embedding_queue "
                "WHERE status = 'pending' "
                "ORDER BY (SELECT COUNT(*) FROM embedding_queue q2"
                "          WHERE q2.status = 'pending'"
                "          AND q2.doc_id = embedding_queue.doc_id),"
                "         id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._queue_item(row) for row in rows]

    def pending_for_docs(self, doc_ids: list[str]) -> list[EmbeddingQueueItem]:
        """Все незавершённые задачи по документам (обработка целиком)."""
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, doc_id, chunk_index, area, source_path, model "
                f"FROM embedding_queue WHERE status = 'pending' "
                f"AND doc_id IN ({placeholders}) ORDER BY id",
                doc_ids,
            ).fetchall()
        return [self._queue_item(row) for row in rows]

    def drop_queue_rows(self, ids: list[int]) -> None:
        """Убрать выполненные/брошенные задачи из очереди."""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"DELETE FROM embedding_queue WHERE id IN ({placeholders})", ids
            )

    def drop_queue_model(self, model: str) -> None:
        """Убрать задачи модели из очереди (модель удалена)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM embedding_queue WHERE model = ?", (model,))

    @staticmethod
    def _queue_item(row: sqlite3.Row) -> EmbeddingQueueItem:
        return EmbeddingQueueItem(
            id=int(row["id"]),
            doc_id=str(row["doc_id"]),
            chunk_index=int(row["chunk_index"]),
            area=str(row["area"]),
            source_path=str(row["source_path"]),
            model=str(row["model"]),
        )

    def counts(self) -> StoreCounts:
        """Служебная информация: сколько чего в базе."""
        with self._lock:

            def one(sql: str) -> int:
                return int(self._conn.execute(sql).fetchone()[0])

            return StoreCounts(
                documents=one("SELECT COUNT(*) FROM documents"),
                sources=one("SELECT COUNT(*) FROM document_sources"),
                chunks=one("SELECT COUNT(*) FROM chunks"),
                pending_embeddings=one(
                    "SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'"
                ),
            )

    # --- запись ---

    def _bind_source(
        self, doc_id: str, area: str, filename: str, source_path: str
    ) -> list[str]:
        """Закрепить ``(area, filename)`` за ``doc_id``, вытеснив прежний документ.

        У файла один источник: если за ``(area, filename)`` был закреплён
        другой документ (файл изменился), связь снимается, а сам документ
        вычищается, если у него не осталось других источников. Возвращает id
        вычищенных документов — по ним вызывающий удаляет сырой текст и
        сайдкары. Нужен открытый ``with`` (вызывается внутри транзакции).
        """
        stale = self._conn.execute(
            "SELECT DISTINCT doc_id FROM document_sources"
            " WHERE area = ? AND filename = ? AND doc_id != ?",
            (area, filename, doc_id),
        ).fetchall()
        self._conn.execute(
            "DELETE FROM document_sources"
            " WHERE area = ? AND filename = ? AND doc_id != ?",
            (area, filename, doc_id),
        )
        evicted: list[str] = []
        for row in stale:
            old_id = str(row["doc_id"])
            left = self._conn.execute(
                "SELECT COUNT(*) FROM document_sources WHERE doc_id = ?", (old_id,)
            ).fetchone()[0]
            if int(left) == 0:
                self._purge_document(old_id)
                evicted.append(old_id)
        self._conn.execute(
            "INSERT INTO document_sources"
            "(doc_id, area, filename, source_path, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(doc_id, area, filename) DO UPDATE SET source_path ="
            " CASE WHEN excluded.source_path != '' THEN excluded.source_path"
            " ELSE document_sources.source_path END",
            (doc_id, area, filename, source_path, utcnow()),
        )
        return evicted

    def add_source(
        self, doc_id: str, area: str, filename: str, source_path: str = ""
    ) -> list[str]:
        """Закрепить источник; вернуть id вытесненных документов (для чистки текста)."""
        with self._lock, self._conn:
            return self._bind_source(doc_id, area, filename, source_path)

    def has_source(self, area: str, filename: str) -> bool:
        """Есть ли в индексе источник ``(area, filename)``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM document_sources"
                " WHERE area = ? AND filename = ? LIMIT 1",
                (area, filename),
            ).fetchone()
        return row is not None

    def source_document(self, area: str, filename: str) -> str | None:
        """id документа, закреплённого за источником ``(area, filename)``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT doc_id FROM document_sources"
                " WHERE area = ? AND filename = ? LIMIT 1",
                (area, filename),
            ).fetchone()
        return str(row["doc_id"]) if row else None

    def source_path(self, area: str, filename: str) -> str:
        """Путь источника (пусто — виртуальный элемент)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT source_path FROM document_sources"
                " WHERE area = ? AND filename = ? LIMIT 1",
                (area, filename),
            ).fetchone()
        return str(row["source_path"]) if row else ""

    def source_filenames_under(self, area: str, prefix: str) -> list[str]:
        """Имена источников области, начинающихся с ``prefix`` (части контейнера)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT filename FROM document_sources"
                " WHERE area = ? AND substr(filename, 1, ?) = ?"
                " ORDER BY filename",
                (area, len(prefix), prefix),
            ).fetchall()
        return [str(row["filename"]) for row in rows]

    def _purge_document(self, doc_id: str) -> None:
        """Удалить документ и все производные строки (нужен открытый `with`)."""
        self._conn.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM embedding_queue WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM tags WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    def remove_source(self, area: str, filename: str) -> str | None:
        """Удалить источник ``(area, filename)``.

        Если у документа не осталось источников — документ удаляется целиком
        (чанки, FTS, очередь эмбеддингов). Возвращает id удалённого документа,
        если он вычищен полностью, иначе None.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT doc_id FROM document_sources WHERE area = ? AND filename = ?",
                (area, filename),
            ).fetchone()
            if row is None:
                return None
            doc_id = str(row["doc_id"])
            self._conn.execute(
                "DELETE FROM document_sources WHERE area = ? AND filename = ?",
                (area, filename),
            )
            left = self._conn.execute(
                "SELECT COUNT(*) FROM document_sources WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()[0]
            if int(left) == 0:
                self._purge_document(doc_id)
                return doc_id
            return None

    def delete_document(self, doc_id: str) -> None:
        """Удалить документ целиком (источники, чанки, FTS, очередь)."""
        with self._lock, self._conn:
            self._purge_document(doc_id)

    def save_document(
        self,
        doc: Document,
        area: str,
        filename: str,
        chunks: list[Chunk],
        jobs: list[EmbeddingJob],
        source_path: str = "",
    ) -> list[str]:
        """Атомарно записать документ, источник, чанки (с FTS5) и очередь.

        Прежний документ того же файла ``(area, filename)`` вытесняется
        (см. ``_bind_source``). Возвращает id вытесненных документов, чтобы
        вызывающий удалил их сырой текст и сайдкары.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO documents"
                "(id, content_hash, title, char_count, text_key, created_at, "
                " extractor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.id,
                    doc.content_hash,
                    doc.title,
                    doc.char_count,
                    doc.text_key,
                    doc.created_at,
                    doc.extractor,
                ),
            )
            evicted = self._bind_source(doc.id, area, filename, source_path)
            for chunk in chunks:
                cur = self._conn.execute(
                    "INSERT INTO chunks(doc_id, chunk_index, char_start, char_end) "
                    "VALUES (?, ?, ?, ?)",
                    (doc.id, chunk.index, chunk.char_start, chunk.char_end),
                )
                self._conn.execute(
                    "INSERT INTO chunks_fts(rowid, stemmed, doc_id, chunk_index) "
                    "VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, chunk.stemmed, doc.id, chunk.index),
                )
            self._conn.executemany(
                "INSERT OR IGNORE INTO embedding_queue"
                "(doc_id, chunk_index, area, source_path, model, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                [
                    (
                        j.doc_id,
                        j.chunk_index,
                        j.area,
                        j.source_path,
                        j.model,
                        j.created_at,
                    )
                    for j in jobs
                ],
            )
        return evicted

    def enqueue_document(
        self,
        doc_id: str,
        area: str,
        source_path: str = "",
        models: list[str] | None = None,
    ) -> None:
        """Поставить задачи для уже проиндексированного документа (дубликат).

        Документ повторяется в другом источнике: строки с ``(doc_id, chunk)``
        уже были, но у нового источника свой сайдкар — ставим его задачи под
        каждой из ``models`` (по умолчанию — placeholder-v1).
        """
        for model in models or ["placeholder-v1"]:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO embedding_queue"
                    "(doc_id, chunk_index, area, source_path, model, "
                    "status, created_at) "
                    "SELECT doc_id, chunk_index, ?, ?, ?, 'pending', ?"
                    " FROM chunks WHERE doc_id = ?",
                    (area, source_path, model, utcnow(), doc_id),
                )

    def enqueue_all(self, models: list[str]) -> None:
        """Поставить задачи для всех источников под указанными моделями.

        Нужно при установке новой модели (миграция): по ней пересчитываются
        все существующие источники — пакетами, обычными запусками embed.
        """
        for model in models:
            with self._lock, self._conn:
                rows = self._conn.execute(
                    "SELECT DISTINCT doc_id, area, source_path FROM document_sources"
                ).fetchall()
                for row in rows:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO embedding_queue"
                        "(doc_id, chunk_index, area, source_path, model, "
                        "status, created_at) "
                        "SELECT doc_id, chunk_index, ?, ?, ?, 'pending', ?"
                        " FROM chunks WHERE doc_id = ?",
                        (
                            str(row["area"]),
                            str(row["source_path"]),
                            model,
                            utcnow(),
                            str(row["doc_id"]),
                        ),
                    )

    def enqueue_missing(
        self,
        models: list[str],
        has_sidecar: Callable[[str, str, str], bool],
        models_for: Callable[[str, str], tuple[str, ...]] | None = None,
    ) -> None:
        """Поставить задачи только для источников без сайдкара под моделью.

        То же, что ``enqueue_all``, но уже отработанные источники (сайдкар на
        месте) в очередь не возвращает. Нужно ``--reindex``: рестарт не должен
        перезаполнять очередь всем подряд — только тем, чего реально нет.

        ``models_for(area, source_path)`` — модели конкретного источника по
        конфигу (своя модель папки, выключенная семантика); ``None`` — брать
        общий список ``models``. Пустой результат — источник не индексируется
        семантически.
        """
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT DISTINCT doc_id, area, source_path FROM document_sources"
            ).fetchall()
            for row in rows:
                doc_id, area, source_path = (
                    str(row["doc_id"]),
                    str(row["area"]),
                    str(row["source_path"]),
                )
                chosen = (
                    models_for(area, source_path)
                    if models_for is not None
                    else tuple(models)
                )
                for model in chosen:
                    if has_sidecar(model, doc_id, source_path):
                        continue
                    self._conn.execute(
                        "INSERT OR IGNORE INTO embedding_queue"
                        "(doc_id, chunk_index, area, source_path, model, "
                        "status, created_at) "
                        "SELECT doc_id, chunk_index, ?, ?, ?, 'pending', ?"
                        " FROM chunks WHERE doc_id = ?",
                        (area, source_path, model, utcnow(), doc_id),
                    )

    def doc_ids_in_paths(
        self, prefixes: list[str], *, include_virtual: bool = False
    ) -> set[str]:
        """id документов, у которых есть источник под одним из префиксов путей.

        Нужно для скоупа векторного поиска: модель ищет только там, где ею
        индексировали, — старые векторы из других папок не дают мусор.
        ``prefixes`` — каталоги областей (как хранится ``source_path``);
        ``include_virtual`` — добавить источники без файла на диске
        (``source_path = ''``, web/вставленный текст).
        """
        conditions: list[str] = []
        params: list[object] = []
        for prefix in prefixes:
            if not prefix:
                continue
            conditions.append("substr(source_path, 1, ?) = ?")
            params.extend([len(prefix), prefix])
        if include_virtual:
            conditions.append("source_path = ''")
        if not conditions:
            return set()
        sql = "SELECT DISTINCT doc_id FROM document_sources WHERE " + " OR ".join(
            conditions
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {str(row["doc_id"]) for row in rows}

    def all_sources(self) -> list[SourceRow]:
        """Все источники (для удаления сайдкаров модели)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, area, filename, source_path FROM document_sources"
            ).fetchall()
        return [
            SourceRow(
                doc_id=str(row["doc_id"]),
                area=str(row["area"]),
                filename=str(row["filename"]),
                source_path=str(row["source_path"]),
            )
            for row in rows
        ]

    # --- эмбеддинги (локальное производное для векторного поиска, модуль 3) ---

    def put_embeddings(
        self, model: str, rows: list[tuple[str, int, bytes]]
    ) -> list[int]:
        """Записать/заменить векторы чанков документа под моделью.

        ``rows`` — ``(doc_id, chunk_index, vector)``; ``vector`` — f16-блоб
        (см. ``sidecar.pack_f16_vectors``). Возвращает стабильные ``id`` строк
        в том же порядке (по ним ANN-индекс ссылается на чанки). Строки
        удаляются каскадом при удалении документа (FK ON DELETE CASCADE).
        """
        ids: list[int] = []
        with self._lock, self._conn:
            for doc_id, chunk_index, vector in rows:
                self._conn.execute(
                    "INSERT INTO embeddings(model, doc_id, chunk_index, vector) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(model, doc_id, chunk_index) "
                    "DO UPDATE SET vector = excluded.vector",
                    (model, doc_id, chunk_index, vector),
                )
                row = self._conn.execute(
                    "SELECT id FROM embeddings"
                    " WHERE model = ? AND doc_id = ? AND chunk_index = ?",
                    (model, doc_id, chunk_index),
                ).fetchone()
                ids.append(int(row["id"]))
        return ids

    def embeddings_for_model(self, model: str) -> list[EmbeddingRow]:
        """Все векторы чанков под моделью (для точного скана-фолбэка)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, chunk_index, vector FROM embeddings "
                "WHERE model = ? ORDER BY doc_id, chunk_index",
                (model,),
            ).fetchall()
        return [self._embedding_row(row) for row in rows]

    def embeddings_for_doc(self, model: str, doc_id: str) -> list[EmbeddingRow]:
        """Векторы чанков одного документа под моделью.

        Нужно воркеру, чтобы не пересчитывать чанки, которые уже в БД
        (упавший прогон оставил векторы, но не сайдкар): повторный проход
        берёт их из таблицы и считает только недостающие.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, chunk_index, vector FROM embeddings "
                "WHERE model = ? AND doc_id = ? ORDER BY chunk_index",
                (model, doc_id),
            ).fetchall()
        return [self._embedding_row(row) for row in rows]

    def embeddings_ids_for_model(self, model: str) -> list[EmbeddingRow]:
        """Все строки модели (для реконсиляции ANN-индекса с таблицей)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, chunk_index, vector FROM embeddings "
                "WHERE model = ? ORDER BY id",
                (model,),
            ).fetchall()
        return [self._embedding_row(row) for row in rows]

    def embeddings_for_ids(self, ids: list[int]) -> list[EmbeddingRow]:
        """Строки по ``id`` (ANN-хиты → doc_id/chunk; протухшие отсекаются)."""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, doc_id, chunk_index, vector FROM embeddings "
                f"WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        return [self._embedding_row(row) for row in rows]

    @staticmethod
    def _embedding_row(row: sqlite3.Row) -> EmbeddingRow:
        return EmbeddingRow(
            id=int(row["id"]),
            doc_id=str(row["doc_id"]),
            chunk_index=int(row["chunk_index"]),
            vector=bytes(row["vector"]),
        )

    def drop_embeddings_model(self, model: str) -> None:
        """Убрать векторы модели (``model remove`` — вместе с сайдкарами)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM embeddings WHERE model = ?", (model,))

    def embedding_models(self) -> list[str]:
        """Модели, у которых есть векторы (для ``persinder ann rebuild``)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT model FROM embeddings ORDER BY model"
            ).fetchall()
        return [str(row["model"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
