#!/usr/bin/env bash
# run.sh — запустить индексацию test-content и поиск.
#
#   ./run.sh              — watcher (5 сек в фоне) + очередь эмбеддингов + демо-поиск
#   ./run.sh "запрос"     — то же, но поиск по твоему запросу
#   ./run.sh --watch      — watcher в фоне, не останавливать (Ctrl+C чтобы убить)
#
# Требуется: ./setup.sh уже выполнен (venv .venv + модель).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

VENV="$REPO/.venv"
PY="$VENV/Scripts/python.exe"
[ -f "$PY" ] || PY="$VENV/bin/python"
PERSINDER="$PY -m persinder"

CONFIG="$(dirname "$REPO")/test-content/persinder.yaml"
if [ ! -f "$CONFIG" ]; then
    echo "ОШИБКА: конфиг не найден: $CONFIG" >&2
    echo "Ожидался test-content рядом с репозиторием (../test-content/persinder.yaml)" >&2
    exit 1
fi

# Каталог данных — внутри test-content (в конфиге он в ignore).
DATA_DIR="$(dirname "$CONFIG")/persinder-data"
export PERSINDER_MODEL_DIR="${PERSINDER_MODEL_DIR:-$HOME/.persinder/models}"

if [ ! -f "$PY" ]; then
    echo "ОШИБКА: venv не найден. Сначала запусти ./setup.sh" >&2
    exit 1
fi

echo "Конфиг:   $CONFIG"
echo "Данные:   $DATA_DIR"
echo "Модели:   $PERSINDER_MODEL_DIR"
echo

MODE="${1:-}"
QUERY="${2:-}"

if [ "$MODE" = "--watch" ]; then
    # Долгий режим: watcher работает, пока не прервёшь.
    echo "Watcher запущен (Ctrl+C — остановить). Эмбеддинги и поиск — вручную."
    exec $PERSINDER watch --config "$CONFIG" --data-dir "$DATA_DIR"
fi

# --- разовый проход ---
echo "[1/3] Скан областей и индексация (один проход watcher'а)..."
$PERSINDER watch --config "$CONFIG" --data-dir "$DATA_DIR" &
WATCH_PID=$!
sleep 8
kill "$WATCH_PID" 2>/dev/null || true
wait "$WATCH_PID" 2>/dev/null || true

echo
echo "[2/3] Эмбеддинги: очередь -> векторы -> сайдкары..."
$PERSINDER embed --background --data-dir "$DATA_DIR"

echo
echo "[3/3] Поиск..."
if [ -n "$QUERY" ]; then
    $PERSINDER search --data-dir "$DATA_DIR" "$QUERY"
else
    # демо-запросы по обоим документам тестового корпуса
    $PERSINDER search --data-dir "$DATA_DIR" "история поиска видео" || true
    echo
    $PERSINDER search --data-dir "$DATA_DIR" "тетради Грамши" || true
fi

echo
echo "Готово. Повторный запуск проиндексирует только изменения."
echo "Долгий режим (watcher всегда работает): ./run.sh --watch"
