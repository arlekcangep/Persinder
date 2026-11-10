#!/usr/bin/env python3
"""Скачать модель эмбеддингов (e5-small INT8 ONNX) в каталог приложения.

Работает на Windows и Linux **без Docker**: только стандартная библиотека.
Кладёт модель в ``<home>/models/<ключ>/`` (по умолчанию
``~/.persinder/models/e5-small-int8-v1/``), туда же смотрит
``persinder model install`` — скрипт можно использовать вместо него, когда
модель нужно скачать заранее (CI, офлайн-передача весов).

Повторный запуск докачивает недостающее (уже скачанные файлы пропускаются).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from persinder.config import Settings
from persinder.embed import CATALOG, install, models_root

DEFAULT_KEY = "e5-small-int8-v1"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Скачать модель e5-small (INT8 ONNX) в каталог приложения"
    )
    parser.add_argument(
        "--key",
        default=DEFAULT_KEY,
        help=f"ключ из каталога ({', '.join(CATALOG)})",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="каталог приложения (корень; по умолчанию ~/.persinder); "
        "модель ляжет в <home>/models",
    )
    args = parser.parse_args()

    if args.key not in CATALOG:
        print(f"модель не в каталоге: {args.key!r} (доступно: {', '.join(CATALOG)})")
        return 1

    home = args.home or Settings.default().home
    root = models_root(home)
    root.mkdir(parents=True, exist_ok=True)
    target = root / args.key
    print(f"каталог: {target}")
    try:
        install(root, args.key)
    except Exception as exc:  # noqa: BLE001 — сеть бывает разной
        print(f"ОШИБКА: {exc}")
        return 1
    print("готово: " + ", ".join(name for _, name in CATALOG[args.key].files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
