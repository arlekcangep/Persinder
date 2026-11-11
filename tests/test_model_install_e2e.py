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
from persinder.domain import Area
from persinder.embed import (
    PLACEHOLDER,
    file_sidecar_path,
    is_installed,
    load,
    read_sidecar,
    virtual_sidecar_path,
)
from persinder.embed.registry import CATALOG, ModelSpec
from persinder.ingest import IngestRequest, IngestService
from persinder.interfaces import Ingest, Search
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore

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

        def fake_serve(
            ingest: Ingest, search: Search, host: str, port: int, log=None
        ) -> None:
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

    def test_virtual_document_sidecar_under_new_model(self):
        """Документ не из файлов: сайдкар новой модели — в ``home/virtual/``.

        Пустой ``source_path`` — штатный виртуальный режим (текст вставлен/web,
        файла на диске нет): сайдкар живёт в ``data_dir/virtual/<модель>/``,
        а не рядом с файлами.
        """
        text = "вставленный текст без файла на диске про нейросети"
        self.settings.ensure()
        store = SqliteStore(self.settings.db_path)
        try:
            ingest = IngestService(
                store,
                FsTextStore(self.settings.text_dir),
                NoopStemmer(),
                virtual_dir=self.settings.virtual_dir,
                embedding_models=(PLACEHOLDER,),
            )
            outcome = ingest.ingest(
                IngestRequest(
                    filename="web-note.md", area=Area.WEB, text=text, source_path=""
                )
            )
        finally:
            store.close()
        doc_id = outcome.document_id
        assert doc_id is not None

        with (
            mock.patch.dict(CATALOG, {NEW_MODEL: self.spec}),
            mock.patch("persinder.embed.registry.OnnxEmbedder", FakeOnnxEmbedder),
        ):
            self._serve_once()
            self.assertTrue(
                virtual_sidecar_path(
                    self.settings.virtual_dir, doc_id, PLACEHOLDER
                ).exists(),
                "виртуальный документ должен считаться заглушкой",
            )
            with redirect_stdout(io.StringIO()):
                cli.run_model_install(self.settings, NEW_MODEL)
            self._serve_once()
            self._drain_queue()

        sidecar = virtual_sidecar_path(self.settings.virtual_dir, doc_id, NEW_MODEL)
        self.assertTrue(sidecar.exists(), f"нет виртуального сайдкара: {sidecar}")
        data = read_sidecar(sidecar)
        assert data is not None
        self.assertEqual(data.fingerprint.model, NEW_MODEL)
        # Рядом с файлами его быть не должно — файла на диске нет.
        self.assertFalse(
            (self.area / ".persinder" / NEW_MODEL / "web-note.md.pdr").exists()
        )


if __name__ == "__main__":
    unittest.main()
