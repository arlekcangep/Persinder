"""Воркер эмбеддингов: очередь → векторы → сайдкары.

``EmbedWorker`` разбирает ``embedding_queue``: берёт порцию задач, группирует
по документу, читает сырой текст из ``TextStore``, нарезает теми же чанками,
что и ``ingest``, считает векторы через эмбеддер нужной модели и пишет сайдкар
(§7) атомарно. Сайдкар — кэш: его отсутствие не страшно, векторы просто
считаются снова.

Задачи привязаны к модели (``embedding_queue.model``): у файла могут
одновременно жить сайдкары нескольких моделей (D24). Эмбеддер подбирается
фабрикой по ключу модели (``registry.build_embedder``): реальная e5-small или
встроенная заглушка ``PlaceholderEmbedder``.
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
from .models import ChunkParams, EmbedFingerprint, EmbedStats
from .sidecar import file_sidecar_path, virtual_sidecar_path, write_sidecar

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
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._embedder_factory = embedder_factory
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._virtual_dir = Path(virtual_dir) if virtual_dir else None

    def run_once(self, limit: int = 100) -> EmbedStats:
        """Обработать до ``limit`` задач очереди; вернуть итоги.

        Документ обрабатывается целиком: если среди порции есть его задачи,
        берутся все его незавершённые задачи (их может быть больше ``limit``).
        Ошибки не валят проход: задачи остаются в очереди и уйдут в следующий.
        """
        stats = EmbedStats()
        rows = self._store.pending_embeddings(limit)
        doc_ids = list(dict.fromkeys(str(row["doc_id"]) for row in rows))
        for doc_id in doc_ids:
            try:
                self._process_doc(doc_id, stats)
            except OSError as exc:
                stats.errors += 1
                stats.messages.append(f"{doc_id}: {exc}")
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
            embedder = self._embedder_factory(model)
            vectors = embedder.embed([text[a:b] for a, b in spans])
            fingerprint = EmbedFingerprint(
                model=embedder.name, dim=embedder.dim, quant="f16"
            )
            payload = [
                (start, end, vectors[index]) for index, (start, end) in enumerate(spans)
            ]
            targets = {str(row["source_path"]) for row in model_rows}
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
