#!/usr/bin/env bash
# run.sh — запуск единого сервиса Persinder.
#
#   ./run.sh                      — serve; конфиг ищется в папке запуска,
#                                   затем в ~/.persinder (persinder.yaml, .yml)
#   ./run.sh --config PATH        — явный конфиг
#   ./run.sh --home PATH          — каталог приложения
#   ./run.sh --host 0.0.0.0 --port 8756
#
# Вся логика — в `persinder serve` (src/persinder/__main__.py): поиск конфига,
# создание home, watcher, эмбеддинги. Здесь только выбор python из venv и exec.
#
# Требуется: ./setup.sh уже выполнен (venv .venv + модель).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv"
if [ -f "$VENV/Scripts/python.exe" ]; then
    PY="$VENV/Scripts/python.exe"     # Windows (Git Bash)
elif [ -f "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"             # Linux/macOS
else
    echo "ОШИБКА: venv не найден: $VENV" >&2
    echo "Сначала запусти ./setup.sh" >&2
    exit 1
fi

exec "$PY" -m persinder serve "$@"
