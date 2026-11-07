#!/usr/bin/env bash
# run.sh — запустить индексацию и поиск по конфигу.
#
#   ./run.sh                          — конфиг по умолчанию (persinder.yaml/yml в папке запуска)
#   ./run.sh --config PATH            — явный путь к конфигу
#   ./run.sh --config PATH "запрос"   — то же + поиск по запросу
#   ./run.sh "запрос"                 — поиск по запросу (конфиг по умолчанию)
#   ./run.sh --watch                  — watcher в фоне, не останавливать (Ctrl+C)
#   ./run.sh --config PATH --watch    — то же, но с явным конфигом
#
# Требуется: ./setup.sh уже выполнен (venv .venv + модель).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_DIR="$(pwd)"   # где запустили скрипт — отсюда ищем конфиг по умолчанию
cd "$REPO"

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
PERSINDER="$PY -m persinder"

# --- разбор аргументов: [--config PATH] [--watch] [запрос] ---
CONFIG=""
MODE=""
QUERY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --config)
            if [ $# -lt 2 ]; then
                echo "ОШИБКА: --config требует путь" >&2
                exit 1
            fi
            CONFIG="$2"
            shift 2
            ;;
        --config=*)
            CONFIG="${1#--config=}"
            shift
            ;;
        --watch)
            MODE="--watch"
            shift
            ;;
        *)
            QUERY="$1"
            shift
            ;;
    esac
done

# Конфиг: явный (--config) или по умолчанию в папке запуска.
# Ищем persinder.yaml, затем persinder.yml.
if [ -z "$CONFIG" ]; then
    if [ -f "$START_DIR/persinder.yaml" ]; then
        CONFIG="$START_DIR/persinder.yaml"
    elif [ -f "$START_DIR/persinder.yml" ]; then
        CONFIG="$START_DIR/persinder.yml"
    fi
fi
if [ -z "$CONFIG" ] || [ ! -f "$CONFIG" ]; then
    echo "ОШИБКА: конфиг не найден." >&2
    echo "Искал в запущенной папке: $START_DIR/persinder.{yaml,yml}" >&2
    echo "Укажи путь явно: ./run.sh --config /путь/к/persinder.yaml" >&2
    exit 1
fi

# Каталог приложения — рядом с конфигом (внутри test-content; в конфиге в ignore).
# Модели живут в <PERSINDER_HOME>/models — отдельного каталога моделей нет.
PERSINDER_HOME="$(dirname "$CONFIG")/persinder-data"

echo "Конфиг:   $CONFIG"
echo "Home:     $PERSINDER_HOME  (модели: $PERSINDER_HOME/models)"
echo

if [ "$MODE" = "--watch" ]; then
    # Долгий режим: watcher работает, пока не прервёшь.
    echo "Watcher запущен (Ctrl+C — остановить). Эмбеддинги и поиск — вручную."
    exec $PERSINDER watch --config "$CONFIG" --home "$PERSINDER_HOME"
fi

# --- разовый проход ---
echo "[1/3] Скан областей и индексация (один проход watcher'а)..."
$PERSINDER watch --config "$CONFIG" --home "$PERSINDER_HOME" &
WATCH_PID=$!
sleep 8
kill "$WATCH_PID" 2>/dev/null || true
wait "$WATCH_PID" 2>/dev/null || true

echo
echo "[2/3] Эмбеддинги: очередь -> векторы -> сайдкары..."
$PERSINDER embed --background --home "$PERSINDER_HOME"

echo
echo "[3/3] Поиск..."
if [ -n "$QUERY" ]; then
    $PERSINDER search --home "$PERSINDER_HOME" "$QUERY"
else
    # демо-запросы по обоим документам тестового корпуса
    $PERSINDER search --home "$PERSINDER_HOME" "история поиска видео" || true
    echo
    $PERSINDER search --home "$PERSINDER_HOME" "тетради Грамши" || true
fi

echo
echo "Готово. Повторный запуск проиндексирует только изменения."
echo "Долгий режим (watcher всегда работает): ./run.sh --watch"
