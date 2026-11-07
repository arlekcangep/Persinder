"""Реальный эмбеддер на ONNX: multilingual-e5-small (INT8).

Адаптер за портом ``Embedder`` (см. ``interfaces.py``). Модель и токенизатор
берутся из подпапки ``<модель>/`` глобального каталога моделей
(``~/.persinder/models/<ключ>/``, см. ``Settings.models_dir`` и реестр
``embed/registry.py``) — скачиваются один раз, вне репозитория, командой
``persinder model install``; на Windows работает так же, Docker не нужен.

Требования модели e5 (см. §12.1 архитектуры): к тексту приписывается
``passage:`` (к запросу — ``query:``, это понадобится на слое поиска).
Вектор — среднее по токенам (mean pooling) с учётом маски внимания,
нормализованный к единичной длине.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MAX_LENGTH = 512
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "


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
        self._session: Any = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self._tokenizer: Any = tokenizers.Tokenizer.from_file(
            str(self._model_dir / "tokenizer.json")
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed(_PASSAGE_PREFIX, texts)

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self._embed(_QUERY_PREFIX, texts)

    def _embed(self, prefix: str, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
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

        pooled: list[list[float]] = []
        for i in range(batch):
            total = 0.0
            for j in range(length):
                total += float(attention[i][j])
            if total <= 0:
                pooled.append([0.0] * self.dim)
                continue
            vector = [0.0] * self.dim
            for j in range(length):
                if not attention[i][j]:
                    continue
                row = last_hidden[i][j]
                for k in range(self.dim):
                    vector[k] += float(row[k])
            norm = sum(v * v for v in vector) ** 0.5
            pooled.append([v / norm for v in vector] if norm else vector)
        return pooled

    @staticmethod
    def available(model_dir: str | Path) -> bool:
        """Есть ли скачанная модель (model.onnx + tokenizer.json)."""
        root = Path(model_dir)
        return (root / "model.onnx").is_file() and (root / "tokenizer.json").is_file()
