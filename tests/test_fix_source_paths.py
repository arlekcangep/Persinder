"""Тест одноразового скрипта ``scripts/fix_source_paths.py``.

Скрипт лечит старую БД: заполняет пустой ``source_path`` там, где файл реально
есть на диске, не трогает виртуальные документы и убирает устаревшие задачи
очереди, которые иначе увели бы сайдкары в ``virtual/``.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import NoopStemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fix_source_paths.py"


class FixSourcePathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.home = root / "home"
        self.vault = root / "vault"
        self.vault.mkdir()
        self.config = root / "persinder.yaml"
        self.config.write_text(
            "areas:\n"
            f"  - path: {self.vault}\n"
            "    role: personal\n"
            "    formats: [markdown]\n",
            encoding="utf-8",
        )
        self.home.mkdir(parents=True)
        self.store = SqliteStore(self.home / "persinder.db")
        self.ingest = IngestService(
            self.store,
            FsTextStore(self.home / "pending_texts"),
            NoopStemmer(),
            virtual_dir=self.home / "virtual",
        )
        # Реальный файл, но запись как в старой БД: без source_path.
        self.file = self.vault / "note.md"
        self.file.write_text("заметка про котиков", encoding="utf-8")
        self.file_doc = self._ingest("note.md", "заметка про котиков", Area.PERSONAL)
        # Виртуальный документ (вставленный текст): пути на диске нет.
        self.virtual_doc = self._ingest(
            "web-note.md", "вставленный текст про нейросети", Area.WEB
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _ingest(self, filename: str, text: str, area: Area) -> str:
        result = self.ingest.ingest(
            IngestRequest(filename=filename, area=area, text=text)
        )
        doc_id = result.document_id
        assert doc_id is not None
        return doc_id

    def _run(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "--home",
                str(self.home),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def _queue_docs(self) -> set[str]:
        return {
            item["doc_id"]
            for item in self.store.pending_embeddings(limit=1000)
            if item["source_path"] == ""
        }

    def test_dry_run_changes_nothing(self):
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY-RUN", result.stdout)
        self.assertEqual(self.store.source_path(Area.PERSONAL.value, "note.md"), "")
        self.assertEqual(len(self._queue_docs()), 2)

    def test_fills_real_path_and_drops_stale_queue_rows(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(
            self.store.source_path(Area.PERSONAL.value, "note.md"),
            str(self.file),
        )
        # У реального документа виртуальных источников нет — его задачи с
        # пустым путём устарели; у виртуального документа задача осталась.
        self.assertEqual(self._queue_docs(), {self.virtual_doc})
        # Виртуальный документ скрипт не тронул.
        self.assertEqual(self.store.source_path(Area.WEB.value, "web-note.md"), "")

    def test_clean_virtual_removes_only_relocated_sidecars(self):
        real_sidecar = self.home / "virtual" / "placeholder-v1" / f"{self.file_doc}.pdr"
        virtual_sidecar = (
            self.home / "virtual" / "placeholder-v1" / f"{self.virtual_doc}.pdr"
        )
        real_sidecar.parent.mkdir(parents=True, exist_ok=True)
        real_sidecar.write_bytes(b"sidecar")
        virtual_sidecar.write_bytes(b"sidecar")

        result = self._run("--clean-virtual")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse(real_sidecar.exists(), "сайдкар переехавшего документа удалён")
        self.assertTrue(virtual_sidecar.exists(), "сайдкар виртуального документа цел")

    def test_archive_part_resolved_by_container(self):
        """Часть архива: файла нет, но контейнер (первый компонент) на диске есть."""
        archive = self.vault / "bundle.zip"
        archive.write_bytes(b"PK")
        result = self.ingest.ingest(
            IngestRequest(
                filename="bundle.zip/inner.md",
                area=Area.PERSONAL,
                text="внутренний документ архива",
            )
        )
        self.assertIsNotNone(result.document_id)

        run = self._run()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            self.store.source_path(Area.PERSONAL.value, "bundle.zip/inner.md"),
            str(archive / "inner.md"),
        )

    def test_idempotent(self):
        self.assertEqual(self._run().returncode, 0)
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("восстановлено: 0", second.stdout)
        self.assertIn("Устаревших задач в очереди: 0", second.stdout)


if __name__ == "__main__":
    unittest.main()
