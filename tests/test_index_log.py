"""Тесты ``IndexLog``: колесо событий индексации для консоли и web UI."""

import unittest
from datetime import datetime, timezone

from persinder.api import (
    KIND_EMBED,
    KIND_INDEX,
    KIND_SYSTEM,
    IndexLog,
    parse_time_bound,
)


class IndexLogTest(unittest.TestCase):
    def setUp(self):
        self.log = IndexLog(max_events=5)
        self.log.enable_console(False)  # тесты не засоряют консоль

    def test_write_and_read(self):
        self.log.info("проиндексирован: a.txt (документ 1, чанков: 2)")
        self.log.warning("пропущен: b.bin (формат не обслуживается)")
        self.log.error("ошибка: c.pdf — boom")

        events = self.log.entries()
        self.assertEqual(
            [ev.message for ev in events],
            [
                "проиндексирован: a.txt (документ 1, чанков: 2)",
                "пропущен: b.bin (формат не обслуживается)",
                "ошибка: c.pdf — boom",
            ],
        )
        self.assertEqual([ev.level for ev in events], ["info", "warning", "error"])
        # id растёт по порядку, ts — ISO-строка
        self.assertEqual([ev.id for ev in events], [1, 2, 3])
        for ev in events:
            self.assertIn("T", ev.ts)

    def test_entries_after_id_incremental(self):
        for i in range(4):
            self.log.info(f"файл {i}")
        events = self.log.entries(after_id=2)
        self.assertEqual([ev.id for ev in events], [3, 4])

    def test_entries_limit_returns_last(self):
        for i in range(10):
            self.log.info(f"файл {i}")
        events = self.log.entries(limit=3)
        self.assertEqual([ev.message for ev in events], ["файл 7", "файл 8", "файл 9"])

    def test_ring_buffer_evicts_oldest(self):
        for i in range(8):  # max_events=5
            self.log.info(f"файл {i}")
        events = self.log.entries()
        self.assertEqual(
            [ev.message for ev in events],
            ["файл 3", "файл 4", "файл 5", "файл 6", "файл 7"],
        )
        self.assertEqual(events[0].id, 4)  # старые отброшены, id продолжает расти

    def test_last_id(self):
        self.log.info("x")
        self.log.info("y")
        self.assertEqual(self.log.last_id, 2)

    def test_to_dict_shape(self):
        self.log.info("событие")
        payload = self.log.entries()[0].to_dict()
        self.assertEqual(set(payload), {"id", "ts", "level", "kind", "message"})
        self.assertIsInstance(payload["ts"], str)
        self.assertIsInstance(payload["message"], str)
        self.assertEqual(payload["kind"], "system")

    def test_kind_defaults_to_system(self):
        self.log.info("служебное")
        self.log.info("файл", kind=KIND_INDEX)
        self.log.error("сайдкар", kind=KIND_EMBED)
        self.assertEqual(
            [ev.kind for ev in self.log.entries()], ["system", "index", "embed"]
        )

    def test_entries_filter_by_kind(self):
        self.log.info("файл 1", kind=KIND_INDEX)
        self.log.info("сайдкар 1", kind=KIND_EMBED)
        self.log.info("файл 2", kind=KIND_INDEX)
        self.log.info("служебное")
        self.assertEqual(
            [ev.message for ev in self.log.entries(kind=KIND_INDEX)],
            ["файл 1", "файл 2"],
        )
        self.assertEqual(
            [ev.message for ev in self.log.entries(kind=KIND_EMBED)], ["сайдкар 1"]
        )
        self.assertEqual(
            [ev.message for ev in self.log.entries(kind=KIND_SYSTEM)], ["служебное"]
        )

    def test_entries_filter_by_time_range(self):
        self.log.info("вчера")
        self.log.info("сегодня")
        self.log.info("завтра")
        self.log._events[0].ts = "2026-11-10T23:59:59+00:00"
        self.log._events[1].ts = "2026-11-11T00:00:00+00:00"
        self.log._events[2].ts = "2026-11-12T08:00:00+00:00"

        since = parse_time_bound("2026-11-11")
        until = parse_time_bound("2026-11-11", end_of_day=True)
        assert since is not None and until is not None
        events = self.log.entries(since=since, until=until)
        self.assertEqual([ev.message for ev in events], ["сегодня"])

    def test_parse_time_bound(self):
        self.assertIsNone(parse_time_bound(""))
        self.assertEqual(
            parse_time_bound("2026-11-10"),
            datetime(2026, 11, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_time_bound("2026-11-10", end_of_day=True),
            datetime(2026, 11, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_time_bound("2026-11-10T12:30:00Z"),
            datetime(2026, 11, 10, 12, 30, tzinfo=timezone.utc),
        )
        with self.assertRaises(ValueError):
            parse_time_bound("позавчера")

    def test_console_on_by_default(self):
        log = IndexLog()
        self.assertTrue(log._console)
        self.assertIs(log._print, print)  # по умолчанию — обычный print

    def test_console_can_be_disabled(self):
        printed: list[str] = []
        log = IndexLog()
        log.enable_console(False)
        log._print = lambda *a, **k: printed.append(a[0])  # подмена печати
        log.info("тихое")
        self.assertEqual(printed, [], "отключённая консоль ничего не печатает")

    def test_console_prints_flushed(self):
        printed: list[tuple[str, object]] = []
        log = IndexLog()
        log._print = lambda *a, **k: printed.append((a[0], k))
        log.info("видно сразу")
        self.assertEqual(len(printed), 1)
        text, kwargs = printed[0]
        self.assertIn("видно сразу", text)
        self.assertTrue(kwargs.get("flush", False), "вывод идёт с flush=True")

    def test_format_contains_level_and_time(self):
        printed: list[str] = []
        self.log.enable_console(True)
        self.log._print = lambda *a, **k: printed.append(a[0])
        self.log.info("какой-то файл")
        self.assertEqual(len(printed), 1)
        self.assertIn("[INFO]", printed[0])
        self.assertIn("какой-то файл", printed[0])


if __name__ == "__main__":
    unittest.main()
