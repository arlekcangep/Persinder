"""Тесты ``/context`` и резолвера текста (шаг 2, вариант C).

Покрывают три уровня:

* ``TextResolver`` — источник текста (textstore → перечитывание оригинала),
  резку по символьным смещениям и статусы ``source_missing``/``error``;
* ``context_stream`` / ``context_stream_batch`` — NDJSON-фрагменты
  (включая чанкование длинных срезов);
* интеграцию через реальный uvicorn: ``/context`` поверх textstore и
  перечитывание после потери текста.

Смещения ``start``/``end`` — символы документа (вариант б).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from persinder.api.fastapi_server import (
    context_stream,
    context_stream_batch,
    make_fastapi_server,
)
from persinder.api.textresolver import SOURCE_ERROR, SOURCE_MISSING, TextResolver
from persinder.domain import Area, Document
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.search import SearchService
from persinder.sources import default_registry
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore

TEXT = "Привет мир"  # 10 символов, 19 байт в UTF-8: 12 + 1 + 6


def _save_document(
    store: SqliteStore,
    doc_id: str,
    source_path: str = "",
    extractor: str = "",
) -> None:
    """Документ без чанков и очереди: для перечитывания оригинала."""
    store.save_document(
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
    )


def _fragments(body: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in body.splitlines() if line]


class TextResolverTest(unittest.TestCase):
    """Резолвер: textstore, перечитывание, ошибки, границы UTF-8."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.registry = default_registry()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _resolver(self, registry=None) -> TextResolver:
        return TextResolver(self.store, self.texts, registry)

    # --- textstore ---

    def test_resolve_from_textstore(self):
        """Текст из textstore: резка по символам без перечитывания."""
        self.texts.put("doc1", TEXT)
        resolver = self._resolver()
        text, error = resolver.resolve("doc1", 0, 6)
        self.assertIsNone(error)
        assert text is not None
        self.assertEqual(text.text, "Привет")
        self.assertEqual(text.total_chars, 10)

    def test_resolve_offset_shifts_window(self):
        self.texts.put("doc1", TEXT)
        resolver = self._resolver()
        text, error = resolver.resolve("doc1", 0, 3, offset=7)
        self.assertIsNone(error)
        assert text is not None
        self.assertEqual(text.text, "мир")

    def test_resolve_slices_by_chars(self):
        """Символьные смещения режут по символам — никаких байтовых границ."""
        self.texts.put("doc1", TEXT)
        resolver = self._resolver()
        text, _ = resolver.resolve("doc1", 1, 6)
        assert text is not None
        self.assertEqual(text.text, "ривет")
        text, _ = resolver.resolve("doc1", 4, 5)
        assert text is not None
        self.assertEqual(text.text, "е")

    def test_resolve_clamps_to_document_bounds(self):
        self.texts.put("doc1", TEXT)
        resolver = self._resolver()
        text, _ = resolver.resolve("doc1", 0, 10_000)
        assert text is not None
        self.assertEqual(text.text, TEXT)
        self.assertEqual(text.total_chars, 10)

    def test_resolve_invalid_range(self):
        self.texts.put("doc1", TEXT)
        resolver = self._resolver()
        text, error = resolver.resolve("doc1", 6, 0)
        self.assertIsNone(text)
        self.assertIn("invalid", error or "")

    # --- перечитывание оригинала ---

    def test_resolve_rereads_source_file(self):
        """Текста в textstore нет — оригинал перечитывается экстрактором."""
        file = Path(self._tmp.name) / "doc.txt"
        file.write_text(TEXT, encoding="utf-8")
        _save_document(self.store, "doc1", source_path=str(file), extractor="text")
        resolver = self._resolver(registry=self.registry)
        text, error = resolver.resolve("doc1", 0, 6)
        self.assertIsNone(error)
        assert text is not None
        self.assertEqual(text.text, "Привет")

    def test_resolve_reread_missing_file_is_error(self):
        """Файл удалён, textstore пуст — честная ошибка, не молчаливый пусто."""
        missing = Path(self._tmp.name) / "gone.txt"
        _save_document(self.store, "doc1", source_path=str(missing), extractor="text")
        resolver = self._resolver(registry=self.registry)
        text, error = resolver.resolve("doc1", 0, 6)
        self.assertIsNone(text)
        assert error is not None
        self.assertIn(SOURCE_ERROR, error)

    def test_resolve_unknown_extractor_is_error(self):
        """Реестр не знает имя экстрактора — перечитывать нечем."""
        file = Path(self._tmp.name) / "doc.txt"
        file.write_text(TEXT, encoding="utf-8")
        _save_document(self.store, "doc1", source_path=str(file), extractor="nope")
        resolver = self._resolver(registry=self.registry)
        text, error = resolver.resolve("doc1", 0, 6)
        self.assertIsNone(text)
        self.assertEqual(error, SOURCE_ERROR)

    def test_resolve_without_registry_is_error(self):
        """``registry=None``: перечитывание невозможно — ошибка, не краш."""
        file = Path(self._tmp.name) / "doc.txt"
        file.write_text(TEXT, encoding="utf-8")
        _save_document(self.store, "doc1", source_path=str(file), extractor="text")
        resolver = self._resolver(registry=None)
        text, error = resolver.resolve("doc1", 0, 6)
        self.assertIsNone(text)
        self.assertEqual(error, SOURCE_ERROR)

    def test_resolve_virtual_document_without_text_is_missing(self):
        """Виртуальный документ (source_path='') без textstore — source_missing."""
        _save_document(self.store, "doc1", source_path="", extractor="")
        resolver = self._resolver()
        text, error = resolver.resolve("doc1", 0, 6)
        self.assertIsNone(text)
        self.assertEqual(error, SOURCE_MISSING)

    def test_resolve_unknown_document_is_missing(self):
        resolver = self._resolver()
        text, error = resolver.resolve("no-such-doc", 0, 6)
        self.assertIsNone(text)
        self.assertEqual(error, SOURCE_MISSING)


class ContextStreamTest(unittest.TestCase):
    """NDJSON-фрагменты: успех, ошибки, чанкование, батч."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.resolver = TextResolver(
            self.store, self.texts, default_registry()
        )
        self.texts.put("doc1", TEXT)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    async def _stream(self, *args) -> bytes:
        chunks: list[bytes] = []
        async for b in context_stream(self.resolver, *args):
            chunks.append(b)
        return b"".join(chunks)

    def test_stream_fragment(self):
        body = asyncio.run(self._stream("doc1", 0, 6, "utf-8"))
        frags = _fragments(body)
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["type"], "fragment")
        self.assertEqual(frags[0]["doc_id"], "doc1")
        self.assertEqual(frags[0]["text"], "Привет")
        self.assertEqual((frags[0]["start"], frags[0]["end"]), (0, 6))

    def test_stream_without_resolver_is_missing(self):
        async def collect():
            chunks = []
            async for b in context_stream(None, "doc1", 0, 6, "utf-8"):
                chunks.append(b)
            return chunks

        body = b"".join(asyncio.run(collect()))
        frags = _fragments(body)
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["error"], SOURCE_MISSING)

    def test_stream_unknown_doc_is_missing(self):
        body = asyncio.run(self._stream("unknown", 0, 6, "utf-8"))
        frags = _fragments(body)
        self.assertEqual(frags[0]["error"], SOURCE_MISSING)

    def test_stream_chunks_large_text(self):
        """Длинный срез режется на фрагменты: байтовый бюджет, символьные позиции."""
        long_text = "я" * 200  # 200 символов, 400 байт
        self.texts.put("big", long_text)
        with mock.patch(
            "persinder.api.fastapi_server._CONTEXT_CHUNK_BYTES", 63
        ):
            body = asyncio.run(self._stream("big", 0, 200, "utf-8"))
        frags = _fragments(body)
        self.assertGreater(len(frags), 1)
        joined = "".join(str(frag["text"]) for frag in frags)
        self.assertEqual(joined, long_text)
        # позиции — символы и непрерывны: end предыдущего == start следующего
        for i in range(len(frags) - 1):
            self.assertEqual(frags[i]["end"], frags[i + 1]["start"])
        self.assertEqual(frags[-1]["end"], 200)
        # фрагмент не больше бюджета байт
        for frag in frags:
            self.assertLessEqual(len(str(frag["text"]).encode("utf-8")), 64)

    def test_batch_streams_items_in_order(self):
        async def collect():
            chunks = []
            async for b in context_stream_batch(
                self.resolver,
                [
                    {"doc_id": "doc1", "start": 0, "end": 3},
                    {"doc_id": "nope", "start": 0, "end": 3},
                    {"doc_id": 12, "start": 0, "end": 3},
                ],
                "utf-8",
            ):
                chunks.append(b)
            return chunks

        body = b"".join(asyncio.run(collect()))
        frags = _fragments(body)
        self.assertEqual(len(frags), 3)
        self.assertEqual(frags[0]["doc_id"], "doc1")
        self.assertEqual(frags[0]["text"], "При")
        self.assertEqual(frags[1]["error"], SOURCE_MISSING)
        self.assertEqual(frags[2]["error"], "invalid args")


class ContextServerTest(unittest.TestCase):
    """Реальный uvicorn: /health, /context через textstore и перечитывание."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteStore(root / "db.sqlite")
        self.texts = FsTextStore(root / "texts")
        self.registry = default_registry()
        ingest = IngestService(
            self.store, self.texts, NoopStemmer(), chunk_size=40, chunk_overlap=0
        )
        search = SearchService(self.store, NoopStemmer())
        self.ingest = ingest
        self.server = make_fastapi_server(
            ingest,
            search,
            "127.0.0.1",
            0,
            store=self.store,
            textstore=self.texts,
            registry=self.registry,
        )
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self._bound_port()}"

    def _bound_port(self) -> int:
        for _ in range(200):
            server = self.server._server
            if server is not None and getattr(server, "servers", None):
                # ``servers`` — asyncio.Server со списком сокетов (uvicorn 0.54).
                return server.servers[0].sockets[0].getsockname()[1]
            time.sleep(0.05)
        raise RuntimeError("uvicorn не поднялся")

    def tearDown(self):
        self.server.close()
        self.thread.join(timeout=5)
        self.store.close()
        self._tmp.cleanup()

    def _get(self, path: str) -> dict[str, object]:
        import urllib.request

        with urllib.request.urlopen(self.base + path) as resp:
            return json.load(resp)

    def test_health(self):
        self.assertEqual(self._get("/health")["status"], "ok")

    def test_context_round_trip(self):
        """POST /ingest (виртуальный документ) → /context из textstore."""
        payload = json.dumps(
            {"filename": "a.md", "area": "personal", "text": TEXT}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/ingest",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            doc_id = json.load(resp)["document_id"]

        with urllib.request.urlopen(
            self.base + f"/context?doc_id={doc_id}&start=0&end=6"
        ) as resp:
            raw = resp.read()
        frags = _fragments(raw)
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["text"], "Привет")

    def test_context_unknown_doc_returns_error(self):
        with urllib.request.urlopen(
            self.base + "/context?doc_id=nope&start=0&end=6"
        ) as resp:
            raw = resp.read()
        frags = _fragments(raw)
        self.assertEqual(frags[0]["error"], SOURCE_MISSING)

    def test_context_rereads_source_after_text_loss(self):
        """Текст из textstore удалён — /context перечитывает оригинал."""
        file = Path(self._tmp.name) / "doc.txt"
        file.write_text(TEXT, encoding="utf-8")
        result = self.ingest.ingest(
            IngestRequest(
                filename="doc.txt",
                area=Area.PERSONAL,
                text=TEXT,
                source_path=str(file),
                extractor="text",
            )
        )
        assert result.document_id is not None
        doc_id = result.document_id

        # текст в textstore пропал — остался только файл на диске
        (Path(self._tmp.name) / "texts" / f"{doc_id}.txt").unlink()

        with urllib.request.urlopen(
            self.base + f"/context?doc_id={doc_id}&start=0&end=6"
        ) as resp:
            raw = resp.read()
        frags = _fragments(raw)
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["text"], "Привет")


if __name__ == "__main__":
    unittest.main()
