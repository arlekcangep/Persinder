"""E2E: установка новой модели эмбеддингов и реальные результаты её работы.

Сценарий повторяет жизнь пользователя: сервис уже проиндексировал файлы на
заглушке, затем ставят новую модель (``model install``), сервис дорабатывает
очередь — и рядом с каждым файлом обязан появиться сайдкар **новой** модели в
``.persinder/<модель>/``, а векторы — в таблице ``embeddings``.

Сеть не нужна: каталог модели указывает на локальный ``file://``-апстрим, а
тяжёлый ONNX подменяется детерминированным фейком (проверяем оркестрацию:
install → state → очередь → воркер → сайдкар).
"""

from __future__ import annotations

import io
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from persinder import __main__ as cli
from persinder.config import Settings
from persinder.embed import (
    PLACEHOLDER,
    file_sidecar_path,
    is_installed,
    load,
    read_sidecar,
)
from persinder.embed.registry import CATALOG, ModelSpec
from persinder.interfaces import Ingest, Search
from persinder.store import SqliteStore

NEW_MODEL = "test-embed-v1"
FILES = {
    "a.md": "первый документ про котиков и коврики",
    "b.md": "второй документ про собак и прогулки",
}


class FakeOnnxEmbedder:
    """Подмена OnnxEmbedder: имя модели — из каталога, вектор детерминирован."""

    dim = 8

    def __init__(self, model_dir: str | Path) -> None:
        self.name = Path(model_dir).name

    def _vector(self, text: str) -> list[float]:
        raw = [float((sum(map(ord, text)) + i * 7) % 23 + 1) for i in range(self.dim)]
        norm = sum(value * value for value in raw) ** 0.5
        return [value / norm for value in raw]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class ModelInstallE2ETest(unittest.TestCase):
    """``model install`` новой модели → её сайдкары и векторы на месте."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.area = root / "notes"
        self.area.mkdir()
        self.files: dict[str, Path] = {}
        for name, text in FILES.items():
            path = self.area / name
            path.write_text(text, encoding="utf-8")
            self.files[name] = path
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
        self.settings = Settings(home=root / "home")

        # Локальный «апстрим» каталога: install скачает по file:// (без сети).
        upstream = root / "upstream"
        upstream.mkdir()
        (upstream / "model.onnx").write_bytes(b"fake-onnx")
        (upstream / "tokenizer.json").write_bytes(b"fake-tokenizer")
        self.spec = ModelSpec(
            key=NEW_MODEL,
            title="test model",
            dim=FakeOnnxEmbedder.dim,
            base_url=upstream.as_uri() + "/",
            files=(("model.onnx", "model.onnx"), ("tokenizer.json", "tokenizer.json")),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _serve_once(self) -> str:
        """Один запуск полного сервиса (HTTP подменён), как ``./run.sh``."""

        def fake_serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
            time.sleep(1.0)  # дать watcher'у и потоку эмбеддингов отработать

        out = io.StringIO()
        with mock.patch.object(cli, "serve", fake_serve), redirect_stdout(out):
            rc = cli.run_serve(self.config, self.settings, reindex=True)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def _drain_queue(self) -> None:
        """Добрать хвост очереди (гонка watcher/embed в момент остановки)."""
        with redirect_stdout(io.StringIO()):
            rc = cli.run_embed(
                self.settings.home, limit=100, all_=False, model=None, background=False
            )
        self.assertEqual(rc, 0)

    def test_install_new_model_produces_sidecars_next_to_files(self):
        with (
            mock.patch.dict(CATALOG, {NEW_MODEL: self.spec}),
            mock.patch("persinder.embed.registry.OnnxEmbedder", FakeOnnxEmbedder),
        ):
            # 1) Первый запуск: база индексируется и считается заглушкой.
            self._serve_once()
            for path in self.files.values():
                self.assertTrue(
                    file_sidecar_path(path, PLACEHOLDER).exists(),
                    f"нет сайдкара заглушки: {path}",
                )

            # 2) Ставим новую модель — как `persinder model install`.
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.run_model_install(self.settings, NEW_MODEL)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertTrue(is_installed(self.settings.models_dir, NEW_MODEL))
            state = load(self.settings.models_state_path)
            self.assertEqual((state.embedding, state.search), (NEW_MODEL, PLACEHOLDER))

            # 3) Сервис дорабатывает очередь новой модели.
            self._serve_once()
            self._drain_queue()

        # Сайдкары новой модели — рядом с файлами, в .persinder/<модель>/.
        folder = self.area / ".persinder" / NEW_MODEL
        self.assertTrue(folder.is_dir(), f"нет папки сайдкаров новой модели: {folder}")
        for path in self.files.values():
            sidecar = file_sidecar_path(path, NEW_MODEL)
            self.assertTrue(sidecar.exists(), f"нет сайдкара новой модели: {sidecar}")
            data = read_sidecar(sidecar)
            assert data is not None
            self.assertEqual(data.fingerprint.model, NEW_MODEL)
            self.assertEqual(data.fingerprint.dim, FakeOnnxEmbedder.dim)
            self.assertEqual(len(data.chunks), 1)

        store = SqliteStore(self.settings.db_path)
        try:
            self.assertEqual(len(store.embeddings_for_model(NEW_MODEL)), len(FILES))
            self.assertEqual(store.counts()["pending_embeddings"], 0)
        finally:
            store.close()

    def test_restart_after_install_does_not_recompute(self):
        """После установки модели повторные запуски не считают всё заново."""
        with (
            mock.patch.dict(CATALOG, {NEW_MODEL: self.spec}),
            mock.patch("persinder.embed.registry.OnnxEmbedder", FakeOnnxEmbedder),
        ):
            self._serve_once()
            with redirect_stdout(io.StringIO()):
                cli.run_model_install(self.settings, NEW_MODEL)
            self._serve_once()
            self._drain_queue()
            # Повторный старт: очередь пуста, воркер ничего не считает.
            again = self._serve_once()

        self.assertNotIn("Embed: документов", again, again)
        store = SqliteStore(self.settings.db_path)
        try:
            self.assertEqual(store.counts()["pending_embeddings"], 0)
        finally:
            store.close()

    def test_install_on_stale_db_writes_sidecars_next_to_files(self):
        """БД до ``source_path``: пустые пути не уводят сайдкары в ``virtual/``."""
        import sqlite3

        with (
            mock.patch.dict(CATALOG, {NEW_MODEL: self.spec}),
            mock.patch("persinder.embed.registry.OnnxEmbedder", FakeOnnxEmbedder),
        ):
            self._serve_once()
            # Имитируем старую БД: у источников путь не записан.
            conn = sqlite3.connect(self.settings.db_path)
            with conn:
                conn.execute("UPDATE document_sources SET source_path = ''")
            conn.close()

            with redirect_stdout(io.StringIO()):
                cli.run_model_install(self.settings, NEW_MODEL)
            # Очередь новой модели уже содержит задачи с пустым путём.
            self._serve_once()
            self._drain_queue()

        for path in self.files.values():
            self.assertTrue(
                file_sidecar_path(path, NEW_MODEL).exists(),
                f"нет сайдкара новой модели рядом с файлом: {path}",
            )
        virtual = list((self.settings.virtual_dir / NEW_MODEL).glob("*.pdr"))
        self.assertEqual(virtual, [], "сайдкары не должны уходить в virtual/")


if __name__ == "__main__":
    unittest.main()
