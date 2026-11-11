#!/usr/bin/env python3
"""deploy/deploy.py — сценарии проверки и сборки Persinder.

Доступные цели (см. также --list):

    test    — все имеющиеся тесты:
                * pytest по tests/ (Python-часть);
                * vitest по web/ (юнит-тесты клиента), если есть node_modules.
    build   — сборка проекта:
                * веб-клиент: npm run build:static (typecheck + vite build +
                  копия в src/persinder/api/static); пропускается с warning,
                  если нет node_modules или node — тогда используется уже
                  закоммиченная статика;
                * wheel пакета в dist/ (python -m build, иначе pip wheel).

Использование:
    python3 deploy/deploy.py test
    python3 deploy/deploy.py build
    ./deploy.sh test                 # то же самое, обёртка в корне

Скрипт находит python проекта сам: PERSINDER_DEPLOY_PYTHON, затем venv
(.venv в корне, ~/venvs/persinder), затем текущий интерпретатор.
Нужен только stdlib.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DIST = ROOT / "dist"


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _python_works(path: str | Path) -> bool:
    """Проверить, что python реально запускается (в venv с хоста Windows
    на Linux лежит .venv/Scripts/python.exe — файл есть, но не исполняется)."""
    try:
        proc = subprocess.run(
            [str(path), "-c", "import sys"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def find_python() -> str:
    """Python, которым прогоняются pytest/сборка: venv проекта или текущий."""
    override = os.environ.get("PERSINDER_DEPLOY_PYTHON")
    if override and _python_works(override):
        return override
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows (Git Bash)
        ROOT / ".venv" / "bin" / "python",  # Linux/macOS
        Path.home() / "venvs" / "persinder" / "bin" / "python",
        Path.home() / "venvs" / "persinder" / "Scripts" / "python.exe",
    ]
    for path in candidates:
        if path.is_file() and _python_works(path):
            return str(path)
    return sys.executable


def run(cmd: list[str], cwd: Path | None = None) -> bool:
    """Запустить команду, транслируя вывод; True при коде 0."""
    print(f"$ {' '.join(cmd)}" + (f"   (cwd: {cwd})" if cwd else ""))
    try:
        proc = subprocess.run(cmd, cwd=cwd)
    except FileNotFoundError as exc:
        print(f"ОШИБКА: не найден исполняемый файл: {exc}")
        return False
    return proc.returncode == 0


def step(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------


def target_test(py: str) -> int:
    ok = True

    step("Тесты: pytest (Python)")
    if run([py, "-m", "pytest", "tests", "-q"], cwd=ROOT):
        print("OK: pytest")
    else:
        ok = False
        print("ПРОВАЛ: pytest")

    step("Тесты: vitest (веб-клиент)")
    vitest = WEB / "node_modules" / "vitest" / "vitest.mjs"
    node = shutil.which("node")
    if vitest.is_file() and node:
        if run([node, str(vitest), "run"], cwd=WEB):
            print("OK: vitest")
        else:
            ok = False
            print("ПРОВАЛ: vitest")
    else:
        print("SKIP: нет web/node_modules или node — тесты клиента пропущены.")

    print("\nИТОГ:", "все тесты пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


def target_build(py: str) -> int:
    ok = True

    step("Сборка: веб-клиент (build:static)")
    node_modules = WEB / "node_modules"
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not (node_modules / "vite").is_dir():
        print(
            "SKIP: нет web/node_modules — использую уже закоммиченную статику "
            "в src/persinder/api/static (пересборка: npm install && "
            "npm run build:static, см. web/README.md)."
        )
    elif npm:
        ok &= run([npm, "run", "build:static"], cwd=WEB)
    elif node:
        # npm недоступен (или bin-ссылки сломаны на общей папке) —
        # повторяем шаги build:static напрямую через node.
        print("npm не найден, запускаю шаги build:static напрямую через node")
        ok &= run([node, "node_modules/vue-tsc/bin/vue-tsc.js", "--noEmit"], cwd=WEB)
        ok &= run([node, "node_modules/vite/bin/vite.js", "build"], cwd=WEB)
        ok &= run([node, "scripts/copy-static.mjs"], cwd=WEB)
    else:
        print("SKIP: нет node — использую закоммиченную статику как есть.")

    step("Сборка: wheel пакета -> dist/")
    DIST.mkdir(exist_ok=True)
    # Предпочтительно python -m build (полноценная изолированная сборка);
    # если пакета build нет — pip wheel без зависимостей.
    has_build_module = run([py, "-c", "import build"], cwd=ROOT)
    if has_build_module:
        built = run([py, "-m", "build", "--wheel", "--outdir", str(DIST)], cwd=ROOT)
    else:
        print("python -m build недоступен, собираю через pip wheel")
        built = run(
            [py, "-m", "pip", "wheel", "--no-deps", "-w", str(DIST), str(ROOT)],
            cwd=ROOT,
        )
    if built:
        wheels = sorted(DIST.glob("*.whl"))
        for wheel in wheels:
            print(f"  артефакт: {wheel}")
        if not wheels:
            print("ПРОВАЛ: dist/ пуст")
            ok = False
    else:
        ok = False

    print("\nИТОГ:", "сборка завершена" if ok else "СБОРКА ПРОВАЛЕНА")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

TARGETS = {
    "test": ("прогнать все тесты (pytest + vitest)", target_test),
    "build": ("собрать веб-клиент и wheel в dist/", target_build),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сценарии проверки и сборки Persinder."
    )
    parser.add_argument("target", nargs="?", help="цель: " + " | ".join(TARGETS))
    parser.add_argument("--list", action="store_true", help="показать цели и выйти")
    args = parser.parse_args(argv)

    if args.list or not args.target:
        py = find_python()
        print("Цели deploy.py:")
        for name, (desc, _) in TARGETS.items():
            print(f"  {name:8} — {desc}")
        print(f"\nPython для тестов/сборки: {py}")
        return 0

    if args.target not in TARGETS:
        parser.error(f"неизвестная цель: {args.target!r} (см. --list)")

    py = find_python()
    print(f"Python: {py}")
    _, func = TARGETS[args.target]
    return func(py)


if __name__ == "__main__":
    sys.exit(main())
