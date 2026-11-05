"""Воркер эмбеддингов: очередь → векторы → сайдкары.

``EmbedWorker`` разбирает ``embedding_queue``: берёт порцию задач, группирует
по документу, читает сырой текст из ``TextStore``, нарезает теми же чанками,
что и ``ingest``, считает векторы через ``Embedder`` и пишет сайдкар (§7)
атомарно. Сайдкар — кэш: его отсутствие не страшно, векторы просто считаются
снова.

Реальный эмбеддер (ONNX e5-small) подключается через порт ``Embedder``;
сейчас стоит детерминированная заглушка ``PlaceholderEmbedder``, чтобы вся
механика работала и тестировалась без тяжёлой зависимости.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from ..domain import chunk_text
from ..interfaces import Embedder, MetaStore, TextStore
from ..morphology import tokenize
from .models import ChunkParams, EmbedFingerprint, EmbedStats
from .sidecar import file_sidecar_path, virtual_sidecar_path, write_sidecar


class PlaceholderEmbedder:
    """Заглушка: детерминированный вектор из токенов (без внешней модели).

    Каждый токен ложится в одну из ``dim`` координат по стабильному хэшу
    (md5 — не зависит от случайной соли процесса), вектор нормируется.
    Отпечаток ``placeholder-v1``: когда придёт реальная модель, старые
    сайдкары не совпадут с её отпечатком и пересчитаются.
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
        embedder: Embedder,
        chunk_size: int = 1200,
        chunk_overlap: int = 200,
        virtual_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._embedder = embedder
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
        vectors = self._embedder.embed([text[a:b] for a, b in spans])

        fingerprint = EmbedFingerprint(
            model=self._embedder.name, dim=self._embedder.dim, quant="f16"
        )
        params = ChunkParams(size=self._chunk_size, overlap=self._chunk_overlap)
        payload = [
            (start, end, vectors[index]) for index, (start, end) in enumerate(spans)
        ]

        targets = {str(row["source_path"]) for row in rows}
        sidecars = 0
        for target in targets:
            if not target:
                if self._virtual_dir is None:
                    continue
                path = virtual_sidecar_path(self._virtual_dir, doc_id)
            else:
                path = file_sidecar_path(target)
            write_sidecar(path, fingerprint, params, payload)
            sidecars += 1

        self._store.drop_queue_rows([int(row["id"]) for row in rows])
        stats.docs += 1
        stats.chunks += len(payload)
        stats.sidecars += sidecars
