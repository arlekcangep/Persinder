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

from ..api.index_log import KIND_EMBED, IndexLog
from ..domain import chunk_text
from ..interfaces import Embedder, MetaStore, TextStore
from ..morphology import tokenize
from ..vectorindex import UsannIndex
from .models import ChunkParams, EmbedFingerprint, EmbedStats
from .sidecar import (
    file_sidecar_path,
    pack_f16_vectors,
    virtual_sidecar_path,
    write_sidecar_packed,
)

if TYPE_CHECKING:
    from ..store.models import EmbeddingQueueItem

# Сколько недостающих чанков документа считаем и пишем за одну порцию. Весь
# документ разом (у огромного PDF десятки тысяч чанков) держал бы в памяти
# все векторы — это гигабайты списков Python и «заморозка» ПК. Порциями память
# остаётся ~константной: в один момент развёрнуты только чанки порции.
_CHUNK_BATCH = 512


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
        log: IndexLog | None = None,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._embedder_factory = embedder_factory
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._virtual_dir = Path(virtual_dir) if virtual_dir else None
        self._ann_dir = Path(ann_dir) if ann_dir else None
        self._log = log
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
        Ошибки не валят проход: задачи падающего документа снимаются, чтобы
        воркер не молотил его каждым проходом (вечный re-embed), а прочие
        документы посчитаются. В конце прохода ANN-индексы реконсилируются с
        таблицей ``embeddings``.
        """
        stats = EmbedStats()
        self._indices.clear()
        try:
            stats.queue_before = self._store.counts()["pending_embeddings"]
            rows = self._store.pending_embeddings(limit)
            paths: dict[str, str] = {
                str(row["doc_id"]): str(row["source_path"])
                for row in rows
                if row["source_path"]
            }
            doc_ids = list(dict.fromkeys(str(row["doc_id"]) for row in rows))
            for doc_id in doc_ids:
                try:
                    self._process_doc(doc_id, stats)
                except Exception as exc:  # noqa: BLE001 — одна задача не валит проход
                    # Ошибка одного документа (OOM, битый файл, нет модели) не
                    # должна блокировать остальные: задачи снимаются (иначе
                    # падающий документ молотится каждым проходом вечно — CPU
                    # горит, очередь не убывает), а без сайдкара они вернутся
                    # на следующем старте через enqueue_missing. Прочие документы
                    # посчитаются в этом же проходе.
                    stats.errors += 1
                    stats.messages.append(f"{doc_id}: {exc}")
                    if self._log is not None:
                        name = paths.get(doc_id) or f"документ {doc_id[:8]}"
                        self._log.error(
                            f"эмбеддинг: {name} — {exc} (задачи сняты, "
                            "вернутся при переиндексации)",
                            kind=KIND_EMBED,
                        )
                    self._store.drop_queue_doc(doc_id)
            for model, index in self._indices.items():
                live = [
                    row["id"] for row in self._store.embeddings_ids_for_model(model)
                ]
                index.sync(live)
            stats.pending = self._store.counts()["pending_embeddings"]
        finally:
            self._indices.clear()
        return stats

    def _process_doc(self, doc_id: str, stats: EmbedStats) -> None:
        rows = self._store.pending_for_docs([doc_id])
        if not rows:
            return
        # Человекочитаемое имя для журнала: путь источника, иначе короткий id.
        label = next(
            (str(row["source_path"]) for row in rows if row["source_path"]),
            f"документ {doc_id[:8]}",
        )
        if self._log is not None:
            # Сколько задач у документа и под какими моделями — чтобы по
            # журналу было видно, за что взялся воркер и чем это кончилось.
            models = ", ".join(sorted({str(row["model"]) for row in rows}))
            self._log.info(
                f"эмбеддинг: {label} — задач: {len(rows)} ({models})",
                kind=KIND_EMBED,
            )

        try:
            text = self._textstore.read(doc_id)
        except FileNotFoundError:
            # Документа нет (удалён/вытеснен), а задачи остались — это сироты.
            self._store.drop_queue_rows([int(row["id"]) for row in rows])
            stats.skipped += 1
            if self._log is not None:
                left = self._store.counts()["pending_embeddings"]
                self._log.warning(
                    f"пропущен: {label} (нет текста, снято задач: {len(rows)},"
                    f" в очереди: {left})",
                    kind=KIND_EMBED,
                )
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
            # Уже посчитанные чанки документа (упавший прогон оставил векторы
            # без сайдкара; doc_id — хэш содержимого, так что они актуальны) —
            # это сырые f16-блабы из таблицы, распаковывать их не нужно. Их не
            # пересчитываем и в ANN повторно не добавляем: id стабильны, они
            # уже в индексе.
            existing = {
                row["chunk_index"]: bytes(row["vector"])
                for row in self._store.embeddings_for_doc(model, doc_id)
            }
            missing = [
                (index, start, end)
                for index, (start, end) in enumerate(spans)
                if index not in existing
            ]
            # Сайдкар собирается из уже упакованных чанков; недостающие
            # считаются и пишутся порциями по _CHUNK_BATCH. Развёрнутые
            # векторы в памяти держатся только для текущей порции — огромный
            # документ (150 МБ PDF) больше не накапливает гигабайты списков.
            packed: list[tuple[int, int, bytes]] = [
                (start, end, existing[index])
                for index, (start, end) in enumerate(spans)
                if index in existing
            ]
            # id задач очереди по номеру чанка: снимаем задачи по мере записи
            # порций, чтобы у большого документа очередь убывала по ходу
            # (иначе счётчик «стоит» минутами и кажется, что воркер встал).
            queue_ids: dict[int, int] = {
                int(row["chunk_index"]): int(row["id"]) for row in model_rows
            }
            index_obj: UsannIndex | None = None

            def _ann_add(
                ids_sub: list[int],
                vectors_sub: list[list[float]],
                model: str,
                embedder: Embedder,
            ) -> None:
                """ANN best-effort порцией: сбой не валит документ (сайдкар будет)."""
                nonlocal index_obj
                if not ids_sub:
                    return
                try:
                    if index_obj is None:
                        index_obj = self._index(model, embedder.dim)
                    if index_obj is not None:
                        index_obj.add(ids_sub, vectors_sub)
                except Exception as exc:  # noqa: BLE001 — ANN не блокирует сайдкар
                    stats.errors += 1
                    stats.messages.append(
                        f"ANN {model}: {exc.__class__.__name__}: {exc}"
                    )

            for offset in range(0, len(missing), _CHUNK_BATCH):
                part = missing[offset : offset + _CHUNK_BATCH]
                computed = embedder.embed([text[a:b] for _, a, b in part])
                rows_for_db: list[tuple[str, int, bytes]] = []
                for (index, start, end), vector in zip(part, computed, strict=True):
                    blob = pack_f16_vectors([vector], embedder.dim)
                    packed.append((start, end, blob))
                    rows_for_db.append((doc_id, index, blob))
                ids = self._store.put_embeddings(model, rows_for_db)
                _ann_add(ids, computed, model, embedder)
                done = [queue_ids.pop(index, None) for index, _, _ in part]
                self._store.drop_queue_rows([i for i in done if i is not None])
                if len(spans) > _CHUNK_BATCH and self._log is not None:
                    left = self._store.counts()["pending_embeddings"]
                    self._log.info(
                        f"эмбеддинг: {label} ({model}) — {len(packed)}"
                        f" из {len(spans)} чанков, в очереди: {left}",
                        kind=KIND_EMBED,
                    )

            fingerprint = EmbedFingerprint(
                model=embedder.name, dim=embedder.dim, quant="f16"
            )
            # Сайдкар — источник правды: пишется сразу после векторов в БД и до
            # ANN-индекса. Если бы первым шёл ANN, его сбой (битый .usearch от
            # упавшего прогона, OOM) оставлял бы документ без сайдкара, а его
            # задачи — в очереди: следующий проход пересчитывал бы документ
            # заново, и так без конца (вечный re-embed — жрёт CPU и память).
            # Так документ завершается, даже если ANN упадёт.
            targets = {str(row["source_path"]) for row in model_rows}
            for target in targets:
                if not target:
                    if self._virtual_dir is None:
                        continue
                    path = virtual_sidecar_path(self._virtual_dir, doc_id, model)
                else:
                    path = file_sidecar_path(target, model)
                write_sidecar_packed(path, fingerprint, params, packed)
                stats.sidecars += 1
            stats.chunks += len(packed)
            # Остались задачи чанков, уже лежавших в БД (existing), и задач,
            # которых нет в текущей нарезке (файл пересобрали) — снимаем их.
            self._store.drop_queue_rows(list(queue_ids.values()))
            counted = True
            if self._log is not None:
                left = self._store.counts()["pending_embeddings"]
                self._log.info(
                    f"сайдкар: {label} ({model}, чанков: {len(packed)},"
                    f" в очереди: {left})",
                    kind=KIND_EMBED,
                )

        if counted:
            stats.docs += 1
