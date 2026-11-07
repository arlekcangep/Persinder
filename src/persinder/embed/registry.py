"""Реестр моделей эмбеддингов: каталог, установка/удаление, фабрика.

Модель — это каталог с весами и токенизатором внутри каталога приложения:
``<home>/models/<ключ>/`` (см. ``Settings.models_dir``). Отдельного глобального
каталога моделей нет — они живут в подкаталоге ``home``.
Заглушка ``placeholder-v1`` встроена — скачивать её не надо.

Активные модели (на профиль, ``home/models.json``, см. ``state.py``):
``embedding`` — какой считаем новые/изменённые файлы, ``search`` — какой ищем.
Пока ``embedding`` ≠ ``search`` (миграция после ``model install``), новые
файлы индексируются **обеими** моделями, чтобы поиск на старой не терял
свежих документов (D24).
"""

from __future__ import annotations

import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..interfaces import Embedder
from .onnx import OnnxEmbedder
from .service import PlaceholderEmbedder

PLACEHOLDER = "placeholder-v1"

_KEY_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


@dataclass(frozen=True)
class ModelSpec:
    """Запись каталога известных моделей (что умеет скачивать ``install``)."""

    key: str
    title: str
    dim: int
    base_url: str
    files: tuple[tuple[str, str], ...]  # (URL-суффикс, локальное имя)


CATALOG: dict[str, ModelSpec] = {
    "e5-small-int8-v1": ModelSpec(
        key="e5-small-int8-v1",
        title="multilingual-e5-small (ONNX INT8)",
        dim=384,
        base_url="https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/",
        files=(
            ("onnx/model_int8.onnx", "model.onnx"),
            ("tokenizer.json", "tokenizer.json"),
            ("config.json", "config.json"),
            ("special_tokens_map.json", "special_tokens_map.json"),
        ),
    ),
}


def models_root(home: Path) -> Path:
    """Каталог моделей: ``<home>/models`` (внутри каталога приложения)."""
    return home / "models"


def validate_key(key: str) -> None:
    """Ключ модели — безопасное имя подпапки (только ``[a-z0-9._-]``).

    ``.`` и ``..`` отдельно запрещены (иначе ``root / key`` уходит из каталога).
    """
    if not key or key in {".", ".."} or any(ch not in _KEY_OK for ch in key):
        raise ValueError(f"недопустимое имя модели: {key!r}")


def installed_keys(root: Path) -> list[str]:
    """Установленные реальные модели (подпапки с model.onnx + tokenizer.json)."""
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir()
        and (d / "model.onnx").is_file()
        and (d / "tokenizer.json").is_file()
    )


def is_installed(root: Path, key: str) -> bool:
    """Модель на месте: есть веса и токенизатор."""
    target = root / key
    return (target / "model.onnx").is_file() and (target / "tokenizer.json").is_file()


def install(root: Path, key: str) -> None:
    """Скачать модель из каталога в ``root/key`` (докачивает недостающее).

    ``urllib`` — только stdlib, работает на Windows без Docker.
    """
    validate_key(key)
    spec = CATALOG.get(key)
    if spec is None:
        raise KeyError(
            f"модель не в каталоге: {key!r} (доступно: {', '.join(sorted(CATALOG))})"
        )
    target = root / key
    target.mkdir(parents=True, exist_ok=True)
    for suffix, name in spec.files:
        dest = target / name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        _fetch(spec.base_url + suffix, dest)


def _fetch(url: str, dest: Path) -> None:
    """Скачать URL в dest атомарно."""
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "persinder"})
    with urllib.request.urlopen(request, timeout=120) as resp, part.open("wb") as out:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
    os.replace(part, dest)


def remove_weights(root: Path, key: str) -> None:
    """Удалить файлы модели (сайдкары убирает вызывающий — вместе, D24)."""
    validate_key(key)
    if key == PLACEHOLDER:
        raise ValueError("встроенную заглушку удалить нельзя")
    shutil.rmtree(root / key, ignore_errors=True)


def build_embedder(root: Path, key: str) -> Embedder:
    """Эмбеддер по ключу модели (заглушка — встроенная, реальная — из подпапки)."""
    if key == PLACEHOLDER:
        return PlaceholderEmbedder()
    validate_key(key)
    if not is_installed(root, key):
        raise FileNotFoundError(
            f"модель не установлена: {key}\nустанови её: persinder model install {key}"
        )
    return OnnxEmbedder(root / key)
