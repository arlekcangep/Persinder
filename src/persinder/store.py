"""SQLite-метастор: документы, источники, чанки, FTS5, очередь эмбеддингов.

Потокобезопасен: HTTP-сервер обрабатывает запросы в отдельных потоках,
поэтому соединение открыто с ``check_same_thread=False`` и защищено блокировкой.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Chunk, Document, EmbeddingJob, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    title        TEXT,
    char_count   INTEGER NOT NULL,
    text_key     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    area       TEXT NOT NULL,
    filename   TEXT NOT NULL,
    created_at TEXT NOT NULL,
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
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    UNIQUE(doc_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    tag    TEXT NOT NULL,
    UNIQUE(doc_id, tag)
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
        self._conn.commit()

    # --- чтение ---

    def find_document_by_hash(self, content_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM documents WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return row["id"] if row else None

    def search_morph(self, stemmed_query: str, limit: int = 20) -> list[dict]:
        """Морфологический поиск по FTS5 (запрос уже стеммирован)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, chunk_index, bm25(chunks_fts) AS score "
                "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                (stemmed_query, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_embeddings(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, chunk_index, area FROM embedding_queue "
                "WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        """Служебная информация: сколько чего в базе."""
        with self._lock:
            def one(sql: str) -> int:
                return int(self._conn.execute(sql).fetchone()[0])

            return {
                "documents": one("SELECT COUNT(*) FROM documents"),
                "sources": one("SELECT COUNT(*) FROM document_sources"),
                "chunks": one("SELECT COUNT(*) FROM chunks"),
                "pending_embeddings": one(
                    "SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'"
                ),
            }

    # --- запись ---

    def add_source(self, doc_id: str, area: str, filename: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO document_sources"
                "(doc_id, area, filename, created_at) VALUES (?, ?, ?, ?)",
                (doc_id, area, filename, utcnow()),
            )

    def save_document(
        self,
        doc: Document,
        area: str,
        filename: str,
        chunks: list[Chunk],
        jobs: list[EmbeddingJob],
    ) -> None:
        """Атомарно записать документ, источник, чанки (с FTS5) и очередь."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO documents"
                "(id, content_hash, title, char_count, text_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc.id,
                    doc.content_hash,
                    doc.title,
                    doc.char_count,
                    doc.text_key,
                    doc.created_at,
                ),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO document_sources"
                "(doc_id, area, filename, created_at) VALUES (?, ?, ?, ?)",
                (doc.id, area, filename, utcnow()),
            )
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
                "(doc_id, chunk_index, area, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                [(j.doc_id, j.chunk_index, j.area, j.created_at) for j in jobs],
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
