"""Тесты единой точки запуска ``serve``: HTTP + watcher + эмбеддинги.

Проверяем оркестрацию: watcher первым проходом индексирует файл, поток
эмбеддингов разбирает очередь, остановка закрывает ресурсы. HTTP-сервер
подменяется, чтобы тест не висел на ``serve_forever``.
"""

from __future__ import annotations

import io
import os
import socket
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import mock

from persinder import __main__ as cli
from persinder.api import make_fastapi_server, make_server
from persinder.cli import embedding as embedding_mod
from persinder.cli import parser as parser_mod
from persinder.cli import search as search_mod
from persinder.cli import serve as serve_mod
from persinder.config import Settings
from persinder.domain import Area
from persinder.embed import ModelState
from persinder.ingest import IngestRequest, IngestService
from persinder.interfaces import Ingest, Search
from persinder.morphology import NoopStemmer
from persinder.search import SearchHit, SearchQuery, SearchResult, SearchService
from persinder.sources import AreaConfig, default_registry
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore

TEXT = "Кот сидит на коврике в комнате. The quick brown fox. " * 10


class ServeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.area = root / "notes"
        self.area.mkdir()
        (self.area / "note.md").write_text(TEXT, encoding="utf-8")
        self.config = root / "persinder.yaml"
        self.config.write_text(
            "areas:\n"
            f"  - path: {self.area}\n"
            "    role: personal\n"
            "    formats: [markdown, text]\n"
            "watch:\n"
            "  poll_interval: 1\n"
            "  debounce: 0\n",
            encoding="utf-8",
        )
        self.settings = Settings(home=root / "data")

    def tearDown(self):
        self._tmp.cleanup()

    def test_serve_indexes_and_embeds_then_stops(self):
        served: list[tuple[str, int]] = []
        health: dict[str, Any] = {}

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            served.append((host, port))
            # даём watcher'у и потоку эмбеддингов отработать первый проход
            time.sleep(1.0)
            # снимок статистики — как её увидит /health после прохода
            health.update(stats() if stats is not None else {})

        out = io.StringIO()
        with (
            mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
            redirect_stdout(out),
        ):
            rc = cli.run_serve(self.config, self.settings)

        self.assertEqual(rc, 0)
        self.assertEqual(len(served), 1)
        # /health отдаёт живые счётчики: очередь пуста, векторы посчитаны
        self.assertEqual(health.get("documents"), 1)
        self.assertGreater(health.get("vectors") or 0, 0)
        self.assertEqual(health.get("pending_embeddings"), 0)
        self.assertIn("model", health)

        text = out.getvalue()
        self.assertIn("Останавливаюсь", text)
        self.assertLess(
            text.index("Останавливаюсь"), text.index("Persinder остановлен.")
        )

        store = SqliteStore(self.settings.db_path)
        try:
            counts = store.counts()
        finally:
            store.close()
        self.assertEqual(counts["documents"], 1)
        self.assertGreater(counts["chunks"], 0)
        # поток эмбеддингов успел разобрать очередь
        self.assertEqual(counts["pending_embeddings"], 0)

    def test_serve_restores_lost_text_and_requeues(self):
        """Потерянный текст восстанавливается по файлу, документ снова в очереди.

        Раньше такой документ просто не ставился в очередь («нет текста»):
        файл лежал на диске, сайдкара не было, и он молча выпадал из семантики
        навсегда. Теперь сверка журнала видит расхождение, файл читается заново,
        текст восстанавливается, очередь досчитывается.
        """

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            time.sleep(1.0)

        with (
            mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
            redirect_stdout(io.StringIO()),
        ):
            cli.run_serve(self.config, self.settings)

        store = SqliteStore(self.settings.db_path)
        try:
            texts = list(self.settings.text_dir.glob("*.txt"))
            self.assertEqual(len(texts), 1, "текст проиндексированного документа")
            doc_id = texts[0].stem
            # пользователь удалил текст и сайдкар, а файл на диске остался
            for sidecar in self.area.rglob("*.pdr"):
                sidecar.unlink()
            texts[0].unlink()
            store.drop_queue_model("placeholder-v1")
        finally:
            store.close()

        def fake_serve2(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            time.sleep(2.5)

        out = io.StringIO()
        with (
            mock.patch.object(serve_mod, "serve_fastapi", fake_serve2),
            redirect_stdout(out),
        ):
            rc = cli.run_serve(self.config, self.settings)

        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("сырой текст потерян", printed)
        self.assertTrue(
            (self.settings.text_dir / f"{doc_id}.txt").is_file(),
            "текст восстановлен из файла",
        )
        store = SqliteStore(self.settings.db_path)
        try:
            self.assertEqual(store.find_document_by_hash(doc_id), doc_id)
            self.assertEqual(
                store.counts()["pending_embeddings"], 0, "очередь досчитана"
            )
        finally:
            store.close()
        self.assertTrue(
            list(self.area.rglob("*.pdr")), "сайдкар появился после досчёта"
        )

    def test_serve_shares_one_log_between_files_and_embeddings(self):
        """События файлов и эмбеддингов — в одном журнале (оба видны в /logs)."""
        captured: dict[str, Any] = {}
        real_build = embedding_mod.build_embed_worker

        def spy_build(settings: Settings, log: Any = None, registry: Any = None) -> Any:
            captured["embed_log"] = log
            return real_build(settings, log, registry)

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log: Any = None,
            stats: Any = None,
            **kwargs,
        ) -> None:
            captured["http_log"] = log
            time.sleep(1.0)

        out = io.StringIO()
        with (
            mock.patch.object(embedding_mod, "build_embed_worker", spy_build),
            mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
            redirect_stdout(out),
        ):
            rc = cli.run_serve(self.config, self.settings)

        self.assertEqual(rc, 0)
        http_log = captured.get("http_log")
        assert http_log is not None, "журнал передан HTTP-серверу"
        self.assertIs(http_log, captured.get("embed_log"))
        kinds = {ev.kind for ev in http_log.entries()}
        self.assertIn("index", kinds, "события файлов видны в журнале сервиса")

    def test_serve_semantic_false_disables_embeddings(self):
        self.config.write_text(
            "semantic: false\n"
            "areas:\n"
            f"  - path: {self.area}\n"
            "    role: personal\n"
            "    formats: [markdown, text]\n"
            "watch:\n"
            "  poll_interval: 1\n"
            "  debounce: 0\n",
            encoding="utf-8",
        )

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            time.sleep(1.0)  # дать watcher'у проиндексировать файл

        out = io.StringIO()
        with (
            mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
            redirect_stdout(out),
        ):
            rc = cli.run_serve(self.config, self.settings)

        self.assertEqual(rc, 0)
        self.assertIn("Semantic: выключено", out.getvalue())
        store = SqliteStore(self.settings.db_path)
        try:
            counts = store.counts()
        finally:
            store.close()
        self.assertEqual(counts["documents"], 1, "морфологическая индексация работает")
        self.assertEqual(counts["pending_embeddings"], 0, "эмбеддинги не ставятся")

    def test_serve_finds_config_in_home(self):
        home = self.settings.home
        home.mkdir(parents=True, exist_ok=True)
        (home / "persinder.yaml").write_text(
            "areas:\n"
            f"  - path: {self.area}\n"
            "    role: personal\n"
            "    formats: [markdown, text]\n"
            "watch:\n"
            "  poll_interval: 1\n"
            "  debounce: 0\n",
            encoding="utf-8",
        )
        served: list[str] = []

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            served.append(host)
            time.sleep(1.0)

        empty = self.root / "empty"
        empty.mkdir()
        out = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(empty)
            with (
                mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
                redirect_stdout(out),
            ):
                rc = cli.main(["serve", "--home", str(home)])
        finally:
            os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertEqual(len(served), 1)
        self.assertIn("использую", out.getvalue())

        store = SqliteStore(self.settings.db_path)
        try:
            counts = store.counts()
        finally:
            store.close()
        self.assertEqual(counts["documents"], 1)

    def test_serve_finds_config_in_launch_dir(self):
        launch = self.root / "launch"
        launch.mkdir()
        (launch / "persinder.yml").write_text(
            "areas:\n"
            f"  - path: {self.area}\n"
            "    role: personal\n"
            "    formats: [markdown, text]\n"
            "watch:\n"
            "  poll_interval: 1\n"
            "  debounce: 0\n",
            encoding="utf-8",
        )
        served: list[str] = []

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            served.append(host)
            time.sleep(1.0)

        out = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(launch)
            with (
                mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
                redirect_stdout(out),
            ):
                rc = cli.main(["serve", "--home", str(self.settings.home)])
        finally:
            os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertEqual(len(served), 1)
        self.assertIn("использую", out.getvalue())

        store = SqliteStore(self.settings.db_path)
        try:
            counts = store.counts()
        finally:
            store.close()
        self.assertEqual(counts["documents"], 1)

    def test_serve_without_config_is_http_only(self):
        served: list[str] = []

        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            served.append(host)

        empty = self.root / "empty"
        empty.mkdir()
        out = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(empty)
            with (
                mock.patch.object(parser_mod, "serve_fastapi", fake_serve),
                redirect_stdout(out),
            ):
                rc = cli.main(["serve", "--home", str(self.settings.home)])
        finally:
            os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertEqual(len(served), 1)
        self.assertIn("только HTTP", out.getvalue())
        self.assertIn("Останавливаюсь", out.getvalue())

    # --- полная переиндексация (D25) ---

    def _run_serve(self, *, reindex: bool, delay: float) -> str:
        def fake_serve(
            ingest: Ingest,
            search: Search,
            host: str,
            port: int,
            log=None,
            stats=None,
            **kwargs,
        ) -> None:
            time.sleep(delay)

        out = io.StringIO()
        with (
            mock.patch.object(serve_mod, "serve_fastapi", fake_serve),
            redirect_stdout(out),
        ):
            rc = cli.run_serve(self.config, self.settings, reindex=reindex)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_startup_reads_catalog_before_journal(self):
        """Старт идёт от файлов: каталог печатается, новый файл индексируется."""
        out = self._run_serve(reindex=False, delay=1.0)

        self.assertIn("Каталог: 1 файлов в 1 областях; новых: 1, удалённых: 0", out)

    def test_reindex_clears_queue_but_keeps_journal(self):
        """``--reindex``: очередь очищается, журнал не сбрасывается."""
        self._run_serve(reindex=False, delay=2.0)
        journal = self.settings.home / "journal.jsonl"
        before = journal.read_text(encoding="utf-8")
        self.assertIn("indexed", before)

        out = self._run_serve(reindex=True, delay=3.0)

        self.assertIn("Полная переиндексация: очередь эмбеддингов очищена", out)
        self.assertIn("Переиндексация:", out)
        self.assertIn("без изменений 1", out, "неизменённый файл не перечитывается")
        self.assertIn("изменено 0", out)
        # журнал не тронут: его строки на месте
        after = journal.read_text(encoding="utf-8")
        for line in before.splitlines():
            self.assertIn(line, after)

    def test_reindex_catches_changed_file_with_same_mtime(self):
        """Правку с сохранённым mtime видит только сверка хэшей, а не watcher."""
        text = (self.area / "note.md").read_text(encoding="utf-8")
        stat = (self.area / "note.md").stat()
        self._run_serve(reindex=False, delay=2.0)
        # тот же размер, тот же mtime — по (size, mtime) файл «не менялся»
        edited = text.replace("Кот", "Кит", 1)
        self.assertEqual(len(edited), len(text))
        (self.area / "note.md").write_text(edited, encoding="utf-8")
        os.utime(self.area / "note.md", ns=(stat.st_atime_ns, stat.st_mtime_ns))

        out = self._run_serve(reindex=True, delay=4.0)

        self.assertIn("изменено 1", out, "сверка хэшей заметила правку")
        store = SqliteStore(self.settings.db_path)
        try:
            import hashlib

            new_id = hashlib.sha256(edited.encode("utf-8")).hexdigest()
            old_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self.assertEqual(store.find_document_by_hash(new_id), new_id)
            self.assertIsNone(
                store.find_document_by_hash(old_id), "старый текст вытеснен"
            )
            self.assertEqual(store.counts()["pending_embeddings"], 0)
        finally:
            store.close()


class SearchScopesTest(unittest.TestCase):
    """Скоупы поиска: глобальная модель + модели папок, без пересечений."""

    def test_global_and_area_models(self):
        areas = (
            AreaConfig(path=Path("/g"), role=Area.PERSONAL),
            AreaConfig(path=Path("/own"), role=Area.PERSONAL, embedding_model="m2"),
            AreaConfig(path=Path("/off"), role=Area.PERSONAL, semantic=False),
        )
        scopes = cli.search_scopes(areas, ModelState(embedding="m1", search="m1"))
        self.assertEqual(set(scopes), {"m1", "m2"})
        self.assertEqual(scopes["m1"].paths, ("/g" + os.sep,))
        self.assertTrue(scopes["m1"].allow_virtual)
        self.assertEqual(scopes["m2"].paths, ("/own" + os.sep,))
        self.assertFalse(scopes["m2"].allow_virtual)

    def test_same_model_merges_paths(self):
        areas = (
            AreaConfig(path=Path("/g"), role=Area.PERSONAL),
            AreaConfig(path=Path("/own"), role=Area.PERSONAL, embedding_model="m1"),
        )
        scopes = cli.search_scopes(areas, ModelState(embedding="m1", search="m1"))
        self.assertEqual(scopes["m1"].paths, ("/g" + os.sep, "/own" + os.sep))
        self.assertTrue(scopes["m1"].allow_virtual)

    def test_models_for_source(self):
        models = ModelState(embedding="global", search="global")
        areas = (
            AreaConfig(path=Path("/a"), role=Area.PERSONAL, embedding_model="m2"),
            AreaConfig(path=Path("/b"), role=Area.PERSONAL, semantic=False),
        )
        self.assertEqual(cli.models_for_source(areas, "/a/x.md", models), ("m2",))
        self.assertEqual(cli.models_for_source(areas, "/b/x.md", models), ())
        self.assertEqual(cli.models_for_source(areas, "/c/x.md", models), ("global",))
        self.assertEqual(cli.models_for_source(areas, "", models), ("global",))
        self.assertEqual(
            cli.models_for_source(areas, "/a/x.md", models, semantic=False), ()
        )


class _StubSearch:
    """Заглушка сервиса поиска: предсказуемый ответ для HTTP-тестов."""

    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []

    def search(self, query: SearchQuery) -> SearchResult:
        self.queries.append(query)
        return SearchResult(
            query=query.text,
            hits=(
                SearchHit(
                    "doc-1",
                    0,
                    0.75,
                    chunk_start=10,
                    chunk_end=30,
                    area="personal",
                    source_path="/notes/a.md",
                    filename="a.md",
                ),
            ),
            semantic_hits=(SearchHit("doc-2", 1, 0.5),),
        )


class SearchHttpTest(unittest.TestCase):
    """CLI ``search``: HTTP-first и локальный fallback."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stub = _StubSearch()
        self.server = make_server(
            cast(Ingest, None), self.stub, host="127.0.0.1", port=0
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = Settings(
            home=self.root / "data", port=self.server.server_address[1]
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_search_over_http_round_trip(self):
        result = cli.search_over_http(self.settings, "кот", 3)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.query, "кот")
        self.assertEqual(
            result.hits,
            (
                SearchHit(
                    "doc-1",
                    0,
                    0.75,
                    chunk_start=10,
                    chunk_end=30,
                    area="personal",
                    source_path="/notes/a.md",
                    filename="a.md",
                ),
            ),
        )
        self.assertEqual(result.semantic_hits, (SearchHit("doc-2", 1, 0.5),))
        self.assertEqual(self.stub.queries, [SearchQuery(text="кот", limit=3)])

    def test_search_over_http_passes_min_score(self):
        result = cli.search_over_http(self.settings, "кот", 3, 0.9)

        self.assertIsNotNone(result)
        self.assertEqual(
            self.stub.queries[-1],
            SearchQuery(text="кот", limit=3, min_score=0.9),
        )

    def test_search_over_http_passes_layer_limits(self):
        result = cli.search_over_http(
            self.settings, "кот", 3, morph_limit=2, semantic_limit=7
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            self.stub.queries[-1],
            SearchQuery(text="кот", limit=3, morph_limit=2, semantic_limit=7),
        )

    def test_search_over_http_unreachable_returns_none(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings = Settings(home=self.root / "data", port=port)

        self.assertIsNone(cli.search_over_http(settings, "кот", 3))

    def test_run_search_prefers_http(self):
        result = SearchResult(
            query="кот",
            hits=(
                SearchHit(
                    "doc-1",
                    0,
                    0.5,
                    area="personal",
                    source_path="/notes/a.md",
                    filename="a.md",
                ),
            ),
        )
        out = io.StringIO()
        with (
            mock.patch.object(search_mod, "search_over_http", return_value=result),
            mock.patch.object(search_mod, "local_search") as local,
            redirect_stdout(out),
        ):
            rc = cli.run_search(None, "кот", 5, port=self.settings.port)

        self.assertEqual(rc, 0)
        local.assert_not_called()
        self.assertIn("морфология: 1, семантика: 0", out.getvalue())
        self.assertIn("/notes/a.md (personal)", out.getvalue())

    def test_run_search_falls_back_to_local(self):
        result = SearchResult(query="кот", semantic_hits=(SearchHit("d", 0, 0.5),))
        out = io.StringIO()
        with (
            mock.patch.object(search_mod, "search_over_http", return_value=None),
            mock.patch.object(search_mod, "local_search", return_value=result) as local,
            redirect_stdout(out),
        ):
            rc = cli.run_search(
                None,
                "кот",
                5,
                port=self.settings.port,
                min_score=0.9,
                morph_limit=2,
                semantic_limit=7,
            )

        self.assertEqual(rc, 0)
        local.assert_called_once()
        self.assertEqual(local.call_args.args[1:4], ("кот", 5, None))
        self.assertEqual(local.call_args.args[4], 0.9)
        self.assertEqual(local.call_args.args[5], 2)
        self.assertEqual(local.call_args.args[6], 7)
        self.assertIn("ВНИМАНИЕ", out.getvalue())

    def test_run_search_local_flag_skips_http(self):
        result = SearchResult(query="кот")
        with (
            mock.patch.object(search_mod, "search_over_http") as http,
            mock.patch.object(search_mod, "local_search", return_value=result),
            redirect_stdout(io.StringIO()),
        ):
            rc = cli.run_search(None, "кот", 5, local=True)

        self.assertEqual(rc, 0)
        http.assert_not_called()

    def test_run_search_explicit_config_is_local(self):
        config = self.root / "custom.yaml"
        result = SearchResult(query="кот")
        with (
            mock.patch.object(search_mod, "search_over_http") as http,
            mock.patch.object(search_mod, "local_search", return_value=result) as local,
            redirect_stdout(io.StringIO()),
        ):
            rc = cli.run_search(None, "кот", 5, config)

        self.assertEqual(rc, 0)
        http.assert_not_called()
        local.assert_called_once()
        self.assertEqual(local.call_args.args[3], config)

    def test_home_before_subcommand_is_honored(self):
        """``persinder --home X search`` не должен молча искать в ~/.persinder."""
        with mock.patch.object(parser_mod, "run_search", return_value=0) as run:
            rc = cli.main(["--home", "/tmp/other-home", "search", "кот"])

        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.args[0], Path("/tmp/other-home"))

    def test_home_after_subcommand_is_honored(self):
        with mock.patch.object(parser_mod, "run_search", return_value=0) as run:
            rc = cli.main(["search", "--home", "/tmp/after-home", "кот"])

        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.args[0], Path("/tmp/after-home"))


class CliContextTest(unittest.TestCase):
    """CLI-сниппеты (шаг 3): ``/context`` по каждому хиту и локальный резолвер."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = Settings(home=self.root)
        self.store = SqliteStore(self.settings.db_path)
        self.texts = FsTextStore(self.settings.text_dir)
        self.ingest = IngestService(
            self.store, self.texts, NoopStemmer(), chunk_size=100, chunk_overlap=0
        )
        result = self.ingest.ingest(
            IngestRequest(
                filename="note.md",
                area=Area.PERSONAL,
                text="Кот сидит на коврике и мурчит.",
            )
        )
        assert result.document_id is not None
        self.doc_id = result.document_id
        self.server = make_fastapi_server(
            self.ingest,
            SearchService(self.store, NoopStemmer()),
            "127.0.0.1",
            0,
            store=self.store,
            textstore=self.texts,
            registry=default_registry(),
        )
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.settings = Settings(home=self.root, port=self._bound_port())

    def _bound_port(self) -> int:
        for _ in range(200):
            server = self.server._server
            if server is not None and getattr(server, "servers", None):
                return server.servers[0].sockets[0].getsockname()[1]
            time.sleep(0.05)
        raise RuntimeError("uvicorn не поднялся")

    def tearDown(self):
        self.server.close()
        self.thread.join(timeout=5)
        self.store.close()
        self._tmp.cleanup()

    def _hit(self, start: int = 0, end: int = 40) -> SearchHit:
        return SearchHit(
            self.doc_id,
            0,
            1.0,
            chunk_start=start,
            chunk_end=end,
            area="personal",
            source_path="/notes/note.md",
            filename="note.md",
        )

    def test_context_over_http_returns_text(self):
        text = search_mod.context_over_http(self.settings, self._hit())
        self.assertIn("коврике", text or "")

    def test_context_over_http_none_without_spans(self):
        text = search_mod.context_over_http(
            self.settings, SearchHit(self.doc_id, 0, 1.0)
        )
        self.assertIsNone(text)

    def test_print_contexts_http_prints_snippet(self):
        result = SearchResult(query="кот", hits=(self._hit(),))
        buf = io.StringIO()
        with redirect_stdout(buf):
            search_mod._print_contexts_http(self.settings, result)
        self.assertIn("коврике", buf.getvalue())

    def test_print_contexts_local_resolves_from_db(self):
        result = SearchResult(query="кот", hits=(self._hit(),))
        buf = io.StringIO()
        with redirect_stdout(buf):
            search_mod._print_contexts_local(self.settings, result)
        self.assertIn("коврике", buf.getvalue())

    def test_print_contexts_local_skips_without_spans(self):
        result = SearchResult(query="кот", hits=(SearchHit(self.doc_id, 0, 1.0),))
        buf = io.StringIO()
        with redirect_stdout(buf):
            search_mod._print_contexts_local(self.settings, result)
        self.assertEqual(buf.getvalue(), "")


class HelpTest(unittest.TestCase):
    """``--help`` не падает: argparse форматирует description через ``%``."""

    def test_all_subcommands_print_help(self):
        # Литерал ``5%`` в описании — это ``%`` для argparse; без ``%%``
        # ``serve --help`` падал с "unsupported format character".
        for argv in (
            ["--help"],
            ["serve", "--help"],
            ["watch", "--help"],
            ["embed", "--help"],
            ["model", "--help"],
            ["search", "--help"],
            ["ann", "--help"],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main(argv)
            self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
