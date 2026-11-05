#!/usr/bin/env python3
"""Скачать модель эмбеддингов multilingual-e5-small (ONNX INT8) в локальный кэш.

Работает на Windows и Linux **без Docker**: только стандартная библиотека.
Модель берётся с HuggingFace (экспорт Xenova — уже ONNX + tokenizer.json).

Куда кладём:
  по умолчанию ~/.persinder/models/e5-small/  (общий кэш, вне репозитория);
  переопределить: --dir ... или PERSINDER_MODEL_DIR.

Состав каталога после скачивания:
  model.onnx              — INT8 ONNX (~120 МБ)
  tokenizer.json          — быстрый токенизатор
  config.json, special_tokens_map.json
  fingerprint.json        — служебное: источник, quant, dim, sha256

Повторный запуск докачивает недостающее (уже скачанные файлы пропускаются).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/"
# (URL-суффикс, локальное имя)
FILES = [
    ("onnx/model_int8.onnx", "model.onnx"),
    ("tokenizer.json", "tokenizer.json"),
    ("config.json", "config.json"),
    ("special_tokens_map.json", "special_tokens_map.json"),
]
FINGERPRINT = {
    "source": "Xenova/multilingual-e5-small",
    "model": "intfloat/multilingual-e5-small",
    "format": "onnx",
    "quant": "int8",
    "dim": 384,
}


def default_dir() -> Path:
    env = os.environ.get("PERSINDER_MODEL_DIR")
    if env:
        return Path(env)
    return Path.home() / ".persinder" / "models" / "e5-small"


def _fetch(url: str, dest: Path) -> int:
    """Скачать URL в dest атомарно; вернуть число байт."""
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "persinder-setup"})
    with urllib.request.urlopen(request, timeout=120) as resp, part.open("wb") as out:
        size = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    os.replace(part, dest)
    return size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Скачать модель e5-small (INT8 ONNX)")
    parser.add_argument("--dir", type=Path, default=None, help="куда положить")
    args = parser.parse_args()
    target = args.dir or default_dir()
    target.mkdir(parents=True, exist_ok=True)

    for suffix, name in FILES:
        dest = target / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"есть: {name} ({dest.stat().st_size // 1024} КБ)")
            continue
        url = BASE + suffix
        print(f"качаю: {name} ...")
        try:
            size = _fetch(url, dest)
        except Exception as exc:  # noqa: BLE001 — сеть бывает разной
            print(f"  ОШИБКА: {exc}")
            return 1
        print(f"  готово: {size // 1024} КБ")

    fingerprint = dict(FINGERPRINT)
    fingerprint["model_sha256"] = _sha256(target / "model.onnx")
    (target / "fingerprint.json").write_text(
        json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"каталог модели: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
