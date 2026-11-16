"""Тесты стартового обхода каталогов (``sources.catalog``, D25).

Индексация идёт **от файлов**, а не от журнала: на старте читаются полные
каталоги областей и сравниваются с журналом по именам. Новые файлы
индексируются, исчезнувшие — убираются из индекса. Хэши на этом этапе не
считаются: по именам это и не нужно.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.domain import Area
from persinder.sources import (
    AreaConfig,
    Journal,
    JournalEvent,
    WatchConfig,
    sync_catalog,
)
from persinder.sources.models import ChangeKind
from persinder.sources.scanner import AreaScanner


def _stat(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


class CatalogSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.area = AreaConfig(path=root / "area", role=Area.PERSONAL)
        self.area.path.mkdir()
        self.journal = Journal(root / "journal.jsonl")
        self.scanner = AreaScanner(WatchConfig(debounce=0))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str = "текст заметки") -> Path:
        path = self.area.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        # файл не «свежий»: debounce его не откладывает
        past = time.time() - 60
        os.utime(path, (past, past))
        return path

    def _settle(self, name: str, event: JournalEvent = JournalEvent.INDEXED) -> None:
        size, mtime = _stat(self.area.path / name)
        self.journal.record(self.area, name, event, size=size, mtime_ns=mtime)

    def _sync(self):
        return sync_catalog((self.area,), self.scanner, self.journal)

    def test_new_file_is_indexed_first(self):
        """Файл на диске, которого журнал не знает, — новый."""
        self._write("note.md")

        stats = self._sync()

        self.assertEqual((stats.files, stats.added, stats.removed), (1, 1, 0))
        events = [event for result in stats.results for event in result.events]
        self.assertEqual([event.rel_path for event in events], ["note.md"])
        self.assertIs(events[0].kind, ChangeKind.CREATED)
        self.assertIn("новых: 1", stats.line())

    def test_known_settled_file_gives_no_events(self):
        """Устроенный файл не трогается: по именам он есть и в журнале."""
        self._write("note.md")
        self._settle("note.md")

        stats = self._sync()

        self.assertTrue(stats.empty)
        self.assertIn("новых и удалённых нет", stats.line())

    def test_missing_settled_file_is_removed(self):
        """Журнал знает файл, а на диске его нет — удаление."""
        self._write("note.md")
        self._settle("note.md")
        (self.area.path / "note.md").unlink()

        stats = self._sync()

        self.assertEqual((stats.added, stats.removed), (0, 1))
        events = [event for result in stats.results for event in result.events]
        self.assertIs(events[0].kind, ChangeKind.DELETED)
        self.assertEqual(events[0].rel_path, "note.md")

    def test_unfinished_record_is_read_again(self):
        """Недоведённый файл (sent/discovered) перечитывается, хоть имя и знакомо."""
        self._write("note.md")
        self._settle("note.md", JournalEvent.SENT)

        stats = self._sync()

        self.assertEqual(stats.added, 1)

    def test_returned_file_is_indexed_again(self):
        """Файл удаляли (deleted), он вернулся — это снова новый файл."""
        self._write("note.md")
        self._settle("note.md", JournalEvent.DELETED)

        stats = self._sync()

        self.assertEqual(stats.added, 1)

    def test_fresh_file_waits_for_debounce(self):
        """Только что записанный файл не читается: его ещё пишут."""
        path = self.area.path / "fresh.md"
        path.write_text("ещё пишется", encoding="utf-8")
        scanner = AreaScanner(WatchConfig(debounce=30))

        stats = sync_catalog((self.area,), scanner, self.journal)

        self.assertTrue(stats.empty, "файл моложе debounce — ждём следующий скан")
        self.assertEqual(stats.files, 1, "но в каталоге он уже виден")

    def test_several_areas_are_independent(self):
        """Журнал одной области не делает файл «известным» в другой."""
        other = AreaConfig(path=Path(self._tmp.name) / "other", role=Area.FOREIGN)
        other.path.mkdir()
        (other.path / "note.md").write_text("чужой файл", encoding="utf-8")
        past = time.time() - 60
        os.utime(other.path / "note.md", (past, past))
        self._write("note.md")
        self._settle("note.md")

        stats = sync_catalog((self.area, other), self.scanner, self.journal)

        self.assertEqual(stats.files, 2)
        self.assertEqual(stats.added, 1)
        self.assertEqual(stats.results[0].area.path, other.path)
