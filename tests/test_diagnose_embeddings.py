"""Тест диагностики ``scripts/diagnose_embeddings.py --doc``.

Режим ``--doc`` ищет документ по подстроке пути/имени, а если не нашёл — по
тексту чанка через FTS. В ``chunks_fts`` лежат стемы, поэтому запрос надо
стеммить тем же анализатором, что и при индексации, и разбивать на отдельные
токены: иначе фраза ищется как один несуществующий токен и скрипт ложно
сообщает «не найден в БД».
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.ingest import IngestRequest, IngestService
from persinder.morphology import default_stemmer
from persinder.store import SqliteStore
from persinder.textstore import FsTextStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_embeddings.py"


class DiagnoseEmbeddingsDocTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
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
            default_stemmer(),
            virtual_dir=self.home / "virtual",
        )
        self.file = self.vault / "note.md"
        self.file.write_text("жил был серый волчок", encoding="utf-8")
        result = self.ingest.ingest(
            IngestRequest(
                filename="note.md",
                area=Area.PERSONAL,
                text="жил был серый волчок",
            )
        )
        self.assertIsNotNone(result.document_id)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

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

    def test_finds_document_by_plain_word(self):
        result = self._run("--doc", "волчок")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("не найден в БД", result.stdout)
        self.assertIn("note.md", result.stdout)

    def test_finds_document_by_phrase_in_chunk_text(self):
        """Фраза должна искаться по токенам, а не как один стем."""
        result = self._run("--doc", "серый волчок")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("не найден в БД", result.stdout)
        self.assertIn("note.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
