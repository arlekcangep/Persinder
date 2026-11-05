"""ANN-индекс USearch под модель: локальное производное (§8).

Один mmap-файл на модель: ``<ann_dir>/<модель>.usearch``. Ключ — стабильный
``id`` строки таблицы ``embeddings`` (см. ``SqliteStore.put_embeddings``);
по нему результаты поиска обратно сопоставляются с ``(doc_id, chunk_index)``.

Метрика — косинус. Векторы нормированы (эмбеддеры выдают единичную длину),
USearch считает ``distance = 1 - cos`` (меньше — ближе); наружу отдаём
``score = 1 - distance`` — косинус, как и в точном скане.

Правило «ключ неизменяем»: ``(model, doc_id, chunk_index)`` жёстко связано с
содержимым чанка (doc_id — хэш текста), поэтому повторный ``add`` того же ключа
пропускается (USearch дубликаты запрещает), а не перезаписывается.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import numpy as np

_index_cls: Any = None


def _index_class() -> Any:
    """Ленивый импорт USearch с гарантированным порядком (важно!).

    ``usearch`` при импорте грузит NumKong через ``ctypes.CDLL(RTLD_GLOBAL)``:
    если это случается раньше ``onnxruntime``, их символы конфликтуют и процесс
    падает с segfault. Импортируем onnxruntime первым (это дешёвая no-op, если
    он уже загружен), а сам USearch — лениво, только при реальной работе с ANN.
    """
    global _index_cls
    if _index_cls is None:
        with contextlib.suppress(ImportError):
            import onnxruntime  # type: ignore[import-untyped]  # noqa: F401

        from usearch.index import Index

        _index_cls = Index
    return _index_cls


class UsannIndex:
    """Персистентный ANN-индекс одной модели (USearch, mmap)."""

    def __init__(self, path: Path, dim: int) -> None:
        index_class = _index_class()
        self._path = Path(path)
        self._index: Any
        if self._path.exists():
            self._index = index_class(
                ndim=dim, metric="cos", dtype="f32", path=str(self._path)
            )
        else:
            self._index = index_class(ndim=dim, metric="cos", dtype="f32")

    # --- запись ---

    def add(self, ids: list[int], vectors: list[list[float]]) -> None:
        """Добавить ключи с векторами; уже существующие ключи не трогаются."""
        pairs = [
            (key, vector)
            for key, vector in zip(ids, vectors, strict=True)
            if not self._index.contains(int(key))
        ]
        if not pairs:
            return
        keys = np.asarray([pair[0] for pair in pairs], dtype=np.uint64)
        vectors_array = np.asarray([pair[1] for pair in pairs], dtype=np.float32)
        self._index.add(keys, vectors_array)
        self._save()

    def remove(self, ids: list[int]) -> None:
        """Удалить ключи (протухшие строки таблицы, ``model remove``)."""
        present = [key for key in ids if self._index.contains(int(key))]
        if not present:
            return
        self._index.remove(np.asarray(present, dtype=np.uint64))
        self._save()

    def sync(self, live_ids: list[int]) -> None:
        """Привести индекс к таблице: убрать ключи, которых нет в ``embeddings``."""
        live = set(live_ids)
        self.remove([key for key in self.keys() if key not in live])

    # --- чтение ---

    def search(self, vector: list[float], limit: int) -> list[tuple[int, float]]:
        """Top-k ближайших: ``[(id, score)]``, score = косинус (выше — ближе)."""
        query = np.asarray([vector], dtype=np.float32)
        matches = self._index.search(query, limit)
        return [
            (int(key), float(1.0 - distance))
            for key, distance in zip(matches.keys, matches.distances, strict=False)
        ]

    def keys(self) -> set[int]:
        return {int(key) for key in self._index.keys}

    @property
    def size(self) -> int:
        return int(self._index.size)

    # --- жизненный цикл ---

    def _save(self) -> None:
        self._index.save(str(self._path))

    def drop(self) -> None:
        """Удалить файл индекса (``model remove`` / ``ann rebuild``)."""
        if self._path.exists():
            self._path.unlink()
