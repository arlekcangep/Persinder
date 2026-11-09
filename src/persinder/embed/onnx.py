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

from pathlib import Path
from typing import Any

import numpy as np

_MAX_LENGTH = 512
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "
# Вместимость одного прогона модели (последовательностей). Память активаций
# растёт как batch × длина²: 16 последовательностей по 512 токенов дают пик
# RSS ~1.2 ГБ, 4 — ~560 МБ. Идём мини-батчами по 4: заметно экономнее, но
# всё ещё быстрее, чем по одной последовательности.
_BATCH_SIZE = 4


class OnnxEmbedder:
    """e5-small INT8 через onnxruntime (CPU). Отпечаток — ``e5-small-int8-v1``."""

    name = "e5-small-int8-v1"
    dim = 384

    def __init__(self, model_dir: str | Path) -> None:
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
        self._session: Any = onnxruntime.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
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
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            vectors.extend(
                self._embed_batch(prefix, texts[start : start + _BATCH_SIZE])
            )
        return vectors

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
