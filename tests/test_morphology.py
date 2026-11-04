"""Тесты морфологического слоя."""

import unittest

from persinder.morphology import NoopStemmer, analyze, analyze_to_string, tokenize


class TokenizeTest(unittest.TestCase):
    def test_lowercase_and_split(self):
        self.assertEqual(tokenize("Hello, МИР 42!"), ["hello", "мир", "42"])

    def test_empty(self):
        self.assertEqual(tokenize(""), [])

    def test_punctuation_only(self):
        self.assertEqual(tokenize("... !!! ???"), [])


class NoopStemmerTest(unittest.TestCase):
    def test_analyze_returns_tokens(self):
        stemmer = NoopStemmer()
        self.assertEqual(analyze("Мама мыла раму", stemmer), ["мама", "мыла", "раму"])

    def test_analyze_to_string(self):
        stemmer = NoopStemmer()
        self.assertEqual(analyze_to_string("Привет, мир!", stemmer), "привет мир")


if __name__ == "__main__":
    unittest.main()
