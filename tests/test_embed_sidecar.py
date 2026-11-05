"""Тесты сайдкаров эмбеддингов: формат, атомарность, устойчивость к битым файлам."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persinder.embed import (
    ChunkParams,
    EmbedFingerprint,
    delete_model_sidecars,
    file_sidecar_path,
    read_sidecar,
    virtual_sidecar_path,
    write_sidecar,
)

DIM = 384


def make_chunks(count: int = 3) -> list[tuple[int, int, list[float]]]:
    """Случайно-детерминированные векторы для теста."""
    out: list[tuple[int, int, list[float]]] = []
    for i in range(count):
        start = i * 10
        vector = [float((j + i) % 7) / 7 for j in range(DIM)]
        out.append((start, start + 10, vector))
    return out


class SidecarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "x.pdr"
        self.fingerprint = EmbedFingerprint(
            model="placeholder-v1", dim=DIM, quant="f16"
        )
        self.params = ChunkParams(size=1200, overlap=200)

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        chunks = make_chunks()
        write_sidecar(self.path, self.fingerprint, self.params, chunks)
        data = read_sidecar(self.path)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.fingerprint, self.fingerprint)
        self.assertEqual(data.params, self.params)
        self.assertEqual(len(data.chunks), len(chunks))
        for got, (start, end, vector) in zip(data.chunks, chunks, strict=True):
            self.assertEqual((got.char_start, got.char_end), (start, end))
            self.assertEqual(len(got.vector), DIM)
            # f16 — близко, но не бит-в-бит
            for a, b in zip(got.vector, vector, strict=True):
                self.assertAlmostEqual(a, b, places=3)

    def test_atomic_no_tmp_left(self):
        write_sidecar(self.path, self.fingerprint, self.params, make_chunks())
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_corrupt_returns_none(self):
        self.path.write_bytes(b"garbage bytes, not a sidecar")
        self.assertIsNone(read_sidecar(self.path))

    def test_truncated_returns_none(self):
        chunks = make_chunks()
        write_sidecar(self.path, self.fingerprint, self.params, chunks)
        data = self.path.read_bytes()
        self.path.write_bytes(data[: len(data) // 2])
        self.assertIsNone(read_sidecar(self.path))

    def test_wrong_vector_length_rejected(self):
        bad = [(0, 10, [1.0, 2.0, 3.0])]  # 3 вместо DIM
        with self.assertRaises(ValueError):
            write_sidecar(self.path, self.fingerprint, self.params, bad)

    def test_unknown_quant_rejected(self):
        fp = EmbedFingerprint(model="m", dim=4, quant="f16")
        write_sidecar(self.path, fp, self.params, [(0, 5, [1.0, 2.0, 3.0, 4.0])])
        # подменяем код квантования на несуществующий (7): quant — байты 8-9
        data = bytearray(self.path.read_bytes())
        data[8:10] = struct.pack(">H", 7)
        self.path.write_bytes(bytes(data))
        self.assertIsNone(read_sidecar(self.path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_sidecar(self.root / "нет.pdr"))

    def test_path_derivation(self):
        self.assertEqual(
            file_sidecar_path("/vault/books/deep.pdf", "placeholder-v1"),
            Path("/vault/books/.persinder/placeholder-v1/deep.pdf.pdr"),
        )
        self.assertEqual(
            virtual_sidecar_path("/data/virtual", "abc123", "placeholder-v1"),
            Path("/data/virtual/placeholder-v1/abc123.pdr"),
        )

    def test_delete_model_sidecars_only_that_model(self):
        """model remove убирает сайдкары одной модели, другие не трогает (D24)."""
        src = self.root / "vault" / "a.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("текст", encoding="utf-8")
        fp = EmbedFingerprint(model="m1", dim=4, quant="f16")
        params = ChunkParams(size=1200, overlap=200)
        write_sidecar(file_sidecar_path(src, "m1"), fp, params, [(0, 5, [1.0] * 4)])
        write_sidecar(file_sidecar_path(src, "m2"), fp, params, [(0, 5, [1.0] * 4)])
        virtual = self.root / "virtual"
        write_sidecar(
            virtual_sidecar_path(virtual, "doc1", "m2"), fp, params, [(0, 5, [1.0] * 4)]
        )

        delete_model_sidecars(virtual, "m2", [{"source_path": str(src)}])

        self.assertFalse(file_sidecar_path(src, "m2").exists())
        self.assertTrue(file_sidecar_path(src, "m1").exists(), "чужая модель жива")
        self.assertFalse(virtual_sidecar_path(virtual, "doc1", "m2").exists())
        # повторное удаление не падает (файлов уже нет)
        delete_model_sidecars(virtual, "m2", [{"source_path": str(src)}])


if __name__ == "__main__":
    unittest.main()
