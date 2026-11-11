#!/usr/bin/env bash
# deploy.sh — обёртка над deploy/deploy.py (цели test/build см. там).
#
#   ./deploy.sh test     — все тесты (pytest + vitest)
#   ./deploy.sh build    — сборка веб-клиента и wheel в dist/
#
# Просто запускает python-скрипт тем python'ом, который реально работает:
# venv проекта (.venv), затем системный python3.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

py_works() { "$1" -c 'import sys' >/dev/null 2>&1; }

PY=""
for cand in \
    "$REPO/.venv/Scripts/python.exe" \
    "$REPO/.venv/bin/python"; do
    if [ -f "$cand" ] && py_works "$cand"; then
        PY="$cand"
        break
    fi
done
[ -n "$PY" ] || PY="python3"

exec "$PY" "$REPO/deploy/deploy.py" "$@"
