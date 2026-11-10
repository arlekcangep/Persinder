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
from typing import cast
from unittest import mock

from persinder import __main__ as cli
from persinder.api import make_server
from persinder.config import Settings
from persinder.domain import Area
from persinder.embed import ModelState
from persinder.interfaces import Ingest, Search
from persinder.search import SearchHit, SearchQuery, SearchResult
from persinder.sources import AreaConfig
from persinder.store import SqliteStore

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

        def fake_serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
            served.append((host, port))
            # даём watcher'у и потоку эмбеддингов отработать первый проход
            time.sleep(1.0)

        out = io.StringIO()
        with mock.patch.object(cli, "serve", fake_serve), redirect_stdout(out):
            rc = cli.run_serve(self.config, self.settings)

        self.assertEqual(rc, 0)
        self.assertEqual(len(served), 1)

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

        def fake_serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
            time.sleep(1.0)  # дать watcher'у проиндексировать файл

        out = io.StringIO()
        with mock.patch.object(cli, "serve", fake_serve), redirect_stdout(out):
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

        def fake_serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
            served.append(host)
            time.sleep(1.0)

        empty = self.root / "empty"
        empty.mkdir()
        out = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(empty)
            with mock.patch.object(cli, "serve", fake_serve), redirect_stdout(out):
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

        def fake_serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
            served.append(host)
            time.sleep(1.0)

        out = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(launch)
            with mock.patch.object(cli, "serve", fake_serve), redirect_stdout(out):
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

        def fake_serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
            served.append(host)

        empty = self.root / "empty"
        empty.mkdir()
        out = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(empty)
            with mock.patch.object(cli, "serve", fake_serve), redirect_stdout(out):
                rc = cli.main(["serve", "--home", str(self.settings.home)])
        finally:
            os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertEqual(len(served), 1)
        self.assertIn("только HTTP", out.getvalue())
        self.assertIn("Останавливаюсь", out.getvalue())


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
                    "контекст",
                    area="personal",
                    source_path="/notes/a.md",
                    filename="a.md",
                ),
            ),
            semantic_hits=(SearchHit("doc-2", 1, 0.5, None),),
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
                    "контекст",
                    area="personal",
                    source_path="/notes/a.md",
                    filename="a.md",
                ),
            ),
        )
        self.assertEqual(result.semantic_hits, (SearchHit("doc-2", 1, 0.5, None),))
        self.assertEqual(self.stub.queries, [SearchQuery(text="кот", limit=3)])

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
            mock.patch.object(cli, "search_over_http", return_value=result),
            mock.patch.object(cli, "_local_search") as local,
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
            mock.patch.object(cli, "search_over_http", return_value=None),
            mock.patch.object(cli, "_local_search", return_value=result) as local,
            redirect_stdout(out),
        ):
            rc = cli.run_search(None, "кот", 5, port=self.settings.port)

        self.assertEqual(rc, 0)
        local.assert_called_once()
        self.assertEqual(local.call_args.args[1:], ("кот", 5, None))
        self.assertIn("ВНИМАНИЕ", out.getvalue())

    def test_run_search_local_flag_skips_http(self):
        result = SearchResult(query="кот")
        with (
            mock.patch.object(cli, "search_over_http") as http,
            mock.patch.object(cli, "_local_search", return_value=result),
            redirect_stdout(io.StringIO()),
        ):
            rc = cli.run_search(None, "кот", 5, local=True)

        self.assertEqual(rc, 0)
        http.assert_not_called()

    def test_run_search_explicit_config_is_local(self):
        config = self.root / "custom.yaml"
        result = SearchResult(query="кот")
        with (
            mock.patch.object(cli, "search_over_http") as http,
            mock.patch.object(cli, "_local_search", return_value=result) as local,
            redirect_stdout(io.StringIO()),
        ):
            rc = cli.run_search(None, "кот", 5, config)

        self.assertEqual(rc, 0)
        http.assert_not_called()
        local.assert_called_once()
        self.assertEqual(local.call_args.args[3], config)


if __name__ == "__main__":
    unittest.main()
