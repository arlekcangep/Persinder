"""Реальный эмбеддер на ONNX: multilingual-e5-small (INT8).

Адаптер за портом ``Embedder`` (см. ``interfaces.py``). Модель и токенизатор
берутся из подпапки ``<модель>/`` каталога приложения
(``<home>/models/<ключ>/``, см. ``Settings.models_dir`` и реестр
``embed/registry.py``) — скачиваются один раз командой
``persinder model install``; на Windows работает так же, Docker не нужен.

Требования модели e5 (см. §12.1 архитектуры): к тексту приписывается
``passage:`` (к запросу — ``query:``, это понадобится на слое поиска).
Вектор — среднее по токенам (mean pooling) с учётом маски внимания,
нормализованный к единичной длине.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

_CORES = max(1, os.cpu_count() or 2)

_MAX_LENGTH = 512
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "
# Вместимость одного прогона модели (последовательностей). Память активаций
# растёт как batch × длина²: 16 последовательностей по 512 токенов дают пик
# RSS ~1.2 ГБ, 4 — ~560 МБ. Идём мини-батчами по 4: заметно экономнее, но
# всё ещё быстрее, чем по одной последовательности.
_BATCH_SIZE = 4
# Параллельных мини-батчей на один вызов embed(). run() и токенизатор
# потокобезопасны; пул потоков ORT принадлежит сессии, так что параллельные
# батчи делят его (не размножая потоки). Выигрыш — только когда у машины есть
# свободные ядра: на 2-ядерной один прогон уже насыщает оба, параллельность
# лишь множит память (замер: 9 vs 10 чанк/с). Поэтому по умолчанию воркеров
# ~половина ядер (на 2 ядрах — 1), но не больше 4; переопределить:
# PERSINDER_EMBED_WORKERS=N.
_DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))


def _embed_workers() -> int:
    """Число параллельных мини-батчей (env ``PERSINDER_EMBED_WORKERS``)."""
    raw = os.environ.get("PERSINDER_EMBED_WORKERS", "")
    if raw.strip():
        try:
            return max(1, min(8, int(raw)))
        except ValueError:
            pass
    return _DEFAULT_WORKERS


def _cpu_limit(percent: float | None) -> float | None:
    """Доля ядер, которую эмбеддинги в среднем не должны превышать.

    ``percent`` — прямое значение (0.3 = 30%) или env ``PERSINDER_CPU_LIMIT``
    в процентах (30 = 30%). ``None`` — без ограничения.
    """
    if percent is None:
        raw = os.environ.get("PERSINDER_CPU_LIMIT", "")
        if not raw.strip():
            return None
        try:
            percent = float(raw) / 100.0
        except ValueError:
            return None
    return max(0.0, min(1.0, float(percent)))


class _DutyCycle:
    """Жёсткий лимит средней загрузки: не выше ``limit`` от всех ядер.

    Считается по дельтам текущего прогона: ``cpu`` процессорных секунд
    (``time.process_time`` — сумма по всем потокам процесса) на ``wall``
    стен-секунд должны укладываться в ``limit * cores`` на КАЖДОМ цикле
    «прогон + сон». Тогда и в любом окне средняя не превышает лимит, а не
    «в среднем с момента запуска» (что банковало простой и пропускало
    всплески — 370 мс прогон пеггил оба ядра, а сон потом не компенсировал).
    """

    def __init__(self, limit: float, cores: int) -> None:
        self._budget = max(1e-6, limit * cores)  # cpu-секунд на стен-секунду

    def tick(self, cpu: float, wall: float) -> None:
        if cpu <= wall * self._budget:
            return  # прогон уже укладывается — не спим
        need = cpu / self._budget - wall
        if need > 0:
            time.sleep(need)


class OnnxEmbedder:
    """e5-small INT8 через onnxruntime (CPU). Отпечаток — ``e5-small-int8-v1``."""

    name = "e5-small-int8-v1"
    dim = 384

    def __init__(
        self, model_dir: str | Path, *, cpu_limit: float | None = None
    ) -> None:
        # Тяжёлые зависимости подключаются лениво: это опциональный адаптер.
        import onnxruntime  # type: ignore[import-untyped]
        import tokenizers  # type: ignore[import-untyped]

        self._model_dir = Path(model_dir)
        model_path = self._model_dir / "model.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(
                f"модель не найдена: {model_path}\n"
                "скачай её: persinder model install e5-small-int8-v1"
            )
        options: Any = (  # pyright: ignore[reportUnknownVariableType]
            onnxruntime.SessionOptions()  # pyright: ignore[reportUnknownMemberType]
        )
        # Арена ONNX держит буферы прошлых прогонов и раздувает RSS: при
        # батче 16 пик ~1.2 ГБ против ~560 МБ без арены (батч 4). Плата —
        # чуть более дорогое выделение памяти на каждый run, зато RSS не
        # накапливается.
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        limit = _cpu_limit(cpu_limit)
        if limit is not None:
            # Один run() не должен расползаться на все ядра: внутри — не
            # больше ``limit * cores`` потоков, а среднее ограничение держит
            # duty-cycle ниже.
            options.intra_op_num_threads = max(1, round(_CORES * limit))
        # GPU (DirectML) — если установлен onnxruntime-directml: INT8-операции
        # e5 DirectML умеет, неподдерживаемые узлы ORT сам уронит на CPU.
        # Без пакета — чисто CPU, ничего не ломается.
        try:
            # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            available: Any = onnxruntime.get_available_providers()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        except Exception:  # noqa: BLE001 — провайдеры не должны ронять запуск
            available = []
        if "DmlExecutionProvider" in available:
            providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        self._session: Any = onnxruntime.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=providers,
        )
        self._tokenizer: Any = tokenizers.Tokenizer.from_file(
            str(self._model_dir / "tokenizer.json")
        )
        # Прогрев: первый run() ORT тратит сотни мс на ленивую оптимизацию
        # графа. Делаем один крошечный прогон при загрузке, чтобы первый
        # документ не платил эту цену.
        ids = self._tokenizer.encode(_PASSAGE_PREFIX + "прогрев").ids[:4] or [1]
        self._session.run(
            None,
            {
                "input_ids": [ids],
                "attention_mask": [[1] * len(ids)],
                "token_type_ids": [[0] * len(ids)],
            },
        )
        self._workers = _embed_workers()
        self._throttle = _DutyCycle(limit, _CORES) if limit is not None else None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed(_PASSAGE_PREFIX, texts)

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self._embed(_QUERY_PREFIX, texts)

    def _embed(self, prefix: str, texts: list[str]) -> list[list[float]]:
        """Векторы для ``texts``, дробя порцию на мини-батчи ``_BATCH_SIZE``.

        Память ONNX зависит от размера прогона: один run() с батчем в сотни
        последовательностей по 512 токенов требует гигабайты на активации
        внимания. Дробить порцию надо не в воркере (он не знает о модели),
        а здесь, на границе вызова.

        Мини-батчи считаются параллельно (``self._workers`` одновременных
        run() на одной сессии) — порядок векторов сохраняется. Для одного
        батча и в тестах (фейки без self._workers) — последовательно.
        """
        if not texts:
            return []
        batches = [
            texts[start : start + _BATCH_SIZE]
            for start in range(0, len(texts), _BATCH_SIZE)
        ]
        if len(batches) == 1:
            return self._embed_batch(prefix, batches[0])
        workers = getattr(self, "_workers", 1)
        if getattr(self, "_throttle", None) is not None:
            # Под лимитом CPU параллельность мешает плавному троттлингу:
            # считаем по одному батчу и спим после каждого (см. _throttle_tick).
            workers = 1
        if workers <= 1:
            vectors: list[list[float]] = []
            for batch in batches:
                vectors.extend(self._embed_batch_throttled(prefix, batch))
            return vectors
        with ThreadPoolExecutor(max_workers=workers) as pool:
            t0 = time.perf_counter()
            c0 = time.process_time()
            per_batch = list(pool.map(partial(self._embed_batch, prefix), batches))
            self._throttle_tick(time.process_time() - c0, time.perf_counter() - t0)
        return [vector for batch in per_batch for vector in batch]

    def _embed_batch_throttled(
        self, prefix: str, texts: list[str]
    ) -> list[list[float]]:
        """Один мини-батч с выравниванием загрузки по лимиту CPU."""
        throttle = getattr(self, "_throttle", None)
        if throttle is None:
            return self._embed_batch(prefix, texts)
        t0 = time.perf_counter()
        c0 = time.process_time()
        vectors = self._embed_batch(prefix, texts)
        throttle.tick(time.process_time() - c0, time.perf_counter() - t0)
        return vectors

    def _throttle_tick(self, cpu: float, wall: float) -> None:
        """Выровнять среднюю загрузку по лимиту CPU (если он задан)."""
        if self._throttle is not None:
            self._throttle.tick(cpu, wall)

    def _embed_batch(self, prefix: str, texts: list[str]) -> list[list[float]]:
        """Один прогон модели: не больше ``_BATCH_SIZE`` последовательностей."""
        encoded = [self._tokenizer.encode(prefix + text) for text in texts]
        batch = len(encoded)
        length = max(1, min(max(len(e.ids) for e in encoded), _MAX_LENGTH))

        input_ids = [[0] * length for _ in range(batch)]
        attention = [[0] * length for _ in range(batch)]
        token_type = [[0] * length for _ in range(batch)]
        for i, item in enumerate(encoded):
            ids = item.ids[:_MAX_LENGTH]
            for j, token_id in enumerate(ids):
                input_ids[i][j] = token_id
                attention[i][j] = 1

        last_hidden = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention,
                "token_type_ids": token_type,
            },
        )[0]

        # Mean pooling с маской внимания + нормализация. В чистом Python это
        # тройной цикл batch × length × dim (сотни тысяч операций на батч) —
        # столько же времени, сколько сама модель. Numpy делает это за
        # десятки микросекунд на батч.
        mask = np.asarray(attention, dtype=np.float32)  # (batch, length)
        sums = (last_hidden * mask[:, :, None]).sum(axis=1)  # (batch, dim)
        counts = mask.sum(axis=1, keepdims=True)
        counts[counts == 0] = 1.0  # пустой чанк → нулевой вектор ниже
        pooled = sums / counts
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        pooled = pooled / norms
        return pooled.tolist()

    @staticmethod
    def available(model_dir: str | Path) -> bool:
        """Есть ли скачанная модель (model.onnx + tokenizer.json)."""
        root = Path(model_dir)
        return (root / "model.onnx").is_file() and (root / "tokenizer.json").is_file()
