"""Тесты морфологического слоя."""

import threading
import unittest

from persinder.morphology import (
    NoopStemmer,
    SnowballStemmer,
    analyze,
    analyze_to_string,
    tokenize,
)


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


class SnowballThreadSafetyTest(unittest.TestCase):
    def test_concurrent_stemming_has_no_errors(self):
        try:
            stemmer = SnowballStemmer()
        except ImportError:
            self.skipTest("snowballstemmer не установлен")

        tokens = tokenize("Проверка потокобезопасности стеммера " * 50)
        errors: list[Exception] = []

        def worker() -> None:
            for _ in range(50):
                try:
                    stemmer.stem(tokens)
                except Exception as exc:
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
