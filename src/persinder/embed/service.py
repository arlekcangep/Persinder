"""Воркер эмбеддингов: очередь → векторы → сайдкары (+ ANN-индекс).

``EmbedWorker`` разбирает ``embedding_queue``: берёт порцию задач, группирует
по документу, читает сырой текст из ``TextStore``, нарезает теми же чанками,
что и ``ingest``, считает векторы через эмбеддер нужной модели и пишет сайдкар
(§7) атомарно. Сайдкар — кэш: его отсутствие не страшно, векторы просто
считаются снова.

Задачи привязаны к модели (``embedding_queue.model``): у файла могут
одновременно жить сайдкары нескольких моделей (D24). Эмбеддер подбирается
фабрикой по ключу модели (``registry.build_embedder``): реальная e5-small или
встроенная заглушка ``PlaceholderEmbedder``.

Помимо сайдкара воркер материализует векторы в таблицу ``embeddings`` (модуль 3)
и поддерживает ANN-индекс USearch (``ann_dir``): добавленные ключи попадают в
индекс сразу, протухшие (документ удалён) убираются реконсиляцией в конце
прохода. Индекс — локальное производное, пересобирается ``persinder ann rebuild``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain import chunk_text
from ..interfaces import Embedder, MetaStore, TextStore
from ..morphology import tokenize
from ..vectorindex import UsannIndex
from .models import ChunkParams, EmbedFingerprint, EmbedStats
from .sidecar import (
    file_sidecar_path,
    pack_f16_vectors,
    virtual_sidecar_path,
    write_sidecar,
)

if TYPE_CHECKING:
    from ..store.models import EmbeddingQueueItem


class PlaceholderEmbedder:
    """Заглушка: детерминированный вектор из токенов (без внешней модели).

    Каждый токен ложится в одну из ``dim`` координат по стабильному хэшу
    (md5 — не зависит от случайной соли процесса), вектор нормируется.
    Отпечаток ``placeholder-v1``; служит запасным вариантом, пока не
    установлена реальная модель (``persinder model install``).
    """

    name = "placeholder-v1"
    dim = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        # для заглушки префикс query/passage не важен — вектор от текста
        return self._embed(texts)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:2], "little") % self.dim
                vector[index] += 1.0
            norm = math.sqrt(sum(v * v for v in vector))
            if norm > 0:
                vector = [v / norm for v in vector]
            vectors.append(vector)
        return vectors


class EmbedWorker:
    """Один проход: обработать порцию очереди на эмбеддинги."""

    def __init__(
        self,
        store: MetaStore,
        textstore: TextStore,
        embedder_factory: Callable[[str], Embedder],
        chunk_size: int = 1200,
        chunk_overlap: int = 200,
        virtual_dir: Path | None = None,
        ann_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._embedder_factory = embedder_factory
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._virtual_dir = Path(virtual_dir) if virtual_dir else None
        self._ann_dir = Path(ann_dir) if ann_dir else None
        self._indices: dict[str, UsannIndex] = {}
        # Эмбеддеры кэшируются по модели: ONNX-сессия (веса ~100 МБ) создаётся
        # один раз, а не на каждый документ/проход. Иначе пересчёт сотен файлов
        # постоянно перезагружает модель — это и есть «жрёт процессор».
        self._embedders: dict[str, Embedder] = {}

    def _embedder(self, model: str) -> Embedder:
        """Эмбеддер модели — из кэша, иначе создать и запомнить."""
        embedder = self._embedders.get(model)
        if embedder is None:
            embedder = self._embedder_factory(model)
            self._embedders[model] = embedder
        return embedder

    def _index(self, model: str, dim: int) -> UsannIndex | None:
        """Открыть/создать ANN-индекс модели (кэш на проход)."""
        if self._ann_dir is None:
            return None
        index = self._indices.get(model)
        if index is None:
            index = UsannIndex(self._ann_dir / f"{model}.usearch", dim)
            self._indices[model] = index
        return index

    def run_once(self, limit: int = 100) -> EmbedStats:
        """Обработать до ``limit`` задач очереди; вернуть итоги.

        Документ обрабатывается целиком: если среди порции есть его задачи,
        берутся все его незавершённые задачи (их может быть больше ``limit``).
        Ошибки не валят проход: задачи остаются в очереди и уйдут в следующий.
        В конце прохода ANN-индексы реконсилируются с таблицей ``embeddings``.
        """
        stats = EmbedStats()
        self._indices.clear()
        try:
            rows = self._store.pending_embeddings(limit)
            doc_ids = list(dict.fromkeys(str(row["doc_id"]) for row in rows))
            for doc_id in doc_ids:
                try:
                    self._process_doc(doc_id, stats)
                except Exception as exc:  # noqa: BLE001 — одна задача не валит проход
                    # Ошибка одного документа (OOM, битый файл, нет модели) не
                    # должна блокировать остальные: задачи остаются в очереди и
                    # уйдут следующим проходом, а прочие документы посчитаются.
                    stats.errors += 1
                    stats.messages.append(f"{doc_id}: {exc}")
            for model, index in self._indices.items():
                live = [
                    row["id"] for row in self._store.embeddings_ids_for_model(model)
                ]
                index.sync(live)
        finally:
            self._indices.clear()
        return stats

    def _process_doc(self, doc_id: str, stats: EmbedStats) -> None:
        rows = self._store.pending_for_docs([doc_id])
        if not rows:
            return

        try:
            text = self._textstore.read(doc_id)
        except FileNotFoundError:
            # Документа нет (удалён/вытеснен), а задачи остались — это сироты.
            self._store.drop_queue_rows([int(row["id"]) for row in rows])
            stats.skipped += 1
            return

        spans = chunk_text(text, self._chunk_size, self._chunk_overlap)
        params = ChunkParams(size=self._chunk_size, overlap=self._chunk_overlap)

        # Задачи группируются по модели: у документа могут быть задачи
        # нескольких моделей (миграция, D24) — у каждой свой сайдкар.
        by_model: dict[str, list[EmbeddingQueueItem]] = {}
        for row in rows:
            by_model.setdefault(str(row["model"]), []).append(row)

        counted = False
        for model, model_rows in by_model.items():
            embedder = self._embedder(model)
            vectors = embedder.embed([text[a:b] for a, b in spans])
            fingerprint = EmbedFingerprint(
                model=embedder.name, dim=embedder.dim, quant="f16"
            )
            payload = [
                (start, end, vectors[index]) for index, (start, end) in enumerate(spans)
            ]
            # Локальное производное (модуль 3): векторы в SQLite под модель —
            # по ним работает векторный поиск (таблица embeddings, §8).
            ids = self._store.put_embeddings(
                model,
                [
                    (
                        doc_id,
                        index,
                        pack_f16_vectors([vectors[index]], embedder.dim),
                    )
                    for index in range(len(spans))
                ],
            )
            # ANN-индекс USearch: ключи строк embeddings, векторы сразу при расчёте.
            index = self._index(model, embedder.dim)
            if index is not None:
                index.add(ids, vectors)
            targets = {str(row["source_path"]) for row in model_rows}
            if "" in targets:
                # Задача могла быть поставлена по устаревшей записи без пути
                # (БД до появления source_path): берём реальные пути источников
                # документа, чтобы сайдкар лёг рядом с файлом, а не в virtual/.
                real = set(self._store.source_paths_for_doc(doc_id))
                targets = ((targets - {""}) | real) or {""}
            for target in targets:
                if not target:
                    if self._virtual_dir is None:
                        continue
                    path = virtual_sidecar_path(self._virtual_dir, doc_id, model)
                else:
                    path = file_sidecar_path(target, model)
                write_sidecar(path, fingerprint, params, payload)
                stats.sidecars += 1
            stats.chunks += len(payload)
            self._store.drop_queue_rows([int(row["id"]) for row in model_rows])
            counted = True

        if counted:
            stats.docs += 1
