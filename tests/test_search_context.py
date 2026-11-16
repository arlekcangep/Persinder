"""Тесты контекста хита: по границам чанка и по найденному слову."""

import unittest

from persinder.morphology import NoopStemmer, SnowballStemmer
from persinder.search.context import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_CONTEXT_SENTENCES,
    context_around,
    hit_context,
    match_start,
    sentence_context,
)

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


class SentenceContextTest(unittest.TestCase):
    """Контекст по предложениям от найденного слова."""

    def test_default_is_three_sentences(self):
        self.assertEqual(DEFAULT_CONTEXT_SENTENCES, 3)

    def test_middle_of_text(self):
        text = "a. b. c. d. e. f. g. h."
        pos = text.index("d")
        self.assertEqual(sentence_context(text, pos), "a. b. c. d. e. f. g.")

    def test_near_start_clamps_to_first_sentence(self):
        text = "a. b. c. d. e. f. g."
        pos = text.index("a")
        self.assertEqual(sentence_context(text, pos), "a. b. c. d.")

    def test_near_end_clamps_to_last_sentence(self):
        text = "a. b. c. d. e. f. g."
        pos = text.index("g")
        self.assertEqual(sentence_context(text, pos), "d. e. f. g.")

    def test_zero_sentences_returns_hit_sentence_only(self):
        text = "a. b. c. d. e."
        # Позиция на стыке (после предыдущего знака) относится к следующему
        # предложению: "c." — то самое, где найдено слово.
        self.assertEqual(sentence_context(text, text.index("c"), sentences=0), "c.")

    def test_newline_is_sentence_boundary(self):
        text = "ab\ncd\nef\ngh\nij"
        self.assertEqual(
            sentence_context(text, text.index("cd"), sentences=1),
            "ab\ncd\nef",
        )

    def test_negative_sentences_rejected(self):
        with self.assertRaises(ValueError):
            sentence_context("a. b.", 0, sentences=-1)

    def test_empty_text(self):
        self.assertEqual(sentence_context("", 0), "")

    def test_no_sentence_breaks_returns_all(self):
        text = "одно длинное предложение без точек и переносов"
        self.assertEqual(sentence_context(text, 3), text)


class MatchStartTest(unittest.TestCase):
    """Поиск позиции найденного слова морфологически."""

    def test_finds_first_matching_token(self):
        text = "котики гуляют по крыше"
        span = (0, len(text))
        found = match_start(text, span, frozenset({"котики"}), NoopStemmer())
        assert found is not None
        self.assertEqual(text[found : found + 6], "котики")

    def test_not_found_returns_none(self):
        text = "котики гуляют"
        found = match_start(text, (0, len(text)), frozenset({"пёс"}), NoopStemmer())
        self.assertIsNone(found)

    def test_none_span_returns_none(self):
        self.assertIsNone(match_start("abc", None, frozenset({"a"}), NoopStemmer()))

    def test_snowball_finds_derived_form(self):
        stemmer = SnowballStemmer()
        text = "смотрим на кота, он мурлычет"
        span = (0, len(text))
        found = match_start(text, span, frozenset(stemmer.stem(["кот"])), stemmer)
        assert found is not None
        word = text[found : found + 4]
        self.assertEqual(word, "кота")
        self.assertEqual(stemmer.stem([word]), ["кот"])

    def test_span_boundaries_respected(self):
        text = "кот и кошка"
        # Ищем только во втором слове.
        found = match_start(text, (4, len(text)), frozenset({"кот"}), NoopStemmer())
        self.assertIsNone(found)


class HitContextTest(unittest.TestCase):
    """Контекст хита: от найденного слова, а не весь чанк."""

    def test_context_around_word_not_whole_chunk(self):
        text = (
            "один. два. три. целевое слово. четыре. пять. шесть."
            " семь. восемь. девять. десять."
        )
        span = (0, len(text))
        stemmer = SnowballStemmer()
        ctx = hit_context(text, span, frozenset(stemmer.stem(["целевой"])), stemmer)
        self.assertIn("целевое слово", ctx)
        self.assertNotIn("десять", ctx)

    def test_fallback_to_chunk_context_when_word_missing(self):
        # Слова нет в чанке (не должно случаться в реальности) — fallback
        # на контекст по границам чанка, чтобы хит не остался без текста.
        text = "котики\tгуляют.\nтихо\nспят."
        ctx = hit_context(text, (0, len(text)), frozenset({"пёс"}), NoopStemmer())
        self.assertTrue(ctx)


if __name__ == "__main__":
    unittest.main()
