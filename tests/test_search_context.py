"""Тесты контекста хита: строки до и после чанка."""

import unittest

from persinder.search.context import DEFAULT_CONTEXT_LINES, context_around

TEXT = "\n".join(f"строка {index}" for index in range(10))


def span_of(fragment: str) -> tuple[int, int]:
    start = TEXT.index(fragment)
    return start, start + len(fragment)


class ContextAroundTest(unittest.TestCase):
    def test_default_is_three_lines(self):
        self.assertEqual(DEFAULT_CONTEXT_LINES, 3)

    def test_middle_of_text(self):
        start, end = span_of("строка 4")
        context = context_around(TEXT, start, end)
        self.assertEqual(
            context,
            "\n".join(f"строка {index}" for index in range(1, 8)),
        )

    def test_near_start_clamps_to_first_line(self):
        start, end = span_of("строка 1")
        context = context_around(TEXT, start, end)
        self.assertEqual(
            context,
            "\n".join(f"строка {index}" for index in range(0, 5)),
        )

    def test_near_end_clamps_to_last_line(self):
        start, end = span_of("строка 8")
        context = context_around(TEXT, start, end)
        self.assertEqual(
            context,
            "\n".join(f"строка {index}" for index in range(5, 10)),
        )

    def test_zero_lines_returns_hit_line_only(self):
        start, end = span_of("строка 4")
        self.assertEqual(context_around(TEXT, start, end, lines=0), "строка 4")

    def test_chunk_inside_long_line_expands_to_whole_line(self):
        text = "первая строка\nвторая длинная строка\nтретья строка"
        start = text.index("длинная")
        context = context_around(text, start, start + len("длинная"), lines=0)
        self.assertEqual(context, "вторая длинная строка")

    def test_chunk_spanning_lines_includes_all_hit_lines(self):
        text = "a\nb\nc\nd\ne"
        context = context_around(text, 0, 5, lines=0)  # "a\nb\nc"
        self.assertEqual(context, "a\nb\nc")

    def test_negative_lines_rejected(self):
        with self.assertRaises(ValueError):
            context_around(TEXT, 0, 1, lines=-1)

    def test_out_of_range_span_is_clamped(self):
        self.assertEqual(context_around("a\nb", -10, 10, lines=0), "a\nb")

    def test_empty_text(self):
        self.assertEqual(context_around("", 0, 0), "")


if __name__ == "__main__":
    unittest.main()
